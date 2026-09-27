<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, url } from './api'
import type { Artist } from './types'
import Icon from './components/Icon.vue'
const props = defineProps<{query: string}>()
const emit = defineEmits<{apply: [query: string]; close: []}>()
const current = new URLSearchParams(props.query)
const selected = ref((current.get('artist_ids') || '').split(',').filter(Boolean)), allArtists = ref(!current.has('artist_ids'))
const artistQuery = ref(''), artistOffset = ref(0), artists = ref<Artist[]>([]), artistTotal = ref(0)
const tags = ref(current.get('tags') || ''), types = ref(current.has('types') ? (current.get('types') || '').split(',').filter(Boolean) : ['illust', 'manga', 'ugoira'])
const hidden = ref(current.get('show_hidden') === '1'), sort = ref(current.get('sort') || 'newest')
const period = ref(current.get('period') || ''), after = ref(current.get('posted_after') || ''), before = ref(current.get('posted_before') || '')
const error = ref(''), dialog = ref<HTMLElement | null>(null)
const expanded = ref('type'), selectedCount = computed(() => (!allArtists.value ? 1 : 0) + (types.value.length !== 3 ? 1 : 0) + (tags.value ? 1 : 0) + (hidden.value ? 1 : 0) + (period.value ? 1 : 0))
let previousFocus: HTMLElement | null = null, generation = 0, timer = 0
async function loadArtists(offset = 0) {
  artistOffset.value = offset
  const epoch = ++generation
  try {
    const data = await api<{items: Artist[]; total: number}>(url('/api/artists', '', {offset, limit: 50, q: artistQuery.value}))
    if (generation === epoch) { artists.value = data.items; artistTotal.value = data.total }
  } catch { if (generation === epoch) error.value = '画师暂时无法读取，其余筛选仍可使用' }
}
function searchArtists() { clearTimeout(timer); timer = window.setTimeout(() => { void loadArtists() }, 260) }
function toggleType(value: string) { types.value = types.value.includes(value) ? types.value.filter(item => item !== value) : [...types.value, value] }
function toggleArtist(value: string) { selected.value = selected.value.includes(value) ? selected.value.filter(item => item !== value) : [...selected.value, value] }
function reset() { selected.value = []; allArtists.value = true; tags.value = ''; types.value = ['illust', 'manga', 'ugoira']; hidden.value = false; sort.value = 'newest'; period.value = ''; after.value = before.value = '' }
function apply() {
  const query = new URLSearchParams(props.query)
  for (const key of ['artist_ids', 'types', 'tags', 'show_hidden', 'sort', 'period', 'posted_after', 'posted_before', 'ceiling', 'as_of', 'page']) query.delete(key)
  if (!allArtists.value) query.set('artist_ids', selected.value.join(','))
  if (types.value.length !== 3) query.set('types', types.value.join(','))
  if (tags.value.trim()) query.set('tags', tags.value.split(/[,，]/).map(value => value.trim()).filter(Boolean).join(','))
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
onMounted(async () => { previousFocus = document.activeElement as HTMLElement; document.querySelector('#app')?.setAttribute('inert', ''); await nextTick(); dialog.value?.querySelector('button')?.focus(); void loadArtists() })
onBeforeUnmount(() => { generation++; clearTimeout(timer); document.querySelector('#app')?.removeAttribute('inert'); void nextTick(() => previousFocus?.focus({preventScroll: true})) })
</script>
<template><Teleport to="body"><div class="filter-overlay" role="presentation" @click.self="emit('close')"><section ref="dialog" class="filter-panel" role="dialog" aria-modal="true" aria-label="筛选作品" @keydown="key"><header class="filter-panel-head"><h2>筛选作品</h2><button class="quiet" aria-label="关闭筛选" @click="emit('close')"><Icon name="close" /></button></header><p class="filter-panel-summary">可组合类型、画师、标签和时间范围。已选 {{ selectedCount }} 项。</p><div class="filter-scroll">
    <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'type'" @click="expanded = expanded === 'type' ? '' : 'type'">作品类型 <small>{{ types.length }} / 3 已选</small><Icon name="down" /></button><div v-if="expanded === 'type'" class="filter-section-body"><div class="filter-chips"><button v-for="[value,label] in [['illust','插画'],['manga','漫画'],['ugoira','动图']]" :key="value" class="filter-chip" :class="{active: types.includes(value)}" :aria-pressed="types.includes(value)" @click="toggleType(value)">{{ label }}</button></div></div></section>
    <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'artists'" @click="expanded = expanded === 'artists' ? '' : 'artists'">画师 <small>{{ allArtists ? '全部画师' : selected.length + ' 位已选' }}</small><Icon name="down" /></button><div v-if="expanded === 'artists'" class="filter-section-body"><label class="filter-check"><input v-model="allArtists" type="checkbox">全部画师</label><template v-if="!allArtists"><input v-model="artistQuery" type="search" placeholder="搜索画师" aria-label="搜索筛选画师" @input="searchArtists"><div class="filter-selection-actions"><button @click="selected = [...new Set([...selected, ...artists.map(a => String(a.id))])]">选择当前页</button><button @click="selected = []">清空选择</button></div><div class="filter-chips"><button v-for="artist in artists" :key="artist.id" class="filter-chip" :class="{active: selected.includes(String(artist.id))}" :aria-pressed="selected.includes(String(artist.id))" @click="toggleArtist(String(artist.id))">{{ artist.name }}</button></div><div v-if="artistTotal > 50" class="filter-pager"><button :disabled="artistOffset === 0" @click="loadArtists(Math.max(0, artistOffset - 50))">上一页</button><span>{{ Math.floor(artistOffset / 50) + 1 }} / {{ Math.ceil(artistTotal / 50) }}</span><button :disabled="artistOffset + 50 >= artistTotal" @click="loadArtists(artistOffset + 50)">下一页</button></div></template></div></section>
    <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'tags'" @click="expanded = expanded === 'tags' ? '' : 'tags'">标签 <small>{{ tags ? '已设置' : '不限' }}</small><Icon name="down" /></button><div v-if="expanded === 'tags'" class="filter-section-body"><input v-model="tags" placeholder="多个标签用逗号隔开" aria-label="标签"><small>匹配任意一个标签</small></div></section>
    <section class="filter-section"><button class="filter-section-title" :aria-expanded="expanded === 'dates'" @click="expanded = expanded === 'dates' ? '' : 'dates'">投稿时间 <small>{{ period || '不限' }}</small><Icon name="down" /></button><div v-if="expanded === 'dates'" class="filter-section-body"><select v-model="period" aria-label="投稿时间"><option value="">不限</option><option value="24h">24 小时内</option><option value="week">一周内</option><option value="month">一个月内</option><option value="half_year">半年内</option><option value="year">一年内</option><option value="custom">指定期间</option></select><div v-if="period === 'custom'" class="filter-dates"><label>从<input v-model="after" type="date"></label><label>到<input v-model="before" type="date"></label></div></div></section>
    <section class="filter-section"><label class="filter-check"><input v-model="hidden" type="checkbox">显示已隐藏作品</label></section>
    <p v-if="error" class="error">{{ error }}</p>
  </div><footer class="filter-panel-foot"><button class="softbtn" @click="reset">重置</button><button class="primary" @click="apply">应用筛选</button></footer></section></div></Teleport></template>
