"""Independent checks of the routing evaluation, using the actual runtime."""

import asyncio
import json

import pytest

from evals.routing.cases import CASES, WORKERS, roster_records
from evals.routing.harness import (
    LiveResponder, MemoryRoster, ScriptedResponder, response_with_calls,
    run_case, score_dispatches,
)
from evals.routing.run import measure_discovery, seed_catalog, seed_publications, summarize
from server.services.execution.catalog import AgentCatalog


def test_nested_rosters_preserve_targets_and_distractors():
    small, large = list(roster_records(100)), list(roster_records(1000))
    assert len(small) == 100
    assert large[:100] == small
    assert set(WORKERS).issubset(dict(small))


def test_scoring_counts_later_wrong_and_duplicate_dispatches():
    case = CASES[0]
    expected = list(case.expected_agents)
    assert score_dispatches(case, expected, set(WORKERS))["automatic_correct"] is True
    assert score_dispatches(case, expected + ["Email to Bob supplier contract"], set(WORKERS))["automatic_correct"] is False
    assert score_dispatches(case, expected * 2, set(WORKERS))["duplicate_dispatches"] == 1


def test_empty_response_is_not_counted_as_correct_clarification():
    case = next(case for case in CASES if case.expected_action == "ask")
    result = score_dispatches(case, [], set(WORKERS))
    assert result["automatic_correct"] is None
    assert result["manual_review_required"] is True


@pytest.mark.parametrize("mode", ["legacy", "retrieval", "focus"])
def test_harness_uses_real_runtime_and_no_worker_execution(tmp_path, mode):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    roster = seed_catalog(catalog, list(roster_records(100)), "enriched")
    case = CASES[0]
    responses = [
        response_with_calls(("send_message_to_user", {"message": "I'll narrow that list."})),
        response_with_calls(("send_message_to_agent", {"agent_name": case.expected_agents[0], "instructions": case.query})),
        response_with_calls(text="Submitted."),
    ]
    result = asyncio.run(run_case(case, mode, catalog, roster, ScriptedResponder(responses)))
    assert result["runtime_success"], result["runtime_error"]
    assert result["automatic_correct"] is True
    assert result["dispatches"] == list(case.expected_agents)
    assert len(result["model_calls"]) == 3
    assert all(call["usage"] is None for call in result["model_calls"])
    assert not any("gold" in str(call) for call in result["tool_calls"])


def test_harness_records_real_search_and_later_wrong_dispatch(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    roster = seed_catalog(catalog, list(roster_records(100)), "names_only")
    case = CASES[0]
    responses = [
        response_with_calls(("find_agents", {"query": "Tokyo", "exact_name": "Tokyo restaurant search"})),
        response_with_calls(("send_message_to_agent", {"agent_name": "Tokyo restaurant search", "instructions": case.query})),
        response_with_calls(("send_message_to_agent", {"agent_name": "Kyoto hotel search", "instructions": "An unintended extra dispatch"})),
        response_with_calls(text="Submitted."),
    ]
    result = asyncio.run(run_case(case, "focus", catalog, roster, ScriptedResponder(responses)))
    assert result["runtime_success"], result["runtime_error"]
    assert result["automatic_correct"] is False
    assert result["unexpected_dispatches"] == ["Kyoto hotel search"]
    assert len(result["discovery_trace"]["expansions"]) == 1


def test_runtime_failure_after_correct_dispatch_is_not_scored_as_success(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    roster = seed_catalog(catalog, list(roster_records(100)), "names_only")
    case = CASES[0]

    class FailsAfterDispatch:
        calls = 0

        async def __call__(self, system, messages, schemas):
            self.calls += 1
            if self.calls == 1:
                return response_with_calls(("send_message_to_agent", {
                    "agent_name": case.expected_agents[0], "instructions": case.query,
                }))
            raise RuntimeError("simulated provider failure")

    result = asyncio.run(run_case(case, "focus", catalog, roster, FailsAfterDispatch()))
    assert result["dispatches"] == list(case.expected_agents)
    assert result["automatic_correct"] is False
    assert result["outcome"] == "runtime_failed"
    assert len(result["model_calls"]) == 2
    assert result["model_calls"][-1]["status"] == "error"


def test_retrieval_report_does_not_claim_model_accuracy(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    records = list(roster_records(100))
    seed_catalog(catalog, records, "names_only")
    case = CASES[1]
    seed_publications(catalog, case)
    row = measure_discovery(catalog, case, "focus", [name for name, _ in records])
    assert row["initial_all_targets_present"] is True
    assert row["initial_roster_characters"] <= 6000
    row.update(scale=100, metadata="names_only")
    summary = summarize([row], live=False)
    assert "Live routing evaluation was not run" in summary
    assert "not a held-out benchmark" in summary


def test_live_budget_stops_before_any_network_call(monkeypatch):
    from server import openrouter_client
    async def forbidden(**kwargs):
        pytest.fail("Ceiling must reject before making a model request")
    monkeypatch.setattr(openrouter_client, "request_chat_completion", forbidden)
    responder = LiveResponder(model="example", api_key="not-a-real-key", max_calls=1,
        max_estimated_usd=0.000001, input_rate=10, output_rate=10)
    with pytest.raises(RuntimeError, match="ceiling"):
        asyncio.run(responder("system", [{"role": "user", "content": "hello"}], []))
    assert responder.calls == 0


def test_legacy_schema_snapshot_matches_production_schema():
    from pathlib import Path
    from server.agents.interaction_agent.tools import get_tool_schemas
    baseline = Path(__file__).parents[1] / "evals/routing/baseline/tools.json"
    assert get_tool_schemas("legacy") == json.loads(baseline.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case_id", ["current_result_words", "same_person_current_work", "exact_observed_reference"])
def test_current_evidence_retrieves_targets_missing_from_names_and_requests(tmp_path, case_id):
    case = next(case for case in CASES if case.case_id == case_id)
    records = list(roster_records(1000))
    names = [name for name, _ in records]
    baseline = AgentCatalog(tmp_path / "instructions.db")
    current = AgentCatalog(tmp_path / "current.db")
    seed_catalog(baseline, records, "enriched")
    seed_catalog(current, records, "current_state")
    before = measure_discovery(baseline, case, "retrieval", names)
    after = measure_discovery(current, case, "focus", names)
    assert before["initial_all_targets_present"] is False
    assert after["initial_all_targets_present"] is True
    assert after["initial_roster_characters"] <= 6000


def test_silent_completion_does_not_replace_published_focus(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    seed_catalog(catalog, list(roster_records(100)), "current_state")
    case = next(case for case in CASES if case.case_id == "unpublished_interruption")
    seed_publications(catalog, case, "current_state")
    assert catalog.get_agent("Kyoto hotel search")["last_run_status"] == "completed"
    assert catalog.recent_published()[0]["agent_name"] == "Email to Alice Q3 budget"
    assert catalog.shortlist(case.query)["cards"][0]["agent_name"] == "Email to Alice Q3 budget"


def test_known_paraphrase_failure_is_retained(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    records = list(roster_records(1000))
    seed_catalog(catalog, records, "current_state")
    case = next(case for case in CASES if case.case_id == "paraphrase")
    result = measure_discovery(catalog, case, "focus", [name for name, _ in records])
    assert result["initial_all_targets_present"] is False


def test_stale_offered_card_is_rejected_before_dispatch_in_real_loop(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.db")
    roster = seed_catalog(catalog, list(roster_records(100)), "current_state")
    case = CASES[0]

    class StateChangesAfterLookup:
        calls = 0

        async def __call__(self, system, messages, schemas):
            self.calls += 1
            if self.calls == 1:
                catalog.record_dispatch(case.expected_agents[0], "The work changed after the card was shown.")
                return response_with_calls(("send_message_to_agent", {
                    "agent_name": case.expected_agents[0], "instructions": case.query,
                }))
            return response_with_calls(text="I need to check the updated work before continuing.")

    result = asyncio.run(run_case(case, "focus", catalog, roster, StateChangesAfterLookup()))
    assert result["runtime_success"], result["runtime_error"]
    assert result["dispatches"] == []
    assert result["tool_calls"][0]["success"] is False
    assert result["automatic_correct"] is False  # Refusal is not counted as a completed routing task.
