<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { api, url } from './api'
import { LatestIntent } from './interaction'
import { observeCatalogueData } from './state/catalogue'
import { cardSourceLabel, cardSourceReason, isOlderCatalogue, needsCardRefresh, type GalleryBatch, type WorkCard } from './types'
import GalleryCardImage from './GalleryCardImage.vue'
import BookmarkButton from './components/BookmarkButton.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'
const props = defineProps<{query: string; startIndex: number; pageSize: number; continuous: boolean}>()
const emit = defineEmits<{open: [index: number]; artist: [id: number]; message: [text: string]; anchor: [index: number, workId: number]; total: [count: number]; page: [index: number]}>()
const viewport = ref<HTMLDivElement | null>(null), windowElement = ref<HTMLDivElement | null>(null), works = ref<WorkCard[]>([]), total = ref(0)
const page = ref(0), start = ref(0), columns = ref(5), width = ref(1000), loading = ref(false), error = ref('')
const animationSuspended = ref(false)
const request = new LatestIntent()
let observer: ResizeObserver | null = null, restoreIndex = 0, refreshTimer = 0, restoring = false, scrollFrame = 0
let animationIdleTimer = 0
let failedExtension: number | null = null
const REFRESH_MS = 12000
const ANIMATION_RESUME_MS = 220
let catalogueRevision: number | undefined
const rowHeight = computed(() => ((width.value - 16 * (columns.value - 1)) / columns.value) * 1.225 + 84)
const virtual = useVirtualizer(computed(() => ({count: Math.ceil(works.value.length / columns.value),
  getScrollElement: () => viewport.value, estimateSize: () => rowHeight.value, overscan: 2})))
const rows = computed(() => virtual.value.getVirtualItems())
const maxPage = computed(() => Math.max(0, Math.ceil(total.value / props.pageSize) - 1))
async function restore(anchor: number, pixel = 0) {
  restoring = true
  await nextTick()
  virtual.value.measure()
  virtual.value.scrollToOffset(Math.floor(Math.max(0, anchor - start.value) / columns.value) * rowHeight.value + Math.min(pixel, rowHeight.value - 1))
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  restoring = false
}
function visibleDeficit() {
  const visibleRows = rows.value.filter(row => {
    const top = viewport.value?.scrollTop || 0, bottom = top + (viewport.value?.clientHeight || 0)
    return row.end >= top && row.start <= bottom
  })
  const indices = visibleRows.length ? visibleRows.flatMap(row =>
    Array.from({length: columns.value}, (_, col) => row.index * columns.value + col))
    : Array.from({length: Math.min(works.value.length, columns.value * 4)}, (_, index) => index)
  return indices.some(index => {
    const work = works.value[index]
    return !!work && needsCardRefresh(work)
  })
}
function queueRefresh() {
  if (document.visibilityState !== 'visible' || !visibleDeficit()) {
    clearTimeout(refreshTimer); refreshTimer = 0; return
  }
  if (!refreshTimer) refreshTimer = window.setTimeout(() => { refreshTimer = 0; backgroundRefresh() }, REFRESH_MS)
}
async function fetchRange(offset: number, limit: number, anchor: number, background = false, quiet = false) {
  if (!background) { failedExtension = null; error.value = '' }
  const ticket = request.begin(); loading.value = true
  try {
    const data = await api<GalleryBatch>(url('/api/library', props.query, {offset, limit, compact: 1}), {signal: ticket.signal})
    if (!ticket.current() || isOlderCatalogue(data.catalogue_revision, catalogueRevision)) return
    catalogueRevision = data.catalogue_revision ?? catalogueRevision
    start.value = offset; total.value = data.total; works.value = data.items; emit('total', data.total)
    observeCatalogueData(data.catalogue_revision)
    if (!background) { restoreIndex = anchor; await restore(anchor); if (!ticket.current()) return; if (!props.continuous) emit('page', page.value) }
    await nextTick()
  } catch (failure) {
    if (ticket.current()) {
      if (background && works.value.length) {
        if (!quiet) emit('message', '图库暂时无法刷新，已载入的作品仍可浏览')
      } else error.value = failure instanceof Error ? failure.message : '图库未能载入'
    }
  }
  finally { if (ticket.current()) { loading.value = false; queueRefresh() } }
}
function load(index: number, anchor = index * props.pageSize) {
  page.value = index
  return fetchRange(index * props.pageSize, props.pageSize, anchor)
}
function restart() {
  catalogueRevision = undefined
  clearTimeout(refreshTimer); refreshTimer = 0
  if (props.continuous) {
    const offset = Math.floor(Math.max(0, props.startIndex - columns.value * 12) / columns.value) * columns.value
    void fetchRange(offset, Math.min(480, columns.value * 48), props.startIndex)
  } else void load(Math.floor(props.startIndex / props.pageSize), props.startIndex)
}
function refresh(quiet = false) {
  if (document.visibilityState !== 'visible') return
  if (loading.value) { if (!refreshTimer) refreshTimer = window.setTimeout(() => { refreshTimer = 0; refresh(quiet) }, 1000); return }
  if (works.value.length) void fetchRange(start.value, works.value.length, restoreIndex, true, quiet)
}
function backgroundRefresh() { refresh(true) }
function archiveUpdated() { refresh() }
function visibilityChanged() {
  if (document.visibilityState === 'visible' && visibleDeficit()) {
    clearTimeout(refreshTimer); refreshTimer = 0; backgroundRefresh()
  } else queueRefresh()
}
async function extend(direction: number) {
  if (loading.value || restoring || !props.continuous) return
  const amount = Math.floor(192 / columns.value) * columns.value
  const end = start.value + works.value.length
  if (direction < 0 && start.value === 0 || direction > 0 && end >= total.value) return
  const ticket = request.begin(); loading.value = true; error.value = ''; failedExtension = null
  try {
    const offset = direction > 0 ? end : Math.max(0, start.value - amount)
    const limit = direction > 0 ? amount : start.value - offset
    const data = await api<GalleryBatch>(url('/api/library', props.query, {offset, limit, compact: 1}), {signal: ticket.signal})
    if (!ticket.current() || isOlderCatalogue(data.catalogue_revision, catalogueRevision)) return
    catalogueRevision = data.catalogue_revision ?? catalogueRevision
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
    observeCatalogueData(data.catalogue_revision)
    await restore(anchor, pixel); if (!ticket.current()) return
  } catch (failure) {
    if (ticket.current()) { failedExtension = direction; error.value = failure instanceof Error ? failure.message : '更多作品暂时无法载入' }
  }
  finally { if (ticket.current()) { loading.value = false; queueRefresh() } }
}
function retryFailedRange() {
  if (failedExtension !== null) void extend(failedExtension)
  else restart()
}
function scroll() {
  animationSuspended.value = true
  clearTimeout(animationIdleTimer)
  animationIdleTimer = window.setTimeout(() => {
    animationIdleTimer = 0
    animationSuspended.value = false
  }, ANIMATION_RESUME_MS)
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
  queueRefresh()
  if (!props.continuous || loading.value || error.value) return
  if (row.index > Math.ceil(works.value.length / columns.value) - 12) void extend(1)
  else if (row.index < 6) void extend(-1)
}
watch(() => [props.query, props.pageSize, props.continuous], restart)
onMounted(() => {
  observer = new ResizeObserver(entries => {
    const rect = entries[0]?.contentRect
    if (rect && Math.abs(rect.width - width.value) > 1) {
      const anchor = restoreIndex
      const currentRow = rows.value.find(row => row.end > (viewport.value?.scrollTop || 0))
      const pixel = (viewport.value?.scrollTop || 0) - (currentRow?.start || 0)
      width.value = rect.width
      columns.value = Math.max(2, Math.floor((rect.width + 16) / 220))
      if (works.value.length) void restore(anchor, Math.max(0, pixel))
    }
  })
  if (windowElement.value) observer.observe(windowElement.value)
  window.addEventListener('archive-updated', archiveUpdated)
  document.addEventListener('visibilitychange', visibilityChanged)
  restart()
})
onBeforeUnmount(() => { request.cancel(); observer?.disconnect(); clearTimeout(refreshTimer); clearTimeout(animationIdleTimer); cancelAnimationFrame(scrollFrame); window.removeEventListener('archive-updated', archiveUpdated); document.removeEventListener('visibilitychange', visibilityChanged) })
defineExpose({isAtBeginning: () => (!props.continuous ? page.value === 0 : start.value === 0) && (viewport.value?.scrollTop || 0) < 4})
</script>
<template>
  <section class="gallery-view" aria-label="本地图库">
    <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryFailedRange">重试</button></div>
    <div ref="viewport" class="gallery-viewport" @scroll.passive="scroll" :aria-busy="loading">
      <div ref="windowElement" class="gallery-window" :data-window-size="works.length" :data-start="start" :style="{height: `${virtual.getTotalSize()}px`}">
        <div v-for="row in rows" :key="String(row.key)" class="gallery-row" :style="{transform: `translateY(${row.start}px)`, gridTemplateColumns: `repeat(${columns},minmax(0,1fr))`}">
          <article v-for="(work, col) in works.slice(row.index * columns, (row.index + 1) * columns)" :key="work.id"
            class="work-card" :class="{'is-hidden': work.is_hidden}" :data-work-id="work.id">
            <button class="card-open" :aria-label="`查看 ${work.title}${work.x_restrict === 2 ? '，R-18G' : work.x_restrict === 1 ? '，R-18' : ''}${cardSourceLabel(work) ? `，${cardSourceLabel(work)}：${cardSourceReason(work) || '详情可查看原因'}` : ''}`" @click="emit('open', start + row.index * columns + col)"><div class="card-picture"><GalleryCardImage :thumbnail="work.thumbnail" :preview="work.preview || null" :media-revision="catalogueRevision" :preview-status="work.preview_status || 'pending'" :recovery="work.recovery" :archive="work.archive" :processing="work.processing" :animation="work.type === 'ugoira'" :suspended="animationSuspended" :label="work.title" /><RestrictionBadge :value="work.x_restrict" /><span v-if="cardSourceLabel(work)" class="card-source" :title="cardSourceReason(work) || cardSourceLabel(work) || undefined">{{ cardSourceLabel(work) }}</span><span v-if="work.type === 'ugoira' || work.page_count > 1" class="card-count">{{ work.type === 'ugoira' ? '动图' : work.page_count + ' 页' }}</span></div><strong>{{ work.title }}</strong></button>
            <button class="card-artist" @click="emit('artist', work.artist_id)">{{ work.artist_name }}<span v-if="work.is_hidden"> · 已隐藏</span></button>
            <BookmarkButton :work-id="work.id" :initial="work.bookmark" @message="emit('message', $event)" />
          </article>
        </div>
      </div>
      <div v-if="!works.length" class="empty"><h2>{{ loading ? '正在读取本地图库' : '这个范围里还没有作品' }}</h2><p>{{ loading ? '图片不会重新从 Pixiv 下载。' : '调整筛选，或从画师管理添加想要存档的画师。' }}</p><a v-if="!loading" href="/artists">前往画师管理</a></div>
    </div>
    <nav v-if="!continuous" class="pagination" aria-label="图库页码"><button :disabled="page <= 0 || loading" @click="load(page - 1)">上一页</button><label>第 <input aria-label="图库页码" type="number" min="1" :max="maxPage + 1" :value="page + 1" @change="load(Math.max(0, Math.min(maxPage, Number(($event.target as HTMLInputElement).value) - 1)))"> / {{ maxPage + 1 }} 页</label><button :disabled="page >= maxPage || loading" @click="load(page + 1)">下一页</button><span class="muted">{{ total }} 件作品</span></nav>
    <button v-if="continuous && startIndex > 0" class="gallery-jump" :disabled="loading" aria-label="回到开头" title="回到开头" @click="fetchRange(0, 240, 0)">↑</button>
  </section>
</template>
