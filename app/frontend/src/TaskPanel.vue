<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import { activeStates, hasProgressTrack, phaseStatus, selectPrimary, shouldNotifyFailure, type Operation } from './state/taskProgress'
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
const failure = ref<Operation | null>(null)
const active = computed(() => snapshot.value?.operations.filter(op => activeStates.has(op.state)) || [])
const primary = computed(() => selectPrimary(active.value, focusedId.value, runningId.value, [...requestedIds]))
const others = computed(() => active.value.filter(op => op.id !== primary.value?.id))
const othersCount = computed(() => Math.max(others.value.length, (snapshot.value?.active_operation_count || 0) - (primary.value ? 1 : 0)))
const visible = computed(() => !!primary.value || !!focusedId.value || !!failure.value || stale.value)
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
  focusedId.value = id
  requestedIds.add(id)
  failure.value = null
  clearTimeout(failureTimer)
  clearTimeout(timer)
  if (polling) refreshAfterPoll = true
  else if (!stopped) void poll()
}

function showFailure(operation: Operation) {
  failure.value = operation
  clearTimeout(failureTimer)
  failureTimer = window.setTimeout(() => { failure.value = null }, 8000)
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
      if (shouldNotifyFailure(op, previousOperations?.get(op.id), requestedIds.has(op.id))) showFailure(op)
      if (!activeStates.has(op.state) && (activeStates.has(previousOperations?.get(op.id) || '') || requestedIds.has(op.id))) {
        window.dispatchEvent(new CustomEvent('task-completed', {detail: {id: op.id, state: op.state}}))
      }
      if (!activeStates.has(op.state)) requestedIds.delete(op.id)
    }
    previousOperations = new Map(data.operations.map(op => [op.id, op.state]))
    runningId.value = selectPrimary(data.operations, focusedId.value, runningId.value, [...requestedIds])?.id || null
    if (focusedId.value && data.operations.some(op => op.id === focusedId.value && !activeStates.has(op.state))) focusedId.value = null
  } catch { if (!stopped) stale.value = true }
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
        <div class="task-copy"><div class="task-heading"><strong>{{ primary.label }}</strong><span v-if="othersCount" class="task-other-count" :title="otherText">另有 {{ othersCount }} 项</span></div><span class="task-phase">{{ phaseText }}</span></div>
        <button v-if="primary.control?.can_pause || primary.control?.can_resume" class="task-control" :disabled="controlling || stale" :aria-label="controlling ? '正在提交' : primary.pause_requested ? '继续' : '暂停'" :title="primary.pause_requested ? '继续当前任务' : '暂停当前任务'" @click="controlTask(primary)"><Icon :name="controlling ? 'clock' : primary.pause_requested ? 'play' : 'pause'" /><span class="task-control-label">{{ controlling ? '正在提交' : primary.pause_requested ? '继续' : '暂停' }}</span></button>
      </div>
      <div v-if="measurable" class="task-track" role="progressbar" :aria-label="`${primary.label}：${phaseText}`" :aria-valuenow="primary.phase.done" aria-valuemin="0" :aria-valuemax="primary.phase.total ?? undefined"><span :style="{width: trackWidth}" /></div>
      <p v-if="(othersCount || focusedId && !active.some(op => op.id === focusedId)) && !stale" class="task-secondary" :title="otherText">{{ othersCount ? `另有 ${othersCount} 项：` : '' }}{{ otherText }}</p>
    </template>
    <p v-else-if="stale" class="task-primary">任务状态暂不可得，正在重连</p>
    <p v-else-if="focusedId" class="task-secondary">任务已提交，正在确认排队状态</p>
    <p v-if="controlError" class="task-error" role="alert">{{ controlError }}</p>
    <p v-if="failure" class="task-error" role="alert">{{ failure.label }}未完成；原因可在设置的处理日志中查看。</p>
  </aside>
</template>

<style scoped>
/* This informational layer must not cover the controls beneath it. */
.task-status { pointer-events: none; }
.task-copy, .task-heading { display: contents; }
.task-other-count, .task-control .icon { display: none; }
.task-control { pointer-events: auto; border: 0; border-radius: 6px; background: var(--surface2); color: var(--text); padding: 4px 10px; cursor: pointer; flex-shrink: 0; }
.task-control:disabled { opacity: .55; }
.task-secondary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 760px) {
  .task-status { top: 54px; right: 8px; width: calc(100vw - 16px); padding: 0 8px 3px; border-radius: 5px; box-shadow: 0 2px 8px var(--shadow); }
  .task-primary { min-height: 44px; gap: 6px; }
  .task-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 1px; }
  .task-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .task-heading strong { flex: 1; font-size: 12px; line-height: 15px; }
  .task-other-count { display: inline; flex: none; color: var(--muted); font-size: 10px; }
  .task-phase { margin-left: 0; line-height: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .task-control { width: 44px; height: 44px; min-height: 44px; padding: 0; background: transparent; }
  .task-control .icon { display: block; width: 18px; height: 18px; }
  .task-control-label, .task-secondary[title] { display: none; }
  .task-track { height: 2px; margin: 0; }
  .task-error { margin-top: 3px; }
}
</style>
