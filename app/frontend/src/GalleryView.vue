<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { api, url } from './api'
import { LatestIntent } from './interaction'
import { observeCatalogueData } from './state/catalogue'
import { singleArtistScope } from './state/filterSelection'
import { matchingGallerySeed, type GallerySeed } from './state/libraryEntry'
import { cardSourceLabel, cardSourceReason, isOlderCatalogue, needsCardRefresh, type Artist, type GalleryBatch, type WorkCard } from './types'
import Icon from './components/Icon.vue'
import GalleryCardImage from './GalleryCardImage.vue'
import BookmarkButton from './components/BookmarkButton.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'
const props = defineProps<{query: string; startIndex: number; pageSize: number; continuous: boolean; resetKey?: number; initialBatch?: GallerySeed | null}>()
const emit = defineEmits<{open: [index: number]; artist: [id: number]; message: [text: string]; anchor: [index: number, workId: number]; total: [count: number]; page: [index: number]; seedConsumed: []}>()
const viewportRevision = ref(0)
const viewport = ref<HTMLDivElement | null>(null), windowElement = ref<HTMLDivElement | null>(null), works = ref<WorkCard[]>([]), total = ref(0)
const page = ref(0), start = ref(0), columns = ref(5), width = ref(1000), loading = ref(false), error = ref('')
const animationSuspended = ref(false)
const artistScope = computed(() => singleArtistScope(props.query))
const artistProfile = ref<Artist | null>(null), profileError = ref(''), profileLoading = ref(false), bioExpanded = ref(false)
const profileElement = ref<HTMLDivElement | null>(null), profileHeight = ref(0)
const profileRequest = new LatestIntent()
const request = new LatestIntent(), restoration = new LatestIntent()
let observer: ResizeObserver | null = null, profileObserver: ResizeObserver | null = null, restoreIndex = 0, refreshTimer = 0, restoring = false, scrollFrame = 0
let animationIdleTimer = 0
let failedExtension: number | null = null, entryPending = false
const REFRESH_MS = 12000
const ANIMATION_RESUME_MS = 220
let catalogueRevision: number | undefined
let consumedSeed: GallerySeed | null | undefined
const rowHeight = computed(() => ((width.value - 16 * (columns.value - 1)) / columns.value) * 1.225 + 84)
const virtual = useVirtualizer(computed(() => ({count: Math.ceil(works.value.length / columns.value),
  getScrollElement: () => viewport.value, estimateSize: () => rowHeight.value, overscan: 2, scrollMargin: profileHeight.value})))
const rows = computed(() => virtual.value.getVirtualItems())
const maxPage = computed(() => Math.max(0, Math.ceil(total.value / props.pageSize) - 1))
async function restore(anchor: number, pixel = 0, showIntro = false, ownsView: () => boolean = () => true) {
  const ticket = restoration.begin()
  restoring = true
  try {
    await nextTick()
    if (!ticket.current() || !ownsView()) return
    virtual.value.measure()
    const row = Math.floor(Math.max(0, anchor - start.value) / columns.value)
    virtual.value.scrollToOffset(showIntro && anchor === 0 ? 0 : profileHeight.value + row * rowHeight.value + Math.min(pixel, rowHeight.value - 1))
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  } finally { if (ticket.current()) restoring = false }
}
async function loadProfile() {
  const id = artistScope.value, ticket = profileRequest.begin()
  if (!id) { profileLoading.value = false; return }
  profileLoading.value = true; profileError.value = ''
  try {
    const data = await api<Artist>(`/api/artists/${id}`, {signal: ticket.signal})
    if (ticket.current() && artistScope.value === id) artistProfile.value = data
  } catch (failure) {
    if (ticket.current()) profileError.value = failure instanceof Error ? failure.message : '画师资料暂时无法读取'
  } finally { if (ticket.current()) profileLoading.value = false }
}
watch(artistScope, () => {
  artistProfile.value = null; profileError.value = ''; bioExpanded.value = false
  void loadProfile()
}, {immediate: true})
watch(windowElement, element => {
  observer?.disconnect()
  if (element) observer?.observe(element)
}, {flush: 'post'})
watch(profileElement, element => {
  profileObserver?.disconnect()
  if (element) profileObserver?.observe(element)
  else profileHeight.value = 0
}, {flush: 'post'})
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
async function fetchRange(offset: number, limit: number, anchor: number, background = false, quiet = false, prepared?: GalleryBatch) {
  if (!background) {
    failedExtension = null; error.value = ''; entryPending = true
    restoration.cancel(); restoring = false
    cancelAnimationFrame(scrollFrame); scrollFrame = 0
  }
  const ticket = request.begin(); loading.value = true
  try {
    const data = prepared || await api<GalleryBatch>(url('/api/library', props.query, {offset, limit, compact: 1}), {signal: ticket.signal})
    if (!ticket.current() || isOlderCatalogue(data.catalogue_revision, catalogueRevision)) return
    catalogueRevision = data.catalogue_revision ?? catalogueRevision
    if (!background) {
      // A fresh scroller ends native touch inertia; retain loaded cards until this request succeeds.
      restoration.cancel(); restoring = false; restoreIndex = anchor
      viewportRevision.value++
    }
    start.value = offset; total.value = data.total; works.value = data.items; emit('total', data.total)
    if (!background && !props.continuous) page.value = Math.floor(offset / props.pageSize)
    observeCatalogueData(data.catalogue_revision)
    if (!background) { restoreIndex = anchor; await restore(anchor, 0, true, ticket.current); if (!ticket.current()) return; if (!props.continuous) emit('page', page.value) }
    await nextTick()
  } catch (failure) {
    if (ticket.current()) {
      if (background && works.value.length) {
        if (!quiet) emit('message', '图库暂时无法刷新，已载入的作品仍可浏览')
      } else error.value = failure instanceof Error ? failure.message : '图库未能载入'
    }
  }
  finally { if (ticket.current()) { entryPending = false; loading.value = false; queueRefresh() } }
}
function load(index: number, anchor = index * props.pageSize) {
  return fetchRange(index * props.pageSize, props.pageSize, anchor)
}
function restart() {
  restoration.cancel(); restoring = false
  cancelAnimationFrame(scrollFrame); scrollFrame = 0
  catalogueRevision = undefined
  clearTimeout(refreshTimer); refreshTimer = 0
  const seed = props.initialBatch
  const prepared = seed !== consumedSeed ? matchingGallerySeed(seed, props.query, props.startIndex, props.pageSize, props.continuous) : undefined
  if (prepared) {
    consumedSeed = seed
    emit('seedConsumed')
    void fetchRange(prepared.offset, prepared.items.length, props.startIndex, false, false, prepared)
    return
  }
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
    await restore(anchor, pixel, false, ticket.current); if (!ticket.current()) return
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
  if (restoring || entryPending) return
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
watch(() => [props.query, props.pageSize, props.continuous, props.resetKey], restart)
onMounted(() => {
  profileObserver = new ResizeObserver(entries => {
    const entry = entries.find(item => item.target === profileElement.value)
    if (!entry) return
    const height = entry.borderBoxSize?.[0]?.blockSize ?? profileElement.value?.getBoundingClientRect().height ?? 0
    const previous = profileHeight.value, top = viewport.value?.scrollTop || 0
    if (Math.abs(height - previous) < 1) return
    profileHeight.value = height
    // Keep visible cards at the same pixel while async profile/bio height changes.
    if (!restoring && !entryPending && top > previous + 1) {
      const revision = viewportRevision.value, element = viewport.value
      void nextTick(() => {
        if (revision !== viewportRevision.value || element !== viewport.value || entryPending || restoring) return
        virtual.value.measure(); virtual.value.scrollToOffset(Math.max(0, top + height - previous))
      })
    }
  })
  if (profileElement.value) profileObserver.observe(profileElement.value)
  observer = new ResizeObserver(entries => {
    const entry = entries.find(item => item.target === windowElement.value)
    if (!entry) return
    const rect = entry.contentRect
    if (rect && Math.abs(rect.width - width.value) > 1) {
      const anchor = restoreIndex
      const currentRow = rows.value.find(row => row.end > (viewport.value?.scrollTop || 0))
      const pixel = (viewport.value?.scrollTop || 0) - (currentRow?.start || 0)
      width.value = rect.width
      columns.value = Math.max(2, Math.floor((rect.width + 16) / 220))
      if (works.value.length && !entryPending) {
        const revision = viewportRevision.value, element = viewport.value
        void restore(anchor, Math.max(0, pixel), false, () => revision === viewportRevision.value && element === viewport.value && !entryPending)
      }
    }
  })
  if (windowElement.value) observer.observe(windowElement.value)
  window.addEventListener('archive-updated', archiveUpdated)
  document.addEventListener('visibilitychange', visibilityChanged)
  restart()
})
onBeforeUnmount(() => { request.cancel(); restoration.cancel(); profileRequest.cancel(); observer?.disconnect(); profileObserver?.disconnect(); clearTimeout(refreshTimer); clearTimeout(animationIdleTimer); cancelAnimationFrame(scrollFrame); window.removeEventListener('archive-updated', archiveUpdated); document.removeEventListener('visibilitychange', visibilityChanged) })
defineExpose({isAtBeginning: () => (!props.continuous ? page.value === 0 : start.value === 0) && (viewport.value?.scrollTop || 0) < 4})
</script>
<template>
  <section class="gallery-view" aria-label="本地图库">
    <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryFailedRange">重试</button></div>
    <div :key="viewportRevision" ref="viewport" class="gallery-viewport" @scroll.passive="scroll" :aria-busy="loading">
      <div v-if="artistScope" ref="profileElement" class="gallery-artist-intro" :data-artist-id="artistScope">
        <template v-if="artistProfile"><div class="gallery-artist-summary"><img v-if="artistProfile.avatar" class="artist-avatar" :src="artistProfile.avatar" :alt="`${artistProfile.name}头像`"><div v-else class="artist-avatar avatar-empty" aria-hidden="true"><Icon name="artists" /></div><div><h2>{{ artistProfile.name }}</h2><p>{{ artistProfile.work_count ?? total }} 件本地作品</p><a class="artist-pixiv-link" :href="`https://www.pixiv.net/users/${encodeURIComponent(artistProfile.pixiv_id)}`" target="_blank" rel="noopener noreferrer">Pixiv 主页 <Icon name="external" /></a></div></div><p class="gallery-artist-bio" :class="{expanded: bioExpanded}">{{ artistProfile.biography || '暂无画师简介' }}</p><button v-if="(artistProfile.biography?.length || 0) > 120 || (artistProfile.biography?.split('\n').length || 0) > 3" class="text-link" :aria-expanded="bioExpanded" @click="bioExpanded = !bioExpanded">{{ bioExpanded ? '收起简介' : '展开简介' }}</button></template>
        <p v-else-if="profileError" class="error" role="alert">{{ profileError }} <button :disabled="profileLoading" @click="loadProfile">重试</button></p><p v-else class="muted">正在读取本地画师资料…</p>
      </div>
      <div ref="windowElement" class="gallery-window" :data-window-size="works.length" :data-start="start" :style="{height: `${virtual.getTotalSize()}px`}">
        <div v-for="row in rows" :key="String(row.key)" class="gallery-row" :style="{transform: `translateY(${row.start - profileHeight}px)`, gridTemplateColumns: `repeat(${columns},minmax(0,1fr))`}">
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
