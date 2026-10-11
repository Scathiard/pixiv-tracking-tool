<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import { activeStates, completionStatus, elapsedStatus, hasProgressTrack, knownStages, PhaseEstimate, phaseStatus, progressPercent, remainingStatus, selectPrimary, type Operation } from './state/taskProgress'
import { catalogueUpdates } from './state/catalogue'
import Icon from './components/Icon.vue'
interface Snapshot {
  round: {id: string; state: string} | null
  operations: Operation[]; worker_error?: string; automatic_paused?: boolean; catalogue_revision?: number; active_operation_count?: number
}

const emit = defineEmits<{updated: []}>()
const snapshot = ref<Snapshot | null>(null)
const stale = ref(false)
const controlling = ref(false)
const controlError = ref('')
const focusedId = ref<string | null>(null)
const runningId = ref<string | null>(null)
const completion = ref<Operation | null>(null)
const sampledAt = ref(Date.now()), estimateSeconds = ref<number | null>(null)
const estimate = new PhaseEstimate()
const active = computed(() => snapshot.value?.operations.filter(op => activeStates.has(op.state)) || [])
const primary = computed(() => selectPrimary(active.value, focusedId.value, runningId.value, [...requestedIds]))
const others = computed(() => active.value.filter(op => op.id !== primary.value?.id))
const othersCount = computed(() => Math.max(others.value.length, (snapshot.value?.active_operation_count || 0) - (primary.value ? 1 : 0)))
const visible = computed(() => !!primary.value || !!focusedId.value || !!completion.value || stale.value)
const percent = computed(() => stale.value ? null : progressPercent(primary.value))
const remaining = computed(() => primary.value && !stale.value && !snapshot.value?.worker_error ? remainingStatus(primary.value, estimateSeconds.value) : '')
const compactRemaining = computed(() => remaining.value.replace('本阶段约还需 ', '约 ').replace('本阶段剩余时间估算中', '估算中').replace('清单仍在增加，剩余时间待定', '总量未定'))
const elapsed = computed(() => primary.value ? elapsedStatus(primary.value, sampledAt.value) : '')
const stages = computed(() => primary.value ? knownStages(primary.value) : '')
const completionText = computed(() => completion.value ? completionStatus(completion.value) : '')
const completionFailed = computed(() => ['failed', 'partial_failure'].includes(completion.value?.result.outcome || ''))
const measurable = computed(() => hasProgressTrack(primary.value, stale.value, !!snapshot.value?.worker_error))
const phaseText = computed(() => {
  const op = primary.value
  if (!op) return ''
  if (stale.value) return '状态暂不可得，正在重连'
  return phaseStatus(op, snapshot.value?.automatic_paused, !!snapshot.value?.worker_error)
})
const trackWidth = computed(() => {
  const phase = primary.value?.phase
  return phase?.total && phase.total > 0 ? `${Math.max(0, Math.min(100, phase.done / phase.total * 100))}%` : '0%'
})
const otherText = computed(() => {
  const known = others.value.map(op => `${op.label}（${op.state === 'paused' ? '已暂停' : op.state === 'pausing' ? '正在暂停' : op.state === 'queued' ? '等待' : op.state === 'interrupted' ? '中断' : op.state === 'ready' ? '待执行' : '处理中'}）`)
  if (othersCount.value > others.value.length) known.push(`${othersCount.value - others.value.length} 项待处理`)
  if (focusedId.value && !active.value.some(op => op.id === focusedId.value)) known.push('已提交，正在确认排队状态')
  return known.join(' · ')
})

let timer = 0, failureTimer = 0, stopped = false, polling = false, refreshAfterPoll = false
let previousRoundId = '', previousRoundState = ''
let previousOperations: Map<string, string> | null = null
const requestedIds = new Set<string>()
const controller = new AbortController()

async function controlTask(op: Operation) {
  if (controlling.value) return
  controlling.value = true
  controlError.value = ''
  try {
    await api(`/api/tasks/${encodeURIComponent(op.id)}/${op.pause_requested ? 'resume' : 'pause'}`, {method: 'POST'})
    clearTimeout(timer)
    if (polling) refreshAfterPoll = true
    else if (!stopped) await poll()
  } catch { controlError.value = '控制请求未确认，请重试' }
  finally { controlling.value = false }
}

function showTask(id: string) {
  controlError.value = ''
  focusedId.value = id
  requestedIds.add(id)
  completion.value = null
  clearTimeout(failureTimer)
  clearTimeout(timer)
  if (polling) refreshAfterPoll = true
  else if (!stopped) void poll()
}

function showResult(operation: Operation) {
  completion.value = operation
  clearTimeout(failureTimer)
  failureTimer = window.setTimeout(() => { completion.value = null }, 8000)
}

async function poll() {
  if (polling || stopped) return
  polling = true
  try {
    const data = await api<Snapshot>('/api/progress', {signal: controller.signal})
    if (stopped) return
    // A bounded progress snapshot may omit a requested task; absence is not completion.
    const tracking = [...new Set([...requestedIds, ...(runningId.value ? [runningId.value] : [])])]
      .filter(id => !data.operations.some(op => op.id === id)).slice(-4)
    const details = await Promise.allSettled(tracking.map(id => api<Operation>(`/api/tasks/${encodeURIComponent(id)}?limit=1`,
      {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)])})))
    if (stopped) return
    for (const result of details) if (result.status === 'fulfilled') data.operations.push(result.value)
    snapshot.value = data
    stale.value = false
    const catalogueUpdated = catalogueUpdates.observeProgress(data.catalogue_revision)
    const legacyFinished = data.catalogue_revision === undefined && data.round?.id === previousRoundId && previousRoundState !== 'finished' && data.round?.state === 'finished'
    if (catalogueUpdated || legacyFinished) emit('updated')
    previousRoundId = data.round?.id || ''
    previousRoundState = data.round?.state || ''
    for (const op of data.operations) {
      if (!activeStates.has(op.state) && (activeStates.has(previousOperations?.get(op.id) || '') || requestedIds.has(op.id))) {
        showResult(op)
        window.dispatchEvent(new CustomEvent('task-completed', {detail: {id: op.id, state: op.state}}))
      }
      if (!activeStates.has(op.state)) requestedIds.delete(op.id)
    }
    previousOperations = new Map(data.operations.map(op => [op.id, op.state]))
    runningId.value = selectPrimary(data.operations, focusedId.value, runningId.value, [...requestedIds])?.id || null
    if (focusedId.value && data.operations.some(op => op.id === focusedId.value && !activeStates.has(op.state))) focusedId.value = null
    sampledAt.value = Date.now()
    estimateSeconds.value = estimate.observe(primary.value, sampledAt.value, !!data.worker_error)
  } catch { if (!stopped) { stale.value = true; estimate.reset(); estimateSeconds.value = null } }
  polling = false
  if (stopped) return
  if (refreshAfterPoll) { refreshAfterPoll = false; void poll() }
  else timer = window.setTimeout(poll, document.hidden ? 10000 : active.value.length || focusedId.value ? 1500 : 7000)
}

function visibilityChanged() {
  clearTimeout(timer)
  if (polling) refreshAfterPoll = true
  else if (!stopped) timer = window.setTimeout(poll, document.hidden ? 10000 : 0)
}
onMounted(() => { void poll(); document.addEventListener('visibilitychange', visibilityChanged) })
onBeforeUnmount(() => { stopped = true; controller.abort(); clearTimeout(timer); clearTimeout(failureTimer); document.removeEventListener('visibilitychange', visibilityChanged) })
defineExpose({showTask})
</script>

<template>
  <aside v-if="visible" class="task-status" :class="{stale}" data-no-navigation aria-live="polite">
    <template v-if="primary">
      <div class="task-primary">
        <span class="status-dot" />
        <div class="task-copy">
          <div class="task-heading"><strong :title="primary.label">{{ primary.label }}</strong><span v-if="remaining" class="task-mobile-eta" :title="remaining">{{ compactRemaining }}</span><span v-if="othersCount" class="task-other-count" :title="otherText">另有 {{ othersCount }} 项</span></div>
          <div class="task-metrics"><span class="task-phase" :title="phaseText">{{ phaseText }}</span><b v-if="percent !== null" class="task-percent">{{ percent }}%</b><span v-if="controlError || completionText" class="task-inline-feedback" :class="{error: !!controlError || completionFailed}" :title="controlError || completionText" :role="controlError || completionFailed ? 'alert' : 'status'">{{ controlError || completionText }}</span></div>
        </div>
        <button v-if="!snapshot?.worker_error && (primary.control?.can_pause || primary.control?.can_resume)" class="task-control" :disabled="controlling || stale" :aria-label="controlling ? '正在提交' : primary.pause_requested ? '继续' : '暂停'" :title="primary.pause_requested ? '继续当前任务' : '暂停当前任务'" @click="controlTask(primary)"><Icon :name="controlling ? 'clock' : primary.pause_requested ? 'play' : 'pause'" /><span class="task-control-label">{{ controlling ? '正在提交' : primary.pause_requested ? '继续' : '暂停' }}</span></button>
      </div>
      <div v-if="measurable" class="task-track" role="progressbar" :aria-label="`${primary.label}：${phaseText}`" :aria-valuenow="primary.phase.done" aria-valuemin="0" :aria-valuemax="primary.phase.total ?? undefined"><span :style="{width: trackWidth}" /></div>
      <p v-if="elapsed || remaining" class="task-timing"><span :title="'从提交起计算，包括排队和暂停'">{{ elapsed }}</span><span class="task-eta">{{ remaining }}</span></p>
      <p v-if="stages && !stale" class="task-stages" :title="stages">已知阶段：{{ stages }}</p>
      <p v-if="(othersCount || focusedId && !active.some(op => op.id === focusedId)) && !stale" class="task-secondary" :title="otherText">{{ othersCount ? `另有 ${othersCount} 项：` : '' }}{{ otherText }}</p>
    </template>
    <p v-else-if="stale" class="task-primary">任务状态暂不可得，正在重连</p>
    <p v-else-if="focusedId" class="task-secondary">任务已提交，正在确认排队状态</p>
    <p v-if="controlError" class="task-error" role="alert">{{ controlError }}</p>
    <p v-if="completion" class="task-result" :class="{'task-error': completionFailed}" :title="completionText" :role="completionFailed ? 'alert' : 'status'">{{ completionText }}<span v-if="completionFailed" class="task-log-hint">，原因见设置的处理日志。</span></p>
  </aside>
</template>

<style scoped>
/* This informational layer must not cover the controls beneath it. */
.task-status { pointer-events: none; }
.task-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 4px; }
.task-heading, .task-metrics, .task-timing { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; }
.task-heading strong { flex: 1; }
.task-phase { margin-left: 0; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-percent { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--text); }
.task-timing, .task-stages, .task-result { margin: 5px 0 0; font-size: 11px; line-height: 1.5; color: var(--muted); }
.task-stages { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.task-timing { flex-wrap: wrap; gap: 3px 8px; }
.task-timing .task-eta { margin-left: auto; }
.task-mobile-eta, .task-inline-feedback { display: none; }
.task-other-count, .task-control .icon { display: none; }
.task-control { pointer-events: auto; border: 0; border-radius: 6px; background: var(--surface2); color: var(--text); padding: 4px 10px; cursor: pointer; flex-shrink: 0; }
.task-control:disabled { opacity: .55; }
.task-secondary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 760px) {
  /* Reuse the existing header, never add a second layer over the artwork. */
  :global(.app-shell:has(.task-status) .mobile-head .brand > span) { display: none; }
  .task-status { top: 0; left: 87px; right: 6px; width: auto; height: 50px; box-sizing: border-box; padding: 0 3px 3px; border: 0; border-radius: 0; box-shadow: none; background: transparent; }
  .task-primary { min-height: 44px; gap: 6px; }
  .task-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 1px; }
  .task-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .task-heading strong { flex: 1; font-size: 11px; line-height: 15px; }
  .task-other-count { display: none; }
  .task-mobile-eta { display: block; flex: 0 1 auto; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 10px; }
  .task-phase { margin-left: 0; line-height: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .task-control { width: 44px; height: 44px; min-height: 44px; padding: 0; background: transparent; }
  .task-control .icon { display: block; width: 18px; height: 18px; }
  .task-control-label, .task-secondary[title] { display: none; }
  .task-track { height: 2px; margin: 0; }
  .status-dot, .task-timing, .task-stages, .task-log-hint { display: none; }
  .task-percent { font-size: 11px; }
  .task-inline-feedback { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
  .task-metrics:has(.task-inline-feedback) .task-phase, .task-metrics:has(.task-inline-feedback) .task-percent { display: none; }
  .task-result, .task-error { margin-top: 0; max-height: 44px; overflow: hidden; font-size: 11px; line-height: 20px; }
  .task-primary ~ .task-result, .task-primary ~ .task-error { display: none; }
  .task-primary { min-height: 44px; }
}
</style>
