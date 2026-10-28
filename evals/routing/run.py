"""Offline discovery measurements, or opt-in live routing on synthetic data.

Examples:
  python -m evals.routing.run --scales 100 1000 10000 --output evals/routing/results
  python -m evals.routing.run --live --scales 100 --metadata enriched \
      --input-rate 3 --output-rate 15 --max-estimated-usd 2 --max-calls 100

The rates in the example are illustrative inputs, not a current price quote.
All worker executions are mocked even in live mode. Nothing sends email.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import platform
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

from .cases import BASELINE_COMMIT, CASES, CURRENT_RESULTS, OBSERVED_DRAFT_ID, roster_records
from .harness import LiveResponder, MemoryRoster, run_case, run_two_turn_case
from .budget import ReservationLedger, positive_integer, positive_number


def clone_database(source: Path, destination: Path):
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def seed_catalog(catalog, records, metadata, *, current_results=None):
    roster = MemoryRoster(name for name, _ in records)
    catalog.ensure_legacy_backfill(roster)
    if metadata in {"enriched", "current_state"}:
        batch = getattr(catalog, "record_dispatch_many", None)
        if batch:
            batch(records)
        else:
            for name, instructions in records:
                catalog.record_dispatch(name, instructions)
    if metadata == "current_state":
        for name, response in (CURRENT_RESULTS if current_results is None else current_results).items():
            run_id = f"fixture-state-{name}"
            catalog.record_run_started(name, run_id)
            if name == "Email to Alice Q3 budget":
                catalog.record_tool_result(name, run_id, "gmail_create_draft", {},
                                           {"successful": True, "data": {"id": OBSERVED_DRAFT_ID}}, True)
            catalog.record_run_finished(name, run_id, "completed", response)
    return roster


def seed_publications(catalog, case, metadata=None):
    for index, name in enumerate(case.published_agents):
        catalog.record_publication(
            [{"result_id": f"fixture-{case.case_id}-{index}", "agent_name": name,
              "correlation_id": f"fixture-turn-{index}"}],
            kind="send_draft", message=f"Fixture publication for {name}.",
        )
    if case.case_id == "unpublished_interruption" and metadata == "current_state":
        name = "Kyoto hotel search"
        catalog.record_run_started(name, "fixture-unshown-completion")
        catalog.record_run_finished(name, "fixture-unshown-completion", "completed", "Hotel comparison finished silently.")


def measure_discovery(catalog, case, mode, names):
    started = time.perf_counter()
    if mode == "legacy":
        rendered = "\n".join(f'<agent name="{escape(name, quote=True)}" />' for name in names)
        candidates = names
        trace = {"reason": "full legacy roster"}
    else:
        result = catalog.shortlist_for_turn(case.query, case.history, include_focus=mode == "focus", max_cards=12, max_chars=6000)
        rendered = result["rendered"]
        candidates = [card["agent_name"] for card in result["cards"]]
        trace = result["trace"]
    targets = set(case.expected_agents)
    return {
        "case_id": case.case_id, "category": case.category, "mode": mode,
        "initial_candidate_count": len(candidates), "initial_roster_characters": len(rendered),
        "initial_lookup_ms": (time.perf_counter() - started) * 1000,
        "initial_target_recall": len(targets.intersection(candidates)) / len(targets) if targets else None,
        "initial_all_targets_present": targets.issubset(candidates) if targets else None,
        "initial_candidates": candidates if mode != "legacy" else None,
        "initial_trace": trace,
    }


def summarize(results, *, live, baseline_commit=BASELINE_COMMIT):
    grouped = defaultdict(list)
    for row in results:
        if row.get("lookup_stage") == "before_callback_not_actual_followup":
            continue
        grouped[(row["metadata"], row["scale"], row["mode"])].append(row)
    lines = [
        "# Agent discovery development evaluation", "",
        f"Baseline source: `{baseline_commit}`. Cases are hand-authored development scenarios, not a held-out benchmark.", "",
        "Worker execution is mocked. Initial candidate recall measures lookup only; it is not model routing accuracy.", "",
        "Two-turn callback cases are excluded from this pre-run lookup table. Their actual follow-up lookup is recorded by the live harness and summarized in report.md.", "",
        "Names-only populations represent legacy import. Enriched adds first/latest instructions. Current-state adds completed-result excerpts and observed object references. Legacy logs are not backfilled with those fields.", "",
        "Legacy shows every name; retrieval uses lexical search without publication focus; focus also includes recently published work. Both shortlist modes can match an exact observed reference when current-state metadata is available. No extra routing model is involved.", "",
        "| Metadata | Workers | Mode | All targets present | Maximum roster characters | Lookup p95 ms |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for (metadata, scale, mode), rows in sorted(grouped.items()):
        eligible = [row for row in rows if row["initial_all_targets_present"] is not None]
        found = sum(row["initial_all_targets_present"] for row in eligible)
        latencies = sorted(row["initial_lookup_ms"] for row in rows)
        p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        lines.append(f"| {metadata} | {scale} | {mode} | {found}/{len(eligible)} | {max(row['initial_roster_characters'] for row in rows)} | {p95:.2f} |")
    lines += ["", "The character ceiling applies to the candidate block. Conversation history, tool schemas, repeated calls, and worker memory are separate costs.", "",
              "Times are one local lookup per fixture on isolated copies after seeding, not a concurrent serving benchmark. Database bytes and one-time build time are recorded separately in results.jsonl.", ""]
    if live:
        lines += ["Live model results are in results.jsonl. Review every response marked manual_review_required; no automatic ASK-accuracy claim is made.", "", "Reported usage is kept per model call. A missing usage/cost field is unavailable, not zero. Price-based reservations are estimates, not provider billing guarantees."]
    else:
        lines += ["**Live routing evaluation was not run.** No model accuracy, model token savings, provider cost, or user-task completion claim follows from this offline table."]
    misses = [row for row in results if row["initial_all_targets_present"] is False and row["mode"] != "legacy"
              and row.get("lookup_stage") != "before_callback_not_actual_followup"]
    if misses:
        lines += ["", "Initial candidate misses (a live agent may still recover through its one search expansion):", ""]
        for row in misses:
            lines.append(f"- {row['metadata']}, {row['scale']}, {row['mode']}: `{row['case_id']}`")
    return "\n".join(lines) + "\n"


def selected_suite(args):
    if args.suite in {"challenge", "extension"}:
        if args.suite == "challenge":
            from .cases_challenge import CHALLENGE_CASES, roster_records as challenge_records
            cases, records = list(CHALLENGE_CASES), challenge_records
        else:
            from .cases_extension import EXTENSION_CASES, roster_records as extension_records
            cases, records = list(EXTENSION_CASES), extension_records
        if args.split != "all":
            cases = [case for case in cases if case.fixture_group == args.split]
    else:
        if args.split != "all":
            raise ValueError("--split applies only to the challenge or extension suite")
        cases, records = list(CASES), roster_records
    if args.case_ids:
        missing = set(args.case_ids) - {case.case_id for case in cases}
        if missing:
            raise ValueError(f"Unknown cases in selected split: {sorted(missing)}")
        cases = [case for case in cases if case.case_id in args.case_ids]
    if not cases:
        raise ValueError("No cases selected")
    return cases, records


def trial_plan(cases, modes, repeats):
    """Rotate order without changing the product or majority-voting its answers."""
    for repeat in range(repeats):
        for index, case in enumerate(cases):
            offset = (index + repeat) % len(modes)
            for mode in modes[offset:] + modes[:offset]:
                yield case, mode, repeat


def fixture_fingerprint(cases):
    payload = json.dumps([asdict(case) for case in cases], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def code_snapshot():
    root = Path(__file__).resolve().parents[2]
    def git(*arguments):
        result = subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=True)
        return result.stdout.strip()
    try:
        # Include untracked source files in the digest while excluding ignored results/secrets.
        paths = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        digest = hashlib.sha256()
        for relative in sorted(set(paths)):
            path = root / relative
            if path.is_file() and path.suffix in {".py", ".txt", ".json"}:
                digest.update(relative.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
                "source_sha256": digest.hexdigest()}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "source_sha256": None}


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def mutation_hook(case):
    mutation = getattr(case, "mutation", None)
    if not mutation:
        return None
    applied = False
    def before_model_call(catalog, turn_name, call_index):
        nonlocal applied
        target_turn = "followup" if mutation.get("when") == "before_followup_model_call" else "user"
        if not applied and turn_name == target_turn and call_index == mutation.get("call_index", 0):
            catalog.record_dispatch(mutation["agent_name"], mutation["instructions"])
            applied = True
    return before_model_call


def evaluate(args):
    from server.services.execution.catalog import AgentCatalog
    positive_integer(args.repeats, "repeats")
    for scale in args.scales:
        positive_integer(scale, "scale")
    cases, make_records = selected_suite(args)
    # Validate populations and output before requesting any model response.
    populations = {scale: list(make_records(scale)) for scale in args.scales}
    if (args.output / "results.jsonl").exists() or (args.output / "manifest.json").exists():
        raise ValueError("Output already contains a run; choose a new directory")
    responder, ledger = None, None
    if args.live:
        from server.config import get_settings
        if not args.model:
            raise ValueError("Live mode requires an explicit --model identifier")
        key = get_settings().openrouter_api_key
        if not key:
            raise ValueError("OPENROUTER_API_KEY is not configured; offline evaluation needs no key")
        for name in ("input_rate", "output_rate", "max_estimated_usd", "total_budget_usd"):
            positive_number(getattr(args, name), name)
        for name in ("max_calls", "total_max_calls", "max_output_tokens"):
            positive_integer(getattr(args, name), name)
        ledger = ReservationLedger(args.ledger, args.total_budget_usd, args.total_max_calls)
        responder = LiveResponder(model=args.model, api_key=key, max_calls=args.max_calls,
            max_estimated_usd=args.max_estimated_usd, input_rate=args.input_rate,
            output_rate=args.output_rate, max_output_tokens=args.max_output_tokens, ledger=ledger,
            min_call_interval_seconds=args.min_call_interval)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "baseline_commit": BASELINE_COMMIT, "code": code_snapshot(), "live": args.live,
        "suite": args.suite, "split": args.split, "case_ids": [case.case_id for case in cases],
        "fixture_sha256": fixture_fingerprint(cases), "case_count": len(cases),
        "scales": args.scales, "modes": args.modes, "metadata": args.metadata, "repeats": args.repeats,
        "model": args.model if args.live else None, "held_out": False,
        "planned_records": len(cases) * len(args.scales) * len(args.metadata) * len(args.modes) * args.repeats,
        "pricing_per_million": {"input": args.input_rate, "output": args.output_rate,
                                "source": args.price_source} if args.live else None,
        "per_run_limits": {"calls": args.max_calls, "estimated_usd": args.max_estimated_usd,
                           "output_tokens_per_call": args.max_output_tokens,
                           "min_call_interval_seconds": args.min_call_interval} if args.live else None,
        "ledger_path": str(args.ledger.resolve()) if args.live else None,
        "ledger_before": ledger.snapshot() if ledger else None,
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(), "shortlist_max_cards": 12,
        "shortlist_max_characters": 6000, "limits_are_tokens": False,
        "publication_fixtures": ("real callback turn before follow-up for callback cases; content needs review"
                                 if args.suite != "development" else "preseeded references; annotation quality not measured"),
        "status": "running", "record_count": 0, "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.suite != "development":
        if args.suite == "challenge":
            from .cases_challenge import fixture_manifest
        else:
            from .cases_extension import fixture_manifest
        manifest["full_suite_manifest"] = fixture_manifest()
    # Persist the exact case selection before the first request, including after a crash.
    write_json(args.output / "manifest.json", manifest)
    write_json(args.output / "fixtures.json", [asdict(case) for case in cases])
    rows, stop_reason = [], None
    with tempfile.TemporaryDirectory(prefix="routing-eval-") as scratch:
        scratch = Path(scratch)
        for scale in args.scales:
            records = populations[scale]
            names = [name for name, _ in records]
            for metadata in args.metadata:
                seed_path = scratch / f"seed-{scale}-{metadata}.db"
                build_started = time.perf_counter()
                seed_catalog(AgentCatalog(seed_path), records, metadata,
                             current_results={} if args.suite != "development" else None)
                build_ms = (time.perf_counter() - build_started) * 1000
                database_bytes = sum(path.stat().st_size for path in scratch.glob(seed_path.name + "*"))
                for case, mode, repeat in trial_plan(cases, args.modes, args.repeats):
                    case_path = scratch / f"{scale}-{metadata}-{case.case_id}-{mode}-{repeat}.db"
                    clone_database(seed_path, case_path)
                    catalog = AgentCatalog(case_path)
                    if not getattr(case, "callback_results", ()):
                        seed_publications(catalog, case, metadata)
                    row = measure_discovery(catalog, case, mode, names)
                    row.update(scale=scale, metadata=metadata, repeat=repeat, suite=args.suite,
                        fixture_group=getattr(case, "fixture_group", "development"),
                        model=args.model if args.live else None,
                        evaluation="live_model" if args.live else "offline_lookup",
                        expected_action=case.expected_action, expected_agents=list(case.expected_agents),
                        query=case.query, evidence=case.evidence,
                        manual_rubric=getattr(case, "manual_rubric", ""),
                        catalog_build_ms=build_ms, catalog_database_bytes=database_bytes,
                        record_status="completed" if not args.live else "not_run")
                    if getattr(case, "callback_results", ()):
                        row["lookup_stage"] = "before_callback_not_actual_followup"
                    if responder and not stop_reason:
                        execute_case = run_two_turn_case if getattr(case, "callback_results", ()) else run_case
                        result = asyncio.run(execute_case(case, mode, catalog, MemoryRoster(names), responder,
                            model=args.model, before_model_call=mutation_hook(case)))
                        row.update(result)
                        if row.get("record_status") in {"not_run", "incomplete", "operational_error"}:
                            # Fail fast on operational errors too: repeated auth/provider failures are not evidence.
                            stop_reason = row.get("failure_reason") or row["record_status"]
                    elif responder:
                        row.update(record_status="not_run", automatic_correct=None,
                                   failure_reason=f"Campaign phase stopped: {stop_reason}")
                    rows.append(row)
                    with (args.output / "results.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                    manifest.update(record_count=len(rows), live_calls=responder.calls if responder else 0,
                        reserved_estimated_usd=responder.reserved_estimated_usd if responder else None,
                        ledger_after=ledger.snapshot() if ledger else None, stop_reason=stop_reason)
                    write_json(args.output / "manifest.json", manifest)
                    for path in scratch.glob(case_path.name + "*"):
                        path.unlink()
                    if args.live:
                        print(f"{len(rows)}/{manifest['planned_records']} {case.case_id} {mode} repeat={repeat} {row['record_status']}", flush=True)
                print(f"Completed {metadata} population of {scale} workers.", flush=True)
    (args.output / "summary.md").write_text(summarize(rows, live=args.live), encoding="utf-8")
    if args.live:
        from .reporting import build_report, render_markdown
        report = build_report(rows)
        write_json(args.output / "report.json", report)
        (args.output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    manifest.update(status="stopped" if stop_reason else "completed")
    write_json(args.output / "manifest.json", manifest)
    print(f"Saved {len(rows)} records and summary to {args.output}")
    return rows


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--metadata", nargs="+", choices=["names_only", "enriched", "current_state"], default=["names_only", "enriched", "current_state"])
    parser.add_argument("--modes", nargs="+", choices=["legacy", "retrieval", "focus"], default=["legacy", "retrieval", "focus"])
    parser.add_argument("--suite", choices=["development", "challenge", "extension"], default="development")
    parser.add_argument("--split", choices=["all", "smoke", "validation", "extension"], default="all")
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("evals/routing/results") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", help="Required in live mode; no silent model substitution")
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--max-estimated-usd", type=float)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--min-call-interval", type=float, default=0,
                        help="Minimum seconds between request starts in this process; no retries")
    parser.add_argument("--input-rate", type=float)
    parser.add_argument("--output-rate", type=float)
    parser.add_argument("--price-source", default="operator-supplied effective rates")
    parser.add_argument("--ledger", type=Path, default=Path("evals/routing/results/live-budget.sqlite3"))
    parser.add_argument("--total-budget-usd", type=float, default=50)
    parser.add_argument("--total-max-calls", type=int, default=5000)
    return parser


def main():
    parser = build_parser()
    try:
        evaluate(parser.parse_args())
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
