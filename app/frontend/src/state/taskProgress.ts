export interface Operation {
  id: string; label: string; state: string; source?: 'manual' | 'automatic' | 'maintenance'
  kind?: string; pause_requested?: boolean; control?: {can_pause: boolean; can_resume: boolean}
  lane?: 'check' | 'download' | 'local' | null
  parameters?: {parent_operation_id?: string; kind?: string}
  phase: {name: string; done: number; total: number | null; unit: string; detail: string}
  result: {outcome?: string}
}

export const activeStates = new Set(['queued', 'planning', 'ready', 'running', 'interrupted', 'paused', 'pausing'])
const phases: Record<string, string> = {waiting: '等待', checking: '检查', inventory: '清点', metadata: '画师信息', captions: '作品信息', bookmarks: '收藏状态', downloading: '下载', converting: '生成预览', verifying: '核验', reconciling: '整理缺项', repairing: '补齐', thumbnails: '缩略图', deleting: '删除'}
const units: Record<string, string> = {artists: '位画师', works: '件作品', pages: '页', assets: '项原件', previews: '件预览', files: '个文件', items: '项'}

export function selectPrimary(operations: Operation[], focusedId: string | null, runningId: string | null = null, requestedIds: string[] = []): Operation | null {
  const active = operations.filter(op => activeStates.has(op.state))
  const running = active.filter(op => op.state === 'running' || op.state === 'planning')
  const requested = [...requestedIds].reverse().map(id => running.find(op => op.id === id)).find(Boolean)
  return requested || running.find(op => op.id === focusedId)
    || running.find(op => op.id === runningId)
    || running.find(op => op.lane === 'download')
    || running.find(op => op.lane === 'check')
    || running.find(op => op.lane === 'local') || running[0]
    || active.find(op => op.id === focusedId)
    || active.find(op => op.state === 'ready') || active[0] || null
}

export function nextFocus(operations: Operation[], focusedId: string | null, keepMissing = false): string | null {
  if (focusedId && operations.some(op => op.id === focusedId && activeStates.has(op.state))) return focusedId
  if (focusedId && keepMissing && !operations.some(op => op.id === focusedId)) return focusedId
  return selectPrimary(operations, null)?.id || null
}

export function shouldNotifyFailure(op: Operation, previousState: string | undefined, explicitlyRequested: boolean): boolean {
  return op.state === 'finished' && ['failed', 'partial_failure'].includes(op.result.outcome || '')
    && (explicitlyRequested || previousState !== undefined && previousState !== 'finished')
}

export function hasProgressTrack(op: Operation | null, stale = false, workerError = false): boolean {
  return !!op && !stale && !workerError && (op.state === 'running' || op.state === 'planning')
    && op.phase.total !== null && op.phase.total > 0
}

/** The first revision is only a baseline; duplicate and older snapshots cannot refresh a view. */
export function catalogueChanged(previous: number | null, incoming?: number): {revision: number | null; changed: boolean} {
  if (!Number.isSafeInteger(incoming) || incoming! < 0) return {revision: previous, changed: false}
  if (previous === null) return {revision: incoming!, changed: false}
  return {revision: Math.max(previous, incoming!), changed: incoming! > previous}
}

export function phaseStatus(op: Operation, automaticPaused = false, workerError = false): string {
  if (op.state === 'paused') return '已暂停，继续后处理剩余部分'
  if (op.state === 'pausing') return '正在暂停，当前步骤安全结束后停下'
  if (workerError || op.state === 'interrupted') return '已中断，等待恢复'
  if (op.phase.name === 'waiting_connection') return '等待连接 Pixiv，任务进度已保留'
  if (op.state === 'queued') return automaticPaused && op.source === 'automatic' ? '等待恢复自动检查' : '已排队，等待处理'
  if (op.state === 'ready') return '已清点，等待执行'
  const checkingArtists = ['reconcile', 'compare'].includes(op.kind || '') && op.phase.name === 'checking' && op.phase.unit === 'artists'
  const phase = checkingArtists ? '核对画师' : phases[op.phase.name] || op.phase.name || '处理'
  const unit = units[op.phase.unit] || op.phase.unit || '项'
  const done = Number.isFinite(op.phase.done) ? op.phase.done : 0
  return op.phase.total === null ? `${phase} · 已处理 ${done} ${unit}` : `${phase} · ${done} / ${op.phase.total} ${unit}`
}
