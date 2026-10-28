"""Integrity checks for separately frozen, authored extension fixtures."""

from collections import Counter
from dataclasses import asdict
import hashlib
import json

from evals.routing import cases_challenge, cases_extension
from evals.routing.cases_extension import EXTENSION_CASES, WORKERS, fixture_manifest, roster_records


FROZEN_FIXTURE_SHA256 = "8629af30a460699c416581ab9e2863cc6cd49d52fdb52bd81fb4b8dc13f76d93"


def test_twenty_distinct_additional_requests_and_action_coverage():
    assert len(EXTENSION_CASES) == 20
    assert len({case.case_id for case in EXTENSION_CASES}) == 20
    assert len({case.query for case in EXTENSION_CASES}) == 20
    assert not {case.case_id for case in EXTENSION_CASES} & {case.case_id for case in cases_challenge.CHALLENGE_CASES}
    assert not {case.query for case in EXTENSION_CASES} & {case.query for case in cases_challenge.CHALLENGE_CASES}
    assert Counter(case.expected_action for case in EXTENSION_CASES) == {"route": 11, "new": 3, "ask": 3, "answer": 3}
    assert all(case.fixture_group == "extension" and case.manual_rubric and case.evidence for case in EXTENSION_CASES)
    assert all(isinstance(case, cases_challenge.ChallengeCase) for case in EXTENSION_CASES)


def test_gold_worker_names_exist_and_no_publication_is_preseeded():
    assert WORKERS is cases_challenge.WORKERS
    for case in EXTENSION_CASES:
        assert set(case.expected_agents) <= WORKERS.keys()
        assert len(case.expected_agents) == len(set(case.expected_agents))
        assert bool(case.expected_agents) == (case.expected_action == "route")
        assert case.published_agents == ()
        assert case.silent_results == () and case.mutation is None


def test_population_generator_is_shared_deterministic_and_nested():
    assert roster_records is cases_challenge.roster_records
    small, large = list(roster_records(100)), list(roster_records(1000))
    assert len(small) == 100 and len(large) == 1000
    assert small == large[:100] == list(roster_records(100))
    assert dict(small[:len(WORKERS)]) == WORKERS
    assert len({name for name, _ in large}) == len(large)


def test_five_callback_cases_have_valid_explicit_sources():
    callbacks = [case for case in EXTENSION_CASES if case.callback_results]
    assert len(callbacks) == 5
    all_ids = set()
    for case in callbacks:
        events = case.callback_results
        assert len({event["correlation_id"] for event in events}) == 1
        event_ids = {event["result_id"] for event in events}
        assert len(event_ids) == len(events)
        assert not event_ids & all_ids
        all_ids.update(event_ids)
        assert all(event["agent_name"] in WORKERS and event["success"] is True for event in events)
        assert set(case.expected_source_ids) <= event_ids
        assert set(case.expected_source_agents) == {event["agent_name"] for event in events if event["result_id"] in case.expected_source_ids}
        assert case.expected_source_ids
    selective = next(case for case in callbacks if case.case_id == "x10_panel_only")
    assert selective.expected_source_agents == ("Maya panel invitation",)
    assert len(selective.callback_results) == 2 and len(selective.expected_source_ids) == 1


def test_email_callbacks_separate_body_from_status_and_user_confirmation():
    draft_count = 0
    for case in EXTENSION_CASES:
        for event in case.callback_results:
            artifact = json.loads(event["response"])
            assert isinstance(artifact, dict) and artifact["artifact_type"]
            if artifact["artifact_type"] == "email_draft":
                draft_count += 1
                assert artifact["to"] and artifact["subject"] and artifact["body"]
                assert artifact["status"] == "not_sent"
                assert artifact["user_confirmation_required"] is True
                assert "Would you like me to send" not in artifact["body"]
                assert "user_confirmation_required" not in artifact["body"]
    assert draft_count == 5


def test_frozen_fixture_hash_and_manifest_disclose_provenance():
    payload = {"schema_version": 1, "workers": WORKERS, "cases": [asdict(case) for case in EXTENSION_CASES]}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    actual = hashlib.sha256(canonical).hexdigest()
    manifest = fixture_manifest()
    assert actual == FROZEN_FIXTURE_SHA256 == manifest["fixture_sha256"]
    assert manifest == fixture_manifest()
    assert manifest["suite"] == "extension" and manifest["case_count"] == 20
    assert manifest["groups"] == {"extension": [case.case_id for case in EXTENSION_CASES]}
    assert manifest["independent_holdout"] is False
    assert "after smoke" in manifest["note"]
    assert len(manifest["source_sha256"]) == 64
