import { test } from 'node:test'
import assert from 'node:assert/strict'
import { boundInlineTransform, inlineZoomAt, inlineZoomFrame, INLINE_ZOOM_DURATION } from '../src/state/inlineZoom.ts'

test('zoom keeps the image point under the pointer fixed', () => {
  const frame = {width: 400, height: 600}, before = {scale: 2, x: -100, y: -200}, point = {x: 150, y: 200}
  const after = inlineZoomAt(before, 3, point, frame)
  assert.equal((point.x - after.x) / after.scale, (point.x - before.x) / before.scale)
  assert.equal((point.y - after.y) / after.scale, (point.y - before.y) / before.scale)
})
test('panning and resizing never expose space outside the enlarged frame', () => {
  assert.deepEqual(boundInlineTransform({scale: 2, x: 80, y: -1800}, {width: 400, height: 600}), {scale: 2, x: 0, y: -600})
  assert.deepEqual(boundInlineTransform({scale: 9, x: -5000, y: -5000}, {width: 100, height: 200}), {scale: 5, x: -400, y: -800})
})
test('pinching to the minimum resets translation, including floating point tolerance', () => {
  assert.deepEqual(inlineZoomAt({scale: 2.25, x: -100, y: -300}, .8, {x: 10, y: 20}, {width: 400, height: 600}), {scale: 1, x: 0, y: 0})
  assert.deepEqual(boundInlineTransform({scale: 1.003, x: -3, y: -8}, {width: 400, height: 600}), {scale: 1, x: 0, y: 0})
})

test('double-tap motion preserves its focal point and can hand off its current pose to direct input', () => {
  const frame = {width: 400, height: 600}, from = {scale: 1, x: 0, y: 0}, point = {x: 100, y: 200}
  const to = inlineZoomAt(from, 2.25, point, frame)
  assert.deepEqual(inlineZoomFrame(from, to, 0), from)
  assert.deepEqual(inlineZoomFrame(from, to, INLINE_ZOOM_DURATION), to)
  const interrupted = inlineZoomFrame(from, to, INLINE_ZOOM_DURATION / 2)
  assert.ok(interrupted.scale > 1 && interrupted.scale < to.scale)
  assert.equal((point.x - interrupted.x) / interrupted.scale, point.x)
  assert.equal((point.y - interrupted.y) / interrupted.scale, point.y)
  const direct = inlineZoomAt(interrupted, 3, point, frame)
  assert.equal(direct.scale, 3)
  assert.deepEqual(inlineZoomFrame(to, from, INLINE_ZOOM_DURATION + 100), from)
})
