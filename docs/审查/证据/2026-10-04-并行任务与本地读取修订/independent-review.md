# Independent backend review

Scope: `core/bookmarks.py`, `core/library.py`, `core/activity_log.py`, `core/database.py`, `core/read_control.py` in worktree `v0.2.0-build20261004.3`.

- Resolved defect: a completed bookmark Future awaiting its completion callback occupied `_scheduled_ids` capacity; a newly accepted write could fail scheduling and be claimed by recovery as readback-only. Original synthetic probe failed with writes `['1']` instead of `['1','2']`; parent changed queue capacity to unfinished Futures, and the same four independent probes passed.
- Resolved contract defect: pending detail pages set `truncated=True` even when all task summaries were included; parent separated history truncation from `details_complete` / `next_cursor`.
- Regression evidence: `regression.log` records 43 passed across bookmark lock boundaries, existing bookmark behavior, and local read cancellation / actual localhost HTTP disconnect tests; `final-gated.log` records 2 passed after stabilizing callback timing fixtures with remote write gates.
- New regressions in `app/tests/test_bookmark_lock_boundaries.py`: accepted writes survive delayed completion callbacks; LocalReadError after a successful remote write causes readback-only retry; callback shutdown retains ownership; detail pagination is independent from older task history; an already-running recovery scan cannot claim a freshly committed intent before its submitting owner schedules it.
- No further blocker found in the reviewed changes. Evidence uses temporary synthetic SQLite databases and fake remote responses. Real account / real gallery acceptance and physical disk stall behavior remain outside these probes.
- Two pre-existing dependency deprecation warnings came from websockets APIs used by Uvicorn; no checks were weakened.
