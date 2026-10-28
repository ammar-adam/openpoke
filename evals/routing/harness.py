"""Run the real interaction loop with isolated state and no external actions.

Scripted responses test protocol and scoring only. Live responses can measure
worker selection on the development scenarios; they do not test worker actions.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections import Counter
from contextlib import ExitStack
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .cases import Case
from .budget import BudgetExhausted, positive_integer, positive_number


class ModelTransportError(RuntimeError):
    """Sanitized model-service failure; not a routing judgment."""

    def __init__(self, message="Model request failed", http_status=None):
        super().__init__(message)
        self.http_status = http_status


def _http_status(error):
    for _ in range(5):
        if error is None:
            break
        status = getattr(error, "http_status", None) or getattr(getattr(error, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return status
        error = error.__cause__
    return None


class MemoryLog:
    def __init__(self, history=""):
        self.transcript = history
        self.replies = []

    def load_transcript(self):
        return self.transcript

    def render_transcript(self):
        return self.transcript

    def record_user_message(self, content):
        self.transcript += f"\n<user_message>{escape(content, quote=False)}</user_message>"

    def record_agent_message(self, content):
        self.transcript += f"\n<agent_message>{escape(content, quote=False)}</agent_message>"

    def record_reply(self, content):
        self.replies.append(content)
        self.transcript += f"\n<poke_reply>{escape(content, quote=False)}</poke_reply>"

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


async def run_case(case, mode, catalog, roster, responder, *, model=None, before_model_call=None):
    """Run one user turn, preserving the original public harness contract."""
    return await _run_session(case, mode, catalog, roster, responder, model=model,
                              before_model_call=before_model_call, two_turn=False)


async def run_two_turn_case(case, mode, catalog, roster, responder, *, model=None, before_model_call=None):
    """Run a real callback then follow-up with one shared log and catalog.

    Publication evidence is produced only by actual IA publication tool calls.
    Callback dispatches are recorded separately and cannot satisfy the follow-up.
    """
    return await _run_session(case, mode, catalog, roster, responder, model=model,
                              before_model_call=before_model_call, two_turn=True)


def _annotation(case, mode, callback):
    expected = list(getattr(case, "expected_source_ids", ()))
    if mode == "legacy":
        return {"status": "not_applicable", "correct": None,
                "expected_source_ids": expected, "actual_source_ids": []}
    actual, attempted = set(), set()
    for call in callback["tool_calls"]:
        if call["name"] not in {"send_message_to_user", "send_draft"}:
            continue
        supplied = call["arguments"].get("source_result_ids", [])
        if isinstance(supplied, list):
            ids = {value for value in supplied if isinstance(value, str)}
            attempted.update(ids)
            if call["success"]:
                actual.update(ids)
    correct = actual == set(expected)
    status = "correct" if correct else "wrong" if actual - set(expected) else "missing"
    return {"status": status, "correct": correct, "expected_source_ids": expected,
            "actual_source_ids": sorted(actual), "attempted_source_ids": sorted(attempted),
            "rejected_source_ids": sorted(attempted-actual),
            "meaning": "source-ID agreement only; displayed content still needs review"}


async def _run_session(case, mode, catalog, roster, responder, *, model, before_model_call, two_turn):
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
        async def turn(turn_name, payload=None, sources=None):
            initial_roster = set(roster.get_agents())
            runtime = runtime_module.InteractionAgentRuntime(discovery_mode=mode, catalog=catalog)
            if mode == "legacy":
                runtime.tool_schemas = json.loads((baseline / "tools.json").read_text(encoding="utf-8"))
            execute_tool = runtime._execute_tool
            tool_calls, model_calls, dispatches = [], [], []
            failure = None
            turn_started, reply_start = time.perf_counter(), len(log.replies)

            def traced_tool(call):
                result = execute_tool(call)
                tool_calls.append({"turn": turn_name, "name": call.name, "arguments": call.arguments,
                                   "success": result.success, "payload": result.payload})
                if call.name == "send_message_to_agent" and result.success:
                    dispatches.append(call.arguments.get("agent_name", ""))
                return result

            async def model_call(system_prompt, messages):
                nonlocal failure
                serialized = json.dumps({"system": system_prompt, "messages": messages, "tools": runtime.tool_schemas}, ensure_ascii=False)
                call_record = {"turn": turn_name, "input_characters": len(serialized), "usage": None, "model": None, "status": "attempted"}
                call_index = len(model_calls)
                model_calls.append(call_record)
                call_started = time.perf_counter()
                prior_network_calls = getattr(responder, "calls", None)
                try:
                    if before_model_call is not None:
                        try:
                            hook_result = before_model_call(catalog, turn_name, call_index)
                            if inspect.isawaitable(hook_result):
                                await hook_result
                        except Exception:
                            failure = "mutation_hook_error"
                            call_record.update(status="hook_error", error="Evaluation mutation hook failed")
                            raise ModelTransportError("Evaluation mutation hook failed") from None
                    try:
                        response = await responder(system_prompt, messages, runtime.tool_schemas)
                    except BudgetExhausted:
                        failure = "budget_exhausted"
                        call_record.update(status="budget_exhausted", error="No request started: evaluation budget exhausted")
                        raise
                    except Exception as exc:
                        failure = "model_transport_error"
                        status = _http_status(exc)
                        message = f"Model request failed (HTTP {status})" if status else "Model request failed"
                        call_record.update(status="error", error=message, error_type=type(exc).__name__, http_status=status)
                        raise ModelTransportError(message, status) from None
                    call_record.update(usage=response.get("usage"), model=response.get("model"), status="returned",
                                       finish_reasons=[choice.get("finish_reason") for choice in response.get("choices", []) if isinstance(choice, dict)])
                    return response
                finally:
                    call_record["latency_ms"] = (time.perf_counter() - call_started) * 1000
                    if prior_network_calls is not None:
                        call_record["network_calls_started"] = responder.calls - prior_network_calls

            runtime._execute_tool = traced_tool
            runtime._make_llm_call = model_call
            if turn_name == "callback":
                result = await runtime.handle_agent_message(payload) if mode == "legacy" else await runtime.handle_agent_message(
                    payload, source_results=sources, correlation_id=sources[0]["correlation_id"],
                    catalog_generation=catalog.get_generation())
            else:
                result = await runtime.execute(case.query)
            await asyncio.sleep(0)  # Drain mocked submissions while patches remain active.
            scored = score_dispatches(case, dispatches, known_names)
            observed = scored["automatic_correct"]
            record_status = "completed"
            if failure:
                made_request = any(call["status"] == "returned" or call.get("network_calls_started", 0) > 0 for call in model_calls)
                record_status = ("incomplete" if made_request else "not_run") if failure == "budget_exhausted" else "operational_error"
                scored.update(automatic_correct=None, outcome=failure)
            elif not result.success:
                scored.update(automatic_correct=False, outcome="runtime_failed")
            if turn_name == "callback":
                scored["automatic_correct"] = None
                observed = None
            refreshes = sum(call["payload"].get("code") == "agent_state_refresh" for call in tool_calls if isinstance(call["payload"], dict))
            second_stale = sum("state changed again" in str(call["payload"].get("error", "")) for call in tool_calls if isinstance(call["payload"], dict))
            accepted_new = [call["arguments"].get("agent_name") for call in tool_calls if call["name"] == "send_message_to_agent" and call["success"] and call["payload"].get("new_agent_created")]
            trace = getattr(runtime, "last_discovery_trace", None)
            initial = (trace or {}).get("initial")
            initial_names = initial_roster if mode == "legacy" else ({entry["agent_name"] for entry in initial["selected"]} if initial else None)
            targets = set(case.expected_agents)
            initial_present = targets.issubset(initial_names) if targets and initial_names is not None else None
            return {"case_id": case.case_id, "category": case.category, "mode": mode, "turn": turn_name,
                "evidence": case.evidence, "expected_action": "publish" if turn_name == "callback" else case.expected_action,
                "expected_agents": [] if turn_name == "callback" else list(case.expected_agents), "dispatches": dispatches,
                "runtime_success": result.success, "runtime_error": result.error,
                "response": result.response, "published_messages": log.replies[reply_start:],
                "tool_calls": tool_calls, "model_calls": model_calls,
                "discovery_trace": trace, "initial_all_targets_present": initial_present,
                "latency_ms": (time.perf_counter()-turn_started)*1000,
                "record_status": record_status, "failure_reason": failure,
                "observed_dispatch_correct": observed, "refresh_count": refreshes,
                "refresh_then_dispatch": bool(refreshes and dispatches), "second_stale_refusals": second_stale,
                "accepted_new_workers": accepted_new,
                "create_if_missing_set": sum(call["arguments"].get("create_if_missing") is True for call in tool_calls if call["name"] == "send_message_to_agent"),
                "search_expansions": sum(call["name"] == "find_agents" for call in tool_calls), **scored}

        if not two_turn:
            return await turn("user")
        from server.agents.execution_agent.batch_manager import ExecutionBatchManager
        from server.agents.execution_agent.runtime import ExecutionResult
        callback_results = list(getattr(case, "callback_results", ()))
        if not callback_results:
            raise ValueError("A two-turn case requires callback_results")
        sources = [{key: item[key] for key in ("agent_name", "result_id", "correlation_id")} for item in callback_results]
        if any(not isinstance(value, str) or not value for source in sources for value in source.values()):
            raise ValueError("Callback sources require nonempty identifiers")
        if len({source["result_id"] for source in sources}) != len(sources) or len({source["correlation_id"] for source in sources}) != 1:
            raise ValueError("Callback sources must have unique result IDs and one correlation ID")
        for item in callback_results:
            catalog.record_run_started(item["agent_name"], item["result_id"])
            catalog.record_run_finished(item["agent_name"], item["result_id"],
                                       "completed" if item.get("success", True) else "failed", item["response"])
        results = [ExecutionResult(item["agent_name"], bool(item.get("success", True)), item["response"]) for item in callback_results]
        payload = ExecutionBatchManager()._format_batch_payload(results, sources if mode != "legacy" else None)
        callback = await turn("callback", payload, sources)
        annotation = _annotation(case, mode, callback)
        if callback["record_status"] != "completed" or not callback["runtime_success"]:
            return {**callback, "turn": "two_turn", "dispatches": [],
                    "callback_dispatches": callback["dispatches"], "turns": {"callback": callback, "followup": None},
                    "automatic_correct": False if callback["record_status"] == "completed" else None,
                    "outcome": "callback_failed" if callback["record_status"] == "completed" else callback["outcome"], "publication_annotation": annotation,
                    "published_content_manual_review_required": True, "manual_review_required": True}
        for index, item in enumerate(getattr(case, "silent_results", ())):
            run_id = item.get("result_id", f"silent-{case.case_id}-{index}")
            catalog.record_run_started(item["agent_name"], run_id)
            catalog.record_run_finished(item["agent_name"], run_id,
                                       "completed" if item.get("success", True) else "failed", item["response"])
        followup = await turn("followup")
        record = {**followup, "turn": "two_turn", "turns": {"callback": callback, "followup": followup},
                  "callback_dispatches": callback["dispatches"], "publication_annotation": annotation,
                  "published_content_manual_review_required": True, "manual_review_required": True,
                  "model_calls": callback["model_calls"] + followup["model_calls"],
                  "tool_calls": callback["tool_calls"] + followup["tool_calls"],
                  "published_messages": callback["published_messages"] + followup["published_messages"],
                  "latency_ms": (time.perf_counter()-started)*1000,
                  "followup_initial_all_targets_present": followup["initial_all_targets_present"],
                  "followup_automatic_correct": followup["automatic_correct"]}
        if callback["dispatches"] and record["record_status"] == "completed":
            record.update(automatic_correct=False, outcome="unexpected_callback_dispatch")
        if record["record_status"] == "not_run":
            record["record_status"] = "incomplete"  # Callback already consumed requests.
        return record


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
    def __init__(self, *, model, api_key, max_calls, max_estimated_usd, input_rate, output_rate, max_output_tokens=1024, ledger=None, min_call_interval_seconds=0):
        positive_integer(max_calls, "max_calls")
        positive_integer(max_output_tokens, "max_output_tokens")
        for name, value in (("max_estimated_usd", max_estimated_usd), ("input_rate", input_rate), ("output_rate", output_rate)):
            positive_number(value, name)
        if (isinstance(min_call_interval_seconds, bool)
                or not isinstance(min_call_interval_seconds, (int, float))
                or not math.isfinite(min_call_interval_seconds)
                or min_call_interval_seconds < 0):
            raise ValueError("min_call_interval_seconds must be finite and nonnegative")
        self.model, self.api_key = model, api_key
        self.max_calls, self.max_estimated_usd = max_calls, max_estimated_usd
        self.input_rate, self.output_rate = input_rate, output_rate
        self.max_output_tokens = max_output_tokens
        self.calls = 0
        self.reserved_estimated_usd = 0.0
        self.ledger = ledger
        self.last_reservation = None
        self.min_call_interval_seconds = min_call_interval_seconds
        self._last_call_started = None
        self._reservation_lock = asyncio.Lock()

    async def __call__(self, system, messages, schemas):
        from server.openrouter_client import request_chat_completion
        encoded = json.dumps({"system": system, "messages": messages, "tools": schemas}, ensure_ascii=False).encode("utf-8")
        reserved = ((len(encoded) + 2048) * self.input_rate + self.max_output_tokens * self.output_rate) / 1_000_000
        # Serialize request starts, not whole responses. Waiting/cancellation does
        # not reserve budget; a failed request still does. This is local pacing,
        # not a cross-process rate limiter or a provider-error retry policy.
        async with self._reservation_lock:
            if self.calls >= self.max_calls or self.reserved_estimated_usd + reserved > self.max_estimated_usd:
                raise BudgetExhausted("Live evaluation call/estimated-cost ceiling reached; no request was started")
            if self._last_call_started is not None:
                delay = self.min_call_interval_seconds - (time.monotonic() - self._last_call_started)
                if delay > 0:
                    await asyncio.sleep(delay)
            if self.ledger is not None:
                self.last_reservation = self.ledger.reserve(reserved, model=self.model)
            self.calls += 1
            self.reserved_estimated_usd += reserved
            self._last_call_started = time.monotonic()
        try:
            return await request_chat_completion(model=self.model, messages=messages, system=system,
                api_key=self.api_key, tools=schemas, max_tokens=self.max_output_tokens)
        except Exception as exc:
            status = _http_status(exc)
            message = f"Model transport failed (HTTP {status})" if status else f"Model transport failed ({type(exc).__name__})"
            raise ModelTransportError(message, status) from None
