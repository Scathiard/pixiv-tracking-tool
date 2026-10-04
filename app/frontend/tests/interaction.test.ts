import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DirectionGesture, LatestIntent, ReadLifecycle } from '../src/interaction.ts'
import { setImmediate as nextTurn } from 'node:timers/promises'

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

test('read lifecycle commits the latest actual response and suppresses stale errors and finalizers', async () => {
  const lifecycle = new ReadLifecycle()
  let oldResolve!: (value: string) => void, freshResolve!: (value: string) => void
  const commits: string[] = [], failures: unknown[] = [], finishes: string[] = []
  const old = lifecycle.run('old', () => new Promise<string>(resolve => { oldResolve = resolve }), value => commits.push(value), error => failures.push(error), () => finishes.push('old'))
  await nextTurn()
  const fresh = lifecycle.run('new', () => new Promise<string>(resolve => { freshResolve = resolve }), value => commits.push(value), error => failures.push(error), () => finishes.push('new'))
  await nextTurn()
  freshResolve('new'); await fresh
  oldResolve('old'); await old
  assert.deepEqual(commits, ['new']); assert.deepEqual(failures, []); assert.deepEqual(finishes, ['new'])
})

test('repeated polls share one flight and dispose forbids response commits or new reads', async () => {
  const lifecycle = new ReadLifecycle()
  let resolve!: (value: string) => void, calls = 0, commits = 0, finishes = 0
  let signal: AbortSignal | undefined
  const read = (value: AbortSignal) => { calls++; signal = value; return new Promise<string>(done => { resolve = done }) }
  const one = lifecycle.run('poll', read, () => commits++, () => {}, () => finishes++)
  const two = lifecycle.run('poll', read, () => commits++, () => {}, () => finishes++)
  assert.equal(one, two)
  await nextTurn(); assert.equal(calls, 1)
  lifecycle.dispose(); assert.equal(signal?.aborted, true)
  resolve('late'); await one
  await lifecycle.run('poll', read, () => commits++, () => {})
  assert.equal(commits, 0); assert.equal(finishes, 0); assert.equal(calls, 1)
})

test('closing and reopening a read permits fresh results and keeps deliberate abort silent', async () => {
  const lifecycle = new ReadLifecycle()
  const commits: string[] = [], errors: unknown[] = []
  const old = lifecycle.run('logs', signal => new Promise<string>((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), {once: true})
  }), value => commits.push(value), error => errors.push(error))
  await nextTurn(); lifecycle.cancel()
  const fresh = lifecycle.run('logs', async () => 'new logs', value => commits.push(value), error => errors.push(error))
  await Promise.all([old, fresh])
  assert.deepEqual(commits, ['new logs']); assert.deepEqual(errors, [])
})
