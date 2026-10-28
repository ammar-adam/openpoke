"""Export condition-blind review packets and validate their label join.

Give reviewers only the review JSONL, not its separate private mapping. Mode,
model, case IDs, repetition, and metadata settings are omitted from packets;
wording can still reveal a condition, so this is metadata blinding, not a promise
that a reviewer cannot infer it. No LLM judge or automatic question detection.

python -m evals.routing.review export RESULTS --output REVIEW --mapping MAP
python -m evals.routing.review join RESULTS --mapping MAP --labels LABELS --output LABELED
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import uuid

from .reporting import callback_completed, identity, validate_rows

LABEL_FIELDS = {"response_correct", "publication_content_correct", "unnecessary_clarification"}


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _visible(turn):
    actions = [{"agent_name": call.get("arguments", {}).get("agent_name"),
                "instructions": call.get("arguments", {}).get("instructions")}
               for call in turn.get("tool_calls", [])
               if call.get("name") == "send_message_to_agent" and call.get("success") is True]
    return {"published_messages": turn.get("published_messages", []),
            "final_response": turn.get("response", ""), "accepted_worker_instructions": actions}


def prepare_review(rows, fixtures):
    validate_rows(rows)
    cases = {case["case_id"]: case for case in fixtures}
    if len(cases) != len(fixtures):
        raise ValueError("Duplicate fixture case_id")
    packets, entries = [], []
    for row in rows:
        if (row["record_status"] != "completed" and not callback_completed(row)) or row.get("evaluation") == "offline_lookup":
            continue
        case = cases.get(row["case_id"])
        if case is None:
            raise ValueError("A result has no matching saved fixture")
        review_id = uuid.uuid4().hex
        required = ["unnecessary_clarification", "response_correct"] if row["record_status"] == "completed" else []
        if callback_completed(row):
            required.append("publication_content_correct")
        turns = row.get("turns") or {}
        followup = turns.get("followup") or turns.get("user") or row
        packet = {"schema_version": 1, "review_id": review_id,
                  "request_history": case.get("history", ""), "request": case["query"],
                  "expected_action": case["expected_action"], "expected_workers": case.get("expected_agents", []),
                  "rubric": case.get("manual_rubric") or case.get("evidence", ""),
                  "callback_source_material": [{"agent_name": result["agent_name"], "response": result["response"]}
                                               for result in case.get("callback_results", [])],
                  "unshown_background_material": [{"agent_name": result["agent_name"], "response": result["response"]}
                                                   for result in case.get("silent_results", [])],
                  "state_change_before_response": case.get("mutation"),
                  "visible_callback": _visible(turns.get("callback") or {}),
                  "visible_followup": _visible(followup),
                  "followup_run_status": row["record_status"],
                  "label_definitions": {"response_correct": "The follow-up response and accepted worker instructions satisfy the request and rubric; matching only the worker name is insufficient.",
                                        "publication_content_correct": "The completed callback displays the correct intended content; source-ID metadata alone is insufficient.",
                                        "unnecessary_clarification": "The assistant asks a question that the available request and context already resolve."},
                  "required_labels": required,
                  "label_template": {"schema_version": 1, "review_id": review_id, "response_correct": None,
                                     "publication_content_correct": None, "unnecessary_clarification": None,
                                     "reviewer_type": None, "notes": ""}}
        packets.append(packet)
        entries.append({"review_id": review_id, "identity": list(identity(row)),
                        "record_sha256": fingerprint(row), "required_labels": required})
    secrets.SystemRandom().shuffle(packets)
    mapping = {"schema_version": 1, "source_sha256": fingerprint(rows), "fixture_sha256": fingerprint(fixtures),
               "entries": entries, "exported": len(packets), "not_exported": len(rows)-len(packets),
               "note": "Private join mapping: contains conditions. Do not give this file to blinded reviewers."}
    return packets, mapping


def join_labels(rows, mapping, labels):
    validate_rows(rows)
    if mapping.get("schema_version") != 1 or mapping.get("source_sha256") != fingerprint(rows):
        raise ValueError("Source results changed or do not match the review mapping")
    entries = {entry["review_id"]: entry for entry in mapping.get("entries", [])}
    if len(entries) != len(mapping.get("entries", [])):
        raise ValueError("Duplicate review ID in mapping")
    labeled = {}
    for label in labels:
        review_id = label.get("review_id")
        if label.get("schema_version") != 1 or review_id not in entries or review_id in labeled:
            raise ValueError("Each label needs schema_version=1 and a unique known review_id")
        unknown = set(label)-LABEL_FIELDS-{"schema_version", "review_id", "notes", "reviewer_type"}
        if unknown:
            raise ValueError(f"Unknown label fields: {sorted(unknown)}")
        if label.get("reviewer_type") not in {"human", "model_assisted"}:
            raise ValueError("reviewer_type must explicitly identify human or model_assisted review")
        required = entries[review_id]["required_labels"]
        if not set(required).issubset(label):
            raise ValueError("A required label field is missing; use null explicitly if not reviewed")
        for field in LABEL_FIELDS:
            if label.get(field) is not None and not isinstance(label[field], bool):
                raise ValueError(f"{field} must be boolean or null")
        if not isinstance(label.get("notes", ""), str):
            raise ValueError("Review notes must be text")
        labeled[review_id] = {**{field: label.get(field) for field in LABEL_FIELDS},
                              "notes": label.get("notes", ""), "review_id": review_id,
                              "reviewer_type": label["reviewer_type"],
                              "status": "complete" if all(isinstance(label[field], bool) for field in required) else "partial"}
    by_identity = {}
    source_rows = {identity(row): row for row in rows}
    for review_id, entry in entries.items():
        key = tuple(entry["identity"])
        if key in by_identity or key not in source_rows or fingerprint(source_rows[key]) != entry["record_sha256"]:
            raise ValueError("Mapping contains a duplicate, changed, or missing source record")
        by_identity[key] = review_id
    result = []
    for row in rows:
        review_id = by_identity.get(identity(row))
        updated = dict(row)
        if review_id in labeled:
            updated["manual_review"] = labeled[review_id]
        result.append(updated)
    return result


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("results", type=Path)
    export.add_argument("--fixtures", type=Path, help="Defaults to fixtures.json beside the results")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--mapping", type=Path, required=True)
    join = commands.add_parser("join")
    join.add_argument("results", type=Path)
    join.add_argument("--mapping", type=Path, required=True)
    join.add_argument("--labels", type=Path, required=True)
    join.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = read_jsonl(args.results)
    if args.output.exists():
        raise ValueError("Choose a fresh output path; review files are not overwritten")
    if args.command == "export":
        if args.mapping.exists() or args.mapping.resolve() == args.output.resolve():
            raise ValueError("Choose a separate fresh mapping path")
        fixtures = json.loads((args.fixtures or args.results.with_name("fixtures.json")).read_text(encoding="utf-8"))
        packets, mapping = prepare_review(rows, fixtures)
        _write_jsonl(args.output, packets)
        args.mapping.parent.mkdir(parents=True, exist_ok=True)
        with args.mapping.open("x", encoding="utf-8") as handle:
            json.dump(mapping, handle, indent=2)
        print(json.dumps({"exported": len(packets), "not_exported": len(rows)-len(packets),
                          "review": str(args.output), "private_mapping": str(args.mapping)}))
    else:
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        result = join_labels(rows, mapping, read_jsonl(args.labels))
        _write_jsonl(args.output, result)
        print(json.dumps({"records": len(result), "with_review": sum("manual_review" in row for row in result), "output": str(args.output)}))


if __name__ == "__main__":
    main()
