<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, ApiReadError, preparePageImage, url, write } from './api'
import { LatestIntent } from './interaction'
import { freshLibraryQuery } from './positions'
import { readLibraryEntry, type GallerySeed } from './state/libraryEntry'
import { normalizeGalleryBatch, archiveNotice, archiveStatus, localAssetReason, hasNewWorks, isOlderCatalogue, type Anchor, type CursorGalleryBatch, type GalleryBatch, type WorkCard, type Mode, type PageData, type PageBatch, type PageLoader, type WorkDetail } from './types'
import { observeCatalogueData, queueCatalogueUpdate } from './state/catalogue'
import { writeTagValues } from './state/filterSelection'
import { warmFilterCatalogue, invalidateFilterCatalogue, disposeFilterCatalogue } from './state/filterCatalogue'
import { invalidateWorkCache, workDetail, workPath } from './state/workCache'
import { startVisit, releaseVisit, locateVisit, visitPages, needsIdentityPrevious, RandomReadingPreparation, expiredReadingVisit, type ReadingVisit, type ReadingOrder } from './state/reading'
import { CursorReading, type CursorPageBatch } from './state/cursorReading'
import { type StreamInitialOpening, type StreamNeighborLoader, type StreamCandidate } from './state/streamReader'
import { observeVisibleViewport } from './state/viewport'
import FilterPanel from './FilterPanel.vue'
import GalleryView from './GalleryView.vue'
import PageReader from './PageReader.vue'
import StreamReader from './StreamReader.vue'
import StreamChrome from './components/StreamChrome.vue'
import TaskPanel from './TaskPanel.vue'
import ZoomView from './ZoomView.vue'
import ArtistsView from './views/ArtistsView.vue'
import SettingsView from './views/SettingsView.vue'
import UpgradeView, {type StartupStatus} from './views/UpgradeView.vue'
import BookmarkButton from './components/BookmarkButton.vue'
import TagLabel from './components/TagLabel.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'
import Icon from './components/Icon.vue'
import BrandMark from './components/BrandMark.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import { ingestBookmark, refreshBookmark, resumeBookmarkPolling, syncBookmark, addBookmark } from './state/bookmarks'

const mode = ref<Mode>('gallery'), scope = ref(''), ready = ref(false), pending = ref(false), total = ref(0)
const section = ref<'library' | 'artists' | 'settings'>('library'), artistId = ref<number | null>(null)
const startup = ref<StartupStatus | null>(null)
function upgraded() { window.location.reload() }
const detailOrigin = ref<'gallery' | 'feed' | 'artists'>('gallery'), originArtistId = ref<number | null>(null)
const homeScope = ref(''), searchText = ref(''), sortChoice = ref('newest')
const railCounts = ref<{all: number | null; recent: number | null; favorites: number | null}>({all: null, recent: null, favorites: null})
const router = useRouter(), browserRoute = useRoute()
let internalRoute = '', searchTimer = 0
const composing = ref(false)
const filterOpen = ref(false), infoOpen = ref(false), notification = ref(''), error = ref(''), newContent = ref(false)
const sidebarCollapsed = ref(false), mobileMenuOpen = ref(false), notificationError = ref(false)
const mobileMenuButton = ref<HTMLButtonElement | null>(null)
const removePrompt = ref(false)
const galleryAnchor = ref(0), galleryReset = ref(0), readerReset = ref(0), pageSize = ref(100), ordinaryIndex = ref(0), desiredIndex = ref(0)
const continuous = ref(false)
const gallerySeed = ref<GallerySeed | null>(null)
const readingVisit = ref<ReadingVisit | null>(null), readingOrder = ref<ReadingOrder>('sequential')
const flowOrdinalKnown = ref(true)
const readingEnded = ref(false), totalExact = ref(true), cursorExpired = ref(false), revisionLoading = ref(false)
let cursorFeed: CursorReading | null = null
interface Neighbor {work_id: number; revision_id: string | null; page_count: number; cursor: string}
interface CursorNeighbors {anchor_cursor: string; generation: number; continuation_rebased?: boolean; previous: Neighbor | null; next: Neighbor | null}
const galleryOrdinalKnown = ref(true), ordinaryRankKnown = ref(false)
const ordinaryCursor = ref<string | null>(null), ordinaryNeighbors = ref<CursorNeighbors | null>(null)
const galleryView = ref<InstanceType<typeof GalleryView> | null>(null), checkingNewContent = ref(false)
const currentWork = ref<WorkDetail | null>(null), infoWork = ref<WorkDetail | null>(null), activePage = ref<PageData | null>(null)
const initialAnchor = ref<Anchor | null>(null), currentAnchor = ref<Anchor | null>(null), zoomPage = ref<PageData | null>(null)
const streamPanelOpen = ref(false)
const reader = ref<InstanceType<typeof PageReader> | InstanceType<typeof StreamReader> | null>(null), taskPanel = ref<InstanceType<typeof TaskPanel> | null>(null)
const navigation = new LatestIntent(), infoRequest = new LatestIntent(), statusRequest = new LatestIntent()
const navigationPhase = ref('正在定位作品…'), readerActivity = ref({phase: '', error: ''})
const randomPreparation = new RandomReadingPreparation()
const streamInitialOpening = ref<StreamInitialOpening | null>(null)
let randomWarmTimer = 0, randomWarm: AbortController | null = null
let randomLocation: {session: string; work: number; anchor: Anchor} | null = null
let openingKey: string | null = null, adjacentPreload: AbortController | null = null
const readingActivity = computed(() => pending.value ? {phase: navigationPhase.value, error: ''} : error.value ? {phase: '', error: error.value} : readerActivity.value)
function cancelNavigation() {
  navigation.cancel(); adjacentPreload?.abort(); pending.value = false; openingKey = null; desiredIndex.value = ordinaryIndex.value
  reader.value?.cancel(); readerActivity.value = {phase: '', error: ''}
}
function retryReading() { if (error.value) retryNavigation(); else reader.value?.retry() }
function preloadAdjacent(neighbors: CursorNeighbors) {
  adjacentPreload?.abort()
  const controller = adjacentPreload = new AbortController()
  // Two opening responses and two display images at most; no full-work scan.
  for (const neighbor of [neighbors.previous, neighbors.next]) if (neighbor) {
    void workDetail(neighbor.work_id, neighbor.revision_id, controller.signal).then(detail =>
      preparePageImage(detail.pages[0], controller.signal)).catch(() => {})
  }
}
const newContentRequest = new LatestIntent()
const railRequest = new LatestIntent()
let newContentAgain = false, revealNewContent = false, hiddenCatalogueUpdate = false
let toastTimer = 0, lastKeyTime = 0, failedNavigation: (() => void) | null = null
const sessionKey = computed(() => mode.value + ':' + (mode.value === 'reader' ? currentWork.value?.id : '') + ':' + scope.value + ':' + readerReset.value)
let stopViewport: (() => void) | undefined
let filterWarmTimer = 0
function queueFilterWarm() {
  clearTimeout(filterWarmTimer)
  if (!ready.value || pending.value || section.value !== 'library' || document.hidden) return
  filterWarmTimer = window.setTimeout(() => { void warmFilterCatalogue() }, 450)
}
watch([ready, pending, section], queueFilterWarm, {flush: 'post'})
onMounted(() => { stopViewport = observeVisibleViewport() })
onBeforeUnmount(() => { stopViewport?.(); clearTimeout(filterWarmTimer); disposeFilterCatalogue() })
const heading = computed(() => mode.value === 'gallery' ? new URLSearchParams(scope.value).has('favorites') ? '收藏作品' : new URLSearchParams(scope.value).has('recent') ? '最近存入' : '我的图库' : activePage.value?.title || currentWork.value?.title || '阅览')
const filterSummary = computed(() => {
  const params = new URLSearchParams(scope.value)
  const labels = [params.has('artist_ids') ? `${params.get('artist_ids')?.split(',').filter(Boolean).length || 0} 位画师` : params.has('artist_ids_exclude') ? '画师已筛选' : '全部画师',
    params.has('types') ? `${params.get('types')?.split(',').filter(Boolean).length || 0} 种类型` : '全部类型']
  if (params.has('tags') || params.has('tags_exclude')) labels.push('标签已筛选')
  if (params.has('q')) labels.push(`搜索：${params.get('q')}`)
  return labels.join(' · ')
})
async function updateRailCounts(query: string) {
  const ticket = railRequest.begin()
  const base = new URLSearchParams(query)
  base.delete('favorites'); base.delete('recent')
  try {
    const counts = await api<{all: number; recent: number; favorites: number}>(url('/api/library/counts', base.toString()), {signal: ticket.signal})
    if (ticket.current()) { railCounts.value = counts; total.value = counts[currentRail()]; totalExact.value = true }
  } catch { /* Counters are optional while the library itself remains usable. */ }
}
function message(text: string, isError = false) {
  notification.value = text; notificationError.value = isError; clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { notification.value = '' }, notificationError.value ? 9000 : 5000)
}
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  try { localStorage.setItem('pixiv-local-sidebar-collapsed', sidebarCollapsed.value ? '1' : '0') } catch { /* optional preference */ }
}
function closeMobileMenu() { const wasOpen = mobileMenuOpen.value; mobileMenuOpen.value = false; if (wasOpen) void nextTick(() => mobileMenuButton.value?.focus()) }
function navHome() { closeMobileMenu(); brandHome() }
function navArtists() { closeMobileMenu(); showArtists() }
function navFeed() {
  closeMobileMenu()
  if (section.value === 'library') { if (mode.value !== 'feed') void enterFeed(true, mode.value === 'reader'); return }
  void initialize(homeScope.value, '/flow', true)
}
function navSettings() { closeMobileMenu(); showSettings() }
function navRail(rail: 'all' | 'recent' | 'favorites') { closeMobileMenu(); changeRail(rail) }
function route(push: boolean) {
  const path = section.value === 'settings' ? '/settings' : section.value === 'artists' ? artistId.value ? `/artist/${artistId.value}` : '/artists' :
    mode.value === 'gallery' ? '/' : mode.value === 'feed' ? '/flow' : '/illust/' + currentWork.value!.id
  const params = section.value === 'library' ? new URLSearchParams(scope.value) : new URLSearchParams()
  if (section.value === 'library' && mode.value === 'reader' && detailOrigin.value !== 'gallery') {
    params.set('origin', detailOrigin.value)
    if (detailOrigin.value === 'artists' && originArtistId.value) params.set('origin_artist', String(originArtistId.value))
  }
  if (section.value === 'library' && mode.value === 'gallery') params.set('page', String(Math.floor(galleryAnchor.value / pageSize.value) + 1))
  if (section.value === 'library' && mode.value === 'reader' && currentWork.value?.revision_id) params.set('revision_id', currentWork.value.revision_id)
  const query: Record<string, string | string[]> = {}
  for (const [name, value] of params) {
    const previous = query[name]
    query[name] = previous === undefined ? value : Array.isArray(previous) ? [...previous, value] : [previous, value]
  }
  const target = router.resolve({path, query})
  if (target.fullPath === browserRoute.fullPath) { internalRoute = ''; return }
  internalRoute = target.fullPath
  void router[push ? 'push' : 'replace'](target).catch(() => { internalRoute = '' })
}
async function initialize(query: string, path = '/', push = false, saveHome = false) {
  clearTimeout(searchTimer)
  newContentRequest.cancel(); checkingNewContent.value = false; newContentAgain = false; revealNewContent = false
  railRequest.cancel()
  if (path === '/settings' || path === '/artists' || /^\/artist\/\d+$/.test(path)) {
    navigation.cancel(); infoRequest.cancel()
    section.value = path === '/settings' ? 'settings' : 'artists'
    artistId.value = path.startsWith('/artist/') ? Number(path.split('/').at(-1)) : null
    ready.value = true; pending.value = false; error.value = ''
    if (push) route(true)
    return
  }
  openingKey = null; adjacentPreload?.abort(); navigationPhase.value = '正在定位作品…'
  const ticket = navigation.begin(); infoRequest.cancel(); pending.value = true; error.value = ''
  failedNavigation = () => { void initialize(query, path, push, saveHome) }
  let preparedVisit: ReadingVisit | null = null, preparedFeed: CursorReading | null = null, preparedNeighbors: CursorNeighbors | null = null
  try {
    const seed = await readLibraryEntry(query, path, pageSize.value, continuous.value, ticket.signal)
    const batch = seed.data, params = new URLSearchParams(seed.query)
    const match = path.match(/^\/illust\/(\d+)$/)
    let openingDetail: WorkDetail | null = null, openingAnchor: Anchor | null = null
    if (match) {
      const id = Number(match[1])
      preparedNeighbors = await api<CursorNeighbors>(url(`/api/works/${id}/cursor-neighbors`, seed.query), {signal: ticket.signal})
      openingDetail = await workDetail(id, new URLSearchParams(query).get('revision_id'), ticket.signal)
      navigationPhase.value = '正在准备图片，当前画面会保留…'
      await preparePageImage(openingDetail.pages[0], ticket.signal)
      openingAnchor = {work_id: id, work_index: 0, page: 0, index: 0, offset: 0}
    } else if (path === '/flow' && batch.total) {
      if (readingOrder.value === 'random') {
        preparedVisit = await startVisit(seed.query, ticket.signal)
      } else preparedFeed = new CursorReading(seed.query)
      const first = (preparedFeed ? await preparedFeed.load(0, 1, ticket.signal) : await visitPages(preparedVisit!, readingOrder.value, 0, 1, ticket.signal)).items[0]
      if (!first) throw new Error('当前范围已变化，请刷新图库')
      openingAnchor = {work_id: first.work_id, work_index: first.work_index, page: first.page, index: first.index, offset: 0}
    }
    if (!ticket.current()) return
    releaseVisit(readingVisit.value); readingVisit.value = preparedVisit; preparedVisit = null
    scope.value = seed.query; total.value = batch.total; totalExact.value = batch.total_exact !== false
    cursorFeed = preparedFeed; ordinaryNeighbors.value = preparedNeighbors; ordinaryCursor.value = preparedNeighbors?.anchor_cursor ?? null; cursorExpired.value = false
    if (totalExact.value) railCounts.value[currentRail()] = batch.total
    if (saveHome) setHomeScope(query)
    section.value = 'library'
    searchText.value = params.get('q') || ''; sortChoice.value = params.get('sort') || 'newest'
    galleryAnchor.value = seed.anchor; gallerySeed.value = path === '/' ? seed : null; galleryOrdinalKnown.value = true
    galleryReset.value++
    mode.value = 'gallery'; ready.value = true; pending.value = false; newContent.value = false
    filterOpen.value = false; infoOpen.value = false; activePage.value = null; currentAnchor.value = null; zoomPage.value = null
    if (openingAnchor) {
      initialAnchor.value = currentAnchor.value = openingAnchor
      readingEnded.value = false; readerReset.value++
      if (openingDetail) {
        const origin = new URLSearchParams(query)
        detailOrigin.value = origin.get('origin') === 'artists' ? 'artists' : origin.get('origin') === 'feed' ? 'feed' : 'gallery'
        originArtistId.value = Number(origin.get('origin_artist')) || null
        ordinaryRankKnown.value = false
        currentWork.value = infoWork.value = openingDetail
        ordinaryIndex.value = desiredIndex.value = openingAnchor.work_index
        galleryAnchor.value = openingAnchor.work_index
        ingestBookmark(openingDetail.id, openingDetail.bookmark); refreshBookmark(openingDetail.id)
        observeCatalogueData(openingDetail.catalogue_revision)
        mode.value = 'reader'
      } else { mode.value = 'feed'; flowOrdinalKnown.value = true; infoWork.value = null }
    }
    route(push)
  } catch (failure) {
    if (ticket.current()) {
      error.value = failure instanceof Error ? failure.message : '图库未能载入'; pending.value = false
      const previous = new URLSearchParams(scope.value)
      searchText.value = previous.get('q') || ''; sortChoice.value = previous.get('sort') || 'newest'
    }
  } finally { releaseVisit(preparedVisit) }
}
async function ensureVisit(signal: AbortSignal) {
  if (readingVisit.value) return readingVisit.value
  navigationPhase.value = '正在准备随机清单，当前画面会保留…'
  const visit = await randomPreparation.take(scope.value, signal)
  if (signal.aborted) { releaseVisit(visit); signal.throwIfAborted() }
  readingVisit.value = visit
  return visit
}
function stopRandomWarm() {
  clearTimeout(randomWarmTimer); randomWarmTimer = 0
  randomWarm?.abort(); randomWarm = null
}
function queueRandomWarm() {
  stopRandomWarm()
  if (section.value !== 'library' || mode.value === 'gallery' || readingOrder.value !== 'sequential' || pending.value || readerActivity.value.phase || document.visibilityState !== 'visible') return
  const query = scope.value, id = activePage.value?.work_id || currentWork.value?.id
  randomWarmTimer = window.setTimeout(() => {
    randomWarmTimer = 0
    const controller = randomWarm = new AbortController()
    void (async () => {
      const visit = readingVisit.value || await randomPreparation.warm(query)
      if (controller.signal.aborted || query !== scope.value) return
      if (id && (randomLocation?.session !== visit.session_id || randomLocation.work !== id)) {
        const anchor = await locateVisit(visit, id, 'random', 0, controller.signal)
        if (!controller.signal.aborted && query === scope.value) randomLocation = {session: visit.session_id, work: id, anchor}
      }
    })().catch(failure => {
      if (!controller.signal.aborted && expiredReadingVisit(failure)) {
        randomPreparation.clear(); randomLocation = null
        if (readingOrder.value === 'sequential' && readingVisit.value) { releaseVisit(readingVisit.value); readingVisit.value = null }
      }
    }).finally(() => { if (randomWarm === controller) randomWarm = null })
  }, 350)
}
async function randomTarget(page: PageData | null, signal: AbortSignal) {
  for (let attempt = 0; ; attempt++) {
    const visit = await ensureVisit(signal)
    try {
      navigationPhase.value = page ? '正在定位当前作品在随机清单中的位置…' : '正在读取随机首件…'
      const found = page ? randomLocation?.session === visit.session_id && randomLocation.work === page.work_id
        ? randomLocation.anchor : await locateVisit(visit, page.work_id, 'random', 0, signal) : null
      const index = found ? found.index + (page?.page || 0) : 0
      navigationPhase.value = '正在读取当前作品页…'
      const batch = await visitPages(visit, 'random', index, 1, signal)
      const first = batch.items[0]
      if (!first || page && (first.work_id !== page.work_id || first.page !== page.page)) throw new Error('当前作品页已不在此阅读范围内，请返回后重试')
      navigationPhase.value = '正在准备当前页原图，当前画面会保留…'
      await preparePageImage(first, signal)
      return {batch, first}
    } catch (failure) {
      signal.throwIfAborted()
      if (attempt || !expiredReadingVisit(failure)) throw failure
      if (readingVisit.value === visit) { releaseVisit(visit); readingVisit.value = null }
      randomPreparation.clear(); randomLocation = null
    }
  }
}
async function openWork(index: number, push = true, selectedRevision?: string | null, identity?: {id: number; cursor?: string; revision_id?: string | null}) {
  if (!total.value && !identity) return
  if (identity) index = Math.max(0, index)
  const key = `${identity?.id || 'index:' + index}:${selectedRevision || identity?.revision_id || ''}`
  if (pending.value && openingKey === key) return
  openingKey = key; navigationPhase.value = '正在定位作品…'
  desiredIndex.value = index
  clearTimeout(searchTimer); railRequest.cancel()
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void openWork(index, push, selectedRevision, identity) }
  try {
    let target = identity
    const difference = index - ordinaryIndex.value
    const direction = difference > 0 ? 'next' : 'previous'
    if (!target) {
      if (!currentWork.value) throw new Error('请先选择一件作品')
      if (!difference) target = {id: currentWork.value.id, cursor: ordinaryCursor.value || undefined, revision_id: currentWork.value.revision_id}
      else if (Math.abs(difference) === 1) {
        const neighbor = ordinaryNeighbors.value?.[direction]
        if (!neighbor) throw new Error(direction === 'next' ? '已到当前范围末尾' : '已到当前范围开头')
        target = {id: neighbor.work_id, cursor: neighbor.cursor, revision_id: neighbor.revision_id}
      }
      else {
        const data = await api<CursorGalleryBatch>(url('/api/library/cursor', scope.value, {limit: Math.min(500, Math.abs(difference) + 1), compact: 1, anchor_work_id: currentWork.value.id, ...(ordinaryCursor.value ? {cursor: ordinaryCursor.value} : {}), direction}), {signal: ticket.signal})
        const cards = data.items
        const selected = difference > 0 ? cards.at(-1) : cards[0]
        if (!selected) throw new Error('当前范围没有下一件作品')
        index = Math.max(0, ordinaryIndex.value + (difference > 0 ? cards.length - 1 : 1 - cards.length))
        target = {id: selected.id, cursor: selected.cursor || data.boundaries?.[difference > 0 ? cards.length - 1 : 0], revision_id: selected.revision_id}
      }
    }
    let neighbors: CursorNeighbors
    try {
      neighbors = await api<CursorNeighbors>(url(`/api/works/${target.id}/cursor-neighbors`, scope.value, {
        ...(target.cursor ? {cursor: target.cursor} : {}),
        ...(!identity && Math.abs(difference) === 1 && ordinaryCursor.value
          ? {from_cursor: ordinaryCursor.value, from_direction: direction} : {}),
      }), {signal: ticket.signal})
      if (target.cursor && ordinaryNeighbors.value && neighbors.generation !== ordinaryNeighbors.value.generation && !neighbors.continuation_rebased) throw new ApiReadError('图库定位需要更新', 'cursor_expired')
    } catch (failure) {
      if (!(failure instanceof ApiReadError) || failure.code !== 'cursor_expired') throw failure
      ticket.signal.throwIfAborted(); navigationPhase.value = '正在恢复当前位置并继续…'
      // Revalidate once by identity in the original frozen scope, then resume
      // the user's original direction. Invalid/hidden anchors remain explicit.
      const anchorId = !identity && difference ? currentWork.value!.id : target.id
      const rebased = await api<CursorNeighbors>(url(`/api/works/${anchorId}/cursor-neighbors`, scope.value), {signal: ticket.signal})
      ordinaryRankKnown.value = false
      if (!identity && difference) {
        const neighbor = rebased[direction]
        if (!neighbor) throw new Error(direction === 'next' ? '已到当前范围末尾' : '已到当前范围开头')
        target = {id: neighbor.work_id, cursor: neighbor.cursor, revision_id: neighbor.revision_id}
        neighbors = await api<CursorNeighbors>(url(`/api/works/${target.id}/cursor-neighbors`, scope.value, {cursor: neighbor.cursor}), {signal: ticket.signal})
      } else neighbors = rebased
    }
    navigationPhase.value = '正在读取作品信息…'
    const detail = await workDetail(target.id, selectedRevision || target.revision_id, ticket.signal)
    navigationPhase.value = '正在准备图片，当前画面会保留…'
    const displayReady = preparePageImage(detail.pages[0], ticket.signal)
    adjacentPreload?.abort()
    await displayReady
    if (!ticket.current()) return
    const previousMode = mode.value
    currentWork.value = infoWork.value = detail; ordinaryIndex.value = desiredIndex.value = index
    ordinaryNeighbors.value = neighbors; ordinaryCursor.value = neighbors.anchor_cursor; cursorExpired.value = false
    if (neighbors.continuation_rebased) ordinaryRankKnown.value = false
    readingEnded.value = false
    ingestBookmark(detail.id, detail.bookmark); refreshBookmark(detail.id)
    initialAnchor.value = {work_id: detail.id, work_index: index, page: 0, index: 0, offset: 0}
    activePage.value = null; currentAnchor.value = initialAnchor.value
    if (previousMode === 'feed') detailOrigin.value = 'feed'
    readerReset.value++; mode.value = 'reader'; section.value = 'library'; filterOpen.value = false; galleryAnchor.value = index
    observeCatalogueData(detail.catalogue_revision)
    preloadAdjacent(neighbors)
    route(push && previousMode !== 'reader')
    await nextTick()
  } catch (failure) {
    if (ticket.current()) { cursorExpired.value = failure instanceof ApiReadError && failure.code === 'cursor_expired'; error.value = failure instanceof Error ? failure.message : '下一件作品未能载入'; desiredIndex.value = ordinaryIndex.value }
  } finally { if (ticket.current()) { pending.value = false; openingKey = null } }
}
function navigate(delta: number) {
  if (zoomPage.value) return
  if (mode.value === 'feed') {
    const stream = reader.value as InstanceType<typeof StreamReader> | null
    stream?.navigateWork(delta)
  } else void openWork(ordinaryIndex.value + delta)
}
interface StreamSource {loader: PageLoader; prepareNeighbor: StreamNeighborLoader; commit: () => void}
function createStreamSource(feed: CursorReading | null, visit: ReadingVisit | null, order: ReadingOrder, query: string, ordinalKnown = flowOrdinalKnown.value): StreamSource {
  const loader: PageLoader = order === 'random' ? (offset, limit, signal) => visitPages(visit!, order, offset, limit, signal)
    : (offset, limit, signal) => feed!.load(offset, limit, signal)
  const commit = () => {
    if (scope.value !== query || mode.value !== 'feed') return
    cursorFeed = feed
    flowOrdinalKnown.value = order === 'random' || ordinalKnown
  }
  const prepareNeighbor: StreamNeighborLoader = async (page, direction, signal) => {
    if (order === 'sequential' && direction < 0 && needsIdentityPrevious(page)) {
      let nearby: CursorNeighbors
      try { nearby = await api<CursorNeighbors>(url('/api/works/' + page.work_id + '/cursor-neighbors', query,
        page.work_cursor ? {cursor: page.work_cursor} : {}), {signal}) }
      catch (failure) {
        if (!(failure instanceof ApiReadError) || failure.code !== 'cursor_expired' || !page.work_cursor) throw failure
        signal.throwIfAborted()
        nearby = await api<CursorNeighbors>(url('/api/works/' + page.work_id + '/cursor-neighbors', query), {signal})
      }
      if (!nearby.previous) return null
      const previous = nearby.previous, start = Math.max(0, previous.page_count - 4)
      const batch = await api<CursorPageBatch>(url('/api/library/cursor/feed', query, {
        cursor: previous.cursor, anchor_work_id: previous.work_id, page: start, index: start, work_index: 0,
        limit: Math.min(4, previous.page_count),
      }), {signal})
      signal.throwIfAborted()
      const target = batch.items.find(item => item.work_id === previous.work_id && item.page === previous.page_count - 1)
      if (!target) throw new Error('上一件作品已移出这个范围')
      const fork = new CursorReading(query); fork.seed(batch)
      const source = createStreamSource(fork, visit, order, query, false)
      return {batch, target, ...source, tail: true}
    }
    const index = direction > 0 ? page.has_next_work === false ? null : page.next_work_index : page.previous_work_index
    if (index == null || index < 0) return null
    const fork = order === 'sequential' ? feed!.fork() : null
    const source = createStreamSource(fork, visit, order, query, ordinalKnown)
    let batch = await source.loader(index, 4, signal)
    signal.throwIfAborted()
    let target = batch.items.find(item => item.index === index)
    if (!target) return null
    if (direction < 0) {
      const last = target.index - target.page + target.page_count - 1
      const start = Math.max(target.index - target.page, last - 3)
      batch = await source.loader(start, last - start + 1, signal)
      signal.throwIfAborted()
      target = batch.items.find(item => item.index === last)
      if (!target) throw new Error('上一件作品的末页暂时无法读取')
    }
    const candidate: StreamCandidate = {batch, target, ...source, tail: direction < 0}
    return candidate
  }
  return {loader, prepareNeighbor, commit}
}
const streamSource = computed(() => {
  readerReset.value
  return createStreamSource(cursorFeed, readingVisit.value, readingOrder.value, scope.value)
})
async function enterFeed(push = true, fromCurrent = true) {
  if (!total.value) return
  clearTimeout(searchTimer); railRequest.cancel()
  navigationPhase.value = '正在打开流式阅览…'
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void enterFeed(push) }
  try {
    let prepared: CursorReading | null = null
    const detail = currentWork.value, detailPage = detail?.pages.find(page => page.index === (currentAnchor.value?.page || 0)) || detail?.pages[0]
    const selected: PageData | null = fromCurrent && mode.value === 'reader' ? activePage.value || (detail && detailPage ? {
      ...detailPage, work_id: detail.id, work_index: 0, page: detailPage.index, page_count: detail.display_page_count,
      next_work_index: detail.display_page_count, title: detail.title, artist_name: detail.artist_name,
      artist_id: detail.artist_id, pixiv_id: detail.pixiv_id, type: detail.type, x_restrict: detail.x_restrict, revision_id: detail.revision_id,
    } : null) : null
    let first: PageData | undefined, selectedBatch: PageBatch | null = null
    if (readingOrder.value === 'random') { const result = await randomTarget(selected, ticket.signal); first = result.first; selectedBatch = result.batch }
    else {
      prepared = new CursorReading(scope.value)
      if (selected) prepared.recover({...selected, index: selected.page, work_index: 0, next_work_index: selected.page_count})
      selectedBatch = await prepared.load(selected?.page || 0, 4, ticket.signal)
      first = selectedBatch.items[0]
      if (selected && (first?.work_id !== selected.work_id || first.page !== selected.page)) throw new Error('当前作品页已不在此阅读范围内，请返回后重试')
      await preparePageImage(first, ticket.signal)
    }
    if (!first) throw new Error('当前范围已变化，请刷新图库')
    const found = {work_id: first.work_id, page: first.page, index: first.index, work_index: first.work_index}
    if (!ticket.current()) return
    cursorFeed = prepared; flowOrdinalKnown.value = !selected || readingOrder.value === 'random'; cursorExpired.value = false
    initialAnchor.value = {...found, offset: 0}; currentAnchor.value = initialAnchor.value
    readerReset.value++
    readingEnded.value = false
    activePage.value = null; infoWork.value = null; infoOpen.value = false; filterOpen.value = false
    mode.value = 'feed'; section.value = 'library'
    if (selectedBatch) streamInitialOpening.value = {sessionKey: sessionKey.value, batch: selectedBatch}
    route(push)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '流式阅览未能打开' }
  finally { if (ticket.current()) pending.value = false }
}
async function recoverFeed(page: PageData | null) {
  if (readingOrder.value !== 'sequential') return
  navigationPhase.value = '正在恢复当前位置…'
  const ticket = navigation.begin(); pending.value = true
  try {
    const feed = new CursorReading(scope.value); feed.recover(page || undefined)
    const first = (await feed.load(page?.index || 0, 1, ticket.signal)).items[0]
    if (!ticket.current()) return
    cursorFeed = feed; flowOrdinalKnown.value = false; initialAnchor.value = first ? {work_id: first.work_id, work_index: first.work_index, page: first.page, index: first.index, offset: 0} : null
    activePage.value = null; readerReset.value++; error.value = ''
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '当前位置已移出范围，请返回图库' }
  finally { if (ticket.current()) pending.value = false }
}
function gallery(push = true) {
  void initialize(freshLibraryQuery(section.value === 'library' ? scope.value : homeScope.value, true), '/', push)
}
const loader = computed<PageLoader>(() => {
  const visit = readingVisit.value, order = readingOrder.value
  readerReset.value // Rebind the bounded cursor loader after explicit recovery.
  if (mode.value === 'feed') {
    const feed = cursorFeed!
    return order === 'sequential' ? (offset, limit, signal) => feed.load(offset, limit, signal)
      : (offset, limit, signal) => visitPages(visit!, order, offset, limit, signal)
  }
  const work = currentWork.value!, workIndex = ordinaryIndex.value
  return async (offset, limit, signal) => {
    // The prepared opening response already contains a complete one-page work.
    // Missing media still reads again so background repair can appear in place.
    const reusable = offset === 0 && work.display_page_count <= work.pages.length && work.pages.every(page => page.src && page.state === 'verified')
    const detail = reusable ? work : await api<WorkDetail>(workPath(work.id, work.revision_id, offset, limit), {signal})
    return {offset, total: detail.display_page_count, catalogue_revision: detail.catalogue_revision, items: detail.pages.map(page => ({...page, work_id: work.id, work_index: workIndex,
      page: page.index, page_count: detail.display_page_count, title: detail.title, artist_name: detail.artist_name, revision_id: detail.revision_id,
      artist_id: detail.artist_id, pixiv_id: detail.pixiv_id, type: detail.type, x_restrict: detail.x_restrict,
      recovery: detail.recovery, availability: detail.availability, archive: detail.archive, processing: detail.processing, source_update: detail.source_update, next_work_index: detail.display_page_count}))}
  }
})
function focused(page: PageData, anchor: Anchor) {
  if (streamInitialOpening.value?.sessionKey === sessionKey.value) streamInitialOpening.value = null
  if (mode.value === 'feed' && readingOrder.value === 'sequential') cursorFeed?.remember([page])
  const changedWork = activePage.value?.work_id !== page.work_id
  if (mode.value === 'feed' && (page.round || 0) > (activePage.value?.round || 0)) message('新一轮随机阅览')
  if (page.bookmark) ingestBookmark(page.work_id, page.bookmark)
  activePage.value = page; currentAnchor.value = anchor
  if (changedWork) refreshBookmark(page.work_id)
  if (infoOpen.value && infoWork.value?.id !== page.work_id) void loadInfo(page.work_id)
}
async function loadInfo(id: number, refreshRemote = true) {
  const ticket = infoRequest.begin()
  const revision = mode.value === 'reader' ? currentWork.value?.revision_id : activePage.value?.revision_id
  try { const work = await workDetail(id, revision, ticket.signal); if (ticket.current()) { infoWork.value = work; ingestBookmark(id, work.bookmark); if (refreshRemote) refreshBookmark(id) } }
  catch (failure) { if (ticket.current()) message(failure instanceof Error ? failure.message : '信息未能载入', true) }
}
async function task(kind: string, workId?: number) {
  try {
    const result = await write<{operation_id: string; created: boolean}>('/api/tasks', {kind, ...(workId ? {work_ids: [workId]} : {})})
    const label = ({reconcile: '检查并补齐图库', check: '检查新作品', repair: '补下载', verify: '全量核验本地文件', backfill: '补回动图原包', metadata: '刷新作品信息', bookmarks: '同步收藏状态'} as Record<string, string>)[kind] || '任务'
    message(result.created ? `${label}已提交` : `已有相同的${label}任务`)
    await taskPanel.value?.showTask(result.operation_id)
  } catch (failure) { message(failure instanceof Error ? failure.message : '任务未能提交', true) }
}
async function nextIdentity() {
  if (mode.value === 'reader' && currentWork.value) {
    const nearby = await api<CursorNeighbors>(url(`/api/works/${currentWork.value.id}/cursor-neighbors`, scope.value, ordinaryCursor.value ? {cursor: ordinaryCursor.value} : {}))
    return nearby.next ? {id: nearby.next.work_id, round: 0} : null
  }
  if (mode.value === 'feed' && activePage.value) {
    const page = activePage.value
    if (readingOrder.value === 'sequential') {
      const nearby = await api<CursorNeighbors>(url(`/api/works/${page.work_id}/cursor-neighbors`, scope.value, page.work_cursor ? {cursor: page.work_cursor} : {}))
      return nearby.next ? {id: nearby.next.work_id, round: 0} : null
    }
    const batch = await visitPages(readingVisit.value!, readingOrder.value, page.next_work_index, 1, new AbortController().signal)
    const next = batch.items[0]
    return next && next.work_id !== page.work_id ? {id: next.work_id, round: next.round || 0} : null
  }
  return null
}
async function continueAfterRemoval(id: number, next: {id: number; round: number} | null) {
  invalidateWorkCache(id); infoOpen.value = false
  total.value = Math.max(0, total.value - 1)
  if (readingVisit.value) await write(`/api/reading-sessions/${readingVisit.value.session_id}/exclude/${id}`, {})
  if (!next) { readingEnded.value = true; return }
  if (mode.value === 'reader') {
    ordinaryCursor.value = null
    await openWork(ordinaryIndex.value, false, undefined, {id: next.id})
    return
  }
  if (readingOrder.value === 'random') {
    const ticket = navigation.begin()
    const found = await locateVisit(readingVisit.value!, next.id, 'random', next.round, ticket.signal)
    if (!ticket.current()) return
    initialAnchor.value = {...found, offset: 0}; activePage.value = null; readerReset.value++
  } else {
    const feed = new CursorReading(scope.value), ticket = navigation.begin()
    feed.recover({...activePage.value!, work_id: next.id, page: 0, page_count: 1, index: 0, work_index: 0, next_work_index: 1, work_cursor: undefined})
    const first = (await feed.load(0, 1, ticket.signal)).items[0]
    if (!ticket.current() || !first) return
    cursorFeed = feed; flowOrdinalKnown.value = false; initialAnchor.value = {work_id: first.work_id, work_index: first.work_index, page: first.page, index: first.index, offset: 0}
    activePage.value = null; readerReset.value++
  }
}
async function hidden() {
  if (!infoWork.value) return
  const work = infoWork.value
  const hiding = !work.is_hidden
  try {
    const next = work.is_hidden ? null : await nextIdentity()
    await write(`/api/works/${work.id}/preferences`, {hidden: !work.is_hidden}, 'PATCH')
    if (!work.is_hidden) await continueAfterRemoval(work.id, next)
    else { work.is_hidden = false; invalidateWorkCache(work.id) }
    message(hiding ? '已隐藏作品' : '已取消隐藏')
  }
  catch (failure) { message(failure instanceof Error ? failure.message : '操作失败', true) }
}
async function bookmarkSync() { if (infoWork.value) try { await syncBookmark(infoWork.value.id) } catch (failure) { message(failure instanceof Error ? failure.message : '收藏状态未能核对', true) } }
async function remove() {
  if (!infoWork.value) return
  removePrompt.value = false
  try {
    const id = infoWork.value.id, next = await nextIdentity()
    const result = await write<{operation_id: string}>('/api/works/delete', {work_ids: [id], confirmed: true})
    await continueAfterRemoval(id, next); await taskPanel.value?.showTask(result.operation_id)
  } catch (failure) { message(failure instanceof Error ? failure.message : '删除未成功', true) }
}
async function changeReadingOrder(value: string) {
  const order: ReadingOrder = value === 'random' ? 'random' : 'sequential'
  if (order === readingOrder.value) return
  const page = activePage.value, offset = currentAnchor.value?.offset || 0, ticket = navigation.begin()
  pending.value = true; error.value = ''; navigationPhase.value = '正在准备阅览顺序，当前画面会保留…'
  failedNavigation = () => { void changeReadingOrder(value) }
  try {
    let anchor: Anchor | null = null, prepared: CursorReading | null = null, selectedBatch: PageBatch | null = null
    if (order === 'random') {
      const result = await randomTarget(page, ticket.signal), first = result.first
      selectedBatch = result.batch
      anchor = {work_id: first.work_id, work_index: first.work_index, index: first.index, page: first.page, offset}
    } else {
      prepared = new CursorReading(scope.value)
      if (page) prepared.recover({...page, index: page.page, work_index: 0, next_work_index: page.page_count})
      selectedBatch = await prepared.load(page?.page || 0, 4, ticket.signal)
      const first = selectedBatch.items[0]
      if (!first || page && (first.work_id !== page.work_id || first.page !== page.page)) throw new Error('当前作品页已不在此阅读范围内，请返回后重试')
      await preparePageImage(first, ticket.signal)
      anchor = {work_id: first.work_id, work_index: first.work_index, index: first.index, page: first.page, offset}
    }
    if (!ticket.current()) return
    cursorFeed = prepared; flowOrdinalKnown.value = order === 'random'; readingOrder.value = order
    try { localStorage.setItem('pixiv-local-reading-order', order) } catch { /* optional preference */ }
    initialAnchor.value = anchor
    readerReset.value++; readingEnded.value = false
    if (selectedBatch) streamInitialOpening.value = {sessionKey: sessionKey.value, batch: selectedBatch}
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '切换顺序失败' }
  finally { if (ticket.current()) pending.value = false }
}
async function earlierRevisions() {
  const work = currentWork.value
  if (!work?.revisions_has_more || revisionLoading.value) return
  revisionLoading.value = true
  try {
    const result = await api<{items: NonNullable<WorkDetail['revisions']>; has_more: boolean; before: number | null}>(url(`/api/works/${work.id}/revisions`, '', {...(work.revisions_before != null ? {before_ordinal: work.revisions_before} : {}), limit: 100}))
    if (currentWork.value !== work) return
    work.revisions = [...new Map([...(work.revisions || []), ...result.items].map(item => [item.id, item])).values()]; work.revisions_has_more = result.has_more; work.revisions_before = result.before
  } catch (failure) { message(failure instanceof Error ? failure.message : '历史版本暂时无法读取', true) }
  finally { revisionLoading.value = false }
}
async function selectRevision(id: string) {
  if (currentWork.value) await openWork(ordinaryIndex.value, false, id)
}
async function bookmarkPage(page: PageData) {
  if (page.bookmark) ingestBookmark(page.work_id, page.bookmark)
  try { await addBookmark(page.work_id) }
  catch (failure) { message(failure instanceof Error ? failure.message : '收藏未完成', true) }
}
function managePage(page: PageData) {
  activePage.value = page; infoOpen.value = true; void loadInfo(page.work_id)
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape' && mobileMenuOpen.value) { closeMobileMenu(); return }
  if (section.value !== 'library' || event.defaultPrevented || event.isComposing || composing.value || event.ctrlKey || event.metaKey || event.altKey || zoomPage.value || mobileMenuOpen.value || removePrompt.value ||
      (event.target as Element).closest('input,textarea,select,[contenteditable],[role="dialog"],[data-no-navigation]')) return
  if (event.key === 'Escape') { if (infoOpen.value) infoOpen.value = false; else if (filterOpen.value) filterOpen.value = false; else if (mode.value !== 'gallery') gallery(); return }
  if (infoOpen.value || streamPanelOpen.value) return
  if (mode.value !== 'gallery' && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    event.preventDefault(); if (event.repeat && Date.now() - lastKeyTime < 160) return
    lastKeyTime = Date.now(); navigate(event.key === 'ArrowLeft' ? -1 : 1)
  } else if (mode.value !== 'gallery' && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
    event.preventDefault(); if (event.repeat && Date.now() - lastKeyTime < 160) return
    lastKeyTime = Date.now(); reader.value?.stepPage(event.key === 'ArrowUp' ? -1 : 1)
  } else if (mode.value !== 'gallery' && ['PageUp', 'PageDown'].includes(event.key)) {
    event.preventDefault(); reader.value?.scrollBy((event.key === 'PageUp' ? -1 : 1) * window.innerHeight * 0.7)
  }
}
function setHomeScope(query: string) {
  const params = new URLSearchParams(query)
  for (const name of ['ceiling', 'as_of', 'page', 'offset', 'limit', 'page_offset', 'mode', 'origin', 'origin_artist']) params.delete(name)
  homeScope.value = params.toString()
  try { localStorage.setItem('pixiv-local-home-scope', homeScope.value) } catch { /* optional */ }
}
function galleryFocused(index: number) { galleryAnchor.value = index }
function filter(query: string) { void initialize(query, '/', true, true) }
function refresh() { const params = new URLSearchParams(scope.value); params.delete('ceiling'); params.delete('as_of'); void initialize(params.toString()) }
function retryNavigation() {
  if (failedNavigation) failedNavigation(); else refresh()
}
function galleryTotal(count: number, exact: boolean) { total.value = count; totalExact.value = exact }
function recoveredGallery(seed: GallerySeed) { galleryOrdinalKnown.value = false; scope.value = seed.query; galleryAnchor.value = seed.anchor; gallerySeed.value = seed; galleryReset.value++; total.value = seed.data.total; totalExact.value = seed.data.total_exact !== false; route(false) }
function changeRail(rail: 'all' | 'recent' | 'favorites') {
  const params = new URLSearchParams(scope.value)
  for (const name of ['ceiling', 'as_of', 'page', 'favorites', 'recent']) params.delete(name)
  if (rail !== 'all') params.set(rail, '1')
  void initialize(params.toString(), '/', true, true)
}
function openFromGallery(index: number, work: WorkCard) { ordinaryRankKnown.value = galleryOrdinalKnown.value; detailOrigin.value = 'gallery'; void openWork(index, true, undefined, {id: work.id, cursor: work.cursor, revision_id: work.revision_id}) }
function tagFilter(tag: string) {
  const params = new URLSearchParams(homeScope.value)
  params.delete('ceiling'); params.delete('as_of'); params.delete('favorites'); params.delete('recent'); params.delete('tags_exclude'); writeTagValues(params, [tag])
  void initialize(params.toString(), '/', true, true)
}
async function openFolder(id: number) {
  try { await api(`/illust/${id}/open-folder`, {method: 'POST'}) }
  catch (failure) { message(failure instanceof Error ? failure.message : '本地文件夹未能打开', true) }
}
async function rate(value: number) {
  if (!infoWork.value) return
  try { await write(`/api/works/${infoWork.value.id}/preferences`, {rating: value}, 'PATCH'); infoWork.value.rating = value; message('本地评分已保存') }
  catch (failure) { message(failure instanceof Error ? failure.message : '评分未能保存', true) }
}
function currentRail() { const params = new URLSearchParams(scope.value); return params.has('favorites') ? 'favorites' : params.has('recent') ? 'recent' : 'all' }
function search() {
  if (composing.value) return
  clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    const params = new URLSearchParams(scope.value)
    for (const name of ['ceiling', 'as_of', 'page']) params.delete(name)
    if (searchText.value.trim()) params.set('q', searchText.value.trim()); else params.delete('q')
    void initialize(params.toString(), '/', false, true)
  }, 280)
}
function sort() {
  const params = new URLSearchParams(scope.value)
  for (const name of ['ceiling', 'as_of', 'page']) params.delete(name)
  params.set('sort', sortChoice.value); void initialize(params.toString(), '/', true, true)
}
function showArtists(id: number | null = null) { clearTimeout(searchTimer); adjacentPreload?.abort(); navigation.cancel(); railRequest.cancel(); infoRequest.cancel(); pending.value = false; error.value = ''; section.value = 'artists'; artistId.value = id; filterOpen.value = false; infoOpen.value = false; route(true) }
function showSettings() { clearTimeout(searchTimer); adjacentPreload?.abort(); navigation.cancel(); railRequest.cancel(); infoRequest.cancel(); pending.value = false; error.value = ''; section.value = 'settings'; filterOpen.value = false; infoOpen.value = false; route(true) }
function openArtistGallery(id: number) {
  detailOrigin.value = 'gallery'; originArtistId.value = null
  const params = new URLSearchParams({artist_ids: String(id), sort: sortChoice.value})
  void initialize(params.toString(), '/', true)
}
async function artistWorks(id: number) {
  detailOrigin.value = 'artists'; originArtistId.value = id
  const params = new URLSearchParams({artist_ids: String(id), sort: sortChoice.value})
  await initialize(params.toString(), '/', true)
}
async function artistWork(id: number, workId: number) {
  await artistWorks(id)
  if (section.value !== 'library' || new URLSearchParams(scope.value).get('artist_ids') !== String(id)) return
  ordinaryRankKnown.value = false
  await openWork(0, true, undefined, {id: workId})
}
function backFromDetail() {
  if (detailOrigin.value === 'artists') { showArtists(originArtistId.value); return }
  if (detailOrigin.value === 'feed') { void enterFeed(); return }
  gallery()
}
function brandHome() { detailOrigin.value = 'gallery'; void initialize(homeScope.value, '/', true) }
function foreground() { queueFilterWarm(); resumeBookmarkPolling(); if (hiddenCatalogueUpdate) void checkForNewWorks(); if (section.value === 'library' && mode.value !== 'gallery') { const id = activePage.value?.work_id || currentWork.value?.id; if (id) refreshBookmark(id) } }
function chooseTheme(theme: 'dark' | 'light', _event: MouseEvent) {
  document.body.dataset.theme = theme
  try { localStorage.setItem('pixiv-local-theme', theme) } catch { /* optional */ }
}
watch(() => browserRoute.fullPath, path => {
  if (path === internalRoute) { internalRoute = ''; return }
  void initialize(browserRoute.fullPath.slice(browserRoute.path.length).replace(/^\?/, ''), browserRoute.path, false)
})
watch([scope, section], () => {
  stopRandomWarm(); randomPreparation.clear(); randomLocation = null
  streamInitialOpening.value = null
  if (section.value !== 'library') { releaseVisit(readingVisit.value); readingVisit.value = null }
})
watch([scope, section, mode, pending, readingOrder, () => readerActivity.value.phase, () => activePage.value?.work_id, () => currentWork.value?.id], queueRandomWarm, {flush: 'post'})
watch(sessionKey, key => { if (streamInitialOpening.value?.sessionKey !== key) streamInitialOpening.value = null })
onMounted(async () => {
  try { document.body.dataset.theme = localStorage.getItem('pixiv-local-theme') === 'light' ? 'light' : 'dark' } catch { document.body.dataset.theme = 'dark' }
  try { sidebarCollapsed.value = localStorage.getItem('pixiv-local-sidebar-collapsed') === '1' } catch { /* optional preference */ }
  try { setHomeScope(localStorage.getItem('pixiv-local-home-scope') || '') } catch { /* optional */ }
  if (location.pathname === '/' && location.search) setHomeScope(location.search.replace(/^\?/, ''))
  try {
    const status = await api<StartupStatus>('/api/startup')
    if (status.state !== 'ready') { startup.value = status; return }
  } catch { /* A normal connection failure remains visible in the library. */ }
  try {
    const defaults = await api<{page_size: number; pagination_mode: string}>('/api/status')
    if ([50, 100, 200, 500].includes(defaults.page_size)) pageSize.value = defaults.page_size
    continuous.value = defaults.pagination_mode === 'scroll'
    const savedSize = localStorage.getItem('pixiv-local-page-size')
    if (savedSize === 'scroll') continuous.value = true
    else if (savedSize && [50, 100, 200, 500].includes(Number(savedSize))) { continuous.value = false; pageSize.value = Number(savedSize) }
    readingOrder.value = localStorage.getItem('pixiv-local-reading-order') === 'random' ? 'random' : 'sequential'
  } catch { /* Defaults do not block local browsing. */ }
  void initialize(location.pathname === '/' && !location.search ? homeScope.value : location.search, location.pathname)
  window.addEventListener('keydown', key)
  window.addEventListener('focus', foreground)
  window.addEventListener('archive-updated', archiveUpdated)
  document.addEventListener('visibilitychange', catalogueVisibilityChanged)
})
onBeforeUnmount(() => { stopRandomWarm(); randomPreparation.clear(); adjacentPreload?.abort(); navigation.cancel(); infoRequest.cancel(); statusRequest.cancel(); newContentRequest.cancel(); railRequest.cancel(); releaseVisit(readingVisit.value); clearTimeout(toastTimer); clearTimeout(searchTimer); window.removeEventListener('keydown', key); window.removeEventListener('focus', foreground); window.removeEventListener('archive-updated', archiveUpdated); document.removeEventListener('visibilitychange', catalogueVisibilityChanged) })
function changePageSize(value: string) {
  continuous.value = value === 'scroll'
  if (!continuous.value) pageSize.value = Number(value)
  try { localStorage.setItem('pixiv-local-page-size', value) } catch { /* optional preference */ }
}
function refreshCurrentWork() {
  const target = mode.value === 'reader' ? currentWork.value : null
  if (!target) return
  const ticket = statusRequest.begin()
  void workDetail(target.id, target.revision_id, ticket.signal, true).then(work => {
    if (ticket.current() && mode.value === 'reader' && currentWork.value === target && !isOlderCatalogue(work.catalogue_revision, target.catalogue_revision)) {
      currentWork.value = work
      if (infoOpen.value && infoWork.value?.id === work.id) infoWork.value = work
      observeCatalogueData(work.catalogue_revision)
    }
  }).catch(() => { /* The current image and navigation stay usable. */ })
}
function applyFreshScope(data: GalleryBatch) {
  const params = new URLSearchParams(scope.value)
  params.set('ceiling', String(data.ceiling)); params.set('as_of', data.as_of)
  galleryAnchor.value = 0; galleryReset.value++
  total.value = data.total; totalExact.value = data.total_exact !== false; scope.value = params.toString(); newContent.value = false
  route(false)
}
async function checkForNewWorks(reveal = false) {
  if (section.value !== 'library' || !ready.value) return
  if (document.hidden) { hiddenCatalogueUpdate = true; return }
  hiddenCatalogueUpdate = false
  revealNewContent ||= reveal
  if (checkingNewContent.value) { newContentAgain = true; return }
  const ticket = newContentRequest.begin(), previousScope = scope.value
  const params = new URLSearchParams(previousScope)
  const ceiling = Number(params.get('ceiling')) || 0
  params.delete('ceiling'); params.delete('as_of')
  checkingNewContent.value = true
  try {
    const fresh = normalizeGalleryBatch(await api<CursorGalleryBatch>(url('/api/library/cursor', params.toString(), {limit: 1, compact: 1}), {signal: ticket.signal}))
    if (!ticket.current() || scope.value !== previousScope) return
    if (hasNewWorks(fresh, ceiling, total.value)) newContent.value = true
    if (mode.value === 'gallery' && (revealNewContent || newContent.value && !pending.value && galleryView.value?.isAtBeginning())) {
      revealNewContent = false
      applyFreshScope(fresh)
    }
  } catch {
    if (ticket.current() && revealNewContent) { revealNewContent = false; message('暂时无法查看新作品，当前位置已保留', true) }
  } finally {
    if (ticket.current()) {
      checkingNewContent.value = false
      if (newContentAgain) { newContentAgain = false; void checkForNewWorks() }
    }
  }
}
function archiveUpdated() {
  invalidateFilterCatalogue()
  invalidateWorkCache()
  refreshCurrentWork()
  if (mode.value === 'feed' && infoOpen.value && infoWork.value) void loadInfo(infoWork.value.id, false)
  void checkForNewWorks()
}
function catalogueVisibilityChanged() { if (document.hidden) stopRandomWarm(); else { queueRandomWarm(); if (hiddenCatalogueUpdate) void checkForNewWorks() } }
function updated() { queueCatalogueUpdate() }
</script>
<template>
  <UpgradeView v-if="startup" :status="startup" @ready="upgraded" />
  <div v-else class="app-shell" :class="{'sidebar-collapsed': sidebarCollapsed, 'stream-immersive': section === 'library' && mode === 'feed'}" :inert="zoomPage ? true : undefined">
    <header class="app-header" :class="{'is-collapsed': sidebarCollapsed, 'is-open': mobileMenuOpen}">
      <div class="mobile-head"><button ref="mobileMenuButton" class="menu-toggle" :aria-expanded="mobileMenuOpen" aria-controls="app-sidebar" :aria-label="mobileMenuOpen ? '关闭菜单' : '打开菜单'" @click="mobileMenuOpen ? closeMobileMenu() : mobileMenuOpen = true"><Icon :name="mobileMenuOpen ? 'close' : 'menu'" /></button><button class="brand" @click="navHome" aria-label="返回我的图库"><BrandMark /><span><span class="brand-pixiv">Pixiv</span>Tracker</span></button><span class="mobile-head-spacer" /></div>
      <button v-if="mobileMenuOpen" class="sidebar-scrim" aria-label="关闭菜单" @click="closeMobileMenu" />
      <div id="app-sidebar" class="sidebar-body">
        <div class="sidebar-head"><button class="brand" @click="navHome" aria-label="返回我的图库" title="返回我的图库"><BrandMark /><span class="sidebar-label"><span class="brand-pixiv">Pixiv</span>Tracker</span></button><button class="sidebar-toggle" :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar"><Icon :name="sidebarCollapsed ? 'right' : 'left'" /></button><button class="mobile-close" aria-label="关闭菜单" @click="closeMobileMenu"><Icon name="close" /></button></div>
        <label v-if="!sidebarCollapsed || mobileMenuOpen" class="search-field side-search"><Icon name="search" /><input v-model="searchText" type="search" placeholder="搜索作品、画师或标签" aria-label="搜索作品、画师或标签" @compositionstart="composing = true" @compositionend="composing = false; search()" @input="search"></label><button v-else class="side-search-trigger" aria-label="展开侧栏并搜索" title="搜索" @click="toggleSidebar"><Icon name="search" /></button>
        <nav class="sidebar-nav" aria-label="主导航"><button :class="{selected: section === 'library' && mode !== 'feed'}" title="作品" @click="navHome"><Icon name="grid" /><span class="sidebar-label">作品</span></button><button :class="{selected: section === 'artists'}" title="画师" @click="navArtists"><Icon name="artists" /><span class="sidebar-label">画师</span></button><button :class="{selected: section === 'library' && mode === 'feed'}" title="流式阅览" :disabled="section === 'library' && !total" @click="navFeed"><Icon name="flow" /><span class="sidebar-label">流式阅览</span></button><button :class="{selected: section === 'settings'}" title="设置" @click="navSettings"><Icon name="settings" /><span class="sidebar-label">设置</span></button></nav>
        <div class="sidebar-divider" /><div class="sidebar-group-label sidebar-label">图库范围</div><nav class="sidebar-nav sidebar-scopes" aria-label="图库范围"><button :class="{selected: section === 'library' && currentRail() === 'all'}" :aria-pressed="currentRail() === 'all'" title="全部作品" @click="navRail('all')"><Icon name="grid" /><span class="sidebar-label">全部作品</span><small class="sidebar-label">{{ railCounts.all ?? '—' }}</small></button><button :class="{selected: section === 'library' && currentRail() === 'recent'}" :aria-pressed="currentRail() === 'recent'" title="最近存入" @click="navRail('recent')"><Icon name="clock" /><span class="sidebar-label">最近存入</span><small class="sidebar-label">{{ railCounts.recent ?? '—' }}</small></button><button :class="{selected: section === 'library' && currentRail() === 'favorites'}" :aria-pressed="currentRail() === 'favorites'" title="收藏" @click="navRail('favorites')"><Icon name="heart" /><span class="sidebar-label">收藏</span><small class="sidebar-label">{{ railCounts.favorites ?? '—' }}</small></button></nav>
      </div>
    </header>
    <TaskPanel ref="taskPanel" @updated="updated" />
    <template v-if="section === 'library'">
      <template v-if="mode === 'gallery'">
        <div class="gallery-layout" :inert="mobileMenuOpen ? true : undefined">
          <main class="gallery-main"><div class="gallery-controls"><div class="gallery-heading"><h1>{{ heading }}</h1><span>{{ totalExact ? total + ' 件' : '数量按需统计' }} </span><button class="text-link" title="按需遍历当前筛选，计算精确数量及侧栏统计" @click="updateRailCounts(scope)">统计数量</button><button v-if="newContent" class="text-link new-work-entry" :disabled="checkingNewContent" @click="checkForNewWorks(true)">有新作品，查看</button></div><div class="gallery-control-actions"><select v-model="sortChoice" aria-label="图库排序" @change="sort"><option value="newest">最新投稿</option><option value="oldest">最早投稿</option><option value="added">最近存入</option><option value="title">作品名称</option></select><button class="softbtn" :aria-expanded="filterOpen" :title="filterSummary" @click="filterOpen = true"><Icon name="filter" />筛选</button><button class="check-button" aria-label="检查新作品" @click="task('check')"><Icon name="refresh" /><span>检查新作品</span></button></div></div>
            <div v-if="filterSummary !== '全部画师 · 全部类型'" class="active-filters"><span>{{ filterSummary }}</span><button class="text-link" @click="filterOpen = true">调整</button></div>
            <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">{{ cursorExpired ? '重新定位后继续' : '重试' }}</button></div>
            <div v-if="pending" class="gallery-navigation-status" role="status">{{ navigationPhase }} <button @click="cancelNavigation">取消</button></div>
            <GalleryView v-if="ready" ref="galleryView" :query="scope" :start-index="galleryAnchor" :reset-key="galleryReset" :page-size="pageSize" :continuous="continuous" :initial-batch="gallerySeed" :exact-total="totalExact ? total : undefined" @seed-consumed="gallerySeed = null" @open="openFromGallery" @artist="showArtists" @message="message($event, true)" @anchor="galleryFocused" @total="galleryTotal" @recovered="recoveredGallery" @page="galleryAnchor = $event * pageSize; route(false)" /><div v-else class="empty">{{ error ? '图库尚未载入，请重试' : '正在打开本地图库…' }}</div>
          </main></div>
      </template>
      <template v-else><main class="reader-layout" :class="{'stream-layout': mode === 'feed'}" :inert="mobileMenuOpen ? true : undefined">
        <StreamChrome v-if="mode === 'feed'" :page="activePage" :order="readingOrder" :total="total" :total-exact="totalExact" :ordinal-known="flowOrdinalKnown" :activity="readingActivity" :ended="readingEnded" :inactive="infoOpen" @back="gallery()" @order="changeReadingOrder" @artist="openArtistGallery" @tag="tagFilter" @manage="activePage && managePage(activePage)" @navigate="navigate" @message="message($event, true)" @retry="retryReading" @cancel="error = ''; cancelNavigation()" @panel="streamPanelOpen = $event">
          <StreamReader ref="reader" :loader="streamSource.loader" :prepare-neighbor="streamSource.prepareNeighbor" :commit-source="streamSource.commit" :preload-paused="pending" :initial-opening="streamInitialOpening" :session-key="sessionKey" :initial="initialAnchor" :inactive="streamPanelOpen || infoOpen || removePrompt" @focus="focused" @activity="readerActivity = $event" @recover="recoverFeed" @navigate="navigate" />
        </StreamChrome>
        <div v-if="mode !== 'feed' && readingEnded" class="empty reading-finished"><p>已到当前范围末尾</p><button @click="gallery()">返回图库</button></div><div v-if="mode !== 'feed'" class="reader-stage"><Transition name="work"><PageReader v-if="!readingEnded" :key="sessionKey" ref="reader" mode="reader" :loader="loader" :session-key="sessionKey" :initial="initialAnchor" :inactive="!!zoomPage" @focus="focused" @activity="readerActivity = $event" @recover="recoverFeed" @navigate="navigate" @artist="openArtistGallery" @zoom="zoomPage = $event" @bookmark="bookmarkPage" @manage="managePage">
          <template #before><div v-if="mode === 'reader' && currentWork" class="detail-head"><div class="detail-nav"><button class="detail-back" @click="backFromDetail"><Icon name="left" />{{ detailOrigin === 'artists' ? '返回画师' : detailOrigin === 'feed' ? '返回流式阅览' : '返回图库' }}</button><span>{{ ordinaryRankKnown && totalExact ? ordinaryIndex + 1 + ' / ' + total : '当前范围阅览' }}</span></div><div class="detail-title-row"><h1>{{ currentWork.title }}</h1><RestrictionBadge :value="currentWork.x_restrict" /><span class="detail-badge">{{ currentWork.type === 'ugoira' ? '动图' : currentWork.type === 'manga' ? '漫画' : '插画' }}</span><span v-if="currentWork.display_page_count > 1" class="detail-badge">{{ currentWork.display_page_count }} 页</span></div><div class="detail-meta"><span>画师：<button class="text-link" @click="openArtistGallery(currentWork.artist_id)">{{ currentWork.artist_name }}</button></span><span v-if="currentWork.posted_at">发布于 {{ currentWork.posted_at.slice(0, 10) }}</span></div><p v-if="archiveNotice(currentWork)" class="source-notice">{{ archiveNotice(currentWork) }}</p><label v-if="currentWork.revisions && currentWork.revisions.length > 1" class="revision-picker">存档版本 <select :value="currentWork.revision_id" aria-label="存档版本" @change="selectRevision(($event.target as HTMLSelectElement).value)"><option v-for="revision in currentWork.revisions" :key="revision.id" :value="revision.id">{{ revision.change_kind === 'legacy' ? '升级前存档' : `版本 ${revision.ordinal}` }} · {{ revision.created_at.slice(0, 16).replace('T', ' ') }}{{ revision.current ? ' · 默认' : '' }}{{ revision.state !== 'complete' ? ' · 未完整' : '' }}</option></select></label><button v-if="currentWork.revisions_has_more" class="text-link" :disabled="revisionLoading" @click="earlierRevisions">{{ revisionLoading ? '正在读取…' : '加载更早版本' }}</button><div class="detail-tags"><button v-for="tag in currentWork.tags" :key="tag" class="tag" @click="tagFilter(tag)"><TagLabel :name="tag" :details="currentWork.tag_details" /></button></div><div class="detail-actions"><a class="text-link" :href="`https://www.pixiv.net/artworks/${currentWork.pixiv_id}`" target="_blank" rel="noopener noreferrer">在 Pixiv 查看 <Icon name="external" /></a><button class="softbtn" @click="openFolder(currentWork.id)"><Icon name="folder" />本地文件</button><div class="detail-action-right"><button class="softbtn" @click="infoOpen = true; loadInfo(currentWork.id)">整理作品</button></div></div></div><div v-else class="stream-head"><div><h1>流式阅览</h1><p>当前图库范围 · {{ totalExact ? total + ' 件作品' : '数量按需统计' }}</p></div><label class="reading-order">顺序 <select :value="readingOrder" aria-label="流式阅览顺序" @change="changeReadingOrder(($event.target as HTMLSelectElement).value)"><option value="sequential">顺序阅览</option><option value="random">随机阅览</option></select></label><button class="softbtn" @click="gallery()"><Icon name="grid" />返回图库</button></div></template>
          <template #after><template v-if="mode === 'reader' && currentWork"><section class="detail-caption" aria-label="作品简介"><h2>作品简介</h2><div v-if="currentWork.caption" v-html="currentWork.caption" /><p v-else>{{ currentWork.caption_state === 'empty' ? '作者没有填写简介' : '尚未取得简介' }}</p></section></template><div v-else class="stream-end"><p>已到当前范围末尾</p><button @click="gallery()">返回图库</button></div></template>
        </PageReader></Transition></div>
        <aside v-if="infoOpen" class="info-panel" data-no-navigation aria-label="作品整理"><header><h2>作品整理</h2><button @click="infoOpen = false" aria-label="关闭作品整理"><Icon name="close" /></button></header><template v-if="infoWork && (mode === 'reader' || infoWork.id === activePage?.work_id)"><h3>{{ infoWork.title }}</h3><p class="muted">{{ infoWork.artist_name }}</p><p class="muted">{{ infoWork.bookmark_count ?? '—' }} 收藏 · {{ infoWork.view_count ?? '—' }} 浏览</p><div class="tags"><span v-for="tag in infoWork.tags" :key="tag"><TagLabel :name="tag" :details="infoWork.tag_details" /></span></div><div class="info-actions"><BookmarkButton v-if="mode === 'feed'" :work-id="infoWork.id" :initial="infoWork.bookmark" @message="message($event, true)" /><button @click="bookmarkSync"><Icon name="refresh" />同步收藏状态</button><button @click="task('metadata', infoWork.id)"><Icon name="refresh" />刷新作品信息</button><button @click="hidden">{{ infoWork.is_hidden ? '取消隐藏' : '隐藏此作品' }}</button><label>本地评分 <select :value="infoWork.rating" @change="rate(Number(($event.target as HTMLSelectElement).value))"><option :value="0">未评分</option><option v-for="value in [1,2,3,4,5]" :key="value" :value="value">{{ value }} 星</option></select></label><button @click="task('reconcile', infoWork.id)">检查并补齐此作品</button><button @click="task('compare', infoWork.id)" title="重新取得这件作品的原图并比较内容；保留已有存档">重新比对原图</button><button v-if="infoWork.type === 'ugoira'" @click="task('backfill', infoWork.id)">补回动图原包</button><button class="danger" @click="removePrompt = true">永久删除</button></div><details v-if="infoWork.legacy_variants?.length" class="asset-detail"><summary>旧档案的同页文件</summary><div v-for="group in infoWork.legacy_variants" :key="group.page"><p>第 {{ group.page + 1 }} 页</p><p v-for="file in group.files" :key="file.name"><a v-if="file.src" :href="file.src" target="_blank" rel="noopener noreferrer">{{ file.default ? '默认显示' : '另一份' }}：{{ file.name }}</a><span v-else>{{ file.name }}（不可用）</span></p></div></details><details class="asset-detail"><summary>存档状态</summary><p>{{ archiveStatus(infoWork.archive) }}</p><p v-for="asset in infoWork.assets" :key="asset.id">{{ asset.role === 'image' ? `第 ${asset.page + 1} 页` : asset.role === 'ugoira_zip' ? '动图原包' : '播放时序' }}：{{ asset.state === 'verified' ? '已核验' : localAssetReason(asset.error) || '待核验或缺失' }}</p></details></template><p v-else>正在读取作品信息…</p></aside>
        <footer v-if="!readingEnded && mode !== 'feed'" class="reading-toolbar" :class="{'is-detail': mode === 'reader'}">
          <div v-if="readingActivity.phase || readingActivity.error" class="reading-activity" :class="{error: readingActivity.error}" role="status" aria-live="polite">
            <span>{{ readingActivity.error || readingActivity.phase }}</span><button v-if="readingActivity.error" @click="retryReading">重试</button><button v-else @click="cancelNavigation">取消</button><button v-if="readingActivity.error" @click="gallery()">返回图库</button><button v-if="readingActivity.error" @click="error = ''; reader?.cancel()">关闭</button>
          </div>
          <div class="reading-toolbar-inner">
            <template v-if="mode === 'reader'">
              <button class="softbtn reader-nav-button" aria-label="上一件作品" :disabled="!ordinaryNeighbors?.previous" @click="navigate(-1)"><Icon name="left" /><span class="reader-nav-label">上一件</span></button>
              <div class="reading-counter"><strong>{{ currentWork?.title }}</strong><small><span class="reading-work-count">{{ ordinaryRankKnown && totalExact ? ordinaryIndex + 1 + ' / ' + total + ' 件' : '当前范围阅览' }}</span><span class="reading-page-count">第 {{ (activePage?.page ?? 0) + 1 }} / {{ currentWork?.display_page_count || 1 }} 页</span></small></div>
              <BookmarkButton v-if="currentWork" :key="currentWork.id" :work-id="currentWork.id" :initial="currentWork.bookmark" @message="message($event, true)" />
              <button class="reader-nav-button" aria-label="下一件作品" :disabled="!ordinaryNeighbors?.next" @click="navigate(1)"><span class="reader-nav-label">下一件</span><Icon name="right" /></button>
            </template>
            <template v-else>
              <button class="softbtn reader-nav-button" aria-label="上一件作品" :disabled="!activePage || activePage.previous_work_index == null" @click="navigate(-1)"><Icon name="left" /><span class="reader-nav-label">上一件</span></button>
              <button class="reading-counter reading-work-info" aria-label="作品信息与整理" :title="activePage ? activePage.title + ' · 作品信息与整理' : '作品信息与整理'" :disabled="!activePage" @click="activePage && managePage(activePage)"><strong>{{ activePage?.title || '流式阅览' }}</strong><small><span class="reading-work-count">{{ flowOrdinalKnown ? (activePage?.work_index ?? 0) + 1 + ' / ' + (totalExact ? total : '未计数') + ' 件' : '当前范围阅览' }}</span><span class="reading-page-count">第 {{ (activePage?.page ?? 0) + 1 }} / {{ activePage?.page_count || 1 }} 页</span></small></button>
              <BookmarkButton v-if="activePage" :key="activePage.work_id" :work-id="activePage.work_id" @message="message($event, true)" />
              <button class="reader-nav-button" aria-label="下一件作品" :disabled="!activePage || readingOrder === 'random' && total <= 1 || readingOrder === 'sequential' && activePage.has_next_work === false" @click="navigate(1)"><span class="reader-nav-label">下一件</span><Icon name="right" /></button>
            </template>
          </div>
        </footer>
      </main></template>
    </template>
    <ArtistsView v-else-if="section === 'artists'" :inert="mobileMenuOpen ? true : undefined" :artist-id="artistId" :navigation-artist-id="originArtistId" :navigation-pending="pending" :navigation-error="error" @retry-navigation="retryNavigation" @works="artistWorks" @work="artistWork" @message="message" @task-queued="taskPanel?.showTask($event)" />
    <SettingsView v-else :inert="mobileMenuOpen ? true : undefined" :gallery-mode="continuous ? 'scroll' : String(pageSize)" @gallery-mode="changePageSize" @task="task" @theme="chooseTheme" @message="message" />
    <FilterPanel v-if="filterOpen && section === 'library' && mode === 'gallery'" :key="scope" :query="scope" @apply="filter" @close="filterOpen = false" />
    <div v-if="notification" class="toast" :class="{error: notificationError}" :role="notificationError ? 'alert' : 'status'">{{ notification }}<button @click="notification = ''" aria-label="关闭提示"><Icon name="close" /></button></div>
  </div>
  <ZoomView v-if="zoomPage" :page="zoomPage" @close="zoomPage = null" />
  <ConfirmDialog v-if="removePrompt" title="永久删除作品" description="这会删除此作品的全部历史版本和本地文件，并阻止旧任务再次下载。" action="永久删除" danger @cancel="removePrompt = false" @confirm="remove" />
</template>

<style scoped>
.gallery-heading { position: relative; }
.revision-picker, .reading-order { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: var(--muted); font-size: 12px; }
.revision-picker { margin: 12px 0; }
.revision-picker select { max-width: 100%; }
.reading-finished { position: absolute; inset: 0; display: grid; place-content: center; gap: 16px; }
.stream-head { flex-wrap: wrap; }
.reading-work-info { display: block; text-align: center; min-height: 40px; }
.reading-work-info strong::after { content: " ···"; color: var(--muted); }
@media (max-width: 520px) { .reading-order { gap: 4px; font-size: 11px; } .reading-order select { max-width: 110px; padding: 7px; } }
.new-work-entry { position: absolute; left: 0; top: calc(100% - 1px); min-height: 16px; height: 16px; padding: 0; font-size: 10px; color: var(--muted); }
</style>
