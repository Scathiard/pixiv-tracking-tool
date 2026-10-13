export interface Operation {
  id: string; label: string; state: string; source?: 'manual' | 'automatic' | 'maintenance'
  kind?: string; pause_requested?: boolean; control?: {can_pause: boolean; can_resume: boolean}
  lane?: 'check' | 'download' | 'local' | null
  parameters?: {parent_operation_id?: string; kind?: string}
  phase: {name: string; done: number; total: number | null; unit: string; detail: string}
  created_at?: string
  finished_at?: string | null
  result: {outcome?: string; planning_complete?: boolean; counts?: Record<string, number>;
    incremental_check?: {mode: 'incremental-v1'; artists_done: number; artists_total: number; new_works: number; scan_complete: boolean};
    step_progress?: Record<string, {done: number; total: number}>}
}

export const activeStates = new Set(['queued', 'planning', 'ready', 'running', 'interrupted', 'paused', 'pausing'])
const phases: Record<string, string> = {waiting: '等待', checking: '检查', inventory: '清点', metadata: '画师信息', captions: '作品信息', bookmarks: '收藏状态', downloading: '下载', converting: '生成预览', verifying: '核验', reconciling: '整理缺项', repairing: '补齐', thumbnails: '缩略图', deleting: '删除'}
const units: Record<string, string> = {artists: '位画师', works: '件作品', pages: '页', assets: '项原件', previews: '件预览', files: '个文件', items: '项'}

function incrementalCheck(op: Operation) {
  const check = op.result.incremental_check
  return op.kind === 'check' && op.source === 'automatic' && check?.mode === 'incremental-v1' ? check : null
}

/** Discovery steps/pages are implementation units, not newly found works. */
export function progressPhase(op: Operation): Operation['phase'] {
  const check = incrementalCheck(op)
  return check ? {...op.phase, name: 'checking', unit: 'artists', done: check.artists_done, total: check.artists_total} : op.phase
}

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
    && progressPhase(op).total !== null && progressPhase(op).total! > 0
}

/** The first revision is only a baseline; duplicate and older snapshots cannot refresh a view. */
export function catalogueChanged(previous: number | null, incoming?: number): {revision: number | null; changed: boolean} {
  if (!Number.isSafeInteger(incoming) || incoming! < 0) return {revision: previous, changed: false}
  if (previous === null) return {revision: incoming!, changed: false}
  return {revision: Math.max(previous, incoming!), changed: incoming! > previous}
}

export function phaseStatus(op: Operation, automaticPaused = false, workerError = false): string {
  if (workerError) return '后台已中断，请重启程序'
  if (op.state === 'paused') return '已暂停，继续后处理剩余部分'
  if (op.state === 'pausing') return '正在暂停，当前步骤安全结束后停下'
  if (op.state === 'interrupted') return '已中断，等待恢复'
  if (op.phase.name === 'waiting_connection') return '等待连接 Pixiv，任务进度已保留'
  if (op.state === 'queued') return automaticPaused && op.source === 'automatic' ? '等待恢复自动检查' : '已排队，等待处理'
  const check = incrementalCheck(op)
  if (check) return `画师 ${check.artists_done}/${check.artists_total} · 新增 ${check.new_works} 件`
  if (op.state === 'ready') return '已清点，等待执行'
  const checkingArtists = ['reconcile', 'compare'].includes(op.kind || '') && op.phase.name === 'checking' && op.phase.unit === 'artists'
  const phase = checkingArtists ? '核对画师' : phases[op.phase.name] || op.phase.name || '处理'
  const unit = units[op.phase.unit] || op.phase.unit || '项'
  const done = Number.isFinite(op.phase.done) ? op.phase.done : 0
  const known = op.result.step_progress?.[`${op.phase.name}:${op.phase.unit}`]?.total
  if (op.phase.total === null && known !== undefined && known >= done && known > 0) return `${phase} · ${done} / 当前清单 ${known} ${unit}`
  return op.phase.total === null ? `${phase} · 已处理 ${done} ${unit}` : `${phase} · ${done} / ${op.phase.total} ${unit}`
}

/** Server timestamps are UTC, including the older offset-free ISO values. */
export function elapsedStatus(op: Operation, now: number): string {
  if (!op.created_at) return ''
  const value = /(?:Z|[+-]\d\d:\d\d)$/.test(op.created_at) ? op.created_at : op.created_at + 'Z'
  const seconds = Math.max(0, (now - Date.parse(value)) / 1000)
  if (!Number.isFinite(seconds)) return ''
  if (seconds < 60) return '历时不足 1 分钟'
  if (seconds < 3600) return `历时 ${Math.floor(seconds / 60)} 分钟`
  return `历时 ${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分钟`
}

export function progressPercent(op: Operation | null): number | null {
  const phase = op && progressPhase(op)
  if (!phase || phase.total === null || phase.total <= 0) return null
  return Math.floor(Math.max(0, Math.min(100, phase.done / phase.total * 100)))
}

export function completionStatus(op: Operation): string {
  const counts = op.result.counts || {}
  const result = {succeeded: '已完成', partial_failure: '部分完成', failed: '未完成', empty: '无需处理'}[op.result.outcome || ''] || '处理结束'
  const check = incrementalCheck(op)
  if (check) return `${op.label}：${result}（检查 ${check.artists_done}/${check.artists_total} 位画师，新增 ${check.new_works} 件作品）`
  const parts = [['succeeded', '成功'], ['failed', '失败'], ['skipped', '跳过'], ['not_executed', '未执行']]
    .filter(([key]) => (counts[key!] || 0) > 0).map(([key, label]) => `${label} ${counts[key!]}`)
  return `${op.label}：${result}${parts.length ? `（${parts.join('，')}）` : ''}`
}

export function knownStages(op: Operation): string {
  if (incrementalCheck(op)) return ''
  return Object.entries(op.result.step_progress || {}).filter(([key]) => key !== `${op.phase.name}:${op.phase.unit}`)
    .slice(0, 3).map(([key, progress]) => {
      const [phase, unit] = key.split(':')
      const label = phase === 'checking' && unit === 'pages' ? '列表页' : unit === 'artists' ? '画师' : phases[phase!] || phase
      return `${label} ${progress.done}/${progress.total}`
    }).join(' · ')
}

export function waitingDetail(op: Operation): string {
  return /等待|冷却|限流|重试|稍后/.test(op.phase.detail) ? op.phase.detail : ''
}

/** Estimates only the current fixed phase. Discovery, waiting and changed totals reset it. */
export class PhaseEstimate {
  private key = ''
  private samples: {at: number; done: number}[] = []
  private lastAdvance = 0
  reset() { this.key = ''; this.samples = []; this.lastAdvance = 0 }
  observe(op: Operation | null, now: number, stale = false): number | null {
    if (!op || incrementalCheck(op) || stale || op.state !== 'running' || op.phase.total === null || op.phase.total <= 0
        || op.phase.name === 'waiting_connection' || waitingDetail(op)) { this.reset(); return null }
    const key = `${op.id}:${op.phase.name}:${op.phase.unit}:${op.phase.total}`
    const last = this.samples.at(-1)
    if (key !== this.key || (last && (now <= last.at || op.phase.done < last.done))) {
      this.reset(); this.key = key
    }
    const previous = this.samples.at(-1)
    if (!previous || op.phase.done > previous.done) this.lastAdvance = now
    this.samples.push({at: now, done: op.phase.done})
    this.samples = this.samples.filter(sample => now - sample.at <= 90000).slice(-64)
    const first = this.samples[0]!
    const elapsed = (now - first.at) / 1000, completed = op.phase.done - first.done
    if (now - this.lastAdvance >= 30000 || elapsed < 20 || completed < 3 || op.phase.done >= op.phase.total) return null
    return Math.ceil((op.phase.total - op.phase.done) * elapsed / completed)
  }
}

export function remainingStatus(op: Operation, seconds: number | null): string {
  if (waitingDetail(op)) return waitingDetail(op)
  const check = incrementalCheck(op)
  if (check) return op.state === 'ready' && check.scan_complete ? '检查完成，等待保存结案' : ''
  if (op.state !== 'running' && op.state !== 'planning') return ''
  if (op.phase.total === null) return '清单仍在增加，剩余时间待定'
  if (op.phase.total === 0) return '正在清点处理范围'
  if (op.phase.done >= op.phase.total) return '本阶段已处理完，等待收尾'
  if (seconds === null) return '本阶段剩余时间估算中'
  const time = seconds < 60 ? `${Math.max(10, Math.ceil(seconds / 10) * 10)} 秒`
    : seconds < 3600 ? `${Math.ceil(seconds / 60)} 分钟` : `${Math.ceil(seconds / 360) / 10} 小时`
  return `本阶段约还需 ${time}`
}
