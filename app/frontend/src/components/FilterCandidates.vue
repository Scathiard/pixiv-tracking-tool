<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { invertSelection, selectionCount, toggleSelection, type FilterSelection } from '../state/filterSelection'
const props = defineProps<{
  label: string; candidates: {value: string; label: string; detail?: string}[]
  selection: FilterSelection; loading: boolean; error: string
}>()
const emit = defineEmits<{change: [value: FilterSelection]; retry: []}>()
const search = ref(''), viewport = ref<HTMLElement | null>(null)
const catalogue = computed(() => props.candidates.map(item => item.value))
const filtered = computed(() => {
  const term = search.value.trim().toLocaleLowerCase()
  return props.candidates.filter(item => !term || `${item.label} ${item.value}`.toLocaleLowerCase().includes(term))
})
const selectedValues = computed(() => new Set(props.selection.values))
const count = computed(() => selectionCount(props.selection, catalogue.value))
function isSelected(value: string) { return props.selection.all || (props.selection.excluded ? !selectedValues.value.has(value) : selectedValues.value.has(value)) }
const selectedLabels = computed(() => {
  const labels = new Map(props.candidates.map(item => [item.value, item.label]))
  return props.selection.values.map(value => labels.get(value) || value)
})
const virtual = useVirtualizer(computed(() => ({count: filtered.value.length,
  getScrollElement: () => viewport.value, estimateSize: () => 46, overscan: 3})))
const rows = computed(() => virtual.value.getVirtualItems())
watch(search, () => virtual.value.scrollToOffset(0))
function toggle(value: string) { emit('change', toggleSelection(props.selection, value, catalogue.value)) }
</script>
<template>
  <input v-model="search" type="search" :placeholder="`搜索${label}`" :aria-label="`搜索筛选${label}`">
  <p v-if="selection.all" class="candidate-summary">当前不限，点击一项开始筛选。</p>
  <div class="filter-selection-actions">
    <button @click="emit('change', {all: true, values: []})">全选</button>
    <button :disabled="loading || !!error" @click="emit('change', invertSelection(selection, catalogue))">反选</button>
    <button @click="emit('change', {all: false, values: []})">清空选择</button>
  </div>
  <p class="candidate-summary" aria-live="polite">{{ selection.all ? `全部${label}` : count ? `已选 ${count} 项` : `未选择${label}，应用后无作品` }}<span v-if="!loading && !error"> · 共 {{ candidates.length }} 项</span></p>
  <p v-if="!selection.all && selectedLabels.length" class="selected-labels">{{ selection.excluded ? '未选：' : '已选：' }}{{ selectedLabels.slice(0, 8).join('、') }}{{ selectedLabels.length > 8 ? '…' : '' }}</p>
  <p v-if="loading" class="candidate-status" role="status">正在载入全部{{ label }}…</p>
  <div v-else-if="error" class="candidate-status error" role="alert">{{ error }} <button @click="emit('retry')">重试</button></div>
  <p v-else-if="!candidates.length" class="candidate-status">本地图库暂无{{ label }}</p>
  <p v-else-if="!filtered.length" class="candidate-status">没有匹配的{{ label }}，请换个搜索词</p>
  <div v-else ref="viewport" class="candidate-viewport" :aria-label="`${label}候选列表`" :data-candidate-total="candidates.length">
    <div class="candidate-window" :style="{height: `${virtual.getTotalSize()}px`}">
      <button v-for="row in rows" :key="filtered[row.index]!.value" class="filter-candidate"
        :class="{active: isSelected(filtered[row.index]!.value)}"
        :data-value="filtered[row.index]!.value" :aria-pressed="isSelected(filtered[row.index]!.value)"
        :title="filtered[row.index]!.label"
        :style="{transform: `translateY(${row.start}px)`}" @click="toggle(filtered[row.index]!.value)">
        <span class="candidate-mark" aria-hidden="true">{{ isSelected(filtered[row.index]!.value) ? '✓' : '' }}</span>
        <span class="candidate-label">{{ filtered[row.index]!.label }}</span>
        <small v-if="filtered[row.index]!.detail">{{ filtered[row.index]!.detail }}</small>
      </button>
    </div>
  </div>
</template>
<style scoped>
input{width:100%;margin:5px 0}.filter-selection-actions{flex-wrap:wrap}.candidate-summary,.selected-labels,.candidate-status{font-size:12px;color:var(--muted);line-height:1.6;margin:8px 0;overflow-wrap:anywhere}.candidate-summary{color:var(--text)}.candidate-status.error{color:var(--danger)}.selected-labels{max-height:58px;overflow:auto}.candidate-viewport{height:276px;overflow:auto;overscroll-behavior:contain;border:1px solid var(--line);border-radius:8px;touch-action:pan-y}.candidate-window{position:relative;width:100%}.filter-candidate{position:absolute;top:0;left:0;display:flex;align-items:center;gap:8px;width:100%;height:46px;min-height:46px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:var(--surface);padding:8px 10px;text-align:left;font-size:13px}.filter-candidate.active{background:var(--surface2);color:var(--accent)}.candidate-mark{width:20px;height:20px;flex-shrink:0;border:1px solid var(--line);border-radius:4px;text-align:center;line-height:18px}.active .candidate-mark{border-color:var(--accent);color:var(--accent)}.candidate-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.filter-candidate small{font-size:10px;color:var(--muted);flex-shrink:0}@media(max-width:400px){.candidate-viewport{height:230px}.filter-candidate{padding:8px}.filter-candidate small{max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
</style>
