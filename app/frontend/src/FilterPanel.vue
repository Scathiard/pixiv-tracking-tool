<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, url } from './api'
import type { Artist } from './types'
import { loadArtistCatalogue, readTagValues, selectionCount, selectionQuery, writeTagValues, type FilterSelection } from './state/filterSelection'
import FilterCandidates from './components/FilterCandidates.vue'
import Icon from './components/Icon.vue'
const props = defineProps<{query: string}>()
const emit = defineEmits<{apply: [query: string]; close: []}>()
const current = new URLSearchParams(props.query)
function selection(key: string): FilterSelection {
  const excluded = current.has(key + '_exclude')
  const name = excluded ? key + '_exclude' : key
  return {all: !current.has(key) && !excluded, values: key === 'tags' ? readTagValues(current, name) : (current.get(name) || '').split(',').filter(Boolean), excluded}
}
const artistSelection = ref(selection('artist_ids')), tagSelection = ref(selection('tags'))
const artists = ref<Artist[]>([]), tags = ref<string[]>([])
const artistLoading = ref(true), tagLoading = ref(true), artistError = ref(''), tagError = ref('')
const artistCandidates = computed(() => artists.value.map(artist => ({value: String(artist.id), label: artist.name,
  detail: artist.mode === 'stopped' ? '已停止追踪' : artist.mode === 'paused' ? '已暂停' : `ID ${artist.pixiv_id}`})))
const tagCandidates = computed(() => tags.value.map(tag => ({value: tag, label: tag})))
const types = ref(current.has('types') ? (current.get('types') || '').split(',').filter(Boolean) : ['illust', 'manga', 'ugoira'])
const typeOptions = [['illust', '插画'], ['manga', '漫画'], ['ugoira', '动图']]
const hidden = ref(current.get('show_hidden') === '1'), sort = ref(current.get('sort') || 'newest')
const period = ref(current.get('period') || ''), after = ref(current.get('posted_after') || ''), before = ref(current.get('posted_before') || '')
const dialog = ref<HTMLElement | null>(null), expanded = ref('type')
const selectedCount = computed(() => (!artistSelection.value.all ? 1 : 0) + (types.value.length !== 3 ? 1 : 0) + (!tagSelection.value.all ? 1 : 0) + (hidden.value ? 1 : 0) + (period.value ? 1 : 0))
let previousFocus: HTMLElement | null = null
const controller = new AbortController()
async function loadArtists() {
  artistLoading.value = true; artistError.value = ''
  try {
    const values = await loadArtistCatalogue((offset, limit) => api<{items: Artist[]; total: number}>(url('/api/artists', '', {offset, limit}), {signal: controller.signal}))
    if (!controller.signal.aborted) artists.value = values
  } catch { if (!controller.signal.aborted) artistError.value = '画师暂时无法读取，请重试' }
  finally { if (!controller.signal.aborted) artistLoading.value = false }
}
async function loadTags() {
  tagLoading.value = true; tagError.value = ''
  try {
    // Candidates always cover the entire local library, including hidden works.
    const data = await api<{tags: string[]}>('/api/library/tags?show_hidden=1', {signal: controller.signal})
    if (!controller.signal.aborted) tags.value = [...new Set(data.tags)]
  } catch { if (!controller.signal.aborted) tagError.value = '标签暂时无法读取，请重试' }
  finally { if (!controller.signal.aborted) tagLoading.value = false }
}
function toggleType(value: string) { types.value = types.value.includes(value) ? types.value.filter(item => item !== value) : [...types.value, value] }
function reset() { artistSelection.value = {all: true, values: []}; tagSelection.value = {all: true, values: []}; types.value = ['illust', 'manga', 'ugoira']; hidden.value = false; sort.value = 'newest'; period.value = ''; after.value = before.value = '' }
function apply() {
  const query = new URLSearchParams(props.query)
  for (const key of ['artist_ids', 'artist_ids_exclude', 'types', 'tags', 'tags_exclude', 'show_hidden', 'sort', 'period', 'posted_after', 'posted_before', 'ceiling', 'as_of', 'page']) query.delete(key)
  for (const [key, selected, catalogue] of [['artist_ids', artistSelection.value, artistCandidates.value.map(item => item.value)], ['tags', tagSelection.value, tags.value]] as const) {
    const value = selectionQuery(selected, catalogue)
    if (value) {
      const name = value.excluded ? key + '_exclude' : key
      if (key === 'tags') writeTagValues(query, value.values, name)
      else query.set(name, value.values.join(','))
    }
  }
  if (types.value.length !== 3) query.set('types', types.value.join(','))
  if (hidden.value) query.set('show_hidden', '1')
  query.set('sort', sort.value)
  if (period.value) query.set('period', period.value)
  if (period.value === 'custom') { if (after.value) query.set('posted_after', after.value); if (before.value) query.set('posted_before', before.value) }
  emit('apply', query.toString())
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab') return
  const focusable = [...(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled)') || [])].filter(element => element.getClientRects().length)
  const first = focusable[0], last = focusable.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(async () => { previousFocus = document.activeElement as HTMLElement; document.querySelector('#app')?.setAttribute('inert', ''); await nextTick(); dialog.value?.querySelector('button')?.focus(); void loadArtists(); void loadTags() })
onBeforeUnmount(() => { controller.abort(); document.querySelector('#app')?.removeAttribute('inert'); void nextTick(() => previousFocus?.focus({preventScroll: true})) })
</script>
<template><Teleport to="body"><div class="filter-overlay" role="presentation" @click.self="emit('close')"><section ref="dialog" class="filter-panel" role="dialog" aria-modal="true" aria-label="筛选作品" @keydown="key"><header class="filter-panel-head"><h2>筛选作品</h2><button class="quiet" aria-label="关闭筛选" @click="emit('close')"><Icon name="close" /></button></header><p class="filter-panel-summary">可组合类型、画师、标签和时间范围。已选 {{ selectedCount }} 项。</p><div class="filter-scroll">
  <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'type'" @click="expanded = expanded === 'type' ? '' : 'type'">作品类型 <small>{{ types.length }} / 3 已选</small><Icon name="down" /></button><div v-if="expanded === 'type'" class="filter-section-body"><div class="filter-selection-actions"><button @click="types = typeOptions.map(item => item[0]!)">全选</button><button @click="types = typeOptions.map(item => item[0]!).filter(value => !types.includes(value))">反选</button><button @click="types = []">清空选择</button></div><div class="filter-chips"><button v-for="[value,label] in typeOptions" :key="value" class="filter-chip" :class="{active: types.includes(value!)}" :aria-pressed="types.includes(value!)" @click="toggleType(value!)">{{ label }}</button></div><p v-if="!types.length" class="empty-selection">未选择作品类型，应用后无作品</p></div></section>
  <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'artists'" @click="expanded = expanded === 'artists' ? '' : 'artists'">画师 <small>{{ artistSelection.all ? '全部画师' : selectionCount(artistSelection, artistCandidates.map(item => item.value)) + ' 位已选' }}</small><Icon name="down" /></button><div v-if="expanded === 'artists'" class="filter-section-body"><FilterCandidates label="画师" :candidates="artistCandidates" :selection="artistSelection" :loading="artistLoading" :error="artistError" @change="artistSelection = $event" @retry="loadArtists" /></div></section>
  <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'tags'" @click="expanded = expanded === 'tags' ? '' : 'tags'">标签 <small>{{ tagSelection.all ? '全部标签' : selectionCount(tagSelection, tags) + ' 项已选' }}</small><Icon name="down" /></button><div v-if="expanded === 'tags'" class="filter-section-body"><small>匹配任意一个标签；候选来自全部本地作品。</small><FilterCandidates label="标签" :candidates="tagCandidates" :selection="tagSelection" :loading="tagLoading" :error="tagError" @change="tagSelection = $event" @retry="loadTags" /></div></section>
  <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'dates'" @click="expanded = expanded === 'dates' ? '' : 'dates'">投稿时间 <small>{{ period || '不限' }}</small><Icon name="down" /></button><div v-if="expanded === 'dates'" class="filter-section-body"><select v-model="period" aria-label="投稿时间"><option value="">不限</option><option value="24h">24 小时内</option><option value="week">一周内</option><option value="month">一个月内</option><option value="half_year">半年内</option><option value="year">一年内</option><option value="custom">指定期间</option></select><div v-if="period === 'custom'" class="filter-dates"><label>从<input v-model="after" type="date"></label><label>到<input v-model="before" type="date"></label></div></div></section>
  <section class="filter-section"><label class="filter-check"><input v-model="hidden" type="checkbox">显示已隐藏作品</label></section>
</div><footer class="filter-panel-foot"><button class="softbtn" @click="reset">重置</button><button class="primary" @click="apply">应用筛选</button></footer></section></div></Teleport></template>
<style scoped>
.filter-selection-actions{flex-wrap:wrap}.empty-selection{font-size:12px;color:var(--muted);margin:10px 0}
</style>
