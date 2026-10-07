# Observation transaction merge — frozen candidate

- Scope: `archive.py`, `reconcile.py`, `bookmarks.py`, `source_status.py`, and new `test_observation_transactions.py`; baseline `f0df243943ae54bba3842b171dc65435d0efe398`.
- Synthetic SQLite databases and remote inputs only; no outside network, real account, real library, token/config edits, schema changes, commit, build or EXE run.
- Actual `ArchiveHandler.execute(metadata_work)` and `ReconcileHandler.execute(reconcile_snapshot)` share one reservation transaction and one facts transaction, with explicit Session helpers. Remote calls stay outside both transactions; freshness is published only after successful commit, under the existing memory account context check.
- Unchanged immutable metadata projection retains the current source reservation instead of replacing it with an older revision's availability fields. Existing failure classification and source check timestamps remain.
- Round 01: original handlers fail all 4 two-commit assertions (actual 5). Changed observation: 9 UPDATE + 2 INSERT, 36 added WAL frames. Unchanged: 4 UPDATE, 14 frames, but its source reservation/attempt is lost.
- Round 03: stronger unchanged source-version/attempt assertions reproduce that loss (2 failed, 2 passed).
- Round 04: 32 new专项 cases passed. Round 05: 208 passed, 2 existing task-seam tests failed. Round 06 loads the 4 original baseline modules only in that runner's process, retaining extracted sources: both same task-seam failures reproduce; no tracked source is replaced. Parent owns their correction.
- Round 07: final 35专项 cases passed; see `result.json`, `junit.xml`, `stdout.txt`, and final 5-file `source-hashes.json`.

| Equivalent facts | Separate wrapper control | Merged actual handler | DML control / merged | Added WAL frames control / merged |
| --- | --- | --- | --- | --- |
| Metadata changed | 5 commits | 2 commits | 9 UPDATE + 2 INSERT / same | 36 / 30 |
| Metadata unchanged, source attempt retained | 5 commits | 2 commits | 5 UPDATE / same | 15 / 15 |

WAL page size is 4096 bytes; each added frame includes its 24-byte frame header. Measurements pin the original read snapshot to avoid WAL reset confusing the logical-volume count. These are successful transaction and logical WAL measurements, **not wall-time, fsync count or HDD performance claims**. Empty commits are counted as successful transactions too.

Covered: reservation/facts rollback, actual before-commit failure, account change before and while waiting for the writer, source/bookmark/intent versions, deletion suppression, pending bookmark write, delayed post-commit freshness including A→B→A, repeated unchanged reads without new work revisions, standalone wrapper compatibility, and ugoira metadata outside transactions.

Boundary: task admission/control, database storage/checkpoint/FULL configuration, source failure classification and HTTP contracts are unchanged by this child. Page discovery retains the standalone wrapper path; broader catalogue/task write batching and real HDD throughput remain unmeasured. The temporary evidence directories are retained, not automatically deleted.
