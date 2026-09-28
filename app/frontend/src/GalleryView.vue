<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { api, url, write } from './api'
import { LatestIntent } from './interaction'
import type { GalleryBatch, WorkCard } from './types'
import MediaImage from './MediaImage.vue'
import BookmarkButton from './components/BookmarkButton.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'
const props = defineProps<{query: string; startIndex: number; pageSize: number; continuous: boolean}>()
const emit = defineEmits<{open: [index: number]; artist: [id: number]; message: [text: string]; anchor: [index: number, workId: number]; total: [count: number]; page: [index: number]}>()
const viewport = ref<HTMLDivElement | null>(null), works = ref<WorkCard[]>([]), total = ref(0)
const page = ref(0), start = ref(0), columns = ref(5), width = ref(1000), loading = ref(false), error = ref('')
const request = new LatestIntent(), thumbs = new Set<number>()
let observer: ResizeObserver | null = null, restoreIndex = 0, refreshTimer = 0, refreshes = 0, restoring = false, scrollFrame = 0
const rowHeight = computed(() => ((width.value - 16 * (columns.value - 1)) / columns.value) * 1.15 + 77)
const virtual = useVirtualizer(computed(() => ({count: Math.ceil(works.value.length / columns.value),
  getScrollElement: () => viewport.value, estimateSize: () => rowHeight.value, overscan: 2})))
const rows = computed(() => virtual.value.getVirtualItems())
const maxPage = computed(() => Math.max(0, Math.ceil(total.value / props.pageSize) - 1))
async function restore(anchor: number, pixel = 0) {
  restoring = true
  await nextTick()
  virtual.value.scrollToOffset(Math.floor(Math.max(0, anchor - start.value) / columns.value) * rowHeight.value + pixel)
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  restoring = false
}
async function thumbnails(items: WorkCard[]) {
  const missing = items.filter(work => !work.thumbnail && !thumbs.has(work.id)).map(work => work.id)
  if (missing.length) {
    missing.forEach(id => thumbs.add(id))
    while (thumbs.size > 1000) thumbs.delete(thumbs.values().next().value!)
    await write('/api/tasks', {kind: 'thumbnails', work_ids: missing})
  }
  if (items.some(work => !work.thumbnail) && refreshes++ < 6) {
    clearTimeout(refreshTimer); refreshTimer = window.setTimeout(refresh, 5000)
  }
}
async function fetchRange(offset: number, limit: number, anchor: number, background = false) {
  const ticket = request.begin(); loading.value = true; error.value = ''
  try {
    const data = await api<GalleryBatch>(url('/api/library', props.query, {offset, limit, compact: 1}), {signal: ticket.signal})
    if (!ticket.current()) return
    start.value = offset; total.value = data.total; works.value = data.items; emit('total', data.total)
    if (!background) { restoreIndex = anchor; await restore(anchor); if (!props.continuous) emit('page', page.value) }
    await thumbnails(data.items)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '图库未能载入' }
  finally { if (ticket.current()) loading.value = false }
}
function load(index: number, anchor = index * props.pageSize) {
  page.value = index
  return fetchRange(index * props.pageSize, props.pageSize, anchor)
}
function restart() {
  thumbs.clear(); refreshes = 0; clearTimeout(refreshTimer)
  if (props.continuous) {
    const offset = Math.floor(Math.max(0, props.startIndex - columns.value * 12) / columns.value) * columns.value
    void fetchRange(offset, Math.min(480, columns.value * 48), props.startIndex)
  } else void load(Math.floor(props.startIndex / props.pageSize), props.startIndex)
}
function refresh() { if (!loading.value && works.value.length) void fetchRange(start.value, works.value.length, restoreIndex, true) }
async function extend(direction: number) {
  if (loading.value || restoring || !props.continuous) return
  const amount = Math.floor(192 / columns.value) * columns.value
  const end = start.value + works.value.length
  if (direction < 0 && start.value === 0 || direction > 0 && end >= total.value) return
  const ticket = request.begin(); loading.value = true
  try {
    const offset = direction > 0 ? end : Math.max(0, start.value - amount)
    const limit = direction > 0 ? amount : start.value - offset
    const data = await api<GalleryBatch>(url('/api/library', props.query, {offset, limit, compact: 1}), {signal: ticket.signal})
    if (!ticket.current()) return
    const row = rows.value.find(row => row.end > (viewport.value?.scrollTop || 0))
    const anchor = start.value + (row?.index || 0) * columns.value
    const pixel = (viewport.value?.scrollTop || 0) - (row?.start || 0)
    let items = direction > 0 ? [...works.value, ...data.items] : [...data.items, ...works.value]
    let first = direction > 0 ? start.value : offset
    if (items.length > 480) {
      const trim = Math.ceil((items.length - 480) / columns.value) * columns.value
      if (direction > 0) { items = items.slice(trim); first += trim }
      else items = items.slice(0, items.length - trim)
    }
    start.value = first; works.value = items; total.value = data.total
    await restore(anchor, pixel); await thumbnails(data.items)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '更多作品暂时无法载入' }
  finally { if (ticket.current()) loading.value = false }
}
function scroll() {
  if (scrollFrame) return
  scrollFrame = requestAnimationFrame(() => { scrollFrame = 0; readScroll() })
}
function readScroll() {
  if (restoring) return
  const row = rows.value.find(row => row.end > (viewport.value?.scrollTop || 0))
  if (!row) return
  restoreIndex = start.value + row.index * columns.value
  const work = works.value[row.index * columns.value]
  if (work) emit('anchor', restoreIndex, work.id)
  if (!props.continuous || loading.value || error.value) return
  if (row.index > Math.ceil(works.value.length / columns.value) - 12) void extend(1)
  else if (row.index < 6) void extend(-1)
}
watch(() => [props.query, props.pageSize, props.continuous], restart)
onMounted(() => {
  observer = new ResizeObserver(entries => {
    const rect = entries[0]?.contentRect
    if (rect) { width.value = rect.width; columns.value = Math.max(2, Math.floor((rect.width + 16) / 260)) }
  })
  if (viewport.value) observer.observe(viewport.value)
  window.addEventListener('archive-updated', refresh)
  restart()
})
onBeforeUnmount(() => { request.cancel(); observer?.disconnect(); clearTimeout(refreshTimer); cancelAnimationFrame(scrollFrame); window.removeEventListener('archive-updated', refresh) })
</script>
<template>
  <section class="gallery-view" aria-label="本地图库">
    <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="restart">重试</button></div>
    <div ref="viewport" class="gallery-viewport" @scroll.passive="scroll" :aria-busy="loading">
      <div class="gallery-window" :data-window-size="works.length" :data-start="start" :style="{height: `${virtual.getTotalSize()}px`}">
        <div v-for="row in rows" :key="String(row.key)" class="gallery-row" :style="{transform: `translateY(${row.start}px)`, gridTemplateColumns: `repeat(${columns},minmax(0,1fr))`}">
          <article v-for="(work, col) in works.slice(row.index * columns, (row.index + 1) * columns)" :key="work.id"
            class="work-card" :class="{'is-hidden': work.is_hidden}" :data-work-id="work.id">
            <button class="card-open" :aria-label="`查看 ${work.title}${work.x_restrict === 2 ? '，R-18G' : work.x_restrict === 1 ? '，R-18' : ''}`" @click="emit('open', start + row.index * columns + col)"><div class="card-picture"><MediaImage :src="work.thumbnail" :label="work.title" thumbnail /><RestrictionBadge :value="work.x_restrict" /><span v-if="work.type === 'ugoira' || work.page_count > 1" class="card-count">{{ work.type === 'ugoira' ? '动图' : work.page_count + ' 页' }}</span></div><strong>{{ work.title }}</strong></button>
            <button class="card-artist" @click="emit('artist', work.artist_id)">{{ work.artist_name }}<span v-if="work.is_hidden"> · 已隐藏</span></button>
            <BookmarkButton :work-id="work.id" :initial="work.bookmark" @message="emit('message', $event)" />
          </article>
        </div>
      </div>
      <div v-if="!works.length" class="empty"><h2>{{ loading ? '正在读取本地图库' : '这个范围里还没有作品' }}</h2><p>{{ loading ? '图片不会重新从 Pixiv 下载。' : '调整筛选，或从画师管理添加想要存档的画师。' }}</p><a v-if="!loading" href="/artists">前往画师管理</a></div>
    </div>
    <nav v-if="!continuous" class="pagination" aria-label="图库页码"><button :disabled="page <= 0 || loading" @click="load(page - 1)">上一页</button><label>第 <input aria-label="图库页码" type="number" min="1" :max="maxPage + 1" :value="page + 1" @change="load(Math.max(0, Math.min(maxPage, Number(($event.target as HTMLInputElement).value) - 1)))"> / {{ maxPage + 1 }} 页</label><button :disabled="page >= maxPage || loading" @click="load(page + 1)">下一页</button><span class="muted">{{ total }} 件作品</span></nav>
    <div v-if="continuous" class="pagination"><span>连续滚动 · {{ total }} 件作品</span><button :disabled="loading" @click="fetchRange(0, 240, 0)">回到开头</button><span v-if="loading" class="muted">正在载入更多作品…</span></div>
  </section>
</template>
