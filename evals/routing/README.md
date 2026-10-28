# What this evaluation measures

The recorded comparison and its limitations are in
[the offline evidence report](../../docs/evidence/discovery-development.md).

Run from the repository root:

```powershell
.venv/Scripts/python.exe -m evals.routing.run --scales 100 1000 10000 --output evals/routing/results/local
```

The default run makes no model calls and starts no execution workers. It uses the
actual SQLite catalog with synthetic worker records and fifteen hand-authored
development cases. Populations are nested: the same targets remain while similar
workers are added, including many email tasks for Alice. No real account data is
read. Generated databases are temporary; raw measurement files go under the
ignored `results/` directory.

The comparisons separate two choices:

| Setting | Information available |
|---|---|
| `names_only` | Imported worker names |
| `enriched` | Names plus first/latest dispatch instructions |
| `current_state` | The above plus recent completed-result excerpts and observed draft IDs |
| `legacy` mode | Every worker name is included in the prompt |
| `retrieval` mode | A bounded lexical shortlist, including exact observed references when present |
| `focus` mode | The same shortlist also includes recent published work |

The original system prompt and tool definitions are pinned under `baseline/` to
commit `5b5f635935a64ab37884c025d70abb0ed731c094`. The live harness retains the
same interaction loop and substitutes that snapshot for the baseline. In offline
mode only the candidate lookup and rendered roster are measured.

The important fixtures distinguish mechanisms rather than reward generic keyword
matching. “Send it” needs publication evidence; a silent hotel completion must not
replace the published email. Borealis and Helios occur only in completed results.
The opaque draft ID occurs only in a successful structured tool result. Separate
Alice tasks test that a person is not a task identity. A paraphrased latency
request intentionally misses lexical search. New work, an ambiguous email, and a
question answerable from history have no required existing-worker target.

`initial_all_targets_present` asks whether every required worker appears in the
shortlist. It is undefined for cases with no required worker and those cases are
excluded from the recall denominator. The legacy roster naturally scores perfect
inclusion because it contains every worker; that says nothing about whether a
model chooses correctly. `initial_target_recall` also exposes partial retrieval
for the two-target case. Neither metric measures asking unnecessary questions.

Lookup timings cover catalog lookup and candidate rendering, not model latency or
end-to-end user latency. They are single local observations per fixture after
seeding, not concurrent service percentiles. The report records one-time catalog
build time, catalog file bytes, candidate counts, and actual rendered characters.
Characters are not tokenizer counts. Publication sources and successful tool
observations are deliberately pre-seeded; their extraction quality is not measured.

The unit/integration tests use scripted model responses to check real-loop
behavior, including refusing a card whose version changed before dispatch. Such
tests establish protocol behavior, not model routing accuracy. A refusal itself
does not count as a successfully completed routing task.

## Fresh challenge cases and live runs

`--suite challenge` adds 20 fresh cases: six smoke cases and fourteen validation
cases. They cover plural wording, prior-user references, different tasks for the
same person, new work, clarification, direct answers, multiple publications,
silent completions, and state changes between lookup and dispatch. The split
controls the workflow; neither group is an independent statistical holdout.
The original fifteen cases remain a development regression suite.

`--suite extension` adds twenty further authored requests, increasing the new live
scenario set to forty. These were written after smoke review and frozen before
their own calls; they are not an independent holdout. They use the same worker
population but broaden phrasing and task types. Five include real callbacks with
explicit email-body boundaries. Both original suites remain unchanged.

For callback cases the real Interaction Agent receives worker results, publishes
through its actual tools, and then handles the follow-up in the same conversation.
Publication evidence is not seeded as correct. Worker execution remains mocked;
there are no Gmail sends or real-account reads. This measures live model routing
on synthetic scenarios, not successful completion of real email tasks.

Before each run the runner writes the case contents, fixture hashes, code digest,
model, prices, limits, and planned trial count. Use a new output directory each
time. For example, after setting `OPENROUTER_API_KEY` in ignored `.env`:

```powershell
python -m evals.routing.run --live --suite challenge --split smoke --scales 100 --metadata current_state --modes legacy retrieval focus --model openai/gpt-6-luna --input-rate 0.2 --output-rate 0.75 --max-output-tokens 2048 --max-calls 120 --max-estimated-usd 2 --ledger evals/routing/results/live-budget.sqlite3 --total-budget-usd 50 --total-max-calls 5000 --output evals/routing/results/smoke
```

Rates above are conservative maximum advertised token tiers checked on 2026-10-28;
verify effective rates against the [provider model registry](https://openrouter.ai/api/v1/models)
before using them. All invocations in one campaign must use the **same ledger path
and limits**. Reservations occur atomically before requests, persist across CLI
invocations, and are not refunded after failed/unknown requests. They use conservative
input-size estimates and output caps; they are not provider billing measurements.
Reported token usage and cost fields, when present, remain separate evidence.

The runner stops making requests after a budget limit or operational error. It
writes `not_run` placeholders for the remaining trials, and preserves partial
scenarios as `incomplete`. Neither becomes an incorrect routing answer. A provider
failure is `operational_error`. Run a new phase after diagnosing an error; do not
delete the shared ledger to restart the budget.

`--repeats 3` repeats a case without treating three attempts as three independent
cases. Mode order rotates. Paired comparisons require matching, complete, scored
repeat sets in both modes. Missing or unreviewed cases are shown explicitly; there
is no majority-vote product score or statistical significance claim.

The reporter separates initial candidate inclusion, dispatch identity, source-ID
annotations, and reviewed response/publication content. Correct source IDs do not
prove that the model showed the right draft. Choosing the right worker does not
prove that its instructions were correct. ASK/ANSWER text also needs review.

To prepare a blinded review packet and join completed labels:

```powershell
python -m evals.routing.review export evals/routing/results/smoke/results.jsonl --output evals/routing/results/smoke/review.jsonl --mapping evals/routing/results/smoke/review-map.json
python -m evals.routing.review join evals/routing/results/smoke/results.jsonl --mapping evals/routing/results/smoke/review-map.json --labels evals/routing/results/smoke/labels.jsonl --output evals/routing/results/smoke/reviewed.jsonl
```

Review labels must state whether a person or model assisted the review. A model
review must not be presented as human validation. Real-account Gmail execution,
representative user traffic, concurrent multi-user load, and semantic paraphrase
coverage remain separate work.
