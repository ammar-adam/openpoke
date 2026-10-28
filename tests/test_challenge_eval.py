"""Synthetic tests of evaluation semantics; no model requests or accuracy claims."""
from dataclasses import asdict
import copy
import json

import pytest

from evals.routing.cases_challenge import CHALLENGE_CASES, WORKERS, fixture_manifest, roster_records
from evals.routing.reporting import build_report, candidate_presence, followup_score, render_markdown, validate_rows
from evals.routing.review import join_labels, main, prepare_review


def result(case_id="s03_same_person", mode="focus", repeat=0, **extra):
    case = next(c for c in CHALLENGE_CASES if c.case_id == case_id)
    row = {"case_id": case_id, "mode": mode, "repeat": repeat, "suite": "challenge", "fixture_group": case.fixture_group,
           "scale": 100, "metadata": "enriched", "model": "synthetic-test-model", "evaluation": "protocol_test",
           "record_status": "completed", "expected_action": case.expected_action,
           "expected_agents": list(case.expected_agents), "automatic_correct": True,
           "initial_all_targets_present": True, "response": "Submitted.", "tool_calls": []}
    row.update(extra)
    return row


def callback_result(**extra):
    row = result("s02_published_draft", publication_annotation={"correct": True},
                 turns={"callback": {"record_status": "completed", "published_messages": ["Lease draft for Maya."], "tool_calls": []},
                        "followup": {"record_status": "completed", "response": "Submitted.", "tool_calls": []}},
                 lookup_stage="before_callback_not_actual_followup")
    row.update(extra)
    return row


def review_label(packet, **extra):
    label = {**packet["label_template"], "reviewer_type": "model_assisted", "response_correct": True,
             "publication_content_correct": True, "unnecessary_clarification": False}
    label.update(extra)
    return label


def test_fixtures_frozen_before_first_run_and_not_claimed_heldout():
    manifest = fixture_manifest()
    assert manifest["fixture_sha256"] == "7a03e860497bffd0529535ded03e6c4b59ca38f496066b1a3daf86fe76bb82dd"
    assert len(CHALLENGE_CASES) == 20
    assert len(manifest["groups"]["smoke"]) == 6 and len(manifest["groups"]["validation"]) == 14
    assert manifest["independent_holdout"] is False
    assert all(not case.published_agents for case in CHALLENGE_CASES)
    assert all(set(case.expected_source_ids).issubset({event["result_id"] for event in case.callback_results}) for case in CHALLENGE_CASES)
    assert all(case.manual_rubric and set(case.expected_agents).issubset(WORKERS) for case in CHALLENGE_CASES)


def test_challenge_rosters_are_nested_unique_and_preserve_targets():
    small, large = list(roster_records(100)), list(roster_records(1000))
    assert large[:100] == small
    assert len(set(name for name, _ in large)) == 1000
    assert set(WORKERS).issubset(dict(small))
    assert len({instructions.split()[0] for _, instructions in large}) > 4


def test_statuses_are_explicit_and_failed_work_does_not_become_bad_routing_score():
    rows = [result(repeat=0), result(repeat=1, record_status="not_run", automatic_correct=None),
            result(repeat=2, record_status="incomplete", automatic_correct=None),
            result(repeat=3, record_status="operational_error", automatic_correct=None, observed_dispatch_correct=True)]
    group = build_report(rows)["groups"][0]
    assert group["statuses"] == {"completed": 1, "not_run": 1, "incomplete": 1, "operational_error": 1}
    assert group["automatic_dispatch_identity"]["observed"] == 1
    assert group["cases"][0]["scores"] == [True, None, None, None]
    with pytest.raises(ValueError, match="quality score"):
        validate_rows([result(record_status="not_run")])


def test_pre_callback_lookup_is_not_used_as_actual_followup_inclusion():
    row = callback_result(initial_all_targets_present=True)
    assert candidate_presence(row) is None
    row["turns"]["followup"]["discovery_trace"] = {"initial": {"selected": [{"agent_name": "wrong worker"}]}}
    assert candidate_presence(row) is False
    row["followup_initial_all_targets_present"] = True
    assert candidate_presence(row) is True


def test_ask_answer_need_attributed_review_and_wrong_dispatch_cannot_be_overruled():
    row = result("s05_which_draft", automatic_correct=None)
    assert followup_score(row) is None
    row["manual_review"] = {"reviewer_type": "human", "response_correct": True}
    assert followup_score(row) is True
    row["automatic_correct"] = False
    assert followup_score(row) is False
    assert build_report([result("s06_address", automatic_correct=None)])["groups"][0]["completed_unscored"] == 1


def test_repeats_are_not_majority_voted_and_pairs_require_complete_matching_attempts():
    rows = [result(mode=mode, repeat=repeat, automatic_correct=correct)
            for mode, scores in (("legacy", [True, True, False]), ("focus", [True, False, False]))
            for repeat, correct in enumerate(scores)]
    report = build_report(rows)
    pair = report["paired_comparisons"][0]
    assert pair["paired_case_count"] == 1
    assert pair["cases"][0]["repeat_count"] == 3
    assert sorted([pair["cases"][0]["left_rate"], pair["cases"][0]["right_rate"]]) == pytest.approx([1/3, 2/3])
    assert all(group["cases"][0]["mixed_observed_outcomes"] for group in report["groups"])
    partial = copy.deepcopy(rows)
    partial[-1].update(record_status="incomplete", automatic_correct=None)
    assert build_report(partial)["paired_comparisons"][0]["paired_case_count"] == 0
    assert build_report(rows[:-1])["paired_comparisons"][0]["excluded"][0]["reason"] == "missing_or_different_repeat_sets"


def test_correct_identity_and_source_annotation_do_not_imply_semantic_success():
    row = callback_result(manual_review={"reviewer_type": "model_assisted", "response_correct": False,
                                        "publication_content_correct": False, "unnecessary_clarification": False})
    group = build_report([row])["groups"][0]
    assert group["automatic_dispatch_identity"]["correct"] == 1
    assert group["publication_source_metadata"]["correct"] == 1
    assert group["response_semantics_review"]["correct"] == 0
    assert group["publication_content_review"]["correct"] == 0
    assert group["joint_scenario_behavior_review"]["correct"] == 0
    assert group["joint_scenario_behavior_review"]["by_reviewer_type"]["human"]["observed"] == 0
    assert group["joint_scenario_behavior_review"]["by_reviewer_type"]["model_assisted"]["observed"] == 1


@pytest.mark.parametrize("structural_failure", [
    {"callback_dispatches": ["Porto event venues"]},
    {"runtime_success": False},
    {"outcome": "unexpected_callback_dispatch"},
])
def test_joint_score_cannot_pass_structural_failure_with_correct_reviewed_text(structural_failure):
    row = callback_result(manual_review={"reviewer_type": "model_assisted", "response_correct": True,
                                        "publication_content_correct": True}, **structural_failure)
    group = build_report([row])["groups"][0]
    assert group["response_semantics_review"]["correct"] == 1
    assert group["publication_content_review"]["correct"] == 1
    assert group["joint_scenario_behavior_review"]["correct"] == 0
    assert group["joint_scenario_behavior_review"]["structural_failures"] == 1


def test_nested_callback_dispatch_is_a_joint_failure_without_inventing_human_review():
    row = callback_result()
    row["turns"]["callback"]["dispatches"] = ["Porto event venues"]
    joint = build_report([row])["groups"][0]["joint_scenario_behavior_review"]
    assert joint["observed"] == 1 and joint["correct"] == 0
    assert joint["unreviewed_components"] == 1
    assert joint["by_reviewer_type"]["human"]["observed"] == 0
    assert joint["by_reviewer_type"]["model_assisted"]["observed"] == 0


@pytest.mark.parametrize("status", ["not_run", "incomplete", "operational_error"])
def test_unfinished_status_keeps_joint_unknown_even_when_structural_failure_is_visible(status):
    row = callback_result(record_status=status, automatic_correct=None, runtime_success=False,
                          callback_dispatches=["Porto event venues"],
                          manual_review={"reviewer_type": "human", "response_correct": True,
                                         "publication_content_correct": True})
    from evals.routing.reporting import reviewed_scenario_score
    assert reviewed_scenario_score(row) is None
    assert build_report([row])["groups"][0]["joint_scenario_behavior_review"]["observed"] == 0


@pytest.mark.parametrize("failure", ["runtime_failed", "unexpected_callback_dispatch"])
def test_observed_identity_is_distinct_from_routing_protocol_failure(failure):
    row = callback_result(automatic_correct=False, observed_dispatch_correct=True, outcome=failure,
                          runtime_success=failure != "runtime_failed")
    report = build_report([row])
    group = report["groups"][0]
    assert group["automatic_dispatch_identity"] == {"observed": 1, "correct": 1, "fraction": 1}
    assert group["dispatch_or_reviewed_response_attempts"]["correct"] == 0
    assert group["combined_score_metric"] == "routing_protocol_or_reviewed_ask_answer"
    paired = build_report([row, {**row, "mode": "legacy"}])["paired_comparisons"][0]
    assert paired["metric"] == "routing_protocol_or_reviewed_ask_answer"
    assert "Routing protocol / reviewed ASK or ANSWER" in render_markdown(report)


def test_identity_compatibility_fallback_applies_only_to_absent_field():
    unknown = result(automatic_correct=False, observed_dispatch_correct=None)
    old = result(repeat=1, automatic_correct=True)
    group = build_report([unknown, old])["groups"][0]
    assert group["automatic_dispatch_identity"] == {"observed": 1, "correct": 1, "fraction": 1}


def test_completed_callback_survives_later_failure_for_publication_metrics_only():
    row = callback_result(record_status="operational_error", automatic_correct=None,
                          manual_review={"reviewer_type": "human", "publication_content_correct": True})
    group = build_report([row])["groups"][0]
    assert group["completed_callback_count"] == 1
    assert group["publication_source_metadata"]["observed"] == 1
    assert group["publication_content_review"]["observed"] == 1
    assert group["joint_scenario_behavior_review"]["observed"] == 0
    packets, _ = prepare_review([row], [asdict(CHALLENGE_CASES[1])])
    assert packets[0]["required_labels"] == ["publication_content_correct"]


def test_review_packets_hide_condition_and_preserve_instructions():
    row = result(tool_calls=[{"name": "send_message_to_agent", "success": True,
                              "arguments": {"agent_name": "Maya panel invitation", "instructions": "Keep Cedar Auditorium."}}])
    packets, mapping = prepare_review([row], [asdict(CHALLENGE_CASES[2])])
    packet = packets[0]
    assert not {"mode", "model", "record_id", "case_id", "repeat", "metadata", "scale"}.intersection(packet)
    assert "synthetic-test-model" not in json.dumps(packet)
    assert packet["visible_followup"]["accepted_worker_instructions"][0]["instructions"] == "Keep Cedar Auditorium."
    assert "synthetic-test-model" in json.dumps(mapping)
    assert "response_correct" in packet["required_labels"]
    assert "Judge only the follow-up" in packet["label_definitions"]["response_correct"]
    assert "callback actions are checked separately" in packet["label_definitions"]["response_correct"]


def test_label_join_is_validated_and_keeps_model_assistance_explicit():
    rows = [callback_result()]
    packets, mapping = prepare_review(rows, [asdict(CHALLENGE_CASES[1])])
    joined = join_labels(rows, mapping, [review_label(packets[0])])
    assert "manual_review" not in rows[0]
    assert joined[0]["manual_review"]["reviewer_type"] == "model_assisted"
    assert joined[0]["manual_review"]["status"] == "complete"
    for label in [review_label(packets[0], reviewer_type=None), review_label(packets[0], response_correct="yes"),
                  review_label(packets[0], review_id="unknown"), review_label(packets[0], mode="focus")]:
        with pytest.raises(ValueError):
            join_labels(rows, mapping, [label])
    with pytest.raises(ValueError, match="unique"):
        join_labels(rows, mapping, [review_label(packets[0]), review_label(packets[0])])
    changed = copy.deepcopy(rows)
    changed[0]["response"] = "Edited after export"
    with pytest.raises(ValueError, match="changed"):
        join_labels(changed, mapping, [])


def test_partial_reviews_and_unreviewed_callbacks_do_not_become_successes():
    rows = [callback_result()]
    packets, mapping = prepare_review(rows, [asdict(CHALLENGE_CASES[1])])
    joined = join_labels(rows, mapping, [review_label(packets[0], publication_content_correct=None)])
    group = build_report(joined)["groups"][0]
    assert group["publication_content_review"]["unreviewed"] == 1
    assert group["joint_scenario_behavior_review"]["observed"] == 0
    assert joined[0]["manual_review"]["status"] == "partial"


def test_duplicate_results_and_unattributed_review_are_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        build_report([result(), result()])
    with pytest.raises(ValueError, match="reviewer_type"):
        build_report([result(manual_review={"response_correct": True})])


def test_resources_count_reported_usage_once_and_keep_partial_latency_separate():
    calls = [{"status": "returned", "network_calls_started": 1, "latency_ms": 20,
              "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.02}},
             {"status": "returned", "network_calls_started": 1, "latency_ms": 30,
              "usage": {"prompt_tokens": 50, "completion_tokens": 0}}]
    complete = result(model_calls=calls, latency_ms=80, turns={"user": {"model_calls": calls}})
    partial = result(repeat=1, record_status="operational_error", automatic_correct=None, latency_ms=12,
                     model_calls=[{"status": "error", "network_calls_started": 1, "latency_ms": 12, "usage": None}])
    resources = build_report([complete, partial])["groups"][0]["resources"]
    assert resources["recorded_model_invocations"] == 3
    assert resources["network_requests_started"]["reported_total"] == 3
    assert resources["provider_reported_usage"]["prompt_tokens"] == {"reported_total": 150, "observed_calls": 2, "unavailable_calls": 1}
    assert resources["provider_reported_usage"]["cost"] == {"reported_total": 0.02, "observed_calls": 1, "unavailable_calls": 2}
    assert resources["completed_scenario_local_wall_latency"]["p50_ms"] == 80
    assert resources["partial_or_error_scenario_local_wall_latency"]["p50_ms"] == 12
    empty = build_report([result()])["groups"][0]["resources"]
    assert empty["provider_reported_usage"]["cost"]["reported_total"] is None
    assert empty["completed_scenario_local_wall_latency"]["p95_ms"] is None
    assert "INCLUDE configured evaluation pacing" in resources["latency_boundary"]
    assert "evidence of a speedup" in render_markdown(build_report([complete, partial]))


def test_cli_review_export_and_join(tmp_path):
    source, fixtures = tmp_path/"results.jsonl", tmp_path/"fixtures.json"
    source.write_text(json.dumps(result())+"\n", encoding="utf-8")
    fixtures.write_text(json.dumps([asdict(CHALLENGE_CASES[2])]), encoding="utf-8")
    packets, mapping, labels, joined = (tmp_path/name for name in ("review.jsonl", "private.json", "labels.jsonl", "joined.jsonl"))
    main(["export", str(source), "--output", str(packets), "--mapping", str(mapping)])
    packet = json.loads(packets.read_text())
    labels.write_text(json.dumps(review_label(packet, reviewer_type="human"))+"\n", encoding="utf-8")
    main(["join", str(source), "--mapping", str(mapping), "--labels", str(labels), "--output", str(joined)])
    report = build_report([json.loads(joined.read_text())])
    rendered = render_markdown(report)
    assert "dispatch identity" in rendered
    assert "model-assisted" in rendered
    with pytest.raises(ValueError, match="fresh"):
        main(["export", str(source), "--output", str(packets), "--mapping", str(mapping)])
