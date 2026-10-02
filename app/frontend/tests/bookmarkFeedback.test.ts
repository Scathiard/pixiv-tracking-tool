import {test} from 'node:test'
import assert from 'node:assert/strict'
import {createBookmarkFeedback} from '../src/state/bookmarkFeedback.ts'
import {showsBookmarkFact} from '../src/state/bookmarkRules.ts'
import type {BookmarkState} from '../src/types.ts'

function state(overrides: Partial<BookmarkState> = {}): BookmarkState {
  return Object.freeze({work_id: 1, operation_id: null, status: 'confirmed', desired: null,
    value: false, confirmed_at: '2026-10-03T00:00:00Z', error: null, version: 4,
    cached: true, verified: true, fact_account_id: 'account-a', current_account_id: 'account-a',
    account_epoch: 10, ...overrides})
}
function fixture() {
  let now = 0, timerId = 0
  const timers = new Map<number, {at: number; callback: () => void}>()
  const feedback = createBookmarkFeedback({now: () => now,
    setTimer(callback, delay) { const id = ++timerId; timers.set(id, {at: now + delay, callback}); return id },
    clearTimer(id) { timers.delete(id) },
  })
  return {feedback, advance(time: number) {
    now = time
    for (const [id, timer] of timers) if (timer.at <= now) { timers.delete(id); timer.callback() }
  }, timerCount: () => timers.size}
}

test('fast confirmation keeps click feedback for three seconds without changing facts', () => {
  const {feedback, advance} = fixture(), original = state()
  const token = feedback.start(1, original)!
  assert.equal(feedback.active(1), true)
  assert.equal(showsBookmarkFact(original), false)
  feedback.receipt(1, token, state({operation_id: 'new', version: 5, value: true}))
  advance(2999)
  assert.equal(feedback.active(1), true)
  assert.equal(original.value, false)
  assert.equal(original.verified, true)
  advance(3000)
  assert.equal(feedback.active(1), false)
})

test('slow readback keeps the solid heart after three seconds until its terminal result', () => {
  const {feedback, advance, timerCount} = fixture()
  const token = feedback.start(1, state())!
  const pending = state({operation_id: 'new', version: 5, status: 'pending'})
  feedback.receipt(1, token, pending)
  advance(7000)
  assert.equal(feedback.active(1), true)
  assert.equal(timerCount(), 0)
  feedback.observe(1, state({...pending, status: 'confirmed', value: true}))
  assert.equal(feedback.active(1), false)
})

test('cancel has the same acknowledgement and returns to the verified empty heart', () => {
  const {feedback, advance} = fixture(), original = state({value: true})
  const token = feedback.start(1, original)!
  const cancelled = state({operation_id: 'cancel', version: 5, value: false, desired: false})
  feedback.receipt(1, token, cancelled)
  advance(2999)
  assert.equal(feedback.active(1), true)
  assert.equal(showsBookmarkFact(cancelled), false)
  assert.equal(original.value, true)
  advance(3000)
  assert.equal(feedback.active(1), false)
})

for (const status of ['failed', 'unconfirmed'] as const) {
  test(`${status} stops visual waiting while preserving the last verified fact`, () => {
    const {feedback, advance} = fixture(), original = state({value: true})
    const token = feedback.start(1, original)!
    feedback.receipt(1, token, state({operation_id: 'new', version: 5, status: 'pending', value: true}))
    advance(4000)
    feedback.observe(1, state({operation_id: 'new', version: 5, status, value: true,
      error: '模拟失败，保留最后核实状态'}))
    assert.equal(feedback.active(1), false)
    assert.equal(showsBookmarkFact(original), true)
  })
}

test('earlier reads and responses for old operations or versions cannot end this click', () => {
  const {feedback, advance} = fixture()
  const token = feedback.start(1, state())!
  feedback.observe(1, state())
  advance(4000)
  assert.equal(feedback.active(1), true)
  feedback.receipt(1, token, state({operation_id: 'new', version: 5, status: 'pending'}))
  feedback.observe(1, state({operation_id: 'old', version: 5}))
  feedback.observe(1, state({operation_id: 'new', version: 4}))
  assert.equal(feedback.active(1), true)
  feedback.observe(1, state({operation_id: 'new', version: 5}))
  assert.equal(feedback.active(1), false)
})

test('shared work feedback survives component changes and never colors another work', () => {
  const {feedback, advance} = fixture()
  const token = feedback.start(1, state())!
  assert.equal(feedback.active(1), true)
  assert.equal(feedback.active(2), false)
  feedback.receipt(2, token, state({work_id: 2, operation_id: 'other'}))
  feedback.receipt(1, token, state({work_id: 2, operation_id: 'other'}))
  advance(4000)
  assert.equal(feedback.active(1), true)
  assert.equal(feedback.active(2), false)
  feedback.receipt(1, token, state({operation_id: 'new', version: 5}))
  assert.equal(feedback.active(1), false)
})

test('repeat clicks do not restart the minimum time or allocate another operation', () => {
  const {feedback, advance, timerCount} = fixture()
  const token = feedback.start(1, state())!
  feedback.receipt(1, token, state({operation_id: 'new', version: 5}))
  advance(1000)
  assert.equal(feedback.start(1, state()), undefined)
  assert.equal(timerCount(), 1)
  advance(3000)
  assert.equal(feedback.active(1), false)
  assert.notEqual(feedback.start(1, state()), token)
})

test('account changes clear feedback and stale timers or receipts cannot affect the new account', () => {
  const {feedback, advance, timerCount} = fixture()
  const oldToken = feedback.start(1, state())!
  feedback.clear()
  assert.equal(timerCount(), 0)
  assert.equal(feedback.active(1), false)
  advance(1000)
  const otherAccount = state({account_epoch: 11, current_account_id: 'account-b', fact_account_id: 'account-a', version: 1})
  const token = feedback.start(1, otherAccount)!
  feedback.receipt(1, oldToken, state({operation_id: 'old', version: 5}))
  feedback.receipt(1, token, state({operation_id: 'wrong-account', version: 5}))
  advance(3000)
  assert.equal(feedback.active(1), true)
  feedback.receipt(1, token, state({...otherAccount, operation_id: 'new-account', version: 1}))
  advance(4000)
  assert.equal(feedback.active(1), false)
  assert.equal(showsBookmarkFact(otherAccount), false)
})

test('a transport failure ends feedback after the minimum time; recovered pending reads keep waiting', () => {
  const {feedback, advance} = fixture()
  const token = feedback.start(1, state())!
  feedback.receipt(1, token, undefined)
  advance(3000)
  assert.equal(feedback.active(1), false)
  const retryToken = feedback.start(1, state())!
  feedback.receipt(1, retryToken, state({operation_id: 'recovered', status: 'pending', version: 5}))
  advance(7000)
  assert.equal(feedback.active(1), true)
  feedback.observe(1, state({operation_id: 'recovered', status: 'unconfirmed', version: 5}))
  assert.equal(feedback.active(1), false)
})
