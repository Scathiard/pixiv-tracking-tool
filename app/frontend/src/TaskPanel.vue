<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import { activeStates, hasProgressTrack, nextFocus, phaseStatus, selectPrimary, shouldNotifyFailure, type Operation } from './state/taskProgress'
interface Snapshot {
  round: {id: string; state: string} | null
  operations: Operation[]; worker_error?: string; automatic_paused?: boolean
}

const emit = defineEmits<{updated: []}>()
const snapshot = ref<Snapshot | null>(null)
const stale = ref(false)
const focusedId = ref<string | null>(null)
const failure = ref<Operation | null>(null)
const active = computed(() => snapshot.value?.operations.filter(op => activeStates.has(op.state)) || [])
const primary = computed(() => selectPrimary(active.value, focusedId.value))
const others = computed(() => active.value.filter(op => op.id !== primary.value?.id))
const visible = computed(() => !!primary.value || !!failure.value || stale.value)
const measurable = computed(() => hasProgressTrack(primary.value, stale.value, !!snapshot.value?.worker_error))
const phaseText = computed(() => {
  const op = primary.value
  if (!op) return ''
  if (stale.value) return '状态暂不可得，正在重连'
  return phaseStatus(op, snapshot.value?.automatic_paused, !!snapshot.value?.worker_error)
})
const trackWidth = computed(() => {
  const phase = primary.value?.phase
  return phase?.total && phase.total > 0 ? `${Math.max(0, Math.min(100, phase.done / phase.total * 100))}%` : '35%'
})
const otherText = computed(() => others.value.map(op => `${op.label}（${op.state === 'queued' ? '等待' : op.state === 'interrupted' ? '中断' : op.state === 'ready' ? '待执行' : '处理中'}）`).join(' · '))

let timer = 0, failureTimer = 0, stopped = false, polling = false, refreshAfterPoll = false, focusVersion = 0
let previousRoundId = '', previousRoundState = ''
let previousOperations: Map<string, string> | null = null
const requestedIds = new Set<string>()
const controller = new AbortController()

function showTask(id: string) {
  focusedId.value = id
  requestedIds.add(id)
  focusVersion += 1
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
  const startedFocusVersion = focusVersion
  try {
    const data = await api<Snapshot>('/api/progress', {signal: controller.signal})
    snapshot.value = data
    stale.value = false
    if (data.round?.id === previousRoundId && previousRoundState !== 'finished' && data.round?.state === 'finished') emit('updated')
    previousRoundId = data.round?.id || ''
    previousRoundState = data.round?.state || ''
    for (const op of data.operations) {
      if (shouldNotifyFailure(op, previousOperations?.get(op.id), requestedIds.has(op.id))) showFailure(op)
      if (requestedIds.has(op.id)) requestedIds.delete(op.id)
    }
    previousOperations = new Map(data.operations.map(op => [op.id, op.state]))
    if (startedFocusVersion === focusVersion) focusedId.value = nextFocus(data.operations, focusedId.value, !!focusedId.value && requestedIds.has(focusedId.value))
  } catch { if (!stopped) stale.value = true }
  polling = false
  if (stopped) return
  if (refreshAfterPoll) { refreshAfterPoll = false; void poll() }
  else timer = window.setTimeout(poll, document.hidden ? 10000 : active.value.length || focusedId.value ? 1500 : 7000)
}

onMounted(() => { void poll() })
onBeforeUnmount(() => { stopped = true; controller.abort(); clearTimeout(timer); clearTimeout(failureTimer) })
defineExpose({showTask})
</script>

<template>
  <aside v-if="visible" class="task-status" :class="{stale}" data-no-navigation aria-live="polite">
    <template v-if="primary">
      <div class="task-primary"><span class="status-dot" /> <strong>{{ primary.label }}</strong><span class="task-phase">{{ phaseText }}</span></div>
      <div v-if="measurable" class="task-track" :class="{indeterminate: primary.phase.total === null}" role="progressbar" :aria-label="`${primary.label}：${phaseText}`" :aria-valuenow="primary.phase.total ? primary.phase.done : undefined" aria-valuemin="0" :aria-valuemax="primary.phase.total ?? undefined"><span :style="{width: trackWidth}" /></div>
      <p v-if="others.length && !stale" class="task-secondary">另有 {{ others.length }} 项：{{ otherText }}</p>
    </template>
    <p v-else-if="stale" class="task-primary">任务状态暂不可得，正在重连</p>
    <p v-if="failure" class="task-error" role="alert">{{ failure.label }}未完成；原因可在设置的处理日志中查看。</p>
  </aside>
</template>
