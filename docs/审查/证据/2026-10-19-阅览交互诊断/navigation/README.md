# Navigation diagnosis - 2026-10-19

Scope: current `main / 30f8613`, public `0.2.0`, build `20261017.2`. Diagnosis and synthetic replay only. No production code, configuration, real library/account, or running service changed.

This directory is registered for temporary navigation diagnosis scripts and synthetic results. The parent session will copy selected scripts/results into its formal evidence directory before removing task-owned temporary items. Synthetic databases are test inputs only, never an application data path.

## Executed feedback loops

1. `node work/tmp/reading-ux-20261019/navigation/sfc-replay.cjs`
   - Executes the actual `App.vue` script with actual Vue refs/computed, the actual extracted `LatestIntent`, and actual extracted `prepareImage`; APIs, image delivery/decode, router, mounting, bookmarks, and non-exercised dependencies are isolated substitutes.
   - Initial next/previous succeeds. One cursor generation change produces expiry; explicit identity recovery succeeds; a second change expires again.
   - A held API request clears the previous error and keeps `pending=true` with the old work visible. Another arrow cancels the older intent and remains pending.
   - With API responses immediate and only `Image.decode` held, the same silent pending state occurs. Releasing decode immediately lets navigation commit.
   - `pendingReferencedInTemplate=false` means excluding the existing ArtistsView-only `navigation-pending` property: the library/detail/flow UI has no representation of this App-level pending state.
   - Exit 1 is the intended red verdict: it asserts the reported missing feedback, not a harness crash. Results: `sfc-replay-result.json`.
   - This is a source logic replay, not a browser render, physical mobile, or EXE test. Synthetic `decode` delay proves a possible real path, not that the field session hung in decode.

2. `.devtools/v012-env/Scripts/python.exe -u work/tmp/reading-ux-20261019/navigation/library-replay.py`
   - Real `ArchiveDatabase` and `Library`, 3 synthetic works, default unfiltered newest scope, no worker/real network.
   - Normal next reads IDs 3,2 at generation 2.
   - Inserting a remote bookmark for unrelated work 1, value false, expires that existing cursor.
   - Identity recovery of current work 3 reads IDs 3,2 at generation 3.
   - Changing unrelated work 1 bookmark false->true expires it again.
   - Changing only artist `last_checked_at` keeps the recovered cursor valid (generation 4).
   - Exit 0; results: `library-replay-result.json`.
   - Synthetic database directory: `library-615aa183dc2e406cb3ce89d7303f129b/`.

## Unsuccessful attempt retained separately

`py -3.12 .../backend-replay.py` failed immediately: No installed Python found. Running with the existing venv entered full Runtime construction and did not finish promptly; it was stopped with Ctrl+C. It produced no API replay result and is not counted as verification. Its own synthetic directory is `synthetic-b3620fbb96d14cafb5a2d9665ed70a69/` (no real data).

## Findings and limits

- Confirmed by actual backend: a bookmark change outside the current work and irrelevant to the default ordering invalidates all generation-bound cursors. Trigger scope is global (`projection_maintenance.py:138-154`, `_decode_cursor` `library.py:474`). The route maps this to HTTP 409 (`archive_api.py:134-139`).
- Confirmed by source replay: detail switching waits for API and target image preparation, clears the old error immediately, and has no visible App pending feedback (`App.vue:209,227-233,681-684`; `api.ts:187-214`). New arrow input cancels the earlier intent (`interaction.ts:39`).
- Not reproduced: a standalone left/right arithmetic failure with unchanged scope and prompt data/image delivery. The normal isolated path works both directions.
- Production logs supplied by parent show real 409s, successful detail APIs, and later navigation cancellations. These corroborate expiry and abandoned intents. They do not, without media lifecycle events, identify image decode as the exact field wait point or prove the route cancelled itself.
- Existing browser test `cursor-gallery-browser.py:108-118` covers one title change plus explicit recovery while writes stop. It does not cover repeated unrelated bookmark publication or a held image decode during switching.

## Proposed repair boundary (not implemented)

- Keep the current picture stable, but expose explicit switching/repositioning/image-preparation feedback and a usable cancellation/retry action. Do not erase recovery context before a new attempt succeeds.
- On expired navigation, re-anchor by current work identity and resolve the intended adjacent work using fresh valid scope/cursors; avoid requiring users to discover that ordinary arrows reuse a rejected token.
- Narrow invalidation to changes relevant to the active query/order, or introduce validated stable-key continuation. Retain actual deletion/hidden/filter/order safety checks; simply accepting mismatched generations is not a safe repair.
- Keep latest-intent cancellation, but make repeated input semantics and pending ownership visible; a successfully returned older HTTP request is not itself an application failure.

## Field media correlation supplied by parent

`../media-events.jsonl` adds the missing media lifecycle evidence for the same browser session. Media 55 began at browser `at_ms=80336.5` and was cancelled at `83091.2`; media 56 began at `83934.6` and errored at `98283.1` (14,348 ms), server trace `fd6f2991d5fd`; media 87 began at `102582.2` and was cancelled at `108108.1` (5,525.9 ms). Each began after the associated detail API completed. The media 56 HTTP request returned 200 with 2,195,840 bytes in 5,281 ms, yet no load/decode completion preceded the browser error. These are evidence of waiting for unfinished/failed target media after successful APIs, not evidence of a particular decode defect, a damaged source, or corrupt library data.

For ordinary one-step detail navigation, existing `ordinaryNeighbors.previous/next` already provide target work ID, revision ID and cursor. Resolving that ID directly avoids the additional `/api/library/cursor` query at `App.vue:219`, while retaining fresh neighbor validation and expiry recovery. This is a bounded navigation improvement, not by itself the full generation fix. Target media preparation should keep the old picture visible, show its own progress/cancel/retry state, and let a usable thumbnail or image load readiness commit safely without blocking indefinitely on an unobservable full decode stage. Background repair can continue separately, with latest-intent ownership retained.
