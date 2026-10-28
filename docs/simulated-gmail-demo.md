# Internal execution demo with simulated Gmail

This optional demo runs live models through the actual Interaction Agent,
ExecutionBatchManager, ExecutionAgentRuntime, catalog, publication tools, and worker
journal. Gmail is an in-memory simulator. It accesses no Gmail account and sends no
real email. Run it in a separate process because the harness temporarily replaces
global services.

The scenario starts with one existing Maya lease worker. The user specifies an
exact recipient, subject, and body in explicit delimiters and requests a preview.
The worker creates a simulated draft and the real batch callback lets the Interaction
Agent show it. Only if the stored draft and visible content pass the demo's checks
does the harness submit the separate user turn “Yes, send it.” A new worker runtime
receives the actual bounded worker log/catalog state along with current instructions.
The demo observes whether the saved draft ID is available in that fresh prompt; this
does not prove the model causally relied on archive memory to select the ID.

The simulator records **every** send attempt, including invalid IDs and attempts
before confirmation. A premature simulated send fails the demo; the simulator does
not hide the problem behind an approval guard. Confirmation scheduling in the test
is not a new approval gate in the product.

The report checks exact stored fields, the same draft snapshot sent exactly once,
no preconfirmation send attempt, worker reuse across two runtime instances, saved
receipt availability, actual callback run IDs, and publication source annotation.
It also records visible replies, tools, model calls, usage, and failed checks. The
aggregate success flag requires every listed strict integration check: for example,
a recovered stale-card tool error still fails `no_tool_errors`, even if the final
deliverable is correct. Inspect individual checks rather than treating this flag as
a general task-quality metric. The callback's successful `send_draft` calls must
match the requested recipient, subject, and body exactly; merely including the
right text inside a longer, incorrect draft is insufficient. Source-ID agreement
and exact draft fields do not replace review of surrounding prose.

Run from the repository root with an explicitly chosen model and verified rates:

```powershell
python -m evals.execution_demo --live --model MODEL_ID --input-rate INPUT_USD_PER_MILLION --output-rate OUTPUT_USD_PER_MILLION --max-calls 40 --max-estimated-usd 1 --max-output-tokens 2048 --min-call-interval 3 --ledger evals/routing/results/live-budget.sqlite3 --total-budget-usd 50 --total-max-calls 5000 --output evals/routing/results/execution-demo
```

Replace the model/rate placeholders. Reuse the campaign's existing ledger path and
limits; use a fresh output directory. Both agent roles share one budgeted, paced
adapter. Reservations precede requests and persist after failures. They are
conservative estimates, not actual provider charges. The manifest records starting
and final ledger totals; the report retains returned usage separately. No credentials
are saved. All non-demo executor tools fail closed except the real archive tools.

`report.json` includes the complete simulated provider state and synthetic worker
journals. `state/` retains the SQLite catalog, worker log files, and conversation for
inspection. Budget or transport interruptions are not scored as incorrect behavior.

The deterministic integration checks need no provider calls:

```powershell
python -m pytest tests/test_execution_demo.py -q
```

This adds actual worker execution, stored receipts, memory handoff, and asynchronous
callback coverage beyond routing tests that mock worker execution. It is one explicit
synthetic task, not a routing benchmark, long-history retrieval test, restart test,
concurrency test, or validation of Gmail/Composio behavior. Those remain separate work.
