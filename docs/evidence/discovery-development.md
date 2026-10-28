# Recorded offline evidence: continuation-aware discovery

Final run: 2026-10-28, after publication evidence was added to cards. Actual SQLite catalog; synthetic worker records; 15 hand-authored development cases. Zero model calls and zero worker executions. This measures candidate inclusion, not model routing accuracy.

Command: `python -m evals.routing.run --scales 100 1000 10000 --output evals/routing/results/continuation-final-20261028`.

Concise machine-readable measurements: [discovery-development.json](discovery-development.json).

## Comparison at 10,000 workers

| Metadata / lookup | All required targets present | Maximum candidate-block characters | Lookup p95 ms (15 observations) |
|---|---:|---:|---:|
| Original full roster | 12/12 | 483,936 | 6.14 |
| Names / lexical | 7/12 | 3,201 | 22.98 |
| Instructions / lexical | 6/12 | 5,136 | 37.38 |
| Instructions / publication focus | 8/12 | 5,371 | 39.04 |
| Current state and observed IDs / lexical | 9/12 | 5,490 | 41.65 |
| Current state, observed IDs, and publication focus | 11/12 | 5,668 | 38.31 |

Twelve cases require existing workers. The other three are new work, ambiguity, and a direct answer, and are excluded from this recall denominator. Multiple requested workers must all be present. The full roster naturally includes every target; that does not establish successful selection.

Instruction metadata alone reduced recall from 7/12 to 6/12 in this fixture set: generic email terms pulled in similar Alice work during the silent-completion case. More text is not automatically better ranking. The improvement from current-state focus comes from three controlled current-state/object cases and two publication-follow-up cases.

Names imported from existing installations initially lack structured current state and observed references. They retain only the evidence actually recorded after activation; this evaluation does not claim automatic recovery of those fields from old logs.

## Cases that distinguish the mechanisms

| Case | Instructions / lexical | Current state / lexical | Current state / publication focus |
|---|---|---|---|
| `published_followup` | **miss** | **miss** | present |
| `unpublished_interruption` | **miss** | **miss** | present |
| `current_result_words` | **miss** | present | present |
| `same_person_current_work` | **miss** | present | present |
| `exact_observed_reference` | **miss** | present | present |
| `paraphrase` | **miss** | **miss** | **miss** |

The paraphrase deliberately has no shared topic words. It remains a documented lexical-search limitation. Publication references and structured tool observations are seeded correctly; source attribution quality is not tested by this table.

## Size and local lookup observations

| Workers | Full roster characters | Current-state focus max characters | Focus target inclusion | Focus lookup p95 ms | Current-state database MiB | Seed/build seconds |
|---|---:|---:|---:|---:|---:|---:|
| 100 | 4,776 | 5,659 | 11/12 | 18.04 | 0.11 | 0.11 |
| 1,000 | 48,336 | 5,669 | 11/12 | 21.17 | 0.45 | 0.16 |
| 10,000 | 483,936 | 5,668 | 11/12 | 38.31 | 4.37 | 0.78 |

At 100 workers the richer cards are larger than the original bare-name roster. The benefit is a bounded prompt as the population grows, not a promise that richer context is always smaller or lookup is always faster.

## Additional 100,000-worker probe

One final current-state catalog took 8.07 seconds to seed and used 36.62 MiB. Four cases were read against the same database without mutation: direct reference, deliberate paraphrase, current-result words, and exact observed ID. This is a smaller probe, not the full fifteen-case matrix.

| Mode | Targets present (4 cases) | Maximum candidate-block characters | Maximum observed lookup ms |
|---|---:|---:|---:|
| legacy | 4/4 | 4,839,936 | 59.47 |
| retrieval | 3/4 | 4,952 | 107.73 |
| focus | 3/4 | 4,952 | 113.20 |

An earlier probe before the final publication-card addition took 25.50 seconds to seed and reached **393.27 ms retrieval / 325.85 ms focus** for the broad direct-reference query. The final probe is faster, but two local runs do not establish a reliable latency distribution. Bounded returned context does not bound the cost of matching and ranking many records.

The probe used `roster_records(100000)`, `seed_catalog(catalog, records, "current_state")`, then `measure_discovery` for `direct_old`, `paraphrase`, `current_result_words`, and `exact_observed_reference` in all three modes. No publications were seeded in the four-case probe.

## Limits and verification

Lookup timing includes discovery and rendering only. It excludes model calls, queueing, worker execution, and publication. Each fixture has one local timing observation on isolated seeded copies; no claim of concurrent capacity or production latency follows. Catalog bytes cover synthetic metadata and indexes, not historical execution logs. Character counts are not token counts.

17 routing-evaluation tests passed, including real-loop stale-version rejection and actual silent completion preserving publication focus. Scripted model responses test these protocols; they do not measure model judgment. Existing FastAPI lifespan deprecation warnings remain.

Environment: Python 3.12.10, SQLite 3.49.1, Windows-11-10.0.26200-SP0. Baseline prompt/tool snapshot: `5b5f635935a64ab37884c025d70abb0ed731c094`. Bounds: 12 cards / 6,000 characters.

**Not measured:** live worker-selection accuracy, recovery success, unnecessary clarification, tokenizer usage, provider cost, completed user tasks, or multi-tenant concurrency. No calibration or statistical quality claim is made. Raw local measurements are retained in the ignored `evals/routing/results/continuation-final-20261028/` directory.
