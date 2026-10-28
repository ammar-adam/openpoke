# Live routing evidence — 28 October 2026

The measured improvement is smaller model input. Routing quality is mixed. On the main 1,000-worker comparison, `focus` used **76.2% fewer reported input tokens** than the full roster, but selected the correct worker identity in **25/30** applicable attempts versus **28/30** for the full roster. Model-assisted content review favored the bounded approach on some broader scenarios; it does not establish a general accuracy improvement.

This replaces “we only ran mocked tests” with a narrower, defensible statement: **we ran real models through the Interaction Agent loop, with worker execution mocked, and kept the failures.** Actual Gmail delivery and production task completion are not measured by this routing campaign.

A separate [live execution demo](../simulated-gmail-demo.md) ran the actual workers
and callbacks against simulated Gmail on runtime `0b884a6`. All 12 integration
checks passed across 15 model calls: the exact draft was shown, then the same
saved draft was sent once after a separate confirmation. The draft appeared twice
in the conversation. This single synthetic success does not establish real Gmail
behavior or reliability; the [full trace](execution-demo.json) is retained.

## What was compared

| Mode | Information available to the Interaction Agent |
|---|---|
| `legacy` | Entire roster of names, using the pinned original prompt/tool schema. |
| `retrieval` | At most 12 current agent cards and 6,000 characters; exact references, indexed lexical retrieval and conservative prior-user-turn continuation recovery. More results can be requested through `find_agents`; versions are checked before dispatch. |
| `focus` | The same bounded retrieval and dispatch version checks, plus references to work actually published to the user. |

All modes within each phase share its runtime. The Luna phases after the smoke failure include the first wait-yield correction. Commit `0b884a6` subsequently fixed a second case: a successful `wait` in the same batch must not hide a failed tool call, such as a dispatch rejected for stale state. The Sonnet recovery runs that later runtime; the full Luna comparison was not rerun on it. Its 76.2% input reduction and quality scores describe the recorded earlier runtime.

This is a comparison of complete feature configurations: full names versus richer bounded cards, not an isolated experiment on a ranking algorithm. The phase manifests record the actual code commit, dirty-tree flag and source hash. Different runtime cohorts are kept separate.

## Scope and provenance

| Phase | Authored cases | Persisted trials | Purpose |
|---|---:|---:|---|
| Luna smoke, before wait fix | 6 planned | 3 completed, 1 operational error, 14 not run | Discovered repeated-wait behavior; HTTP 429 stopped the phase. Excluded from post-fix comparisons. |
| Luna smoke, after wait fix | 6 | 18 completed | Three modes, one attempt per case. |
| Luna main, 1,000 workers | 14 | 126 completed | Three modes, three attempts per case. |
| Luna extension, 1,000 workers | 20 | 60 completed | Additional scenarios, three modes, one attempt each. |
| Luna stress, 10,000 workers | 14 reused | 28 completed | The two bounded modes, one attempt each; no live full-roster baseline at this scale. |
| First Sonnet confirmation | 8 planned | 1 completed result persisted | Host/session interruption; original running manifest retained with an interruption note. An unfinished second trial may have spent calls. Never pooled with a fresh rerun. |
| Sonnet recovery, 1,000 workers | 8 selected | 24 completed | Three modes, one attempt each, on runtime `0b884a6`; 82 model invocations. |

There are **40 distinct newly authored scenarios**, not hundreds of independent user requests. The original 15-case development set remains regression material. Repeats and reused stress cases do not increase the unique scenario count. Cases were written with implementation knowledge; neither “validation” nor “extension” is an independent holdout.

The primary model was `openai/gpt-6-luna` through OpenRouter. The completed, separate confirmation uses `anthropic/claude-sonnet-4.6`. The original interrupted Sonnet attempt remains separate in [the machine-readable snapshot](live-routing.json); it is not counted as a successful phase.

## Main comparison: 14 cases, three attempts, 1,000 workers

Each mode has 42 completed trials. Denominators differ because a request to answer or ask a question does not require worker dispatch, and some review judgments remain unresolved.

| Measurement | Full roster | Retrieval | Retrieval + published context (`focus`) |
|---|---:|---:|---:|
| All required workers in initial context | 27/27 | 24/27 | 26/27 |
| Correct dispatched worker identities | 28/30 | 25/30 | 25/30 |
| Follow-up meaning/instructions correct, reviewed | 24/36; 6 unresolved | 33/39; 3 unresolved | 31/38; 4 unresolved |
| Displayed callback content correct, reviewed | 10/13; 8 unresolved | 10/10; 11 unresolved | 12/12; 9 unresolved |
| Joint content/behavior score, including structural failures | 21/32; 10 unscored | 25/31; 11 unscored | 26/32; 10 unscored |
| Model invocations | 167 | 191 | 179 |
| Reported input tokens across all calls | 3,439,273 | 873,976 | 817,914 |
| Reported output tokens | 16,549 | 19,645 | 18,848 |

**Do not compare the joint fractions as if their denominators were interchangeable.** Their unresolved cases differ. The archive includes paired-case reports that exclude missing or unresolved comparisons. No significance claim or majority-vote deployment policy is made. Correct worker identity also does not prove the worker received correct instructions, or that it completed them.

The headline token reduction is `1 - 817914 / 3439273`. It covers every recorded call in each main-phase mode, including callback and follow-up turns. It is not a claim that every request is 76.2% cheaper or faster. Bounded modes made more model calls in this run.

## Additional evidence

The 20-case extension produced 60 completed trials. All required workers were present in the initial context for all 11 applicable cases in each mode. Correct dispatch identities were 13/14 for full roster, 14/14 for retrieval, and 13/14 for focus. Joint model-assisted content/behavior judgments were 19/20, 16/19 with one unresolved, and 19/20 respectively. This does **not** show consistent superiority of focus over the original.

Two extension trials rewrote a panel invitation directly in the visible reply, correctly retaining its time and duration, but did not dispatch a worker. The fixed routing gold marks the missing dispatch wrong; the semantic review marks the visible rewrite correct. Neither observation establishes that a stored/provider draft was updated. Keeping both metrics makes this distinction inspectable.

At 10,000 workers, focus included all targets in 9/9 applicable initial contexts; retrieval did so in 8/9. Both selected the correct dispatched identities in 9/10 applicable attempts. These 28 trials have automatic diagnostics only; content correctness remains unreviewed unless an explicitly attributed review is added. They test bounded routing over a larger catalog, not concurrent service to 100 users or distributed database throughput.

The completed Sonnet recovery covers eight selected cases once per mode, on the later runtime. Aggregating its four smoke and four validation cases gives:

| Sonnet recovery measurement | Full roster | Retrieval | Focus |
|---|---:|---:|---:|
| Correct dispatched identities | 5/5 | 4/5 | 4/5 |
| Follow-up meaning/instructions correct, reviewed | 4/7; 1 unresolved | 7/8 | 7/8 |
| Joint content/behavior | 4/6; 2 unscored | 4/6; 2 unscored | 5/6; 2 unscored |

One model-assisted reviewer inspected its condition-blind packets. Both bounded modes requested confirmation after the rent increase in this small subset, but also dispatched unrelated venue research in the panel-update case. Full roster included an unsupported “sent” claim and sent instructions without addressing revised rent. Several callback drafts expanded a terse source sentence into a landlord-style offer; intended speaker/body fidelity remains unresolved rather than assumed correct. These observations are useful failure examples, **not a broad cross-model confirmation** of superiority. The model and runtime both differ from the earlier Luna campaign.

## What the failures taught us

- **Search can miss the right worker.** English stemming helps word forms, not arbitrary paraphrases. Publication context and the previous real user turn recover some continuations, but can also distract from an explicit new topic. Keep candidate recall separate from final selection.
- **The right worker can receive the wrong request.** A scorecard trial selected the expected worker but instructed it not to change anything when the user requested markings. An identity-only score misses this failure.
- **Fresh state is not fresh approval.** Stale-rent cases exposed sending instructions based on an older price, and in some traces an invented reconfirmation without another user message. Version checking can force a reload; it does not implement an approval gate for a specific artifact/version.
- **Silence and unnecessary work are different failures.** Some no-action cases correctly did nothing but omitted the requested acknowledgment. Other callbacks dispatched extra research. Neither should be described as a completed harmful external action: these workers were mocked.

## Review limits

The main and corrected-smoke packets received two model-assisted reviews, followed by explicit adjudication. Extension and Sonnet-recovery packets each received one model-assisted review. **There are zero human semantic reviews.** Review packets hide mode/model/case identifiers, but wording may reveal a condition; this is metadata blinding, not guaranteed experimental blinding.

Original reviewer labels, private join maps, adjudication notes and joined records are preserved. Joins validate the source-result fingerprint and each record identity. Main repeat snapshots were matched back to all 126 original records before aggregation.

Unresolved examples remain `null`: “deal with Maya first” specifies a workstream but not send versus revise; older free-text callback fixtures blur email body and confirmation question; generic “use the latest venue” may not establish that changed state was preserved; “two tickets a day” does not settle total versus per-person intent. Frozen gold labels and automatic scores were not silently rewritten to improve results.

No unnecessary clarification was identified in the reviewed main/extension trials. That narrow finding does not prove the system never asks needless questions: the sample is authored and model-reviewed, and some failures were *missing* necessary clarification.

## Latency, cost and reproducibility

Live calls used three-second evaluation pacing to avoid provider rate limits. Reported scenario and model-invocation timings include that pacing, and worker execution is mocked. **Do not use these durations to claim a production speedup.** The earlier offline lookup timings measure only local retrieval; indexed search need not beat listing names in CPU time to save model input.

Every request reserves a conservative maximum against a shared persistent $50 ledger before HTTP. Reservations are not actual charges. Reports separately sum only provider-returned `usage.cost`, with missing-field counts; neither number is an invoice guarantee or the total cost of this Codex session.

Download [the evidence archive](live-routing-records.zip) and inspect [the summary JSON](live-routing.json). The archive contains raw results, fixtures, phase manifests, review packets/maps/labels and derived reports. `evidence-index.json` records original/public SHA-256 hashes. Only absolute ledger paths in manifests are made repository-relative; result and review records remain byte-identical. Credentials, the ledger database and machine logs are excluded.

To reproduce a derived report after extracting the archive, run from the repository root:

```powershell
python -m evals.routing.reporting PATH_TO_EXTRACTED/luna-validation-1000/reviewed.jsonl --output report.json
```

The output records separate candidate inclusion, dispatch identity, reviewed behavior, displayed content, joint structural failures, usage and unscored counts. See [the evaluation instructions](../../evals/routing/README.md) for fresh paid runs. Fresh outputs will vary; exact source records, not a promised future percentage, are the evidence for the numbers above.
