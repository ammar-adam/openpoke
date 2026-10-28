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
from html import escape
import json
import platform
from pathlib import Path
import sqlite3
import tempfile
import time

from .cases import BASELINE_COMMIT, CASES, CURRENT_RESULTS, OBSERVED_DRAFT_ID, roster_records
from .harness import LiveResponder, MemoryRoster, run_case


def clone_database(source: Path, destination: Path):
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def seed_catalog(catalog, records, metadata):
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
        for name, response in CURRENT_RESULTS.items():
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
        result = catalog.shortlist(case.query, include_focus=mode == "focus", max_cards=12, max_chars=6000)
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
        grouped[(row["metadata"], row["scale"], row["mode"])].append(row)
    lines = [
        "# Agent discovery development evaluation", "",
        f"Baseline source: `{baseline_commit}`. Cases are hand-authored development scenarios, not a held-out benchmark.", "",
        "Worker execution is mocked. Initial candidate recall measures lookup only; it is not model routing accuracy.", "",
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
    misses = [row for row in results if row["initial_all_targets_present"] is False and row["mode"] != "legacy"]
    if misses:
        lines += ["", "Initial candidate misses (a live agent may still recover through its one search expansion):", ""]
        for row in misses:
            lines.append(f"- {row['metadata']}, {row['scale']}, {row['mode']}: `{row['case_id']}`")
    return "\n".join(lines) + "\n"


def evaluate(args):
    from server.services.execution.catalog import AgentCatalog
    responder = None
    if args.live:
        from server.config import get_settings
        key = get_settings().openrouter_api_key
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is not configured; offline evaluation needs no key.")
        if args.input_rate is None or args.output_rate is None or args.max_estimated_usd is None:
            raise SystemExit("Live mode requires --input-rate, --output-rate and --max-estimated-usd. Supply verified effective model prices per million tokens.")
        responder = LiveResponder(model=args.model, api_key=key, max_calls=args.max_calls,
            max_estimated_usd=args.max_estimated_usd, input_rate=args.input_rate, output_rate=args.output_rate)
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="routing-eval-") as scratch:
        scratch = Path(scratch)
        for scale in args.scales:
            records = list(roster_records(scale))
            names = [name for name, _ in records]
            for metadata in args.metadata:
                seed_path = scratch / f"seed-{scale}-{metadata}.db"
                build_started = time.perf_counter()
                seed_catalog(AgentCatalog(seed_path), records, metadata)
                build_ms = (time.perf_counter() - build_started) * 1000
                database_bytes = sum(path.stat().st_size for path in scratch.glob(seed_path.name + "*"))
                for case in CASES:
                    for mode in args.modes:
                        case_path = scratch / f"{scale}-{metadata}-{case.case_id}-{mode}.db"
                        clone_database(seed_path, case_path)
                        catalog = AgentCatalog(case_path)
                        seed_publications(catalog, case, metadata)
                        row = measure_discovery(catalog, case, mode, names)
                        row.update(scale=scale, metadata=metadata, evaluation="live_model" if args.live else "offline_lookup",
                                   catalog_build_ms=build_ms, catalog_database_bytes=database_bytes)
                        if responder:
                            result = asyncio.run(run_case(case, mode, catalog, MemoryRoster(names), responder, model=args.model))
                            row.update(result)
                        rows.append(row)
                        with (args.output / "results.jsonl").open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                        # Keep one isolated case copy at a time, including at 100k.
                        for path in scratch.glob(case_path.name + "*"):
                            path.unlink()
                print(f"Completed {metadata} population of {scale} workers.", flush=True)
    (args.output / "summary.md").write_text(summarize(rows, live=args.live), encoding="utf-8")
    manifest = {"baseline_commit": BASELINE_COMMIT, "live": args.live, "scales": args.scales,
        "modes": args.modes, "metadata": args.metadata, "case_count": len(CASES),
        "model": args.model if args.live else None, "held_out": False,
        "live_calls": responder.calls if responder else 0,
        "reserved_estimated_usd": responder.reserved_estimated_usd if responder else None,
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(), "shortlist_max_cards": 12,
        "shortlist_max_characters": 6000, "limits_are_tokens": False,
        "publication_fixtures": "preseeded correct references; annotation quality is not measured"}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(rows)} records and summary to {args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--metadata", nargs="+", choices=["names_only", "enriched", "current_state"], default=["names_only", "enriched", "current_state"])
    parser.add_argument("--modes", nargs="+", choices=["legacy", "retrieval", "focus"], default=["legacy", "retrieval", "focus"])
    parser.add_argument("--output", type=Path, default=Path("evals/routing/results") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="anthropic/claude-sonnet-4")
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--max-estimated-usd", type=float)
    parser.add_argument("--input-rate", type=float)
    parser.add_argument("--output-rate", type=float)
    args = parser.parse_args()
    if (args.output / "results.jsonl").exists():
        parser.error("output already contains results.jsonl; choose a new directory to avoid mixing runs")
    evaluate(args)


if __name__ == "__main__":
    main()
