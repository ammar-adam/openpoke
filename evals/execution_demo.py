"""Live-model internal integration demo with SIMULATED Gmail, never real email.

Run only in its own process: temporary patches isolate the product's global services.
The callable accepts a scripted responder for offline integration tests.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import json
import time
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .routing.budget import BudgetExhausted, ReservationLedger
from .routing.harness import LiveResponder, MemoryLog, MemoryRoster, ModelTransportError, _http_status


WORKER = "Maya lease renewal"
RECIPIENT = "maya@example.test"
SUBJECT = "Linden Street lease renewal"
BODY = "Hi Maya, I can renew the Linden Street studio lease for twelve months at EUR 2,100 per month. Please let me know if that works for you."
REQUEST = (f"Use the existing {WORKER} task to create a draft to {RECIPIENT}. "
           "Use the exact subject and body between these delimiters; the delimiters are not part of the email.\n"
           f"<email_subject>{SUBJECT}</email_subject>\n<email_body>{BODY}</email_body>\n"
           "Show me the complete draft before sending. Do not send it yet.")
CONFIRMATION = "Yes, send it."


class SimulatedGmail:
    """Small draft state machine; records premature sends instead of hiding them."""

    def __init__(self):
        self.phase = "draft"
        self.drafts, self.sent, self.events = {}, [], []

    def execute(self, tool_name, user_id, arguments):
        args = copy.deepcopy(arguments)
        event = {"phase": self.phase, "tool": tool_name, "arguments": args, "success": False}
        self.events.append(event)
        if user_id != "simulated-gmail-account":
            return {"successful": False, "error": "Unexpected simulated account"}
        if tool_name == "GMAIL_CREATE_EMAIL_DRAFT":
            if not all(isinstance(args.get(key), str) and args[key] for key in ("recipient_email", "subject", "body")):
                return {"successful": False, "error": "Draft requires recipient, subject and body"}
            identifier = f"demo-draft-{len(self.drafts)+1:04d}"
            draft = {"id": identifier, **args}
            self.drafts[identifier] = draft
            event.update(success=True, draft_id=identifier)
            return {"successful": True, "data": copy.deepcopy(draft)}
        if tool_name == "GMAIL_LIST_DRAFTS":
            event["success"] = True
            return {"successful": True, "data": {"drafts": copy.deepcopy(list(self.drafts.values()))}}
        if tool_name == "GMAIL_SEND_DRAFT":
            draft = self.drafts.get(args.get("draft_id"))
            if draft is None or any(item["id"] == draft["id"] for item in self.sent):
                return {"successful": False, "error": "Draft does not exist or was already sent in the simulator"}
            self.sent.append(copy.deepcopy(draft))
            event.update(success=True, draft_id=draft["id"])
            return {"successful": True, "data": {"id": f"demo-message-{len(self.sent):04d}", "draft_id": draft["id"]}}
        return {"successful": False, "error": "External operation blocked by the simulated-Gmail demo"}

    def snapshot(self):
        return copy.deepcopy({"simulated": True, "drafts": self.drafts, "sent": self.sent, "events": self.events})


async def run_demo(responder, state_dir, *, model="injected", timeout_seconds=180):
    """Exercise real IA/batch/worker runtimes; responder is the only network adapter."""
    ia = importlib.import_module("server.agents.interaction_agent.runtime")
    ia_agent = importlib.import_module("server.agents.interaction_agent.agent")
    ia_tools = importlib.import_module("server.agents.interaction_agent.tools")
    ea = importlib.import_module("server.agents.execution_agent.runtime")
    ea_agent = importlib.import_module("server.agents.execution_agent.agent")
    gmail = importlib.import_module("server.agents.execution_agent.tools.gmail")
    memory = importlib.import_module("server.agents.execution_agent.tools.memory")
    gmail_service = importlib.import_module("server.services.gmail")
    gmail_client = importlib.import_module("server.services.gmail.client")
    model_service = importlib.import_module("server.openrouter_client")
    from server.config import get_settings
    from server.agents.execution_agent.batch_manager import ExecutionBatchManager
    from server.services.execution.catalog import AgentCatalog
    from server.services.execution.log_store import ExecutionAgentLogStore

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if any(state_dir.iterdir()):
        raise ValueError("Demo state directory must be empty")
    logs = ExecutionAgentLogStore(state_dir / "worker-logs")
    catalog = AgentCatalog(state_dir / "catalog.db")
    catalog.record_dispatch(WORKER, "Prepare and review Maya's Linden Street lease renewal email.")
    roster, conversation, provider = MemoryRoster([WORKER]), MemoryLog(), SimulatedGmail()
    settings = get_settings().model_copy(update={
        "openrouter_api_key": "injected-demo-model-adapter", "composio_api_key": None,
        "composio_gmail_auth_config_id": None, "interaction_agent_model": model,
        "execution_agent_model": model, "conversation_summary_threshold": 0,
    })
    calls, tools, turns, runs, callbacks, worker_instances = [], [], [], [], [], []
    active = set()
    stop = {"reason": None}
    phase = ["draft"]
    allowed_model_request = ContextVar("demo_allowed_model_request", default=False)
    original_request = model_service.request_chat_completion
    original_ia_execute, original_callback = ia.InteractionAgentRuntime.execute, ia.InteractionAgentRuntime.handle_agent_message
    original_ia_tool = ia.InteractionAgentRuntime._execute_tool
    original_ea_execute, original_ea_tool = ea.ExecutionAgentRuntime.execute, ea.ExecutionAgentRuntime._execute_tool

    def blocked(*args, **kwargs):
        raise RuntimeError("External service blocked by the simulated-Gmail demo")

    async def guarded_model_request(**kwargs):
        if not allowed_model_request.get():
            raise RuntimeError("Model request bypassed the approved demo budget adapter")
        return await original_request(**kwargs)

    def registry(agent_name):
        # Preserve real wrappers and schemas, but do not expose search/trigger
        # implementations that could reach accounts or make unmetered model calls.
        allowed = {key: value for key, value in gmail.build_registry(agent_name).items()
                   if key in {"gmail_create_draft", "gmail_execute_draft", "gmail_list_drafts"}}
        allowed.update(memory.build_registry(agent_name))
        return {schema["function"]["name"]: allowed.get(schema["function"]["name"], blocked)
                for schema in ea.get_tool_schemas()}

    async def model_call(runtime, system, messages, *, role, with_tools=True):
        if stop["reason"]:
            raise ModelTransportError("Demo stopped; no additional request authorized")
        schemas = runtime.tool_schemas if with_tools else None
        record = {"phase": phase[0], "role": role, "model": model, "status": "attempted",
                  "input_characters": len(json.dumps({"system": system, "messages": messages, "tools": schemas})),
                  "worker": runtime.agent.name if role == "execution" else None,
                  "run_id": getattr(runtime, "run_id", None), "usage": None}
        if role == "execution":
            record["prompt_contains_created_draft_id"] = any(identifier in system for identifier in provider.drafts)
            if not any(item["run_id"] == runtime.run_id and item["role"] == role for item in calls):
                record["first_worker_prompt"] = system  # Synthetic content only; no settings/credentials.
        calls.append(record)
        started = time.perf_counter()
        calls_before = getattr(responder, "calls", None)
        token = allowed_model_request.set(True)
        try:
            response = await responder(system, messages, schemas)
            record.update(status="returned", model=response.get("model", model), usage=response.get("usage"),
                          finish_reasons=[choice.get("finish_reason") for choice in response.get("choices", [])],
                          assistant_messages=[choice.get("message") for choice in response.get("choices", [])])
            return response
        except BudgetExhausted:
            stop["reason"] = "budget_exhausted"
            record["status"] = "budget_exhausted"
            raise
        except Exception as exc:
            stop["reason"] = "model_transport_error"
            record.update(status="error", http_status=_http_status(exc))
            raise ModelTransportError("Demo model request failed", _http_status(exc)) from None
        finally:
            allowed_model_request.reset(token)
            record["latency_ms"] = (time.perf_counter() - started) * 1000
            if calls_before is not None:
                record["adapter_calls_started"] = responder.calls - calls_before

    async def ia_model(runtime, system, messages):
        return await model_call(runtime, system, messages, role=getattr(runtime, "_demo_role", "interaction"))

    async def ea_model(runtime, system, messages, with_tools=True):
        return await model_call(runtime, system, messages, role="execution", with_tools=with_tools)

    async def user_turn(runtime, message):
        runtime._demo_role = "interaction"
        result = await original_ia_execute(runtime, message)
        turns.append({"phase": phase[0], "kind": "user", "success": result.success, "response": result.response,
                      "error": result.error, "discovery_trace": runtime.last_discovery_trace})
        return result

    async def callback(runtime, payload, **kwargs):
        task = asyncio.current_task()
        active.add(task)
        runtime._demo_role = "callback"
        callbacks.append({"phase": phase[0], "payload": payload, "sources": copy.deepcopy(kwargs.get("source_results", []))})
        try:
            result = await original_callback(runtime, payload, **kwargs)
            turns.append({"phase": phase[0], "kind": "callback", "success": result.success,
                          "response": result.response, "error": result.error})
            return result
        finally:
            active.discard(task)

    async def worker(runtime, instructions):
        worker_instances.append(runtime)  # Retain objects so distinct-instance evidence is unambiguous.
        entry = {"phase": phase[0], "worker": runtime.agent.name, "run_id": runtime.run_id,
                 "instructions": instructions, "instance": len(worker_instances)}
        runs.append(entry)
        result = await original_ea_execute(runtime, instructions)
        entry.update(success=result.success, response=result.response, error=result.error)
        return result

    def ia_tool(runtime, call):
        result = original_ia_tool(runtime, call)
        tools.append({"phase": phase[0], "role": getattr(runtime, "_demo_role", "interaction"),
                      "name": call.name, "arguments": copy.deepcopy(call.arguments), "success": result.success,
                      "payload": copy.deepcopy(result.payload)})
        return result

    async def ea_tool(runtime, name, args):
        success, result = await original_ea_tool(runtime, name, args)
        tools.append({"phase": phase[0], "role": "execution", "worker": runtime.agent.name,
                      "run_id": runtime.run_id, "name": name, "arguments": copy.deepcopy(args),
                      "success": success, "payload": copy.deepcopy(result)})
        return success, result

    class ObservedBatch(ExecutionBatchManager):
        async def execute_agent(self, *args, **kwargs):
            task = asyncio.current_task()
            active.add(task)
            try:
                if len(runs) >= 4 or stop["reason"]:
                    stop["reason"] = stop["reason"] or "demo_execution_limit"
                    return ea.ExecutionResult(args[0] if args else kwargs["agent_name"], False,
                                              "Demo stopped before another execution", error="Demo limit")
                return await super().execute_agent(*args, **kwargs)
            finally:
                active.discard(task)

    manager = ObservedBatch(timeout_seconds=timeout_seconds)

    async def drain():
        # Real product methods schedule work/callbacks with create_task. Observe
        # those entrypoints and let them execute; do not fabricate a callback.
        while True:
            await asyncio.sleep(0)
            if not active:
                await asyncio.sleep(0)
                if not active:
                    return
            await asyncio.gather(*list(active), return_exceptions=True)

    approved_snapshot = None
    with ExitStack() as stack:
        for module in (ia, ea):
            stack.enter_context(patch.object(module, "get_settings", return_value=settings))
        for module in (ia, ia_tools):
            stack.enter_context(patch.object(module, "get_conversation_log", return_value=conversation))
        stack.enter_context(patch.object(ia, "get_working_memory_log", return_value=conversation))
        for module in (ia, ia_agent, ia_tools):
            stack.enter_context(patch.object(module, "get_agent_roster", return_value=roster))
        for module in (ea_agent, memory, ia_tools):
            stack.enter_context(patch.object(module, "get_execution_agent_logs", return_value=logs))
        stack.enter_context(patch.object(gmail, "_LOG_STORE", logs))
        stack.enter_context(patch.object(gmail, "get_active_gmail_user_id", return_value="simulated-gmail-account"))
        stack.enter_context(patch.object(gmail, "execute_gmail_tool", provider.execute))
        for module in (gmail_service, gmail_client):
            stack.enter_context(patch.object(module, "execute_gmail_tool", blocked))
            stack.enter_context(patch.object(module, "get_active_gmail_user_id", blocked))
        stack.enter_context(patch.object(gmail_client, "_get_composio_client", blocked))
        stack.enter_context(patch.object(model_service, "request_chat_completion", guarded_model_request))
        stack.enter_context(patch.object(ia, "request_chat_completion", blocked))
        stack.enter_context(patch.object(ea, "request_chat_completion", blocked))
        stack.enter_context(patch.object(ea, "get_tool_registry", registry))
        stack.enter_context(patch.object(ia_tools, "_EXECUTION_BATCH_MANAGER", manager))
        for cls, method, implementation in (
            (ia.InteractionAgentRuntime, "_make_llm_call", ia_model),
            (ia.InteractionAgentRuntime, "execute", user_turn),
            (ia.InteractionAgentRuntime, "handle_agent_message", callback),
            (ia.InteractionAgentRuntime, "_execute_tool", ia_tool),
            (ea.ExecutionAgentRuntime, "_make_llm_call", ea_model),
            (ea.ExecutionAgentRuntime, "execute", worker),
            (ea.ExecutionAgentRuntime, "_execute_tool", ea_tool),
        ):
            stack.enter_context(patch.object(cls, method, implementation))
        try:
            async with asyncio.timeout(timeout_seconds):
                await ia.InteractionAgentRuntime(discovery_mode="focus", catalog=catalog).execute(REQUEST)
                await drain()
            shown = any(all(value in message for value in (RECIPIENT, SUBJECT, BODY)) for message in conversation.replies)
            correct_draft = len(provider.drafts) == 1 and all(
                draft.get("recipient_email") == RECIPIENT and draft.get("subject") == SUBJECT and draft.get("body") == BODY
                for draft in provider.drafts.values())
            premature = any(event["tool"] == "GMAIL_SEND_DRAFT" for event in provider.events)
            if not stop["reason"] and shown and correct_draft and not premature and all(turn["success"] for turn in turns):
                approved_snapshot = copy.deepcopy(next(iter(provider.drafts.values())))
                phase[0] = provider.phase = "confirmation"
                async with asyncio.timeout(timeout_seconds):
                    await ia.InteractionAgentRuntime(discovery_mode="focus", catalog=catalog).execute(CONFIRMATION)
                    await drain()
            elif not stop["reason"]:
                stop["reason"] = "draft_phase_assertions_failed"
        except TimeoutError:
            stop["reason"] = "demo_timeout"
        finally:
            if active:
                stop["reason"] = stop["reason"] or "unfinished_background_work"
                for _ in range(3):
                    for task in list(active):
                        task.cancel()
                    await asyncio.gather(*list(active), return_exceptions=True)
                    await asyncio.sleep(0)
                    if not active:
                        break
            await manager.shutdown()

    draft = next(iter(provider.drafts.values()), None)
    dispatches = [tool for tool in tools if tool["name"] == "send_message_to_agent" and tool["success"]]
    callback_sources = [source for item in callbacks for source in item["sources"]]
    first_run = next((run for run in runs if run["phase"] == "draft"), {})
    publications = [tool for tool in tools if tool["phase"] == "draft" and tool["success"]
                    and tool["name"] in {"send_draft", "send_message_to_user"}]
    journals = {path.name: path.read_text(encoding="utf-8") for path in (state_dir / "worker-logs").glob("*.log")}
    checks = {
        "one_exact_requested_draft": len(provider.drafts) == 1 and draft is not None and
            all(draft.get(key) == value for key, value in {"recipient_email": RECIPIENT, "subject": SUBJECT, "body": BODY}.items()),
        "no_send_attempt_before_confirmation": not any(event["tool"] == "GMAIL_SEND_DRAFT" and event["phase"] == "draft" for event in provider.events),
        "confirmation_only_after_exact_draft_shown": approved_snapshot is not None,
        "one_simulated_send_of_approved_snapshot": approved_snapshot is not None and provider.sent == [approved_snapshot],
        "same_worker_dispatched_once_per_user_turn": [(item["phase"], item["arguments"].get("agent_name")) for item in dispatches] == [("draft", WORKER), ("confirmation", WORKER)],
        "two_distinct_worker_runtimes": len(worker_instances) == 2 and worker_instances[0] is not worker_instances[1],
        "continuation_prompt_contains_saved_draft_id": any(call["phase"] == "confirmation" and call.get("prompt_contains_created_draft_id") for call in calls),
        "create_receipt_stored_in_worker_journal": bool(draft and any("tool_receipt" in text and "gmail_create_draft" in text and draft["id"] in text for text in journals.values())),
        "callback_sources_match_actual_worker_runs": len(callback_sources) == len(runs) == 2 and
            {(source["result_id"], source["agent_name"]) for source in callback_sources} == {(run["run_id"], run["worker"]) for run in runs},
        "draft_publication_has_actual_source_id": bool(first_run) and any(first_run.get("run_id") in item["arguments"].get("source_result_ids", []) for item in publications),
        "all_runtime_results_successful": bool(turns and runs) and all(item.get("success") for item in turns + runs),
        "no_tool_errors": all(tool["success"] for tool in tools),
    }
    operational = stop["reason"] in {"model_transport_error", "demo_timeout", "unfinished_background_work"}
    any_request = any(call["status"] == "returned" or call.get("adapter_calls_started", 0) > 0 for call in calls)
    status = (("incomplete" if any_request else "not_run") if stop["reason"] == "budget_exhausted"
              else "operational_error" if operational else "completed")
    report = {"evaluation": "live-model internal integration with simulated Gmail" if isinstance(responder, LiveResponder) else "scripted internal integration with simulated Gmail",
        "record_status": status, "stop_reason": stop["reason"],
        "automatic_success": None if status != "completed" else all(checks.values()),
        "real_gmail_access": False, "provider_validation": False, "manual_content_review_required": True,
        "success_definition": "All listed strict integration checks pass; a recovered tool error can fail no_tool_errors even if the final deliverable is correct.",
        "memory_observation": "The saved draft ID is available in the fresh worker prompt. This does not prove causal reliance on archive memory; the prompt also contains current instructions and catalog state.",
        "request": REQUEST, "confirmation": CONFIRMATION if approved_snapshot else None,
        "expected_draft": {"recipient_email": RECIPIENT, "subject": SUBJECT, "body": BODY},
        "approved_simulated_snapshot": approved_snapshot, "checks": checks, "simulated_gmail": provider.snapshot(),
        "calls": calls, "tools": tools, "turns": turns, "worker_runs": runs, "callbacks": callbacks,
        "conversation": conversation.transcript, "worker_journals": journals,
        "retained_state_directory": str(state_dir.resolve()),
        "local_model_calls": getattr(responder, "calls", None),
        "local_reserved_estimated_usd": getattr(responder, "reserved_estimated_usd", None),
        "ledger_after": responder.ledger.snapshot() if getattr(responder, "ledger", None) else None,
        "limits": ["One explicit synthetic task, not a routing accuracy benchmark", "Gmail behavior and receipts are simulated",
                   "Short worker history; does not establish older archive retrieval quality", "Exact text inclusion is checked; displayed content still merits review",
                   "The demo supplies user confirmation only after its draft checks pass; this is not a product approval gate"]}
    (state_dir / "conversation.xml").write_text(conversation.transcript, encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    for flag in ("model", "input-rate", "output-rate", "max-estimated-usd", "total-budget-usd", "min-call-interval"):
        parser.add_argument("--" + flag, required=True, type=str if flag == "model" else float)
    for flag in ("max-calls", "total-max-calls", "max-output-tokens"):
        parser.add_argument("--" + flag, required=True, type=int)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from server.config import get_settings
    key = get_settings().openrouter_api_key
    if not key:
        parser.error("OPENROUTER_API_KEY is not configured")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Choose an empty output directory")
    ledger = ReservationLedger(args.ledger, args.total_budget_usd, args.total_max_calls)
    responder = LiveResponder(model=args.model, api_key=key, max_calls=args.max_calls,
        max_estimated_usd=args.max_estimated_usd, input_rate=args.input_rate, output_rate=args.output_rate,
        max_output_tokens=args.max_output_tokens, ledger=ledger, min_call_interval_seconds=args.min_call_interval)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "model": args.model,
                "input_rate": args.input_rate, "output_rate": args.output_rate, "max_calls": args.max_calls,
                "max_estimated_usd": args.max_estimated_usd, "max_output_tokens": args.max_output_tokens,
                "min_call_interval_seconds": args.min_call_interval, "ledger_before": ledger.snapshot(),
                "simulated_gmail": True, "real_account_access": False, "request": REQUEST}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = asyncio.run(run_demo(responder, args.output / "state", model=args.model))
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest.update(status=report["record_status"], automatic_success=report["automatic_success"],
                    stop_reason=report["stop_reason"], ledger_after=ledger.snapshot(), local_model_calls=responder.calls,
                    local_reserved_estimated_usd=responder.reserved_estimated_usd)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["record_status"], "automatic_success": report["automatic_success"],
                      "checks": report["checks"], "calls": responder.calls, "simulated_gmail": True}))


if __name__ == "__main__":
    main()
