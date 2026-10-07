# Claim progress transaction merge

The parent extended the same approved reliability work to merge only starting display progress into the existing atomic item claim. The prior fairness change is retained. Files changed by this agent: `app/src/core/tasks.py`, `app/tests/test_planning_fairness.py`, and new `app/tests/test_task_claim_progress.py`; the parent separately corrected the old cooldown test's synthetic sent-request input.

## Result

Actual valid two-frame ZIP through real ReconcileHandler:

| Scheduler | DML commits | Total commits | Ordering |
| --- | --- | --- | --- |
| Untouched HEAD888315a | 4 | 5 | claim, starting display, asset facts, settlement, empty finalize |
| Repaired | 3 | 4 | claim plus starting display, asset facts, settlement, empty finalize |

`check_baseline_zip_commits.py` loads untouched HEAD TaskQueue/TaskContext in memory and runs the new exact ZIP assertion; it failed as expected in 1.40s with an extra DML commit (`pytest-baseline-zip-d1adf6`). The repaired code passes the same assertion and checks the persisted verified asset/hash/size, successful item, real phase count and phase snapshot. No change to SQLite synchronous strength, schema, file publication or result/fact persistence was made. No speedup percentage is inferred from commit reduction.

## Behavior and rollback safety

- `_report_step_in_transaction` owns the existing phase initialization and display snapshot update; both the atomic claim and direct `TaskContext.report_step` reuse it.
- `_execute_item(progress_prepared=True)` checks control state immediately and skips only the already-committed starting display write.
- `_validated_phases` is published only after successful commit. Injected rollback before commit leaves items pending, attempts at zero, no cached phase count, no resource/check reservation and no false phase validation; a subsequent claim recounts correctly.
- Empty candidate/deferred lanes return before acquiring a writer. Check-lane admission first verifies eligible unadmitted work exists, while the existing admission transaction still rechecks its rules. Finalization remains attached to planning/item settlement and deferred-round release; submission and settlement still wake workers.
- Local-lane rollback does not restore a check-lane turn hint it never changed.

## Verification

- `tests/test_task_claim_progress.py` plus `tests/test_planning_fairness.py`: **12 passed in 33.19s**, `pytest-progress-01` (5 new progress tests including two rollback modes, and all 7 fairness tests).
- Existing phase/control-focused tests: **7 passed in 12.12s**, `pytest-progress-02`: restart recertification of old-writer phase counts (one baseline count query then incremental); finite retries for three actual requests; unsent API cooldown retains retry budget/resources; manual pause; persisted planning cursor; later small operation; unknown checking denominator and atomic phase update.
- After tightening rollback to restore turn hints only for check claims, both rollback modes reran: **2 passed in 2.70s**, `pytest-progress-03`.
- The new empty-lane test observes zero writer acquisitions for check/download/local idle calls, then submits work, confirms wake, executes and finalizes it, then again observes zero idle writer acquisitions.
- `git diff --check` passed for agent-owned files, with only the repository's LF/CRLF notice.

The parent corrected `test_cooldown_yields_worker_and_retry_budget_is_finite` by making its synthetic failure an actual status-429 request and marking the remote attempt; its original finite-retry intent now passes. The original unsent-cooldown policy was unchanged and its separate test also passes.

Final SHA-256 at freeze:

- `app/src/core/tasks.py`: `23CD0A96B6CC408312D95B3083AA90B93832298B7182957EE2A62E03142F2F29`
- `app/tests/test_task_claim_progress.py`: `7FB2A99E68BA853F8850462FB6853575329C7E8EB28EBB64A0AC7C514346E3C9`
- `app/tests/test_planning_fairness.py`: `B552E3C604BBC99968F3AE29F50A1BDBEF306742476FD5E7C95851F2A675FA46`

No full-suite, large pilot, EXE, real account or product acceptance claim is made. Those remain the parent task's validation layers. All isolated evidence is retained, with no deletion or automatic cleanup.
