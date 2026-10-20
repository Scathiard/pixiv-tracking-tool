<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { candidateRows } from '../state/candidateRows'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { invertSelection, selectionCount, toggleSelection, type FilterSelection } from '../state/filterSelection'
const props = defineProps<{
  label: string; candidates: {value: string; label: string; detail?: string; translated?: boolean}[]
  selection: FilterSelection; loading: boolean; refreshing?: boolean; error: string; scrollElement: HTMLElement | null
}>()
const emit = defineEmits<{change: [value: FilterSelection]; retry: []}>()
const search = ref(''), viewport = ref<HTMLElement | null>(null), width = ref(320), margin = ref(0)
const context = document.createElement('canvas').getContext('2d')
if (context) context.font = '13px system-ui'
const packed = computed(() => candidateRows(filtered.value, width.value, text => context?.measureText(text).width || text.length * 13))
let observer: ResizeObserver | null = null
function dimensions() {
  if (!viewport.value || !props.scrollElement) return
  width.value = viewport.value.clientWidth
  margin.value = viewport.value.getBoundingClientRect().top - props.scrollElement.getBoundingClientRect().top + props.scrollElement.scrollTop
}
onMounted(() => { observer = new ResizeObserver(dimensions); if (props.scrollElement) observer.observe(props.scrollElement); void nextTick(dimensions) })
onBeforeUnmount(() => observer?.disconnect())
watch(() => [props.loading, filtered.value.length, props.scrollElement, props.selection], () => { void nextTick(dimensions) })
const catalogue = computed(() => props.candidates.map(item => item.value))
const filtered = computed(() => {
  const term = search.value.trim().toLocaleLowerCase()
  return props.candidates.filter(item => !term || `${item.label} ${item.value} ${item.detail || ''}`.toLocaleLowerCase().includes(term))
})
const selectedValues = computed(() => new Set(props.selection.values))
const count = computed(() => selectionCount(props.selection, catalogue.value))
function isSelected(value: string) { return props.selection.all || (props.selection.excluded ? !selectedValues.value.has(value) : selectedValues.value.has(value)) }
const selectedLabels = computed(() => {
  const labels = new Map(props.candidates.map(item => [item.value, item.label]))
  return props.selection.values.map(value => labels.get(value) || value)
})
const virtual = useVirtualizer(computed(() => ({count: packed.value.length,
  getScrollElement: () => props.scrollElement, estimateSize: () => 46, overscan: 5, scrollMargin: margin.value})))
const rows = computed(() => virtual.value.getVirtualItems())
watch(search, () => { void nextTick(() => { dimensions(); virtual.value.scrollToOffset(margin.value) }) })
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
  <div v-if="error" class="candidate-status error" role="alert">{{ error }} <button @click="emit('retry')">重试</button></div>
  <p v-if="refreshing" class="candidate-status" role="status">正在更新{{ label }}候选…</p>
  <p v-if="loading" class="candidate-status" role="status">正在载入全部{{ label }}…</p>
  <p v-else-if="!candidates.length && !error" class="candidate-status">本地图库暂无{{ label }}</p>
  <p v-else-if="!filtered.length && !error" class="candidate-status">没有匹配的{{ label }}，请换个搜索词</p>
  <div v-else ref="viewport" class="candidate-viewport" :aria-label="`${label}候选列表`" :data-candidate-total="candidates.length">
    <div class="candidate-window" :style="{height: `${virtual.getTotalSize()}px`}">
      <div v-for="row in rows" :key="row.index" :ref="element => { if (element) virtual.measureElement(element as Element) }" :data-index="row.index" class="candidate-row" :style="{transform: `translateY(${row.start - margin}px)`}">
        <button v-for="item in packed[row.index]" :key="item.value" class="filter-candidate" :class="{active: isSelected(item.value)}" :data-value="item.value" :aria-pressed="isSelected(item.value)" @click="toggle(item.value)">
          <span class="candidate-mark" aria-hidden="true">{{ isSelected(item.value) ? '✓' : '' }}</span><span class="candidate-label">{{ item.label }}</span><small v-if="item.detail" :data-source="item.translated ? 'pixiv' : undefined" :title="item.translated ? 'Pixiv 中文译名' : undefined">{{ item.detail }}</small>
        </button>
      </div>
    </div>
  </div>
</template>
<style scoped>
input{width:100%;margin:5px 0}.filter-selection-actions{flex-wrap:wrap}.candidate-summary,.selected-labels,.candidate-status{font-size:12px;color:var(--muted);line-height:1.6;margin:8px 0;overflow-wrap:anywhere}.candidate-summary{color:var(--text)}.candidate-status.error{color:var(--danger)}.candidate-viewport{width:100%;overflow:visible}.candidate-window{position:relative;width:100%}.candidate-row{position:absolute;top:0;left:0;width:100%;display:flex;gap:8px;align-items:start;padding-bottom:8px}.filter-candidate{display:flex;align-items:center;gap:8px;max-width:100%;min-width:0;min-height:38px;border:1px solid var(--line);border-radius:7px;background:var(--surface);padding:7px 9px;text-align:left;font-size:13px;white-space:normal}.filter-candidate.active{background:var(--surface2);color:var(--accent);border-color:var(--accent)}.candidate-mark{width:16px;height:16px;flex:none;border:1px solid var(--line);border-radius:4px;text-align:center;line-height:14px}.active .candidate-mark{border-color:var(--accent);color:var(--accent)}.candidate-label{min-width:0;overflow-wrap:anywhere}.filter-candidate small{font-size:10px;color:var(--muted);overflow-wrap:anywhere}
</style>
