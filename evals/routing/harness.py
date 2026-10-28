"""Run the real interaction loop with isolated state and no external actions.

Scripted responses test protocol and scoring only. Live responses can measure
worker selection on the development scenarios; they do not test worker actions.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .cases import Case


class MemoryLog:
    def __init__(self, history=""):
        self.transcript = history
        self.replies = []

    def load_transcript(self):
        return self.transcript

    def render_transcript(self):
        return self.transcript

    def record_user_message(self, content):
        self.transcript += f"\n<user_message>{content}</user_message>"

    def record_agent_message(self, content):
        self.transcript += f"\n<agent_message>{content}</agent_message>"

    def record_reply(self, content):
        self.replies.append(content)
        self.transcript += f"\n<poke_reply>{content}</poke_reply>"

    def record_wait(self, reason):
        pass


class MemoryRoster:
    def __init__(self, names):
        self.names = list(names)

    def load(self):
        pass

    def get_agents(self):
        return list(self.names)

    def add_agent(self, name):
        if name not in self.names:
            self.names.append(name)


class NoExecution:
    async def execute_agent(self, agent_name, instructions, **kwargs):
        return SimpleNamespace(agent_name=agent_name, success=True, response="Execution mocked by routing evaluation.")


def response_with_calls(*calls, text=""):
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": [
        {"id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (name, args) in enumerate(calls)
    ]}}]}


def score_dispatches(case: Case, successful_dispatches: list[str], known_names: set[str]):
    """Score the entire dispatch list. No LLM judge or question-mark heuristic."""
    got = Counter(successful_dispatches)
    expected = Counter(case.expected_agents)
    created = [name for name in successful_dispatches if name not in known_names]
    if case.expected_action == "route":
        correct = got == expected
        outcome = "correct_worker_dispatches" if correct else "incorrect_worker_dispatches"
    elif case.expected_action == "new":
        correct = len(successful_dispatches) == 1 and len(created) == 1
        outcome = "new_worker_created" if correct else "incorrect_new_work_handling"
    else:
        # The response needs human review to distinguish a useful answer from an
        # unnecessary question, refusal, or empty output. Do not invent accuracy.
        correct = False if got else None
        outcome = "unexpected_dispatch" if got else "response_requires_review"
    return {
        "automatic_correct": correct,
        "outcome": outcome,
        "new_workers": created,
        "unexpected_dispatches": list((got - expected).elements()) if case.expected_action == "route" else [],
        "duplicate_dispatches": sum(max(0, n - 1) for n in got.values()),
        "manual_review_required": case.expected_action in {"ask", "answer"},
    }


async def run_case(case, mode, catalog, roster, responder, *, model=None):
    """Responder receives system, messages and schemas; no real worker is run."""
    import importlib
    runtime_module = importlib.import_module("server.agents.interaction_agent.runtime")
    agent_module = importlib.import_module("server.agents.interaction_agent.agent")
    tool_module = importlib.import_module("server.agents.interaction_agent.tools")
    from server.config import get_settings

    log = MemoryLog(case.history)
    known_names = set(roster.get_agents())
    settings = get_settings().model_copy(update={
        "openrouter_api_key": "evaluation-model-client-is-injected",
        "interaction_agent_model": model or get_settings().interaction_agent_model,
        "conversation_summary_threshold": 0,
    })
    tool_calls = []
    model_calls = []
    dispatches = []
    baseline = Path(__file__).parent / "baseline"
    started = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(patch.object(runtime_module, "get_settings", return_value=settings))
        stack.enter_context(patch.object(runtime_module, "get_conversation_log", return_value=log))
        stack.enter_context(patch.object(runtime_module, "get_working_memory_log", return_value=log))
        stack.enter_context(patch.object(runtime_module, "get_agent_roster", return_value=roster))
        stack.enter_context(patch.object(agent_module, "get_agent_roster", return_value=roster))
        stack.enter_context(patch.object(tool_module, "get_agent_roster", return_value=roster))
        stack.enter_context(patch.object(tool_module, "get_conversation_log", return_value=log))
        stack.enter_context(patch.object(tool_module, "get_execution_agent_logs", return_value=SimpleNamespace(record_request=lambda *args: None)))
        stack.enter_context(patch.object(tool_module, "_EXECUTION_BATCH_MANAGER", NoExecution()))
        if mode == "legacy":
            original_prompt = (baseline / "system_prompt.txt").read_text(encoding="utf-8").strip()
            stack.enter_context(patch.object(runtime_module, "build_system_prompt", return_value=original_prompt))
        runtime = runtime_module.InteractionAgentRuntime(discovery_mode=mode, catalog=catalog)
        if mode == "legacy":
            runtime.tool_schemas = json.loads((baseline / "tools.json").read_text(encoding="utf-8"))
        execute_tool = runtime._execute_tool

        def traced_tool(call):
            result = execute_tool(call)
            tool_calls.append({"name": call.name, "arguments": call.arguments, "success": result.success, "payload": result.payload})
            if call.name == "send_message_to_agent" and result.success:
                dispatches.append(call.arguments.get("agent_name", ""))
            return result

        async def model_call(system_prompt, messages):
            serialized = json.dumps({"system": system_prompt, "messages": messages, "tools": runtime.tool_schemas}, ensure_ascii=False)
            call_record = {"input_characters": len(serialized), "usage": None, "model": None, "status": "attempted"}
            model_calls.append(call_record)
            call_started = time.perf_counter()
            prior_network_calls = getattr(responder, "calls", None)
            try:
                response = await responder(system_prompt, messages, runtime.tool_schemas)
                call_record.update(usage=response.get("usage"), model=response.get("model"), status="returned")
                return response
            except Exception as exc:
                call_record.update(status="error", error=str(exc))
                raise
            finally:
                call_record["latency_ms"] = (time.perf_counter() - call_started) * 1000
                if prior_network_calls is not None:
                    call_record["network_calls_started"] = responder.calls - prior_network_calls

        runtime._execute_tool = traced_tool
        runtime._make_llm_call = model_call
        result = await runtime.execute(case.query)
        # Drain mocked asynchronous submissions before tearing down patched tools.
        import asyncio
        await asyncio.sleep(0)
        trace = getattr(runtime, "last_discovery_trace", None)
    scored = score_dispatches(case, dispatches, known_names)
    if not result.success:
        scored.update(automatic_correct=False, outcome="runtime_failed")
    return {
        "case_id": case.case_id, "category": case.category, "mode": mode,
        "evidence": case.evidence, "expected_action": case.expected_action,
        "expected_agents": list(case.expected_agents), "dispatches": dispatches,
        "runtime_success": result.success, "runtime_error": result.error,
        "response": result.response, "published_messages": log.replies,
        "tool_calls": tool_calls, "model_calls": model_calls,
        "discovery_trace": trace, "latency_ms": (time.perf_counter() - started) * 1000,
        **scored,
    }


class ScriptedResponder:
    """Explicit test double. Never use its outcomes as model accuracy."""
    def __init__(self, responses):
        self.responses = iter(responses)

    async def __call__(self, system, messages, schemas):
        try:
            return next(self.responses)
        except StopIteration:
            return response_with_calls(text="Finished scripted protocol test.")


class LiveResponder:
    """Opt-in model adapter with a call ceiling and declared-price cost estimate.

The money ceiling uses supplied prices and conservative input-size estimates;
it is not a provider billing guarantee. Record actual reported usage separately.
"""
    def __init__(self, *, model, api_key, max_calls, max_estimated_usd, input_rate, output_rate, max_output_tokens=1024):
        if min(max_calls, max_estimated_usd, input_rate, output_rate, max_output_tokens) <= 0:
            raise ValueError("All live evaluation limits and prices must be positive")
        self.model, self.api_key = model, api_key
        self.max_calls, self.max_estimated_usd = max_calls, max_estimated_usd
        self.input_rate, self.output_rate = input_rate, output_rate
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self.reserved_estimated_usd = 0.0

    async def __call__(self, system, messages, schemas):
        from server.openrouter_client import request_chat_completion
        encoded = json.dumps({"system": system, "messages": messages, "tools": schemas}, ensure_ascii=False).encode("utf-8")
        reserved = ((len(encoded) + 2048) * self.input_rate + self.max_output_tokens * self.output_rate) / 1_000_000
        if self.calls >= self.max_calls or self.reserved_estimated_usd + reserved > self.max_estimated_usd:
            raise RuntimeError("Live evaluation call/estimated-cost ceiling reached")
        self.calls += 1
        self.reserved_estimated_usd += reserved
        return await request_chat_completion(model=self.model, messages=messages, system=system,
            api_key=self.api_key, tools=schemas, max_tokens=self.max_output_tokens)
