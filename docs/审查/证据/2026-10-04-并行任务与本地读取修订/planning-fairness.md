# Planning/checking fairness diagnosis and minimal repair

Scope: `codex/v0.2.0-build20261004.3`, HEAD `888315a21883ed268a11b0d457207996e8eca9c5`, Python project v012 runtime. The existing uncommitted account/probe changes were retained. No real library, account, token, configuration, schema, EXE, full suite or large seed was used.

## Reproduced defect

`TaskQueue._step("check")` selected `plan` and broke out whenever the operation needed more planning, before scanning ready checking items. The correct planning/checking mutual exclusion consequently became whole-manifest serialization.

Baseline: `run-20261008-003001-4fe230/result.json` and each case's `trace.json`; repeat minimal baseline: `run-20261008-003136-e118ca/result.json`. Real Runtime/TaskQueue/ReconcileHandler, one synthetic stopped artist, 1 or 3 one-page works, fake client and downloader, real isolated SQLite/files. Worker threads are not started in this deterministic driver; the two-worker parameter case alone does not prove concurrency.

| Case | Planning batches before checking | First homepage check | First work snapshot/prepare/download |
| --- | --- | --- | --- |
| 1 work | 4 | tick 5 | tick 6 |
| 3 works | 8 | tick 9 | tick 10 |

Homepage checking was already ready at tick 1, first work snapshot at tick 3; checking executed zero times while `planning_complete=False`. The minimal baseline reproduced twice and `--assert-fairness` exited 1. Local steps were given repeated opportunities, but prepare correctly depended on snapshot, so new repair downloads also waited for complete planning.

Diagnostic causal control: `run-20261008-003119-68ecc8/result.json`. In-memory ready-first override only, no production edits: homepage checking ran at tick 2, snapshot/prepare/download at tick 5, all before complete planning. This rules in candidate ordering; empty dependencies, no occupied resources and no planning cooldown rule out those alternatives for this synthetic counterexample. Ready-first was only a diagnostic and was not shipped because it could instead starve planning.

## Implemented minimal repair

After the parent authorized implementation, only `app/src/core/tasks.py` and new `app/tests/test_planning_fairness.py` were changed.

- A short scheduler-owned per-operation turn hint alternates a planning step and a ready checking item; if checking has no eligible item, planning immediately continues.
- Turn snapshots are rechecked at atomic claim alongside existing same-operation mutual exclusion and resource guards; scheduler lock is not held for DB/network/disk work.
- Planning cooldown now gates only planning, with the deadline rechecked at atomic claim. Previously ready checking can continue; checking cooldown leaves planning eligible.
- Persistent inventory cursor, dependency checks, pause/offline policy and claim budget remain in their existing paths; the transient hint is cleared on operation completion and reconstructed safely on queue restart.

Original replay after repair: `run-20261008-003613-2c2196/result.json`, all 3 cases exit 0 without a diagnostic override. First homepage check is tick 2 and first snapshot/prepare/download tick 5 while planning is incomplete. All 3-work cases still complete all 8 planning batches.

## Verification and limitations

- New专项: `tests/test_planning_fairness.py`, 7 passed in 32.68s (`pytest-01`): real Runtime/Reconcile early checks/downloads; 20 planning batches and 40 ready checks alternate; both cooldown directions; manual pause/offline; reconstructed queue keeps its durable cursor and executes no duplicate items; actual `queue.start()` with two check threads overlaps independent operations while each operation's plan/check peak is exactly 1.
- The two-thread fixture's first-entry event was made deterministic; that changed test was rerun alone: 1 passed in 5.17s (`pytest-03`).
- Existing focused regressions: 6 passed, 1 failed in 15.46s (`pytest-02`); successful tests cover later small operations, pause persistence, planner cursor resume, dependency/priority gate, same-asset serialization and held planning with streaming repair.
- The one failure, `test_cooldown_yields_worker_and_retry_budget_is_finite`, also fails on untouched HEAD TaskQueue loaded in memory by `check_baseline_cooldown.py`: 1 failed in 1.89s (`pytest-baseline-cooldown-01`). Its synthetic `api_rate_limit` has no status, so existing production logic treats it as an unsent wait and refunds retry attempts; the test instead expects exhaustion. This baseline contract mismatch was not altered by this repair and is left for the main conversation.
- `git diff --check` passed; only existing line-ending warnings were emitted.
- No full-suite, large-input performance, real account, EXE or product acceptance claim is made here. This change improves scheduling opportunity; transaction cost remains a separate investigation.

## Reproduction

From worktree `app/`:

```powershell
& 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Scripts\python.exe' 'D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261008-planning-fairness-01\probe.py' --assert-fairness
```

Every invocation creates a new evidence directory; retain all old directories. `result.json` captures source SHA-256 for the four involved production files; baseline and repaired runs can therefore be distinguished even on the same worktree HEAD.
