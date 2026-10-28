"""Describe completed routing attempts without hiding failures or review gaps.

Repeats remain grouped by case. Paired summaries use only cases whose identical
repeat sets completed and were scored in both modes. They are descriptive, not
independent-sample statistics or a majority-vote product policy.
"""
from collections import Counter, defaultdict
from itertools import combinations
import math
import statistics

STATUSES = ("completed", "not_run", "incomplete", "operational_error")
CONDITION_FIELDS = ("suite", "fixture_group", "scale", "metadata", "model", "mode")


def condition(row):
    return tuple(row.get(key) for key in CONDITION_FIELDS)


def identity(row):
    return condition(row) + (row["case_id"], row.get("repeat", 0))


def validate_rows(rows):
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str) or not row["case_id"]:
            raise ValueError("Each result requires a case_id")
        if row.get("record_status") not in STATUSES or not isinstance(row.get("mode"), str):
            raise ValueError("Each result requires an explicit record_status and mode")
        repeat = row.get("repeat", 0)
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 0:
            raise ValueError("repeat must be a nonnegative integer")
        key = identity(row)
        if key in seen:
            raise ValueError("Duplicate case/condition/repeat result")
        seen.add(key)
        if row.get("automatic_correct") is not None and not isinstance(row["automatic_correct"], bool):
            raise ValueError("automatic_correct must be boolean or null")
        if row["record_status"] != "completed" and row.get("automatic_correct") is not None:
            raise ValueError("Unfinished or operationally failed records cannot have a quality score")
        if "manual_review" in row and row["manual_review"].get("reviewer_type") not in {"human", "model_assisted"}:
            raise ValueError("Joined review must identify reviewer_type=human or model_assisted")
    return rows


def followup_score(row):
    """ASK/ANSWER require attributed review; known unexpected dispatches stay wrong."""
    if row["record_status"] != "completed" or row.get("evaluation") == "offline_lookup":
        return None
    automatic = row.get("automatic_correct")
    if row.get("expected_action") in {"ask", "answer"}:
        if automatic is False:
            return False
        reviewed = row.get("manual_review", {}).get("response_correct")
        return reviewed if isinstance(reviewed, bool) else None
    return automatic


def callback_completed(row):
    return ((row.get("turns") or {}).get("callback") or {}).get("record_status") == "completed"


def _joint_protocol_failure(row):
    callback = ((row.get("turns") or {}).get("callback") or {})
    return (row.get("runtime_success") is False or callback.get("runtime_success") is False
            or bool(row.get("callback_dispatches")) or bool(callback.get("dispatches"))
            or row.get("outcome") == "unexpected_callback_dispatch")


def reviewed_scenario_score(row):
    """Joint reviewed content/behavior with structural vetoes; never action success."""
    if row["record_status"] != "completed":
        return None
    if _joint_protocol_failure(row):
        return False
    review = row.get("manual_review", {})
    behavior = review.get("response_correct")
    if not isinstance(behavior, bool):
        return None
    if (row.get("turns") or {}).get("callback"):
        publication = review.get("publication_content_correct")
        return behavior and publication if isinstance(publication, bool) else None
    return behavior


def candidate_presence(row):
    """Never confuse a callback's preliminary lookup with the user's follow-up."""
    if not row.get("expected_agents"):
        return None
    if isinstance(row.get("followup_initial_all_targets_present"), bool):
        return row["followup_initial_all_targets_present"]
    turn = (row.get("turns") or {}).get("followup") or (row.get("turns") or {}).get("user") or row
    initial = (turn.get("discovery_trace") or {}).get("initial")
    if isinstance(initial, dict) and isinstance(initial.get("selected"), list):
        names = {item.get("agent_name") for item in initial["selected"]}
        return set(row["expected_agents"]).issubset(names)
    if row.get("lookup_stage") == "before_callback_not_actual_followup":
        return None
    value = row.get("initial_all_targets_present")
    return value if isinstance(value, bool) else None


def _metric(values):
    scored = [value for value in values if isinstance(value, bool)]
    return {"observed": len(scored), "correct": sum(scored),
            "fraction": sum(scored)/len(scored) if scored else None}


def _review_metric(rows, field):
    result = _metric([row.get("manual_review", {}).get(field) for row in rows])
    result.update(eligible=len(rows), unreviewed=len(rows)-result["observed"],
                  by_reviewer_type={kind: _metric([row.get("manual_review", {}).get(field) for row in rows
                                                 if row.get("manual_review", {}).get("reviewer_type") == kind])
                                    for kind in ("human", "model_assisted")})
    return result


def _nonnegative_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _distribution(values):
    values = sorted(value for value in values if _nonnegative_number(value))
    return {"observed": len(values), "p50_ms": statistics.median(values) if values else None,
            "p95_ms": values[math.ceil(.95*len(values))-1] if values else None}


def resource_metrics(rows):
    # The harness already concatenates callback and follow-up calls at top level.
    # Traversing turns as well would double-count cost and tokens.
    calls = [call for row in rows for call in row.get("model_calls", [])]
    usage = {}
    for name in ("prompt_tokens", "completion_tokens", "cost"):
        observed = [call["usage"][name] for call in calls
                    if isinstance(call.get("usage"), dict) and _nonnegative_number(call["usage"].get(name))]
        usage[name] = {"reported_total": sum(observed) if observed else None,
                       "observed_calls": len(observed), "unavailable_calls": len(calls)-len(observed)}
    network = [call["network_calls_started"] for call in calls if _nonnegative_number(call.get("network_calls_started"))]
    return {"recorded_model_invocations": len(calls),
            "responses_returned": sum(call.get("status") == "returned" for call in calls),
            "network_requests_started": {"reported_total": sum(network) if network else None,
                                         "observed_invocations": len(network), "unavailable_invocations": len(calls)-len(network)},
            "calls_without_usage_object": sum(not isinstance(call.get("usage"), dict) for call in calls),
            "provider_reported_usage": usage,
            "completed_scenario_local_wall_latency": _distribution(row.get("latency_ms") for row in rows if row["record_status"] == "completed"),
            "partial_or_error_scenario_local_wall_latency": _distribution(row.get("latency_ms") for row in rows if row["record_status"] in {"incomplete", "operational_error"}),
            "recorded_model_invocation_local_wall_latency": _distribution(call.get("latency_ms") for call in calls),
            "latency_boundary": "isolated IA scenario, including callback when present; workers mocked; scenario and model-invocation wall times INCLUDE configured evaluation pacing; not production latency, completed user-task latency, or evidence of a speedup",
            "cost_boundary": "Only provider-reported usage.cost in its returned units; ledger reservations and price estimates are excluded."}


def _group(rows):
    cases = defaultdict(list)
    for row in rows:
        cases[row["case_id"]].append(row)
    per_case = []
    for case_id, attempts in sorted(cases.items()):
        attempts.sort(key=lambda row: row.get("repeat", 0))
        scores = [followup_score(row) for row in attempts]
        per_case.append({"case_id": case_id, "repeats": [row.get("repeat", 0) for row in attempts],
                         "record_statuses": [row["record_status"] for row in attempts],
                         "scores": scores, "scored_attempts": _metric(scores),
                         "mixed_observed_outcomes": True in scores and False in scores})
    available_rates = [item["scored_attempts"]["fraction"] for item in per_case if item["scored_attempts"]["observed"]]
    callbacks = [row for row in rows if callback_completed(row)]
    annotations = [(row.get("publication_annotation") or {}).get("correct") for row in callbacks]
    completed = [row for row in rows if row["record_status"] == "completed" and row.get("evaluation") != "offline_lookup"]
    clarification_metric = _review_metric(completed, "unnecessary_clarification")
    clarification_metric["unnecessary"] = clarification_metric.pop("correct")
    for metric in clarification_metric["by_reviewer_type"].values():
        metric["unnecessary"] = metric.pop("correct")
    joint = _review_metric(
        [{**row, "manual_review": {**row.get("manual_review", {}), "scenario_correct": reviewed_scenario_score(row)}} for row in completed], "scenario_correct")
    joint["structural_failures"] = sum(_joint_protocol_failure(row) for row in completed)
    joint["complete_component_reviews"] = sum(
        isinstance(row.get("manual_review", {}).get("response_correct"), bool)
        and (not (row.get("turns") or {}).get("callback")
             or isinstance(row.get("manual_review", {}).get("publication_content_correct"), bool))
        for row in completed)
    joint["unreviewed_components"] = len(completed)-joint["complete_component_reviews"]
    return {"record_count": len(rows), "case_count": len(cases),
            "statuses": {status: sum(row["record_status"] == status for row in rows) for status in STATUSES},
            "candidate_inclusion": _metric([candidate_presence(row) for row in rows]),
            "dispatch_or_reviewed_response_attempts": _metric([followup_score(row) for row in rows]),
            "combined_score_metric": "routing_protocol_or_reviewed_ask_answer",
            "automatic_dispatch_identity": _metric([row.get("observed_dispatch_correct", row.get("automatic_correct"))
                                                      for row in completed if row.get("expected_action") in {"route", "new"}]),
            "mean_of_observed_case_rates": statistics.mean(available_rates) if available_rates else None,
            "cases_with_scored_attempts": len(available_rates), "cases": per_case,
            "publication_source_metadata": _metric(annotations),
            "completed_callback_count": len(callbacks),
            "response_semantics_review": _review_metric(completed, "response_correct"),
            "publication_content_review": _review_metric(callbacks, "publication_content_correct"),
            "joint_scenario_behavior_review": joint,
            "unnecessary_clarification_review": clarification_metric,
            "resources": resource_metrics(rows),
            "completed_unscored": sum(row["record_status"] == "completed" and followup_score(row) is None for row in rows)}


def _paired(rows, scorer=followup_score, metric="routing_protocol_or_reviewed_ask_answer"):
    cohorts = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for row in rows:
        key = condition(row)
        cohorts[key[:-1]][row["mode"]][row["case_id"]][row.get("repeat", 0)] = row
    comparisons = []
    for cohort, modes in sorted(cohorts.items(), key=lambda item: repr(item[0])):
        for left, right in combinations(sorted(modes), 2):
            included, excluded = [], []
            for case_id in sorted(set(modes[left]) | set(modes[right])):
                a, b = modes[left].get(case_id, {}), modes[right].get(case_id, {})
                if not a or set(a) != set(b):
                    excluded.append({"case_id": case_id, "reason": "missing_or_different_repeat_sets"})
                    continue
                if any(row["record_status"] != "completed" for row in list(a.values()) + list(b.values())):
                    excluded.append({"case_id": case_id, "reason": "not_all_attempts_completed"})
                    continue
                scores_a, scores_b = [scorer(a[i]) for i in sorted(a)], [scorer(b[i]) for i in sorted(b)]
                if None in scores_a or None in scores_b:
                    excluded.append({"case_id": case_id, "reason": "missing_quality_review_or_score"})
                    continue
                if any((metric == "reviewed_scenario_behavior" or a[i].get("expected_action") in {"ask", "answer"}) and
                       a[i].get("manual_review", {}).get("reviewer_type") != b[i].get("manual_review", {}).get("reviewer_type") for i in a):
                    excluded.append({"case_id": case_id, "reason": "different_reviewer_types"})
                    continue
                rate_a, rate_b = statistics.mean(scores_a), statistics.mean(scores_b)
                included.append({"case_id": case_id, "repeat_count": len(a), "left_rate": rate_a, "right_rate": rate_b, "difference_right_minus_left": rate_b-rate_a})
            comparisons.append({**dict(zip(CONDITION_FIELDS[:-1], cohort)), "metric": metric, "left_mode": left, "right_mode": right,
                                "paired_case_count": len(included), "cases": included, "excluded": excluded,
                                "mean_case_difference_right_minus_left": statistics.mean(case["difference_right_minus_left"] for case in included) if included else None})
    return comparisons


def build_report(rows):
    validate_rows(rows)
    groups = defaultdict(list)
    for row in rows:
        groups[condition(row)].append(row)
    return {"schema_version": 1, "record_count": len(rows),
            "statuses": dict(Counter(row["record_status"] for row in rows)),
            "groups": [{**dict(zip(CONDITION_FIELDS, key)), **_group(group)} for key, group in sorted(groups.items(), key=lambda item: repr(item[0]))],
            "paired_comparisons": _paired(rows),
            "paired_reviewed_scenario_comparisons": _paired(rows, reviewed_scenario_score, "reviewed_scenario_behavior"),
            "notes": ["Candidate inclusion is not final worker selection or task completion.",
                      "Operational errors, incomplete runs, and not-run records are shown separately; they have no quality score.",
                      "Completed ASK/ANSWER responses need attributed review; an unexpected dispatch still fails.",
                      "The routing protocol score includes matching worker dispatches, a finished IA loop, and no extra callback dispatch; it is broader than worker identity alone.",
                      "Worker identity uses observed_dispatch_correct, with automatic_correct only as a compatibility fallback for old rows missing that field. Identity matching does not establish instruction fidelity or completed worker actions.",
                      "Review metrics distinguish human from model-assisted judgments and retain unreviewed counts.",
                      "Publication metrics use completed callbacks even if the later follow-up fails; joint scenario review requires the whole run to complete.",
                      "Joint scenario review checks displayed content and response/instruction behavior; mocked workers cannot establish actual task completion.",
                      "Response correctness reviews only the follow-up. A failed IA loop or an extra callback dispatch independently forces completed joint scenarios to fail, even when response and publication labels are true.",
                      "Joint structural failures can be known before content review; unreviewed component counts remain separate and those failures are not presented as human judgments.",
                      "Source-ID annotation agreement is metadata only, not proof that the right content was displayed.",
                      "Repeated attempts remain grouped by case; no majority-vote policy or independent-sample significance claim is made.",
                      "Paired case differences require identical repeat sets, all complete and scored in both modes.",
                      "The reporter can account only for supplied records; retain runner placeholders and the planned-record manifest.",
                      "Smoke and validation cases were authored with implementation knowledge and are not independent holdouts."]}


def render_markdown(report):
    lines = ["# Routing challenge results", "", "Candidate inclusion, dispatch identity, reviewed behavior, and publication content are separate measurements.", "",
             "| Group / mode / scale / metadata | Complete | Not run | Incomplete | Operational error | Routing protocol / reviewed ASK or ANSWER | Unscored complete |", "|---|---:|---:|---:|---:|---:|---:|"]
    for group in report["groups"]:
        label = f"{group.get('fixture_group')} / {group['mode']} / {group.get('scale')} / {group.get('metadata')} / {group.get('model')}"
        status, score = group["statuses"], group["dispatch_or_reviewed_response_attempts"]
        lines.append(f"| {label} | {status['completed']} | {status['not_run']} | {status['incomplete']} | {status['operational_error']} | {score['correct']}/{score['observed']} | {group['completed_unscored']} |")
    lines += ["", "Repeats are listed by case in report.json. The attempt counts above are not independent case counts and do not imply a majority-vote product.", "",
              "## Lookup and publication metadata", "",
              "| Group / mode / scale / model | All targets initially present / observed | Correct automatic dispatch identities / observed | Matching publication source IDs / observed | Completed callbacks |",
              "|---|---:|---:|---:|---:|"]
    for group in report["groups"]:
        candidate, dispatch, annotation = (group[key] for key in ("candidate_inclusion", "automatic_dispatch_identity", "publication_source_metadata"))
        label = f"{group.get('fixture_group')} / {group['mode']} / {group.get('scale')} / {group.get('model')}"
        lines.append(f"| {label} | {candidate['correct']}/{candidate['observed']} | {dispatch['correct']}/{dispatch['observed']} | {annotation['correct']}/{annotation['observed']} | {group['completed_callback_count']} |")
    lines += ["", "Source-ID agreement is metadata, not proof of correct displayed content. An unobserved or inapplicable metric stays 0/0, not perfect accuracy.", "", "## Complete paired cases: routing protocol / reviewed ASK or ANSWER", ""]
    for pair in report["paired_comparisons"]:
        difference = pair["mean_case_difference_right_minus_left"]
        rendered = "unavailable" if difference is None else f"{difference:+.3f}"
        lines.append(f"- {pair.get('fixture_group')} / {pair.get('scale')} / {pair.get('metadata')}: {pair['right_mode']} minus {pair['left_mode']}: {rendered}; {pair['paired_case_count']} complete scored cases, {len(pair['excluded'])} excluded. This is descriptive, not statistical significance.")
    lines += ["", "## Reviewed semantics and content", "", "| Group / mode | Behavior reviewed / eligible | Publication reviewed / completed callbacks | Joint correct / evaluated | Structural joint failures | Joint reviewer labels: human / model-assisted |", "|---|---:|---:|---:|---:|---:|"]
    for group in report["groups"]:
        behavior, publication, joint = (group[key] for key in ("response_semantics_review", "publication_content_review", "joint_scenario_behavior_review"))
        kinds = joint["by_reviewer_type"]
        lines.append(f"| {group.get('fixture_group')} / {group['mode']} / {group.get('scale')} / {group.get('model')} | {behavior['observed']}/{behavior['eligible']} | {publication['observed']}/{publication['eligible']} | {joint['correct']}/{joint['observed']} | {joint['structural_failures']} | {kinds['human']['observed']} / {kinds['model_assisted']['observed']} |")
    lines += ["", "## Interpretation", ""] + [f"- {note}" for note in report["notes"]]
    lines += ["", "## Observed model resources", "",
              "| Group / mode / scale / model | Recorded invocations | Reported prompt / completion tokens | Reported usage.cost | Calls missing usage object | Complete IA scenario p50 / p95 ms |",
              "|---|---:|---:|---:|---:|---:|"]
    def display(value):
        return "unavailable" if value is None else f"{value:.6g}"
    for group in report["groups"]:
        resources = group["resources"]
        usage, latency = resources["provider_reported_usage"], resources["completed_scenario_local_wall_latency"]
        label = f"{group.get('fixture_group')} / {group['mode']} / {group.get('scale')} / {group.get('model')}"
        lines.append(f"| {label} | {resources['recorded_model_invocations']} | {display(usage['prompt_tokens']['reported_total'])} / {display(usage['completion_tokens']['reported_total'])} | {display(usage['cost']['reported_total'])} | {resources['calls_without_usage_object']} | {display(latency['p50_ms'])} / {display(latency['p95_ms'])} |")
    lines += ["", "Token/cost sums cover only reported fields; per-field unavailable counts are in report.json. Missing usage is not zero. Provider usage.cost is separate from ledger reservations and is not an invoice guarantee. Scenario and model-invocation wall timings INCLUDE configured evaluation pacing; consult the run manifest for its interval. These local synthetic-scenario IA timings use mocked workers and are not production latency, completed user-task latency, or evidence of a speedup. Partial/error durations are reported separately in JSON."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    markdown = args.markdown or args.output.with_suffix(".md")
    if len({args.results.resolve(), args.output.resolve(), markdown.resolve()}) != 3:
        raise ValueError("Input, JSON report, and Markdown report need separate paths")
    rows = [json.loads(line) for line in args.results.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = build_report(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"records": len(rows), "json": str(args.output), "markdown": str(markdown)}))


if __name__ == "__main__":
    main()
