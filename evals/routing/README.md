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

Live model evaluation is optional and requires `--live`, explicit model prices,
and a spending ceiling. It has not been run for this change. That leaves actual
worker-selection quality, recovery success, unnecessary clarification, model
tokens/cost, and user-task completion unmeasured. This small development suite is
not a held-out benchmark, a multi-tenant load test, or evidence that paraphrases
are solved.
