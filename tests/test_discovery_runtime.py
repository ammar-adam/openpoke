"""Real IA-loop integration tests; models, logs, and external execution are local fakes."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from server.agents.interaction_agent import agent, runtime, tools
from server.agents.execution_agent import batch_manager
from server.services.execution.catalog import AgentCatalog
from server.services.execution import catalog as catalog_module


class MemoryLog:
    def __init__(self):
        self.users = []
        self.replies = []
        self.agent_messages = []
        self.waits = []
        self.requests = []

    def record_user_message(self, message):
        self.users.append(message)

    def record_reply(self, message):
        self.replies.append(message)

    def record_agent_message(self, message):
        self.agent_messages.append(message)

    def record_wait(self, reason):
        self.waits.append(reason)

    def record_request(self, name, instructions):
        self.requests.append((name, instructions))

    def load_transcript(self):
        return "earlier conversation"


class Roster:
    def __init__(self):
        self.names = []

    def load(self):
        pass

    def get_agents(self):
        return self.names.copy()

    def add_agent(self, name):
        self.names.append(name)


def answer(*calls, text=""):
    message = {"content": text}
    if calls:
        message["tool_calls"] = [
            {"id": f"call-{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for index, (name, arguments) in enumerate(calls)
        ]
    return {"choices": [{"message": message}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    log, roster = MemoryLog(), Roster()
    settings = SimpleNamespace(openrouter_api_key="fake", interaction_agent_model="fake", summarization_enabled=False)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "get_conversation_log", lambda: log)
    monkeypatch.setattr(runtime, "get_working_memory_log", lambda: None)
    monkeypatch.setattr(runtime, "get_agent_roster", lambda: roster)
    monkeypatch.setattr(agent, "get_agent_roster", lambda: roster)
    monkeypatch.setattr(tools, "get_conversation_log", lambda: log)
    monkeypatch.setattr(tools, "get_execution_agent_logs", lambda: log)
    monkeypatch.setattr(tools, "get_agent_roster", lambda: roster)
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    dispatched = []

    async def execute_agent(name, instructions, **kwargs):
        dispatched.append((name, instructions, kwargs))
        return SimpleNamespace(success=True)

    monkeypatch.setattr(tools._EXECUTION_BATCH_MANAGER, "execute_agent", execute_agent)
    return SimpleNamespace(log=log, roster=roster, catalog=catalog, dispatched=dispatched)


def make_runtime(env, responses, mode="focus"):
    captured = []
    responses = iter(responses)

    async def client(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        return next(responses)

    instance = runtime.InteractionAgentRuntime(discovery_mode=mode, catalog=env.catalog, model_client=client)
    return instance, captured


def source(name="Alice", result_id="result-a", correlation="turn-a"):
    return {"result_id": result_id, "agent_name": name, "correlation_id": correlation}


def test_legacy_prompt_and_tool_schema_are_unchanged(env):
    env.roster.names = ["Alice", "Bob"]
    original_schemas = copy.deepcopy(tools.TOOL_SCHEMAS)
    instance, captured = make_runtime(env, [answer(text="hello")], "legacy")
    result = asyncio.run(instance.execute("hi"))
    assert result.success
    assert captured[0]["system"] == agent.SYSTEM_PROMPT
    assert captured[0]["tools"] == original_schemas
    assert captured[0]["messages"] == agent.prepare_message_with_history("hi", "earlier conversation")
    tools.get_tool_schemas("focus")
    assert tools.TOOL_SCHEMAS == original_schemas  # No shared-schema mutation.


def test_bounded_prompt_does_not_render_entire_roster(env):
    env.roster.names = [f"unrelated worker {index}" for index in range(200)] + ["Tokyo restaurant"]
    instance, captured = make_runtime(env, [answer(text="okay")], "retrieval")
    assert asyncio.run(instance.execute("Tokyo restaurant")).success
    message = captured[0]["messages"][0]["content"]
    assert "Tokyo restaurant" in message
    assert "unrelated worker 199" not in message
    assert instance.last_discovery_trace["initial"]["rendered_chars"] <= 6000
    assert "find_agents" in {tool["function"]["name"] for tool in captured[0]["tools"]}


def test_one_search_expansion_and_exact_old_name_lookup(env):
    env.catalog.record_dispatch("Old Tokyo plan", "restaurant reservations")
    responses = [
        answer(("find_agents", {"query": "something", "exact_name": "Old Tokyo plan"})),
        answer(("find_agents", {"query": "more"})),
        answer(text="found it"),
    ]
    instance, captured = make_runtime(env, responses)
    assert asyncio.run(instance.execute("the old plan")).success
    tool_messages = [message for message in captured[-1]["messages"] if message["role"] == "tool"]
    assert "Old Tokyo plan" in tool_messages[0]["content"]
    assert json.loads(tool_messages[1]["content"])["status"] == "error"
    assert len(instance.last_discovery_trace["expansions"]) == 1


def test_callback_draft_only_promotes_its_explicit_source(env):
    for name in ("Alice", "Bob"):
        env.catalog.record_dispatch(name, "draft an email")
    responses = [answer(
        ("send_draft", {"to": "alice@example.com", "subject": "Proposal", "body": "Hello", "source_result_ids": ["result-a"]}),
        ("send_message_to_user", {"message": "Would you like me to send it?"}),
    ), answer()]
    instance, _ = make_runtime(env, responses)
    result = asyncio.run(instance.handle_agent_message(
        "Alice and Bob completed drafts", [source(), source("Bob", "result-b")]
    ))
    assert result.success
    assert len(env.log.replies) == 2
    assert [card["agent_name"] for card in env.catalog.recent_published()] == ["Alice"]
    next_turn, captured = make_runtime(env, [answer(text="okay")])
    asyncio.run(next_turn.execute("send it"))
    assert {item["agent_name"] for item in next_turn.last_discovery_trace["initial"]["selected"]} == {"Alice"}


def test_unknown_source_rejects_publication_before_logging(env):
    env.catalog.record_dispatch("Alice", "draft")
    instance, captured = make_runtime(env, [answer(("send_message_to_user", {
        "message": "Here is the result", "source_result_ids": ["invented"]
    })), answer()])
    asyncio.run(instance.handle_agent_message("Alice completed", [source()]))
    assert env.log.replies == []
    assert env.catalog.recent_published() == []
    assert "Unknown source_result_id" in captured[-1]["messages"][-1]["content"]


@pytest.mark.parametrize("show", [False, True])
def test_unattributed_background_update_never_promotes_focus(env, show):
    env.catalog.record_dispatch("Watcher", "background update")
    responses = [answer(("send_message_to_user", {"message": "A notification"})), answer()] if show else [answer()]
    instance, _ = make_runtime(env, responses)
    asyncio.run(instance.handle_agent_message("background completion"))
    assert env.catalog.recent_published() == []


def test_callback_source_context_does_not_leak_into_next_user_turn(env):
    env.catalog.record_dispatch("Alice", "draft")
    instance, captured = make_runtime(env, [
        answer(("send_message_to_user", {"message": "Alice result", "source_result_ids": ["result-a"]})), answer(),
        answer(("send_message_to_user", {"message": "invalid reuse", "source_result_ids": ["result-a"]})), answer(),
    ])
    asyncio.run(instance.handle_agent_message("Alice result", [source()]))
    asyncio.run(instance.execute("next question"))
    assert env.log.replies == ["Alice result"]
    assert "Unknown source_result_id" in captured[-1]["messages"][-1]["content"]


def test_stale_callback_is_dropped_before_any_logging_or_model_call(env):
    previous_generation = env.catalog.get_generation()
    env.catalog.clear()
    instance, captured = make_runtime(env, [])
    result = asyncio.run(instance.handle_agent_message(
        "old result", [source()], catalog_generation=previous_generation
    ))
    assert not result.success
    assert captured == []
    assert env.log.agent_messages == []
    assert env.log.replies == []


def test_clear_during_model_call_prevents_direct_reply_and_dispatch(env):
    async def client(**kwargs):
        env.catalog.clear()
        return answer(("send_message_to_agent", {"agent_name": "Late worker", "instructions": "late", "create_if_missing": True}), text="late answer")

    instance = runtime.InteractionAgentRuntime(discovery_mode="focus", catalog=env.catalog, model_client=client)
    result = asyncio.run(instance.execute("old request"))
    assert not result.success
    assert env.log.replies == []
    assert env.roster.names == []
    assert env.dispatched == []


def test_changed_work_card_refreshes_before_dispatch_and_can_recover(env):
    env.catalog.record_dispatch("Alice", "original draft")
    calls = []
    async def client(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            env.catalog.record_dispatch("Alice", "newer draft: amount is now 42")
            return answer(("send_message_to_agent", {"agent_name": "Alice", "instructions": "continue original"}))
        if len(calls) == 2:
            assert env.dispatched == []
            assert env.log.requests == []
            assert "agent_state_refresh" in kwargs["messages"][-1]["content"]
            assert "42" in kwargs["messages"][-1]["content"]
            return answer(("send_message_to_agent", {"agent_name": "Alice", "instructions": "use the updated amount 42"}))
        return answer(text="Working on the updated draft.")
    instance = runtime.InteractionAgentRuntime(discovery_mode="focus", catalog=env.catalog, model_client=client)
    assert asyncio.run(instance.execute("Alice")).success
    assert len(env.dispatched) == 1
    assert env.dispatched[0][1] == "use the updated amount 42"
    assert len(instance.last_discovery_trace["state_refreshes"]) == 1


def test_repeated_state_changes_stop_after_one_refresh(env):
    env.catalog.record_dispatch("Alice", "original draft")
    calls = []
    async def client(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) <= 2:
            env.catalog.record_dispatch("Alice", f"new revision {len(calls)}")
            return answer(("send_message_to_agent", {"agent_name": "Alice", "instructions": "continue"}))
        assert "changed again" in kwargs["messages"][-1]["content"]
        return answer(text="The draft is still being updated.")
    instance = runtime.InteractionAgentRuntime(discovery_mode="focus", catalog=env.catalog, model_client=client)
    assert asyncio.run(instance.execute("Alice")).success
    assert env.dispatched == []
    assert env.log.requests == []
    assert len(instance.last_discovery_trace["state_refreshes"]) == 1


def test_unseen_existing_agent_requires_read_before_dispatch(env):
    env.catalog.record_dispatch("Old Kyoto trip", "reserve dinner")
    instance, captured = make_runtime(env, [answer(("send_message_to_agent", {
        "agent_name": "Old Kyoto trip", "instructions": "continue"
    })), answer(text="I found the earlier trip.")])
    assert asyncio.run(instance.execute("unmatched question")).success
    assert env.dispatched == []
    assert "agent_state_refresh" in captured[-1]["messages"][-1]["content"]


def test_unicode_state_refresh_bounds_final_tool_envelope(env):
    env.catalog.record_dispatch("Unicode worker", "original")
    captured = []
    async def client(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        if len(captured) == 1:
            env.catalog.record_dispatch("Unicode worker", "東京😀" * 500)
            return answer(("send_message_to_agent", {"agent_name": "Unicode worker", "instructions": "continue"}))
        return answer(text="I have the updated context.")
    instance = runtime.InteractionAgentRuntime(discovery_mode="focus", catalog=env.catalog, model_client=client)
    assert asyncio.run(instance.execute("Unicode worker")).success
    result = captured[-1]["messages"][-1]["content"]
    assert len(result) <= 6000
    assert "agent_state_refresh" in result
    card = json.loads(json.loads(result)["error"]["work_card"].splitlines()[1])
    assert card["agent_name"] == "Unicode worker"
    assert card["version"] == env.catalog.get_agent("Unicode worker")["version"]
    assert env.dispatched == []


@pytest.mark.parametrize("creation_flag,expected", [(None, False), ("true", False), (True, True)])
def test_missing_name_requires_explicit_boolean_creation(env, creation_flag, expected):
    arguments = {"agent_name": "New worker", "instructions": "new task"}
    if creation_flag is not None:
        arguments["create_if_missing"] = creation_flag
    instance, captured = make_runtime(env, [answer(("send_message_to_agent", arguments)), answer()])
    asyncio.run(instance.execute("new task"))
    assert bool(env.roster.names) is expected
    assert bool(env.dispatched) is expected
    assert bool(env.catalog.get_agent("New worker")) is expected


def test_discovery_tool_serialized_output_has_hard_bound_for_unicode(env):
    for index in range(12):
        env.catalog.record_dispatch(f"Topic {index}", "Topic " + "東京😀" * 200)
    instance, captured = make_runtime(env, [answer(("find_agents", {"query": "Topic"})), answer()])
    asyncio.run(instance.execute("Topic"))
    result_message = captured[-1]["messages"][-1]
    assert result_message["role"] == "tool"
    assert len(result_message["content"]) <= 6000
    assert len(instance.last_discovery_trace["expansions"]) == 1


def test_rejected_oversized_search_does_not_echo_unbounded_arguments(env):
    instance, captured = make_runtime(env, [answer(("find_agents", {"query": "x" * 20000})), answer()])
    asyncio.run(instance.execute("search"))
    result_message = captured[-1]["messages"][-1]
    assert json.loads(result_message["content"])["status"] == "error"
    assert len(result_message["content"]) <= 6000
    assert instance.last_discovery_trace["expansions"] == []


def test_overlapping_turns_have_separate_batches_and_trusted_sources(monkeypatch):
    async def scenario():
        ready = asyncio.Event()

        class Worker:
            def __init__(self, agent_name, **kwargs):
                self.name = agent_name

            async def execute(self, instructions):
                await ready.wait()
                return batch_manager.ExecutionResult(self.name, True, instructions)

        monkeypatch.setattr(batch_manager, "ExecutionAgentRuntime", Worker)
        manager = batch_manager.ExecutionBatchManager()
        delivered = []

        async def dispatch(payload, **metadata):
            delivered.append((payload, metadata))

        monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", dispatch)
        tasks = [asyncio.create_task(manager.execute_agent(
            name, "finished", request_id=f"result-{name}", correlation_id=f"turn-{name}", discovery_mode="focus"
        )) for name in ("Alice", "Bob")]
        await asyncio.sleep(0)
        ready.set()
        await asyncio.gather(*tasks)
        assert len(delivered) == 2
        for payload, metadata in delivered:
            assert len(metadata["source_results"]) == 1
            reference = metadata["source_results"][0]
            assert reference["correlation_id"] == metadata["correlation_id"]
            assert reference["result_id"] in payload
            assert reference["agent_name"] in payload
        assert manager.get_pending_executions() == []

    asyncio.run(scenario())


def test_cancelled_worker_balances_batch_and_preserves_other_result(env, monkeypatch):
    async def scenario():
        started = {name: asyncio.Event() for name in ("Alice", "Bob")}
        finish_bob = asyncio.Event()
        class Worker:
            def __init__(self, agent_name, **kwargs):
                self.name = agent_name
            async def execute(self, instructions):
                started[self.name].set()
                await (finish_bob.wait() if self.name == "Bob" else asyncio.Event().wait())
                return batch_manager.ExecutionResult(self.name, True, "Bob finished")
        monkeypatch.setattr(batch_manager, "ExecutionAgentRuntime", Worker)
        manager = batch_manager.ExecutionBatchManager()
        delivered = []
        async def dispatch(payload, **kwargs):
            delivered.append(payload)
        monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", dispatch)
        running = {name: asyncio.create_task(manager.execute_agent(
            name, "work", request_id=name, correlation_id="same-turn",
            discovery_mode="focus", catalog=env.catalog,
        )) for name in ("Alice", "Bob")}
        await asyncio.gather(*(event.wait() for event in started.values()))
        running["Alice"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await running["Alice"]
        assert env.catalog.get_agent("Alice")["last_run_status"] == "interrupted"
        finish_bob.set()
        await running["Bob"]
        assert len(delivered) == 1 and "Bob finished" in delivered[0]
        assert "unknown outcome" in delivered[0]
        assert manager.get_pending_executions() == []
        assert manager._batches == {}
    asyncio.run(scenario())


def test_legacy_batch_payload_has_no_new_metadata():
    manager = batch_manager.ExecutionBatchManager()
    result = batch_manager.ExecutionResult("Alice", True, "done")
    assert manager._format_batch_payload([result]) == "[SUCCESS] Alice: done"


def test_late_legacy_result_after_shutdown_cannot_enter_new_batch(monkeypatch):
    async def scenario():
        manager = batch_manager.ExecutionBatchManager()
        delivered = []

        async def dispatch(payload):
            delivered.append(payload)

        monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", dispatch)
        old_batch = await manager._register_pending_execution("Old", "old", "old-request")
        await manager.shutdown()
        new_batch = await manager._register_pending_execution("New", "new", "new-request")
        assert old_batch != new_batch
        await manager._complete_execution(old_batch, batch_manager.ExecutionResult("Old", True, "late"), "Old")
        assert delivered == []
        await manager._complete_execution(new_batch, batch_manager.ExecutionResult("New", True, "current"), "New")
        assert delivered == ["[SUCCESS] New: current"]

    asyncio.run(scenario())


def test_trigger_style_dispatch_honors_environment_and_captures_generation(env, monkeypatch):
    monkeypatch.setenv("OPENPOKE_AGENT_DISCOVERY", "focus")
    monkeypatch.setattr(catalog_module, "get_agent_catalog", lambda: env.catalog)
    original_generation = env.catalog.get_generation()

    class Worker:
        def __init__(self, agent_name, **kwargs):
            self.name = agent_name

        async def execute(self, instructions):
            env.catalog.clear()  # Callback must retain the generation at dispatch.
            return batch_manager.ExecutionResult(self.name, True, "finished")

    monkeypatch.setattr(batch_manager, "ExecutionAgentRuntime", Worker)
    manager = batch_manager.ExecutionBatchManager()
    delivered = []

    async def dispatch(payload, **metadata):
        delivered.append(metadata)

    monkeypatch.setattr(manager, "_dispatch_to_interaction_agent", dispatch)
    asyncio.run(manager.execute_agent("Scheduled worker", "scheduled task"))
    assert len(delivered) == 1
    metadata = delivered[0]
    assert metadata["discovery_mode"] == "focus"
    assert metadata["catalog"] is env.catalog
    assert metadata["catalog_generation"] == original_generation
    assert metadata["correlation_id"]
    assert metadata["source_results"][0]["correlation_id"] == metadata["correlation_id"]


def test_explicit_legacy_dispatch_stays_legacy_under_focus_environment(env, monkeypatch):
    monkeypatch.setenv("OPENPOKE_AGENT_DISCOVERY", "focus")
    instance, _ = make_runtime(env, [answer(("send_message_to_agent", {
        "agent_name": "Legacy new worker", "instructions": "task"
    })), answer()], mode="legacy")
    asyncio.run(instance.execute("task"))
    assert env.dispatched[0][2]["discovery_mode"] == "legacy"


@pytest.mark.parametrize("arguments", [
    {"agent_name": None, "instructions": "task", "create_if_missing": True},
    {"agent_name": 123, "instructions": "task", "create_if_missing": True},
    {"agent_name": "   ", "instructions": "task", "create_if_missing": True},
    {"agent_name": "New", "instructions": None, "create_if_missing": True},
    {"agent_name": "New", "instructions": ["task"], "create_if_missing": True},
    {"agent_name": "New", "instructions": "task", "create_if_missing": "true"},
    {"agent_name": "New", "instructions": "task", "create_if_missing": 1},
])
def test_malformed_bounded_dispatch_has_no_side_effects(env, arguments):
    async def scenario():
        with tools.tool_context(tools.ToolContext("focus", "turn", env.catalog, env.catalog.get_generation())):
            result = tools.send_message_to_agent(**arguments)
        assert not result.success

    asyncio.run(scenario())
    assert env.roster.names == []
    assert env.log.requests == []
    assert env.catalog.search("New") == []
    assert env.dispatched == []


def test_bounded_dispatch_without_event_loop_has_no_side_effects(env):
    with tools.tool_context(tools.ToolContext("focus", "turn", env.catalog, env.catalog.get_generation())):
        result = tools.send_message_to_agent("New", "task", create_if_missing=True)
    assert not result.success
    assert env.roster.names == []
    assert env.log.requests == []
    assert env.catalog.get_agent("New") is None
    assert env.dispatched == []


def test_plain_final_callback_text_is_visible_but_unattributed(env):
    env.catalog.record_dispatch("Alice", "draft")
    instance, _ = make_runtime(env, [answer(text="Alice's draft is ready")])
    result = asyncio.run(instance.handle_agent_message("Alice completed", [source()]))
    assert result.success
    assert env.log.replies == ["Alice's draft is ready"]
    assert env.catalog.recent_published() == []


def test_clear_history_does_not_yield_to_interim_backfill(env, monkeypatch):
    import server.services as services
    from server.routes import chat

    env.roster.names = ["Old worker"]
    env.catalog.ensure_legacy_backfill(env.roster)
    events = []
    original_clear = env.catalog.clear

    def interim_backfill():
        events.append("backfill")
        env.catalog.ensure_legacy_backfill(env.roster)

    def clear_catalog():
        events.append("catalog")
        original_clear()
        asyncio.get_running_loop().call_soon(interim_backfill)

    def clear_roster():
        events.append("roster")
        env.roster.names.clear()

    monkeypatch.setattr(env.catalog, "clear", clear_catalog)
    monkeypatch.setattr(catalog_module, "get_agent_catalog", lambda: env.catalog)
    monkeypatch.setattr(chat, "get_conversation_log", lambda: SimpleNamespace(clear=lambda: events.append("conversation")))
    monkeypatch.setattr(services, "get_execution_agent_logs", lambda: SimpleNamespace(clear_all=lambda: events.append("execution")))
    monkeypatch.setattr(services, "get_agent_roster", lambda: SimpleNamespace(clear=clear_roster))
    monkeypatch.setattr(chat, "get_trigger_service", lambda: SimpleNamespace(clear_all=lambda: events.append("triggers")))

    async def scenario():
        await chat.clear_history()
        assert "backfill" not in events
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert events == ["catalog", "conversation", "execution", "roster", "triggers", "backfill"]
    assert env.catalog.get_agent("Old worker") is None
