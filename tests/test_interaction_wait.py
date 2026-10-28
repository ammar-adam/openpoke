"""Successful wait yields the shared IA loop without losing same-batch work."""

import asyncio

import pytest

from test_discovery_runtime import answer, env, make_runtime, source


@pytest.mark.parametrize("mode", ["legacy", "retrieval", "focus"])
def test_wait_ends_turn_after_full_batch_and_preserves_user_output(env, mode):
    env.roster.names = ["Alice"]
    env.catalog.record_dispatch("Alice", "invoice")
    instance, captured = make_runtime(env, [answer(
        ("wait", {"reason": "Waiting for the worker"}),
        ("send_message_to_agent", {"agent_name": "Alice", "instructions": "Revise invoice"}),
        ("send_message_to_user", {"message": "I will revise the invoice."}),
    )], mode=mode)

    async def run():
        result = await instance.execute("Alice")
        await asyncio.sleep(0)  # Pending mock workers run on the surviving loop.
        return result

    result = asyncio.run(run())
    assert result.success
    assert result.response == "I will revise the invoice."
    assert len(captured) == 1
    assert env.log.waits == ["Waiting for the worker"]
    assert env.log.replies == ["I will revise the invoice."]
    assert [(name, instructions) for name, instructions, _ in env.dispatched] == [("Alice", "Revise invoice")]


@pytest.mark.parametrize("mode", ["legacy", "retrieval", "focus"])
def test_wait_suppresses_unpublished_assistant_fallback_and_does_not_repeat(env, mode):
    instance, captured = make_runtime(env, [answer(
        ("wait", {"reason": "Already answered"}), text="Duplicate of an earlier answer",
    )], mode=mode)
    result = asyncio.run(instance.execute("same answer"))
    assert result.success
    assert result.response == ""
    assert env.log.replies == []
    assert len(captured) == 1


def test_failed_wait_can_be_corrected_on_next_model_call(env):
    instance, captured = make_runtime(env, [
        answer(("wait", {})),  # Missing required reason: not a terminal success.
        answer(("send_message_to_user", {"message": "A useful answer."})),
        answer(text=""),
    ])
    result = asyncio.run(instance.execute("question"))
    assert result.success
    assert result.response == "A useful answer."
    assert len(captured) == 3
    assert env.log.waits == []
    assert env.log.replies == ["A useful answer."]


def test_wait_preserves_draft_and_source_publication_without_duplicate_reply(env):
    env.catalog.record_dispatch("Alice", "invoice")
    instance, captured = make_runtime(env, [answer(
        ("wait", {"reason": "No extra acknowledgement is needed"}),
        ("send_draft", {"to": "alice@example.test", "subject": "Invoice", "body": "Please review.", "source_result_ids": ["result-a"]}),
        text="Duplicate draft prose",
    )])
    result = asyncio.run(instance.handle_agent_message("Invoice prepared", [source()]))
    assert result.success
    assert result.response == ""
    assert len(captured) == 1
    assert env.log.replies == ["To: alice@example.test\nSubject: Invoice\n\nPlease review."]
    assert [card["agent_name"] for card in env.catalog.recent_published()] == ["Alice"]


@pytest.mark.parametrize("mode", ["legacy", "retrieval", "focus"])
def test_waiting_turn_does_not_block_later_callback_or_leak_wait_state(env, mode):
    env.roster.names = ["Alice"]
    env.catalog.record_dispatch("Alice", "invoice")
    instance, captured = make_runtime(env, [
        answer(("send_message_to_agent", {"agent_name": "Alice", "instructions": "Revise invoice"})),
        answer(("wait", {"reason": "Worker is running"})),
        answer(("send_message_to_user", {"message": "The invoice is ready."})),
        answer(text=""),
    ], mode=mode)

    async def run():
        user_result = await instance.execute("Alice")
        await asyncio.sleep(0)
        assert len(env.dispatched) == 1
        callback_result = await instance.handle_agent_message("Invoice revised", [source()])
        return user_result, callback_result

    user_result, callback_result = asyncio.run(run())
    assert user_result.success and user_result.response == ""
    assert callback_result.success and callback_result.response == "The invoice is ready."
    assert len(captured) == 4
    assert env.log.replies == ["The invoice is ready."]


def test_wait_on_last_allowed_iteration_is_a_clean_yield(env):
    instance, captured = make_runtime(env, [answer(("wait", {"reason": "Done for now"}))])
    instance.MAX_TOOL_ITERATIONS = 1
    result = asyncio.run(instance.execute("question"))
    assert result.success
    assert len(captured) == 1
