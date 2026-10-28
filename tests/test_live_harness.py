"""Offline checks for live-evaluation accounting and real two-turn protocol."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from evals.routing.budget import BudgetExhausted, ReservationLedger
from evals.routing.cases import CASES
from evals.routing.cases_challenge import CHALLENGE_CASES, WORKERS
from evals.routing.harness import (
    LiveResponder, MemoryLog, MemoryRoster, ModelTransportError, ScriptedResponder,
    response_with_calls, run_case, run_two_turn_case,
)
from server.services.execution.catalog import AgentCatalog


def setup_case(tmp_path, case):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    records = dict(WORKERS)
    records.update({name: case.query for name in case.expected_agents})
    catalog.record_dispatch_many(records.items())
    return catalog, MemoryRoster(records)


def dispatch(name, instructions="Continue the requested work."):
    return response_with_calls(("send_message_to_agent", {"agent_name": name, "instructions": instructions}))


def publish(case, ids=None):
    return response_with_calls(("send_message_to_user", {
        "message": case.callback_results[0]["response"],
        "source_result_ids": list(case.expected_source_ids if ids is None else ids),
    }))


def completed():
    return response_with_calls(text="Done.")


def live(**changes):
    values = dict(model="mock-model", api_key="not-a-key", max_calls=5,
                  max_estimated_usd=1, input_rate=1, output_rate=1)
    values.update(changes)
    return LiveResponder(**values)


def test_ledger_persists_and_does_not_silently_change_campaign_limits(tmp_path):
    path = tmp_path / "budget.db"
    first = ReservationLedger(path, total_usd=0.3, max_calls=4)
    first.reserve(0.1)
    second = ReservationLedger(path, total_usd=0.3, max_calls=4)
    second.reserve(0.2)
    assert second.snapshot()["reserved_estimated_usd"] == 0.3
    with pytest.raises(BudgetExhausted):
        first.reserve(0.000001)
    assert first.snapshot()["calls"] == 2
    with pytest.raises(ValueError, match="limits differ"):
        ReservationLedger(path, total_usd=1, max_calls=4)


def test_shared_reservations_are_atomic_across_connections(tmp_path):
    ledger = ReservationLedger(tmp_path / "budget.db", total_usd=10, max_calls=7)

    def attempt(_):
        try:
            ledger.reserve(0.1)
            return True
        except BudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = list(pool.map(attempt, range(20)))
    assert sum(accepted) == 7
    assert ledger.snapshot()["calls"] == 7
    assert ledger.snapshot()["reserved_estimated_usd"] == 0.7


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_money_and_rate_configuration_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        ReservationLedger(tmp_path / "budget.db", total_usd=value, max_calls=1)
    with pytest.raises(ValueError):
        live(input_rate=value)


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_call_configuration_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        ReservationLedger(tmp_path / "budget.db", total_usd=1, max_calls=value)
    with pytest.raises(ValueError):
        live(max_calls=value)


def test_failed_provider_request_retains_shared_reservation_and_safe_status(tmp_path, monkeypatch):
    from server import openrouter_client
    ledger = ReservationLedger(tmp_path / "budget.db", total_usd=1, max_calls=1)
    calls = []

    async def fails(**kwargs):
        calls.append(kwargs)
        response = httpx.Response(429, request=httpx.Request("POST", "https://example.test"))
        raise httpx.HTTPStatusError("secret authorization and raw response", request=response.request, response=response)

    monkeypatch.setattr(openrouter_client, "request_chat_completion", fails)
    first, second = live(ledger=ledger), live(ledger=ledger)
    with pytest.raises(ModelTransportError, match="HTTP 429") as error:
        asyncio.run(first("system", [], []))
    assert "secret" not in str(error.value)
    assert ledger.snapshot()["calls"] == 1
    assert first.calls == 1
    assert first.reserved_estimated_usd > 0
    with pytest.raises(BudgetExhausted):
        asyncio.run(second("system", [], []))
    assert second.calls == 0
    assert len(calls) == 1


def test_budget_before_first_call_is_not_a_routing_failure(tmp_path):
    case = CASES[0]
    catalog, roster = setup_case(tmp_path, case)

    async def stopped(*args):
        raise BudgetExhausted("stop")

    result = asyncio.run(run_case(case, "focus", catalog, roster, stopped))
    assert result["record_status"] == "not_run"
    assert result["automatic_correct"] is None
    assert result["failure_reason"] == "budget_exhausted"
    assert not result["dispatches"]


def test_budget_after_dispatch_is_incomplete_not_incorrect(tmp_path):
    case = CASES[0]
    catalog, roster = setup_case(tmp_path, case)
    responses = iter([dispatch(case.expected_agents[0])])

    async def one_response(*args):
        try:
            return next(responses)
        except StopIteration:
            raise BudgetExhausted("stop")

    result = asyncio.run(run_case(case, "focus", catalog, roster, one_response))
    assert result["record_status"] == "incomplete"
    assert result["automatic_correct"] is None
    assert result["observed_dispatch_correct"] is True


def test_transport_failure_is_sanitized_and_separate_from_routing(tmp_path):
    case = CASES[0]
    catalog, roster = setup_case(tmp_path, case)

    async def fails(*args):
        response = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
        raise httpx.HTTPStatusError("secret bearer value", request=response.request, response=response)

    result = asyncio.run(run_case(case, "focus", catalog, roster, fails))
    assert result["record_status"] == "operational_error"
    assert result["automatic_correct"] is None
    assert result["model_calls"][0]["http_status"] == 401
    assert "secret bearer" not in json.dumps(result)


def test_real_callback_creates_focus_and_silent_completion_does_not_replace_it(tmp_path):
    case = next(case for case in CHALLENGE_CASES if case.case_id == "v03_silent_completion")
    catalog, roster = setup_case(tmp_path, case)
    assert catalog.recent_published() == []
    seen = []

    def inspect(catalog, turn, index):
        if index == 0:
            seen.append((turn, catalog.recent_published()))
            if turn == "callback":
                assert catalog.get_agent(case.expected_agents[0])["last_run_status"] == "completed"

    result = asyncio.run(run_two_turn_case(case, "focus", catalog, roster,
        ScriptedResponder([publish(case), completed(), dispatch(case.expected_agents[0]), completed()]),
        before_model_call=inspect))
    assert result["automatic_correct"] is True, result
    assert result["publication_annotation"]["status"] == "correct"
    assert result["published_content_manual_review_required"] is True
    assert result["callback_dispatches"] == []
    assert result["followup_initial_all_targets_present"] is True
    assert seen[0] == ("callback", [])
    assert seen[1][1][0]["agent_name"] == case.expected_agents[0]
    assert catalog.get_agent(case.silent_results[0]["agent_name"])["last_run_status"] == "completed"
    assert {call["turn"] for call in result["model_calls"]} == {"callback", "followup"}


@pytest.mark.parametrize("mode,annotation", [("focus", "missing"), ("legacy", "not_applicable")])
def test_plain_callback_reply_does_not_invent_publication_annotations(tmp_path, mode, annotation):
    case = next(case for case in CHALLENGE_CASES if case.case_id == "s02_published_draft")
    catalog, roster = setup_case(tmp_path, case)
    result = asyncio.run(run_two_turn_case(case, mode, catalog, roster,
        ScriptedResponder([completed(), dispatch(case.expected_agents[0]), completed()])))
    assert result["publication_annotation"]["status"] == annotation
    assert result["publication_annotation"]["correct"] is (None if mode == "legacy" else False)
    assert catalog.recent_published() == []


def test_selective_callback_wrong_valid_source_is_not_marked_correct(tmp_path):
    case = next(case for case in CHALLENGE_CASES if case.case_id == "v02_selective_publication")
    catalog, roster = setup_case(tmp_path, case)
    wrong = case.callback_results[1]["result_id"]
    result = asyncio.run(run_two_turn_case(case, "focus", catalog, roster,
        ScriptedResponder([publish(case, [wrong]), completed(), dispatch(case.expected_agents[0]), completed()])))
    assert result["publication_annotation"]["status"] == "wrong"
    assert result["publication_annotation"]["actual_source_ids"] == [wrong]


def test_callback_dispatch_cannot_satisfy_followup_success(tmp_path):
    case = next(case for case in CHALLENGE_CASES if case.case_id == "s02_published_draft")
    catalog, roster = setup_case(tmp_path, case)
    result = asyncio.run(run_two_turn_case(case, "focus", catalog, roster,
        ScriptedResponder([publish(case), dispatch(case.expected_agents[0]), dispatch(case.expected_agents[0]), completed(),
                           dispatch(case.expected_agents[0]), completed()])))
    assert result["callback_dispatches"] == list(case.expected_agents)
    assert result["dispatches"] == list(case.expected_agents)
    assert result["followup_automatic_correct"] is True
    assert result["automatic_correct"] is False
    assert result["outcome"] == "unexpected_callback_dispatch"


def test_budget_before_followup_after_callback_is_incomplete(tmp_path):
    case = next(case for case in CHALLENGE_CASES if case.case_id == "s02_published_draft")
    catalog, roster = setup_case(tmp_path, case)
    responses = iter([publish(case), completed()])

    async def callback_only(*args):
        try:
            return next(responses)
        except StopIteration:
            raise BudgetExhausted("stop")

    result = asyncio.run(run_two_turn_case(case, "focus", catalog, roster, callback_only))
    assert result["record_status"] == "incomplete"
    assert result["turns"]["followup"]["record_status"] == "not_run"
    assert result["automatic_correct"] is None
    assert result["publication_annotation"]["correct"] is True


def test_mutation_hook_runs_after_shortlist_and_refresh_is_measured(tmp_path):
    case = CASES[0]
    catalog, roster = setup_case(tmp_path, case)

    def mutate(catalog, turn, index):
        if index == 0:
            assert turn == "user"
            catalog.record_dispatch(case.expected_agents[0], "Changed after the initial shortlist was offered.")

    result = asyncio.run(run_case(case, "focus", catalog, roster,
        ScriptedResponder([dispatch(case.expected_agents[0]), dispatch(case.expected_agents[0]), completed()]),
        before_model_call=mutate))
    assert result["automatic_correct"] is True
    assert result["refresh_count"] == 1
    assert result["refresh_then_dispatch"] is True
    assert result["tool_calls"][0]["success"] is False
    assert result["dispatches"] == list(case.expected_agents)


def test_memory_log_escapes_xml_but_preserves_visible_content():
    log = MemoryLog()
    log.record_user_message("A < B & C")
    log.record_agent_message("<result>&</result>")
    log.record_reply("Draft <&>")
    assert "A &lt; B &amp; C" in log.transcript
    assert "&lt;result&gt;&amp;&lt;/result&gt;" in log.transcript
    assert log.replies == ["Draft <&>"]


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "3"])
def test_invalid_request_spacing_is_rejected(value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        live(min_call_interval_seconds=value)


def test_request_spacing_happens_before_reservation_and_uses_elapsed_time(tmp_path, monkeypatch):
    from evals.routing import harness
    from server import openrouter_client
    ledger = ReservationLedger(tmp_path / "budget.db", total_usd=1, max_calls=5)
    clock, starts, sleeps = [100.0], [], []
    responder = live(ledger=ledger, min_call_interval_seconds=3)

    async def sleep(delay):
        # The second request has not reserved money or a call while waiting.
        assert ledger.snapshot()["calls"] == 1
        assert responder.calls == 1
        sleeps.append(delay)
        clock[0] += delay

    async def request(**kwargs):
        starts.append(clock[0])
        clock[0] += 1  # First response latency consumes part of the interval.
        return completed()

    monkeypatch.setattr(harness.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(harness.asyncio, "sleep", sleep)
    monkeypatch.setattr(openrouter_client, "request_chat_completion", request)

    async def scenario():
        await responder("system", [], [])
        await responder("system", [], [])
        clock[0] += 5  # Already beyond the minimum interval; no additional wait.
        await responder("system", [], [])

    asyncio.run(scenario())
    assert starts == [100, 103, 109]
    assert sleeps == [2]
    assert ledger.snapshot()["calls"] == 3


def test_cancelled_pacing_does_not_reserve_or_start_a_request(tmp_path, monkeypatch):
    from evals.routing import harness
    from server import openrouter_client
    ledger = ReservationLedger(tmp_path / "budget.db", total_usd=1, max_calls=5)
    starts = []
    responder = live(ledger=ledger, min_call_interval_seconds=3)

    async def cancel(delay):
        raise asyncio.CancelledError()

    async def request(**kwargs):
        starts.append(True)
        return completed()

    monkeypatch.setattr(harness.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(harness.asyncio, "sleep", cancel)
    monkeypatch.setattr(openrouter_client, "request_chat_completion", request)

    async def scenario():
        await responder("system", [], [])
        with pytest.raises(asyncio.CancelledError):
            await responder("system", [], [])

    asyncio.run(scenario())
    assert responder.calls == ledger.snapshot()["calls"] == len(starts) == 1
