import { test } from 'node:test'
import assert from 'node:assert/strict'
import { catalogueChanged, completionStatus, elapsedStatus, hasProgressTrack, knownStages, nextFocus, PhaseEstimate, phaseStatus, progressPercent, remainingStatus, selectPrimary, shouldNotifyFailure, type Operation } from '../src/state/taskProgress.ts'

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

test('phase estimates require measured progress and reset on totals, waiting, stale data and pauses', () => {
  const task = operation('one', 'running', 10, 100), estimate = new PhaseEstimate()
  assert.equal(estimate.observe(task, 0), null)
  task.phase.done = 12
  assert.equal(estimate.observe(task, 10000), null)
  task.phase.done = 20
  assert.equal(estimate.observe(task, 20000), 160)
  assert.equal(remainingStatus(task, 160), '本阶段约还需 3 分钟')
  assert.equal(estimate.observe(task, 51000), null, 'stalled work must not retain a countdown')
  task.phase.total = 120
  assert.equal(estimate.observe(task, 52000), null)
  task.phase.done = 30
  assert.equal(estimate.observe(task, 72000), 180)
  assert.equal(estimate.observe(task, 74000, true), null)
  task.phase.total = null
  assert.equal(estimate.observe(task, 95000), null)
  assert.equal(progressPercent(task), null)
  assert.equal(remainingStatus(task, null), '清单仍在增加，剩余时间待定')
  task.result.step_progress = {'verifying:files': {done: 30, total: 120}}
  assert.equal(phaseStatus(task), '核验 · 30 / 已发现 120 个文件')
  task.phase.total = 120; task.phase.detail = '等待主站冷却'
  assert.equal(estimate.observe(task, 100000), null)
  assert.equal(remainingStatus(task, null), '等待主站冷却')
  task.phase.detail = ''; task.state = 'paused'
  assert.equal(estimate.observe(task, 130000), null)
  assert.equal(remainingStatus(task, null), '')
})

test('completion separates processed counts from success and stage summaries never sum incompatible units', () => {
  const task = operation('核对主页', 'finished', 1, 1)
  task.result = {outcome: 'failed', counts: {failed: 1}, step_progress: {
    'checking:artists': {done: 2, total: 3}, 'checking:pages': {done: 10, total: 20}}}
  assert.equal(completionStatus(task), '核对主页：未完成（失败 1）')
  assert.equal(knownStages(task), '画师 2/3 · 列表页 10/20')
  assert.equal(progressPercent(task), 100)
  task.result = {outcome: 'partial_failure', counts: {succeeded: 3, failed: 1, skipped: 2}}
  assert.equal(completionStatus(task), '核对主页：部分完成（成功 3，失败 1，跳过 2）')
})

test('elapsed duration treats legacy server timestamps as UTC and does not imply active processing time', () => {
  const task = {...operation('one', 'running'), created_at: '2026-10-11T04:00:00'}
  assert.equal(elapsedStatus(task, Date.parse('2026-10-11T05:05:00Z')), '历时 1 小时 5 分钟')
  task.created_at = 'invalid'
  assert.equal(elapsedStatus(task, Date.now()), '')
})
