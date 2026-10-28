"""The CLI must preserve trial provenance and stop cleanly without spending."""
import json
from types import SimpleNamespace

import pytest

from evals.routing import run
from evals.routing.cases import CASES


def arguments(tmp_path, *extra):
    return run.build_parser().parse_args([
        "--scales", "100", "--metadata", "enriched", "--output", str(tmp_path / "run"),
        "--ledger", str(tmp_path / "ledger.db"), *extra,
    ])


def test_trial_rotation_preserves_each_pair_and_repeat():
    trials = list(run.trial_plan(CASES[:2], ["legacy", "retrieval", "focus"], 3))
    assert len(trials) == 18
    assert len({(c.case_id, mode, repeat) for c, mode, repeat in trials}) == 18
    assert [trials[index][1] for index in (0, 6, 12)] == ["legacy", "retrieval", "focus"]


def test_offline_manifest_is_written_before_work_and_has_no_live_claim(tmp_path, monkeypatch):
    args = arguments(tmp_path, "--case-ids", "direct_old", "--modes", "focus")
    original = run.measure_discovery
    def measure(*values):
        manifest = json.loads((args.output / "manifest.json").read_text())
        assert manifest["status"] == "running"
        assert manifest["fixture_sha256"] == run.fixture_fingerprint([CASES[0]])
        return original(*values)
    monkeypatch.setattr(run, "measure_discovery", measure)
    rows = run.evaluate(args)
    assert len(rows) == 1 and rows[0]["evaluation"] == "offline_lookup"
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["live_calls"] == 0
    assert "Live routing evaluation was not run" in (args.output / "summary.md").read_text()
    with pytest.raises(ValueError, match="already contains"):
        run.evaluate(args)


@pytest.mark.parametrize("failure_status", ["incomplete", "not_run", "operational_error"])
def test_live_phase_stops_and_never_scores_unrun_rows_as_failures(tmp_path, monkeypatch, failure_status):
    from server import config
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(openrouter_api_key="test-key"))
    class FakeResponder:
        def __init__(self, **kwargs):
            self.calls = 0
            self.reserved_estimated_usd = 0
    monkeypatch.setattr(run, "LiveResponder", FakeResponder)
    attempts = []
    async def simulated_case(case, mode, catalog, roster, responder, **kwargs):
        attempts.append((case.case_id, mode))
        return {"record_status": failure_status, "failure_reason": "test stop", "automatic_correct": None}
    monkeypatch.setattr(run, "run_case", simulated_case)
    args = arguments(tmp_path, "--live", "--model", "test-model", "--case-ids", "direct_old", "published_followup",
                     "--input-rate", "1", "--output-rate", "1", "--max-estimated-usd", "1")
    rows = run.evaluate(args)
    assert len(attempts) == 1
    assert len(rows) == 6
    assert all(row["automatic_correct"] is None for row in rows)
    assert all(row["record_status"] == "not_run" for row in rows[1:])
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["status"] == "stopped" and manifest["stop_reason"] == "test stop"


def test_challenge_split_is_explicit_and_preserves_fixture_hash(tmp_path):
    args = arguments(tmp_path, "--suite", "challenge", "--split", "validation")
    cases, _ = run.selected_suite(args)
    assert len(cases) == 14
    assert all(case.fixture_group == "validation" for case in cases)
    assert run.fixture_fingerprint(cases) == run.fixture_fingerprint(cases)
    args.case_ids = ["s02_published_draft"]
    with pytest.raises(ValueError, match="Unknown cases"):
        run.selected_suite(args)


def test_extension_suite_has_fresh_cases_and_no_foreign_state(tmp_path):
    args = arguments(tmp_path, "--suite", "extension", "--modes", "focus")
    cases, records = run.selected_suite(args)
    assert len(cases) == 20
    assert all(case.fixture_group == "extension" for case in cases)
    assert not {case.case_id for case in cases} & {case.case_id for case in CASES}
    assert len(list(records(100))) == 100


def test_stale_mutation_occurs_after_lookup_once_and_not_in_callback():
    case = SimpleNamespace(mutation={"agent_name": "Example", "instructions": "Changed",
                                     "when": "before_followup_model_call"})
    changes = []
    catalog = SimpleNamespace(record_dispatch=lambda *args: changes.append(args))
    hook = run.mutation_hook(case)
    hook(catalog, "callback", 0)
    assert not changes
    hook(catalog, "followup", 0)
    hook(catalog, "followup", 1)
    assert changes == [("Example", "Changed")]


@pytest.mark.parametrize("repeats", ["0", "-1"])
def test_invalid_repeat_count_never_creates_output(tmp_path, repeats):
    args = arguments(tmp_path, "--repeats", repeats)
    with pytest.raises(ValueError, match="positive integer"):
        run.evaluate(args)
    assert not args.output.exists()
