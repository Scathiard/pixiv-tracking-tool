import { test } from 'node:test'
import assert from 'node:assert/strict'
import { hasProgressTrack, nextFocus, phaseStatus, selectPrimary, shouldNotifyFailure, type Operation } from '../src/state/taskProgress.ts'

function operation(id: string, state: string, done = 0, total: number | null = null): Operation {
  return {id, label: id, state, source: 'manual', phase: {name: 'verifying', done, total, unit: 'files', detail: ''}, result: {}}
}

test('the most recently requested active task stays in focus until it finishes', () => {
  const first = operation('first', 'running', 4, 20)
  const second = operation('second', 'queued')
  assert.equal(selectPrimary([first, second], second.id)?.id, second.id)
  second.state = 'finished'
  assert.equal(selectPrimary([first, second], second.id)?.id, first.id)
  first.state = 'finished'
  assert.equal(selectPrimary([first, second], first.id), null)
})

test('an automatically selected task keeps focus when snapshot ordering changes', () => {
  const first = operation('first', 'running')
  const second = operation('second', 'running')
  const focus = nextFocus([first], null)
  assert.equal(focus, first.id)
  assert.equal(nextFocus([second, first], focus), first.id)
  first.state = 'finished'
  assert.equal(nextFocus([second, first], focus), second.id)
  assert.equal(nextFocus([], 'requested', true), 'requested')
})

test('a just submitted task that finishes before the first poll still reports failure', () => {
  const task = operation('verify', 'finished')
  task.result.outcome = 'partial_failure'
  assert.equal(shouldNotifyFailure(task, undefined, true), true)
  assert.equal(shouldNotifyFailure(task, undefined, false), false)
  assert.equal(shouldNotifyFailure(task, 'finished', false), false)
  assert.equal(shouldNotifyFailure(task, 'running', false), true)
})

test('a known empty phase has no progress bar, while an unknown total may indicate activity', () => {
  const task = operation('verify', 'planning', 0, 0)
  assert.equal(phaseStatus(task), '核验 · 0 / 0 个文件')
  assert.equal(hasProgressTrack(task), false)
  task.phase.total = null
  assert.equal(hasProgressTrack(task), true)
  assert.equal(hasProgressTrack(task, true), false)
  assert.equal(hasProgressTrack(task, false, true), false)
})

test('progress distinguishes real phase counts, unknown totals, queues and interruption', () => {
  const task = operation('verify', 'planning', 12, 50)
  assert.equal(phaseStatus(task), '核验 · 12 / 50 个文件')
  task.phase.total = null
  assert.equal(phaseStatus(task), '核验 · 已处理 12 个文件')
  task.state = 'queued'
  assert.equal(phaseStatus(task), '已排队，等待处理')
  task.source = 'automatic'
  assert.equal(phaseStatus(task, true), '等待恢复自动检查')
  task.state = 'interrupted'
  assert.equal(phaseStatus(task), '已中断，等待恢复')
})
