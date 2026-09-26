import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DirectionGesture, LatestIntent } from '../src/interaction.ts'

test('vertical and diagonal starts cannot turn into work navigation later', () => {
  for (const [x, y] of [[4, 30], [22, 23]]) {
    const gesture = new DirectionGesture()
    gesture.down(1, 0, 0, false); gesture.move(1, x, y); gesture.move(1, -160, 40)
    assert.equal(gesture.up(1), 0)
  }
})
test('a single horizontal gesture changes one work, never a short tap', () => {
  const gesture = new DirectionGesture()
  gesture.down(1, 100, 0, false); gesture.move(1, 20, 5)
  assert.equal(gesture.up(1), 1)
  assert.equal(gesture.up(1), 0)
  gesture.down(1, 0, 0, false); gesture.move(1, 20, 1)
  assert.equal(gesture.up(1), 0)
})
test('multitouch, controls and cancelled gestures cannot navigate', () => {
  const gesture = new DirectionGesture()
  gesture.down(1, 100, 0, false); gesture.down(2, 110, 0, false); gesture.up(2)
  gesture.move(1, 0, 0); assert.equal(gesture.up(1), 0)
  gesture.down(1, 100, 0, true); gesture.move(1, 0, 0); assert.equal(gesture.up(1), 0)
  gesture.down(1, 100, 0, false); gesture.move(1, 0, 0); gesture.cancel(1); assert.equal(gesture.up(1), 0)
})
test('old navigation results cannot replace a newer intent or a closed view', () => {
  const sequence = new LatestIntent(), first = sequence.begin(), second = sequence.begin()
  assert.equal(first.current(), false); assert.equal(first.signal.aborted, true)
  assert.equal(second.current(), true); sequence.cancel(); assert.equal(second.current(), false)
})
