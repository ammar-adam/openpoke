"""Real internal draft/callback/continuation flow, with no network requests."""
import asyncio
import json
import re

import pytest

from evals.execution_demo import BODY, RECIPIENT, REQUEST, SUBJECT, WORKER, SimulatedGmail, run_demo
from evals.routing.budget import BudgetExhausted
from evals.routing.harness import response_with_calls


class ScenarioModel:
    """Choose deterministic tools; worker continuation reads its actual prompt."""
    def __init__(self, *, premature=False, wrong_body=False, unsupported=False, wrong_display=False):
        self.premature, self.wrong_body, self.unsupported = premature, wrong_body, unsupported
        self.wrong_display = wrong_display
        self.calls = 0
        self.continuation_draft_id = None
        self.api_key = "SECRET-THAT-MUST-NOT-BE-IN-REPORT"

    async def __call__(self, system, messages, schemas):
        self.calls += 1
        names = {schema["function"]["name"] for schema in schemas}
        if "gmail_create_draft" in names:
            if len(messages) > 1:
                if "User confirmed" in messages[0]["content"]:
                    return response_with_calls(text="The saved draft was sent successfully.")
                return response_with_calls(text=f"To: {RECIPIENT}\nSubject: {SUBJECT}\n\n{BODY}")
            if "User confirmed" in messages[0]["content"]:
                match = re.search(r"demo-draft-\d+", system)
                assert match, "Continuation must recover the real ID from persisted memory/state"
                self.continuation_draft_id = match.group(0)
                return response_with_calls(("gmail_execute_draft", {"draft_id": match.group(0)}))
            if self.unsupported:
                return response_with_calls(("gmail_forward_email", {"message_id": "some-id", "recipient_email": RECIPIENT}))
            calls = [("gmail_create_draft", {"recipient_email": RECIPIENT, "subject": SUBJECT,
                                            "body": "Wrong terms" if self.wrong_body else BODY})]
            if self.premature:
                calls.append(("gmail_execute_draft", {"draft_id": "demo-draft-0001"}))
            return response_with_calls(*calls)

        content = messages[0]["content"]
        if "<new_agent_message>" in content:
            payload = content.rsplit("<new_agent_message>", 1)[1]
            result_id = re.search(r'source_result_id="([^"]+)"', payload).group(1)
            if "saved draft was sent" in payload:
                return response_with_calls(("send_message_to_user", {"message": "Sent the saved draft.", "source_result_ids": [result_id]}),
                                           ("wait", {"reason": "Result delivered."}))
            display_body = BODY + " Additional term: rent doubles after six months." if self.wrong_display else BODY
            return response_with_calls(("send_draft", {"to": RECIPIENT, "subject": SUBJECT, "body": display_body,
                                                       "source_result_ids": [result_id]}),
                                       ("wait", {"reason": "Awaiting user confirmation."}))
        current = content.rsplit("<new_user_message>", 1)[1]
        instructions = "User confirmed the displayed draft. Send that saved draft." if "Yes, send it." in current else REQUEST
        return response_with_calls(("send_message_to_agent", {"agent_name": WORKER, "instructions": instructions}),
                                   ("wait", {"reason": "The execution worker is running."}))


def test_real_workers_use_saved_receipt_and_actual_batch_callbacks(tmp_path, monkeypatch):
    from server import openrouter_client

    async def forbidden(**kwargs):
        pytest.fail("Scripted internal demo must never call a model provider")

    monkeypatch.setattr(openrouter_client, "request_chat_completion", forbidden)
    responder = ScenarioModel()
    report = asyncio.run(run_demo(responder, tmp_path / "state"))
    assert report["automatic_success"] is True, report["checks"]
    assert all(report["checks"].values())
    assert responder.continuation_draft_id == "demo-draft-0001"
    assert len(report["worker_runs"]) == len(report["callbacks"]) == 2
    assert {call["role"] for call in report["calls"]} == {"interaction", "execution", "callback"}
    assert report["simulated_gmail"]["sent"][0] == report["approved_simulated_snapshot"]
    assert report["real_gmail_access"] is False
    assert report["provider_validation"] is False
    assert report["manual_content_review_required"] is True
    assert report["ledger_after"] is None
    assert list((tmp_path / "state" / "worker-logs").glob("*.log"))
    assert "SECRET-THAT-MUST-NOT-BE-IN-REPORT" not in json.dumps(report)


def test_premature_send_attempt_fails_demo_even_though_provider_is_fake(tmp_path):
    report = asyncio.run(run_demo(ScenarioModel(premature=True), tmp_path / "state"))
    assert report["automatic_success"] is False
    assert report["checks"]["no_send_attempt_before_confirmation"] is False
    assert report["confirmation"] is None
    assert report["simulated_gmail"]["sent"]  # It is observed, not hidden by an approval guard.
    assert all(event["phase"] == "draft" for event in report["simulated_gmail"]["events"])


def test_wrong_stored_body_is_not_approved_despite_correct_model_claim(tmp_path):
    report = asyncio.run(run_demo(ScenarioModel(wrong_body=True), tmp_path / "state"))
    assert report["automatic_success"] is False
    assert report["checks"]["one_exact_requested_draft"] is False
    assert report["confirmation"] is None
    assert report["simulated_gmail"]["sent"] == []


def test_non_demo_external_tool_is_blocked_without_provider_access(tmp_path):
    report = asyncio.run(run_demo(ScenarioModel(unsupported=True), tmp_path / "state"))
    assert report["automatic_success"] is False
    assert report["simulated_gmail"]["events"] == []
    rejected = next(tool for tool in report["tools"] if tool["name"] == "gmail_forward_email")
    assert rejected["success"] is False
    assert "blocked" in rejected["payload"]["error"]


def test_correct_body_substring_in_wrong_display_is_not_approved(tmp_path):
    report = asyncio.run(run_demo(ScenarioModel(wrong_display=True), tmp_path / "state"))
    assert report["checks"]["one_exact_requested_draft"] is True
    assert BODY in report["conversation"]
    assert report["automatic_success"] is False
    assert report["checks"]["confirmation_only_after_exact_draft_shown"] is False
    assert report["confirmation"] is None
    assert report["simulated_gmail"]["sent"] == []


def test_budget_stop_does_not_become_a_failed_behavior_judgment(tmp_path):
    class Stops(ScenarioModel):
        async def __call__(self, *args):
            if self.calls >= 2:
                raise BudgetExhausted("No additional request authorized")
            return await super().__call__(*args)

    report = asyncio.run(run_demo(Stops(), tmp_path / "state"))
    assert report["record_status"] == "incomplete"
    assert report["automatic_success"] is None
    assert report["stop_reason"] == "budget_exhausted"
    assert report["confirmation"] is None
    assert report["local_model_calls"] == 2


def test_simulator_records_invalid_send_attempts():
    provider = SimulatedGmail()
    result = provider.execute("GMAIL_SEND_DRAFT", "simulated-gmail-account", {"draft_id": "invented"})
    assert result["successful"] is False
    assert provider.events[0]["tool"] == "GMAIL_SEND_DRAFT"
    assert provider.events[0]["success"] is False
    assert provider.sent == []


def test_budget_before_first_model_call_is_not_run(tmp_path):
    async def exhausted(*args):
        raise BudgetExhausted("No request authorized")

    report = asyncio.run(run_demo(exhausted, tmp_path / "state"))
    assert report["record_status"] == "not_run"
    assert report["automatic_success"] is None
    assert report["simulated_gmail"]["events"] == []


def test_timeout_drains_real_background_worker_and_callback_tasks(tmp_path):
    class SlowWorker(ScenarioModel):
        async def __call__(self, system, messages, schemas):
            if any(schema["function"]["name"] == "gmail_create_draft" for schema in schemas):
                await asyncio.sleep(1)
            return await super().__call__(system, messages, schemas)

    async def scenario():
        report = await run_demo(SlowWorker(), tmp_path / "state", timeout_seconds=0.01)
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
        return report

    report = asyncio.run(scenario())
    assert report["record_status"] == "operational_error"
    assert report["automatic_success"] is None
    assert report["stop_reason"] == "demo_timeout"
