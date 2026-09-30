<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import { activeStates, hasProgressTrack, phaseStatus, selectPrimary, shouldNotifyFailure, type Operation } from './state/taskProgress'
import { catalogueUpdates } from './state/catalogue'
interface Snapshot {
  round: {id: string; state: string} | null
  operations: Operation[]; worker_error?: string; automatic_paused?: boolean; catalogue_revision?: number; active_operation_count?: number
}

const emit = defineEmits<{updated: []}>()
const snapshot = ref<Snapshot | null>(null)
const stale = ref(false)
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
  const known = others.value.map(op => `${op.label}（${op.state === 'queued' ? '等待' : op.state === 'interrupted' ? '中断' : op.state === 'ready' ? '待执行' : '处理中'}）`)
  if (othersCount.value > others.value.length) known.push(`${othersCount.value - others.value.length} 项待处理`)
  if (focusedId.value && !active.value.some(op => op.id === focusedId.value)) known.push('已提交，正在确认排队状态')
  return known.join(' · ')
})

let timer = 0, failureTimer = 0, stopped = false, polling = false, refreshAfterPoll = false
let previousRoundId = '', previousRoundState = ''
let previousOperations: Map<string, string> | null = null
const requestedIds = new Set<string>()
const controller = new AbortController()

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
      <div class="task-primary"><span class="status-dot" /> <strong>{{ primary.label }}</strong><span class="task-phase">{{ phaseText }}</span></div>
      <div v-if="measurable" class="task-track" role="progressbar" :aria-label="`${primary.label}：${phaseText}`" :aria-valuenow="primary.phase.done" aria-valuemin="0" :aria-valuemax="primary.phase.total ?? undefined"><span :style="{width: trackWidth}" /></div>
      <p v-if="(othersCount || focusedId && !active.some(op => op.id === focusedId)) && !stale" class="task-secondary" :title="otherText">{{ othersCount ? `另有 ${othersCount} 项：` : '' }}{{ otherText }}</p>
    </template>
    <p v-else-if="stale" class="task-primary">任务状态暂不可得，正在重连</p>
    <p v-else-if="focusedId" class="task-secondary">任务已提交，正在确认排队状态</p>
    <p v-if="failure" class="task-error" role="alert">{{ failure.label }}未完成；原因可在设置的处理日志中查看。</p>
  </aside>
</template>

<style scoped>
/* This informational layer must not cover the controls beneath it. */
.task-status { pointer-events: none; }
.task-secondary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
