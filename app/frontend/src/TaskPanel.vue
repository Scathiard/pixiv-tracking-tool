<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
interface Operation {id: string; label: string; state: string; needs_attention: boolean; source?: 'manual' | 'automatic' | 'maintenance'
  phase: {name: string; done: number; total: number | null; unit: string; detail: string}
  result: {outcome?: string; inventory_complete?: boolean; phase_snapshots?: Record<string, {phase: string; unit: string; done: number; total: number | null}>}}
interface Snapshot {
  round: {id: string; state: string; total: number | null; settled: number; outcome: string | null; counts: Record<string, number>} | null
  next_round_count: number; needs_attention_count: number; operations: Operation[]; worker_error?: string; automatic_paused?: boolean
  recovery_issues: {id: string; error: string}[]
}
interface TaskDetail extends Operation {items: {key: string; state: string; result: {message?: string}}[]; has_more_items: boolean}
const emit = defineEmits<{updated: []}>()
const snapshot = ref<Snapshot | null>(null), stale = ref(false), open = ref(false), selected = ref<TaskDetail | null>(null)
const selectedId = ref<string | null>(null), error = ref(''), history = ref<Operation[] | null>(null), cursor = ref<string | null>(null), hasMore = ref(false), itemOffset = ref(0)
const outcome: Record<string, string> = {succeeded: '处理完成', partial_failure: '部分失败', failed: '失败', empty: '无需处理'}
const states: Record<string, string> = {queued: '已排队', planning: '清点中', ready: '已就绪', running: '正在执行', finished: '已结束', interrupted: '已中断，等待恢复'}
const phases: Record<string, string> = {waiting: '等待', checking: '检查', inventory: '清点', metadata: '画师信息', captions: '作品信息', bookmarks: '收藏状态', downloading: '下载', converting: '生成预览', verifying: '核验', thumbnails: '缩略图', deleting: '删除'}
const units: Record<string, string> = {artists: '位画师', works: '件作品', pages: '页', assets: '项原件', previews: '件预览', files: '个文件', items: '项'}
const round = computed(() => snapshot.value?.round)
const activeRound = computed(() => round.value?.state !== 'finished' ? round.value : null)
const queued = computed(() => snapshot.value?.operations.filter(op => op.state === 'queued') || [])
const active = computed(() => !!activeRound.value || !!snapshot.value?.operations.some(op => ['queued', 'planning', 'ready', 'running', 'interrupted'].includes(op.state)))
const percent = computed(() => !stale.value && activeRound.value?.total && ['ready', 'running'].includes(activeRound.value.state) ? Math.floor(activeRound.value.settled / activeRound.value.total * 100) : null)
const label = computed(() => stale.value ? '暂时无法获取状态' : snapshot.value?.worker_error ? '任务已中断，请重启继续' :
  activeRound.value ? activeRound.value.state === 'interrupted' ? '已中断，等待恢复' : activeRound.value.state === 'planning' ? '正在清点' : '正在处理' :
  queued.value.length ? snapshot.value?.automatic_paused && queued.value.every(op => op.source === 'automatic') ? '等待恢复自动检查' : '已排队' : '暂无运行任务')
let timer = 0, stopped = false, previousId = '', previousState = '', detailGeneration = 0
const controller = new AbortController()
async function showTask(id: string, offset = 0) {
  open.value = true; selectedId.value = id
  const generation = ++detailGeneration
  try {
    const result = await api<TaskDetail>(`/api/tasks/${id}?problems_only=true&offset=${offset}`, {signal: controller.signal})
    if (generation === detailGeneration) { selected.value = result; itemOffset.value = offset; error.value = '' }
  } catch (failure) { if (generation === detailGeneration) error.value = failure instanceof Error ? failure.message : '暂时无法读取任务' }
}
async function poll() {
  try {
    const data = await api<Snapshot>('/api/progress', {signal: controller.signal})
    snapshot.value = data; stale.value = false
    if (data.round?.id === previousId && previousState !== 'finished' && data.round?.state === 'finished') emit('updated')
    previousId = data.round?.id || ''; previousState = data.round?.state || ''
    const focused = data.operations.find(op => op.id === selectedId.value)
    if (open.value && focused && focused.state !== selected.value?.state) void showTask(focused.id)
  } catch { if (!stopped) stale.value = true }
  if (!stopped) timer = window.setTimeout(poll, document.hidden ? 5000 : 1500)
}
async function older(reset = false) {
  try {
    const page = await api<{items: Operation[]; has_more: boolean}>('/api/tasks?limit=20' + (!reset && cursor.value ? '&before=' + cursor.value : ''))
    history.value = reset ? page.items : [...(history.value || []), ...page.items]; hasMore.value = page.has_more; cursor.value = page.items.at(-1)?.id || null
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '历史记录暂时无法读取' }
}
async function acknowledge() {
  if (!selected.value) return
  try { await api(`/api/tasks/${selected.value.id}/acknowledge`, {method: 'POST'}); await showTask(selected.value.id) }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '标记未能保存' }
}
async function retry() {
  if (!selected.value) return
  try { const result = await api<{operation_id: string}>(`/api/tasks/${selected.value.id}/retry`, {method: 'POST'}); await showTask(result.operation_id) }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '任务未能提交' }
}
onMounted(() => { void poll() })
onBeforeUnmount(() => { stopped = true; controller.abort(); clearTimeout(timer) })
function openHistory() { open.value = true; void older(true) }
defineExpose({showTask, openHistory})
</script>
<template>
  <div v-if="active || open" class="task-status" :class="{stale}" data-no-navigation @keydown.esc.stop="open = false">
    <button class="task-summary" @click="open = !open" :aria-expanded="open"><span class="status-dot" />{{ label }}<b v-if="percent !== null">{{ percent }}%</b><span v-if="snapshot?.next_round_count"> · 下一轮 {{ snapshot.next_round_count }}</span><span v-if="snapshot?.needs_attention_count" class="attention">{{ snapshot.needs_attention_count }} 项待查看</span><span class="muted">{{ open ? '收起' : '任务记录' }}</span></button>
    <div v-if="activeRound" class="task-track" :class="{indeterminate: percent === null}" role="progressbar" aria-label="本轮处理进度" :aria-valuenow="percent ?? undefined" aria-valuemin="0" aria-valuemax="100"><span :style="{width: percent === null ? '35%' : `${percent}%`}" /></div>
    <section v-if="open" class="task-panel" aria-label="任务记录"><header><h2>任务记录</h2><button @click="open = false">关闭</button></header>
      <p v-if="stale" class="error">暂时无法获取状态，正在重连；不会刷新阅读页面。</p>
      <p v-else-if="activeRound?.total !== null && activeRound?.total">本轮 {{ activeRound.settled }} / {{ activeRound.total }} 项已有处理结论</p><p v-else-if="activeRound?.state === 'planning'">正在清点，清单确定后再显示总进度。</p>
      <p v-if="activeRound" class="result-counts">成功 {{ activeRound.counts.succeeded || 0 }} · 失败 {{ activeRound.counts.failed || 0 }} · 跳过 {{ activeRound.counts.skipped || 0 }} · 未执行 {{ activeRound.counts.not_executed || 0 }}</p>
      <p v-if="snapshot?.next_round_count">下一轮等待 {{ snapshot.next_round_count }} 个任务，本轮分母保持不变。</p>
      <p v-if="snapshot?.recovery_issues.length" class="error">有文件尚未恢复，相关输入已保留，请检查任务原因。</p>
      <div class="task-tabs"><button @click="history = null">当前与最近</button><button @click="older(true)">历史任务</button><button v-if="history && hasMore" @click="older()">更早的记录</button></div>
      <ul class="task-list"><li v-for="operation in history || snapshot?.operations || []" :key="operation.id"><button @click="showTask(operation.id)">{{ operation.label }} <span>{{ outcome[operation.result.outcome || ''] || states[operation.state] }}</span></button><small v-if="operation.state !== 'queued'">{{ phases[operation.phase.name] || operation.phase.name }} {{ operation.phase.done }} / {{ operation.phase.total ?? '总量未知' }} {{ units[operation.phase.unit] || operation.phase.unit }} · {{ operation.phase.detail }}</small></li></ul>
      <div v-if="selected" class="task-selected"><h3>{{ selected.label }} · {{ outcome[selected.result.outcome || ''] || states[selected.state] }}</h3>
        <p v-for="(phase, key) in selected.result.phase_snapshots" :key="key">{{ phases[phase.phase] || phase.phase }} {{ phase.done }} / {{ phase.total ?? '总量未知' }} {{ units[phase.unit] || phase.unit }}</p>
        <p v-if="selected.result.inventory_complete === false" class="error">部分范围未能完成清点，查看下方原因后重试。</p>
        <p v-for="item in selected.items" :key="item.key" class="error">{{ item.result.message || '此项未完成' }} <small>{{ item.key }}</small></p>
        <div class="button-row"><button v-if="itemOffset" @click="showTask(selected.id, Math.max(0, itemOffset - 100))">前 100 项</button><button v-if="selected.has_more_items" @click="showTask(selected.id, itemOffset + 100)">后 100 项</button><button v-if="selected.state === 'finished'" @click="retry">重新执行该范围</button><button v-if="selected.needs_attention" @click="acknowledge">标为已查看（保留记录）</button></div>
      </div><p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>
  </div>
</template>
