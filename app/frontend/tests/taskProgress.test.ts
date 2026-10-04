import { test } from 'node:test'
import assert from 'node:assert/strict'
import { catalogueChanged, hasProgressTrack, nextFocus, phaseStatus, selectPrimary, shouldNotifyFailure, type Operation } from '../src/state/taskProgress.ts'

function operation(id: string, state: string, done = 0, total: number | null = null): Operation {
  return {id, label: id, state, source: 'manual', phase: {name: 'verifying', done, total, unit: 'files', detail: ''}, result: {}}
}

test('maintenance names its existing artist phase explicitly without relabeling new-work checks', () => {
  const task = operation('maintenance', 'running', 2, 83)
  task.kind = 'reconcile'
  task.phase.name = 'checking'; task.phase.unit = 'artists'
  assert.equal(phaseStatus(task), '核对画师 · 2 / 83 位画师')
  task.kind = 'check'
  assert.equal(phaseStatus(task), '检查 · 2 / 83 位画师')
})

test('queued manual submission cannot replace a running task with its waiting counts', () => {
  const first = operation('first', 'running', 4, 20)
  const second = operation('second', 'queued')
  assert.equal(selectPrimary([first, second], second.id)?.id, first.id)
  second.state = 'running'
  assert.equal(selectPrimary([first, second], second.id, first.id)?.id, second.id)
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

test('empty and unknown phases show honest counts without a pretend progress bar', () => {
  const task = operation('verify', 'planning', 0, 0)
  assert.equal(phaseStatus(task), '核验 · 0 / 0 个文件')
  assert.equal(hasProgressTrack(task), false)
  task.phase.total = null
  assert.equal(hasProgressTrack(task), false)
  assert.equal(hasProgressTrack(task, true), false)
  assert.equal(hasProgressTrack(task, false, true), false)
})

test('simultaneous lanes choose download first, keep the current running primary, then move to check and local', () => {
  const check = {...operation('check', 'running', 2, 11), lane: 'check' as const}
  const local = {...operation('local', 'running', 18, 29), lane: 'local' as const}
  const download = {...operation('download', 'running', 1, 2000), lane: 'download' as const}
  const queued = operation('manual', 'queued', 0, 7)
  assert.equal(selectPrimary([local, check, download, queued], queued.id)?.id, download.id)
  assert.equal(selectPrimary([download, local, check], null, check.id)?.id, check.id)
  download.state = 'finished'
  assert.equal(selectPrimary([local, check, download], null, download.id)?.id, check.id)
  check.state = 'finished'
  assert.equal(selectPrimary([local, check, download], null, check.id)?.id, local.id)
  assert.equal(hasProgressTrack(queued), false)
  check.state = 'running'
  assert.equal(selectPrimary([local, check, queued], queued.id, local.id, [check.id, queued.id])?.id, check.id)
  assert.equal(phaseStatus(local), '核验 · 18 / 29 个文件')
  local.phase = {name: 'thumbnails', done: 3, total: 9, unit: 'previews', detail: ''}
  assert.equal(phaseStatus(local), '缩略图 · 3 / 9 件预览')
})

test('catalogue revision establishes a baseline and coalesces duplicate or older snapshots', () => {
  assert.deepEqual(catalogueChanged(null, 42), {revision: 42, changed: false})
  assert.deepEqual(catalogueChanged(42, 47), {revision: 47, changed: true})
  assert.deepEqual(catalogueChanged(47, 47), {revision: 47, changed: false})
  assert.deepEqual(catalogueChanged(47, 46), {revision: 47, changed: false})
  assert.deepEqual(catalogueChanged(47), {revision: 47, changed: false})
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
  assert.equal(phaseStatus(task, false, true), '后台已中断，请重启程序')
  task.state = 'paused'
  assert.equal(phaseStatus(task, false, true), '后台已中断，请重启程序')
  assert.equal(hasProgressTrack(task, false, true), false)
})


test('manual maintenance pause stays visible and resumes without a fake progress bar', () => {
  const paused = {...operation('reconcile', 'paused', 7, 30), kind: 'reconcile', pause_requested: true,
    control: {can_pause: false, can_resume: true}}
  assert.equal(selectPrimary([paused], null)?.id, paused.id)
  assert.equal(phaseStatus(paused), '已暂停，继续后处理剩余部分')
  assert.equal(hasProgressTrack(paused), false)
  paused.state = 'pausing'
  assert.equal(phaseStatus(paused), '正在暂停，当前步骤安全结束后停下')
  const automatic = {...operation('automatic', 'queued'), source: 'automatic' as const}
  assert.equal(phaseStatus(automatic, true), '等待恢复自动检查')
})

test('offline work explains the preserved queue without pretending to run', () => {
  const task = operation('reconcile', 'queued', 12, 50)
  task.phase.name = 'waiting_connection'
  assert.equal(phaseStatus(task), '等待连接 Pixiv，任务进度已保留')
  assert.equal(hasProgressTrack(task), false)
  task.state = 'paused'
  assert.equal(phaseStatus(task), '已暂停，继续后处理剩余部分')
})
