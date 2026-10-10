<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, prepareImage, url, write } from './api'
import { LatestIntent } from './interaction'
import { freshLibraryQuery } from './positions'
import { readLibraryEntry, type GallerySeed } from './state/libraryEntry'
import { archiveNotice, archiveStatus, localAssetReason, hasNewWorks, isOlderCatalogue, type Anchor, type GalleryBatch, type Mode, type PageData, type PageLoader, type WorkDetail } from './types'
import { observeCatalogueData, queueCatalogueUpdate } from './state/catalogue'
import { writeTagValues } from './state/filterSelection'
import { invalidateWorkCache, workDetail, workPath } from './state/workCache'
import { startVisit, releaseVisit, visitEntry, locateVisit, visitPages, type ReadingVisit, type ReadingOrder } from './state/reading'
import { observeVisibleViewport } from './state/viewport'
import FilterPanel from './FilterPanel.vue'
import GalleryView from './GalleryView.vue'
import PageReader from './PageReader.vue'
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
const railCounts = ref({all: 0, recent: 0, favorites: 0})
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
const readingEnded = ref(false)
const galleryView = ref<InstanceType<typeof GalleryView> | null>(null), checkingNewContent = ref(false)
const currentWork = ref<WorkDetail | null>(null), infoWork = ref<WorkDetail | null>(null), activePage = ref<PageData | null>(null)
const initialAnchor = ref<Anchor | null>(null), currentAnchor = ref<Anchor | null>(null), zoomPage = ref<PageData | null>(null)
const reader = ref<InstanceType<typeof PageReader> | null>(null), taskPanel = ref<InstanceType<typeof TaskPanel> | null>(null)
const navigation = new LatestIntent(), infoRequest = new LatestIntent(), statusRequest = new LatestIntent()
const newContentRequest = new LatestIntent()
const railRequest = new LatestIntent()
let newContentAgain = false, revealNewContent = false, hiddenCatalogueUpdate = false
let toastTimer = 0, lastKeyTime = 0, failedNavigation: (() => void) | null = null
const sessionKey = computed(() => mode.value + ':' + (mode.value === 'reader' ? currentWork.value?.id : '') + ':' + scope.value + ':' + readerReset.value)
let stopViewport: (() => void) | undefined
onMounted(() => { stopViewport = observeVisibleViewport() })
onBeforeUnmount(() => { stopViewport?.() })
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
    if (ticket.current()) railCounts.value = counts
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
  void initialize(section.value === 'library' ? scope.value : homeScope.value, '/flow', true)
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
    void updateRailCounts(homeScope.value)
    section.value = path === '/settings' ? 'settings' : 'artists'
    artistId.value = path.startsWith('/artist/') ? Number(path.split('/').at(-1)) : null
    ready.value = true; pending.value = false; error.value = ''
    if (push) route(true)
    return
  }
  const ticket = navigation.begin(); infoRequest.cancel(); pending.value = true; error.value = ''
  failedNavigation = () => { void initialize(query, path, push, saveHome) }
  let preparedVisit: ReadingVisit | null = null
  try {
    const seed = await readLibraryEntry(query, path, pageSize.value, continuous.value, ticket.signal)
    const batch = seed.data, params = new URLSearchParams(seed.query)
    const match = path.match(/^\/illust\/(\d+)$/)
    let openingDetail: WorkDetail | null = null, openingAnchor: Anchor | null = null
    if (match || path === '/flow' && batch.total) {
      preparedVisit = await startVisit(seed.query, ticket.signal)
      if (match) {
        const position = await api<Anchor>(url('/api/library/locate', seed.query, {work_id: Number(match[1])}), {signal: ticket.signal})
        const work = await visitEntry(preparedVisit, position.work_index, ticket.signal)
        if (!work) throw new Error('目标作品已不在这个范围内，请刷新图库')
        openingDetail = await workDetail(work.work_id, new URLSearchParams(query).get('revision_id') || work.revision_id, ticket.signal)
        await prepareImage(openingDetail.pages[0]?.src || null, ticket.signal)
        openingAnchor = {work_id: work.work_id, work_index: position.work_index, page: 0, index: 0, offset: 0}
      } else {
        const first = (await visitPages(preparedVisit, readingOrder.value, 0, 1, ticket.signal)).items[0]
        if (!first) throw new Error('当前范围已变化，请刷新图库')
        openingAnchor = {work_id: first.work_id, work_index: first.work_index, page: first.page, index: first.index, offset: 0}
      }
    }
    if (!ticket.current()) return
    releaseVisit(readingVisit.value); readingVisit.value = preparedVisit; preparedVisit = null
    scope.value = seed.query; total.value = batch.total
    if (saveHome) setHomeScope(query)
    void updateRailCounts(scope.value)
    section.value = 'library'
    searchText.value = params.get('q') || ''; sortChoice.value = params.get('sort') || 'newest'
    galleryAnchor.value = seed.anchor; gallerySeed.value = path === '/' ? seed : null
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
        currentWork.value = infoWork.value = openingDetail
        ordinaryIndex.value = desiredIndex.value = openingAnchor.work_index
        galleryAnchor.value = openingAnchor.work_index
        ingestBookmark(openingDetail.id, openingDetail.bookmark); refreshBookmark(openingDetail.id)
        observeCatalogueData(openingDetail.catalogue_revision)
        mode.value = 'reader'
      } else { mode.value = 'feed'; infoWork.value = null }
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
  const visit = await startVisit(scope.value, signal)
  if (signal.aborted) { releaseVisit(visit); signal.throwIfAborted() }
  readingVisit.value = visit
  return visit
}
async function openWork(index: number, push = true, selectedRevision?: string | null) {
  if (!total.value) return
  index = Math.max(0, Math.min(total.value - 1, index))
  desiredIndex.value = index
  clearTimeout(searchTimer); railRequest.cancel()
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void openWork(index, push) }
  try {
    const visit = await ensureVisit(ticket.signal)
    const work = await visitEntry(visit, index, ticket.signal)
    if (!work) throw new Error('目标作品已不在这个范围内，请刷新图库')
    const detail = await workDetail(work.work_id, selectedRevision || work.revision_id, ticket.signal)
    await prepareImage(detail.pages[0]?.src || null, ticket.signal)
    if (!ticket.current()) return
    const previousMode = mode.value
    currentWork.value = infoWork.value = detail; ordinaryIndex.value = index; total.value = work.total
    readingEnded.value = false
    ingestBookmark(detail.id, detail.bookmark)
    refreshBookmark(detail.id)
    initialAnchor.value = {work_id: work.work_id, work_index: index, page: 0, index: 0, offset: 0}
    activePage.value = null; currentAnchor.value = initialAnchor.value
    if (previousMode === 'feed') detailOrigin.value = 'feed'
    readerReset.value++; mode.value = 'reader'; section.value = 'library'; filterOpen.value = false; galleryAnchor.value = index
    observeCatalogueData(detail.catalogue_revision)
    route(push && previousMode !== 'reader')
    await nextTick()
  } catch (failure) {
    if (ticket.current()) { error.value = failure instanceof Error ? failure.message : '下一件作品未能载入'; desiredIndex.value = ordinaryIndex.value }
  } finally { if (ticket.current()) pending.value = false }
}
function navigate(delta: number) {
  if (zoomPage.value) return
  if (mode.value === 'feed') {
    const target = delta < 0 ? activePage.value?.previous_work_index : activePage.value?.next_work_index
    if (target != null) void reader.value?.jump(target)
  } else void openWork(desiredIndex.value + delta)
}
async function enterFeed(push = true) {
  if (!total.value) return
  clearTimeout(searchTimer); railRequest.cancel()
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void enterFeed(push) }
  try {
    const visit = await ensureVisit(ticket.signal)
    const batch = await visitPages(visit, readingOrder.value, 0, 1, ticket.signal)
    const first = batch.items[0]
    if (!first) throw new Error('当前范围已变化，请刷新图库')
    const found = {work_id: first.work_id, page: first.page, index: first.index, work_index: first.work_index}
    if (!ticket.current()) return
    initialAnchor.value = {...found, offset: 0}; currentAnchor.value = initialAnchor.value
    readerReset.value++
    readingEnded.value = false
    activePage.value = null; infoWork.value = null; infoOpen.value = false; filterOpen.value = false
    mode.value = 'feed'; section.value = 'library'; route(push)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '流式阅览未能打开' }
  finally { if (ticket.current()) pending.value = false }
}
function gallery(push = true) {
  void initialize(freshLibraryQuery(section.value === 'library' ? scope.value : homeScope.value, true), '/', push)
}
const loader = computed<PageLoader>(() => {
  const visit = readingVisit.value, order = readingOrder.value
  if (mode.value === 'feed') return (offset, limit, signal) => visitPages(visit!, order, offset, limit, signal)
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
  if (!readingVisit.value) return null
  if (mode.value === 'feed' && activePage.value) {
    const batch = await visitPages(readingVisit.value, readingOrder.value, activePage.value.next_work_index, 1, new AbortController().signal)
    const next = batch.items[0]
    return next && next.work_id !== activePage.value.work_id ? {id: next.work_id, round: next.round || 0} : null
  }
  if (ordinaryIndex.value + 1 >= total.value) return null
  const next = await visitEntry(readingVisit.value, ordinaryIndex.value + 1, new AbortController().signal)
  return {id: next.work_id, round: 0}
}
async function continueAfterRemoval(id: number, next: {id: number; round: number} | null) {
  invalidateWorkCache(id); infoOpen.value = false
  if (!readingVisit.value) return
  await write(`/api/reading-sessions/${readingVisit.value.session_id}/exclude/${id}`, {})
  total.value = Math.max(0, total.value - 1)
  if (!next) { readingEnded.value = true; return }
  const ticket = navigation.begin()
  const found = await locateVisit(readingVisit.value, next.id, mode.value === 'feed' ? readingOrder.value : 'sequential', next.round, ticket.signal)
  if (!ticket.current()) return
  if (mode.value === 'feed') {
    initialAnchor.value = {...found, offset: 0}; activePage.value = null; readerReset.value++
  } else await openWork(found.work_index, false)
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
  if (order === readingOrder.value || !readingVisit.value) return
  const page = activePage.value, ticket = navigation.begin()
  try {
    const anchor = page ? await locateVisit(readingVisit.value, page.work_id, order, 0, ticket.signal) : null
    if (!ticket.current()) return
    readingOrder.value = order
    try { localStorage.setItem('pixiv-local-reading-order', order) } catch { /* optional preference */ }
    initialAnchor.value = anchor ? {...anchor, index: anchor.index + (page?.page || 0), page: page?.page || 0, offset: 0} : null
    readerReset.value++; readingEnded.value = false
  } catch (failure) { if (ticket.current()) message(failure instanceof Error ? failure.message : '切换顺序失败', true) }
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
  if (infoOpen.value) return
  if (mode.value === 'reader' && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
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
function retryNavigation() { if (failedNavigation) failedNavigation(); else refresh() }
function changeRail(rail: 'all' | 'recent' | 'favorites') {
  const params = new URLSearchParams(scope.value)
  for (const name of ['ceiling', 'as_of', 'page', 'favorites', 'recent']) params.delete(name)
  if (rail !== 'all') params.set(rail, '1')
  void initialize(params.toString(), '/', true, true)
}
function openFromGallery(index: number) { detailOrigin.value = 'gallery'; void openWork(index) }
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
function showArtists(id: number | null = null) { clearTimeout(searchTimer); navigation.cancel(); railRequest.cancel(); infoRequest.cancel(); pending.value = false; section.value = 'artists'; artistId.value = id; filterOpen.value = false; infoOpen.value = false; route(true) }
function showSettings() { clearTimeout(searchTimer); navigation.cancel(); railRequest.cancel(); infoRequest.cancel(); pending.value = false; section.value = 'settings'; filterOpen.value = false; infoOpen.value = false; route(true) }
function openArtistGallery(id: number) {
  detailOrigin.value = 'gallery'; originArtistId.value = null
  const params = new URLSearchParams({artist_ids: String(id), sort: sortChoice.value})
  void initialize(params.toString(), '/', true)
}
async function artistWorks(id: number) {
  detailOrigin.value = 'artists'; originArtistId.value = id
  const params = new URLSearchParams(homeScope.value); params.delete('ceiling'); params.delete('as_of'); params.delete('favorites'); params.delete('recent'); params.delete('artist_ids_exclude'); params.set('artist_ids', String(id))
  await initialize(params.toString(), '/', true)
}
async function artistWork(id: number, workId: number) {
  await artistWorks(id)
  if (section.value !== 'library' || new URLSearchParams(scope.value).get('artist_ids') !== String(id)) return
  const ticket = navigation.begin()
  try {
    const found = await api<Anchor>(url('/api/library/locate', scope.value, {work_id: workId}), {signal: ticket.signal})
    if (ticket.current()) await openWork(found.work_index)
  } catch (failure) { if (ticket.current()) message(failure instanceof Error ? failure.message : '这件作品未能打开', true) }
}
function backFromDetail() {
  if (detailOrigin.value === 'artists') { showArtists(originArtistId.value); return }
  if (detailOrigin.value === 'feed') { void enterFeed(); return }
  gallery()
}
function brandHome() { detailOrigin.value = 'gallery'; void initialize(homeScope.value, '/', true) }
function foreground() { resumeBookmarkPolling(); if (hiddenCatalogueUpdate) void checkForNewWorks(); if (section.value === 'library' && mode.value !== 'gallery') { const id = activePage.value?.work_id || currentWork.value?.id; if (id) refreshBookmark(id) } }
function chooseTheme(theme: 'dark' | 'light', _event: MouseEvent) {
  document.body.dataset.theme = theme
  try { localStorage.setItem('pixiv-local-theme', theme) } catch { /* optional */ }
}
watch(() => browserRoute.fullPath, path => {
  if (path === internalRoute) { internalRoute = ''; return }
  void initialize(browserRoute.fullPath.slice(browserRoute.path.length).replace(/^\?/, ''), browserRoute.path, false)
})
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
onBeforeUnmount(() => { navigation.cancel(); infoRequest.cancel(); statusRequest.cancel(); newContentRequest.cancel(); railRequest.cancel(); releaseVisit(readingVisit.value); clearTimeout(toastTimer); clearTimeout(searchTimer); window.removeEventListener('keydown', key); window.removeEventListener('focus', foreground); window.removeEventListener('archive-updated', archiveUpdated); document.removeEventListener('visibilitychange', catalogueVisibilityChanged) })
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
  total.value = data.total; scope.value = params.toString(); newContent.value = false
  void updateRailCounts(scope.value)
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
    const fresh = await api<GalleryBatch>(url('/api/library', params.toString(), {limit: 1, compact: 1}), {signal: ticket.signal})
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
  invalidateWorkCache()
  refreshCurrentWork()
  if (mode.value === 'feed' && infoOpen.value && infoWork.value) void loadInfo(infoWork.value.id, false)
  void checkForNewWorks()
}
function catalogueVisibilityChanged() { if (hiddenCatalogueUpdate && !document.hidden) void checkForNewWorks() }
function updated() { queueCatalogueUpdate() }
</script>
<template>
  <UpgradeView v-if="startup" :status="startup" @ready="upgraded" />
  <div v-else class="app-shell" :class="{'sidebar-collapsed': sidebarCollapsed}" :inert="zoomPage ? true : undefined">
    <header class="app-header" :class="{'is-collapsed': sidebarCollapsed, 'is-open': mobileMenuOpen}">
      <div class="mobile-head"><button ref="mobileMenuButton" class="menu-toggle" :aria-expanded="mobileMenuOpen" aria-controls="app-sidebar" :aria-label="mobileMenuOpen ? '关闭菜单' : '打开菜单'" @click="mobileMenuOpen ? closeMobileMenu() : mobileMenuOpen = true"><Icon :name="mobileMenuOpen ? 'close' : 'menu'" /></button><button class="brand" @click="navHome" aria-label="返回我的图库"><BrandMark /><span><span class="brand-pixiv">Pixiv</span>Tracker</span></button><span class="mobile-head-spacer" /></div>
      <button v-if="mobileMenuOpen" class="sidebar-scrim" aria-label="关闭菜单" @click="closeMobileMenu" />
      <div id="app-sidebar" class="sidebar-body">
        <div class="sidebar-head"><button class="brand" @click="navHome" aria-label="返回我的图库" title="返回我的图库"><BrandMark /><span class="sidebar-label"><span class="brand-pixiv">Pixiv</span>Tracker</span></button><button class="sidebar-toggle" :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar"><Icon :name="sidebarCollapsed ? 'right' : 'left'" /></button><button class="mobile-close" aria-label="关闭菜单" @click="closeMobileMenu"><Icon name="close" /></button></div>
        <label v-if="!sidebarCollapsed || mobileMenuOpen" class="search-field side-search"><Icon name="search" /><input v-model="searchText" type="search" placeholder="搜索作品、画师或标签" aria-label="搜索作品、画师或标签" @compositionstart="composing = true" @compositionend="composing = false; search()" @input="search"></label><button v-else class="side-search-trigger" aria-label="展开侧栏并搜索" title="搜索" @click="toggleSidebar"><Icon name="search" /></button>
        <nav class="sidebar-nav" aria-label="主导航"><button :class="{selected: section === 'library' && mode !== 'feed'}" title="作品" @click="navHome"><Icon name="grid" /><span class="sidebar-label">作品</span></button><button :class="{selected: section === 'artists'}" title="画师" @click="navArtists"><Icon name="artists" /><span class="sidebar-label">画师</span></button><button :class="{selected: section === 'library' && mode === 'feed'}" title="流式阅览" :disabled="section === 'library' && !total" @click="navFeed"><Icon name="flow" /><span class="sidebar-label">流式阅览</span></button><button :class="{selected: section === 'settings'}" title="设置" @click="navSettings"><Icon name="settings" /><span class="sidebar-label">设置</span></button></nav>
        <div class="sidebar-divider" /><div class="sidebar-group-label sidebar-label">图库范围</div><nav class="sidebar-nav sidebar-scopes" aria-label="图库范围"><button :class="{selected: section === 'library' && currentRail() === 'all'}" :aria-pressed="currentRail() === 'all'" title="全部作品" @click="navRail('all')"><Icon name="grid" /><span class="sidebar-label">全部作品</span><small class="sidebar-label">{{ railCounts.all }}</small></button><button :class="{selected: section === 'library' && currentRail() === 'recent'}" :aria-pressed="currentRail() === 'recent'" title="最近存入" @click="navRail('recent')"><Icon name="clock" /><span class="sidebar-label">最近存入</span><small class="sidebar-label">{{ railCounts.recent }}</small></button><button :class="{selected: section === 'library' && currentRail() === 'favorites'}" :aria-pressed="currentRail() === 'favorites'" title="收藏" @click="navRail('favorites')"><Icon name="heart" /><span class="sidebar-label">收藏</span><small class="sidebar-label">{{ railCounts.favorites }}</small></button></nav>
      </div>
    </header>
    <TaskPanel ref="taskPanel" @updated="updated" />
    <template v-if="section === 'library'">
      <template v-if="mode === 'gallery'">
        <div class="gallery-layout" :inert="mobileMenuOpen ? true : undefined">
          <main class="gallery-main"><div class="gallery-controls"><div class="gallery-heading"><h1>{{ heading }}</h1><span>{{ total }} 件</span><button v-if="newContent" class="text-link new-work-entry" :disabled="checkingNewContent" @click="checkForNewWorks(true)">有新作品，查看</button></div><div class="gallery-control-actions"><select v-model="sortChoice" aria-label="图库排序" @change="sort"><option value="newest">最新投稿</option><option value="oldest">最早投稿</option><option value="added">最近存入</option><option value="title">作品名称</option></select><button class="softbtn" :aria-expanded="filterOpen" :title="filterSummary" @click="filterOpen = true"><Icon name="filter" />筛选</button><button class="check-button" aria-label="检查新作品" @click="task('check')"><Icon name="refresh" /><span>检查新作品</span></button></div></div>
            <div v-if="filterSummary !== '全部画师 · 全部类型'" class="active-filters"><span>{{ filterSummary }}</span><button class="text-link" @click="filterOpen = true">调整</button></div>
            <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">重试</button></div>
            <GalleryView v-if="ready" ref="galleryView" :query="scope" :start-index="galleryAnchor" :reset-key="galleryReset" :page-size="pageSize" :continuous="continuous" :initial-batch="gallerySeed" @seed-consumed="gallerySeed = null" @open="openFromGallery" @artist="showArtists" @message="message($event, true)" @anchor="galleryFocused" @total="total = $event" @page="galleryAnchor = $event * pageSize; route(false)" /><div v-else class="empty">{{ error ? '图库尚未载入，请重试' : '正在打开本地图库…' }}</div>
          </main></div>
      </template>
      <template v-else><main class="reader-layout" :inert="mobileMenuOpen ? true : undefined">
        <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">重试</button><button class="quiet" @click="error = ''">关闭</button></div>
        <div v-if="readingEnded" class="empty reading-finished"><p>已到当前范围末尾</p><button @click="gallery()">返回图库</button></div><div class="reader-stage"><Transition name="work"><PageReader v-if="!readingEnded" :key="sessionKey" ref="reader" :mode="mode === 'feed' ? 'feed' : 'reader'" :loader="loader" :session-key="sessionKey" :initial="initialAnchor" :inactive="!!zoomPage" @focus="focused" @navigate="navigate" @artist="openArtistGallery" @zoom="zoomPage = $event" @bookmark="bookmarkPage" @manage="managePage">
          <template #before><div v-if="mode === 'reader' && currentWork" class="detail-head"><div class="detail-nav"><button class="detail-back" @click="backFromDetail"><Icon name="left" />{{ detailOrigin === 'artists' ? '返回画师' : detailOrigin === 'feed' ? '返回流式阅览' : '返回图库' }}</button><span>{{ ordinaryIndex + 1 }} / {{ total }}</span></div><div class="detail-title-row"><h1>{{ currentWork.title }}</h1><RestrictionBadge :value="currentWork.x_restrict" /><span class="detail-badge">{{ currentWork.type === 'ugoira' ? '动图' : currentWork.type === 'manga' ? '漫画' : '插画' }}</span><span v-if="currentWork.display_page_count > 1" class="detail-badge">{{ currentWork.display_page_count }} 页</span></div><div class="detail-meta"><span>画师：<button class="text-link" @click="openArtistGallery(currentWork.artist_id)">{{ currentWork.artist_name }}</button></span><span v-if="currentWork.posted_at">发布于 {{ currentWork.posted_at.slice(0, 10) }}</span></div><p v-if="archiveNotice(currentWork)" class="source-notice">{{ archiveNotice(currentWork) }}</p><label v-if="currentWork.revisions && currentWork.revisions.length > 1" class="revision-picker">存档版本 <select :value="currentWork.revision_id" aria-label="存档版本" @change="selectRevision(($event.target as HTMLSelectElement).value)"><option v-for="revision in currentWork.revisions" :key="revision.id" :value="revision.id">{{ revision.change_kind === 'legacy' ? '升级前存档' : `版本 ${revision.ordinal}` }} · {{ revision.created_at.slice(0, 16).replace('T', ' ') }}{{ revision.current ? ' · 默认' : '' }}{{ revision.state !== 'complete' ? ' · 未完整' : '' }}</option></select></label><div class="detail-tags"><button v-for="tag in currentWork.tags" :key="tag" class="tag" @click="tagFilter(tag)"><TagLabel :name="tag" :details="currentWork.tag_details" /></button></div><div class="detail-actions"><a class="text-link" :href="`https://www.pixiv.net/artworks/${currentWork.pixiv_id}`" target="_blank" rel="noopener noreferrer">在 Pixiv 查看 <Icon name="external" /></a><button class="softbtn" @click="openFolder(currentWork.id)"><Icon name="folder" />本地文件</button><div class="detail-action-right"><button class="softbtn" @click="infoOpen = true; loadInfo(currentWork.id)">整理作品</button></div></div></div><div v-else class="stream-head"><div><h1>流式阅览</h1><p>当前图库范围 · {{ total }} 件作品</p></div><label class="reading-order">顺序 <select :value="readingOrder" aria-label="流式阅览顺序" @change="changeReadingOrder(($event.target as HTMLSelectElement).value)"><option value="sequential">顺序阅览</option><option value="random">随机阅览</option></select></label><button class="softbtn" @click="gallery()"><Icon name="grid" />返回图库</button></div></template>
          <template #after><template v-if="mode === 'reader' && currentWork"><section class="detail-caption" aria-label="作品简介"><h2>作品简介</h2><div v-if="currentWork.caption" v-html="currentWork.caption" /><p v-else>{{ currentWork.caption_state === 'empty' ? '作者没有填写简介' : '尚未取得简介' }}</p></section></template><div v-else class="stream-end"><p>已到当前范围末尾</p><button @click="gallery()">返回图库</button></div></template>
        </PageReader></Transition></div>
        <aside v-if="infoOpen" class="info-panel" data-no-navigation aria-label="作品整理"><header><h2>作品整理</h2><button @click="infoOpen = false" aria-label="关闭作品整理"><Icon name="close" /></button></header><template v-if="infoWork && (mode === 'reader' || infoWork.id === activePage?.work_id)"><h3>{{ infoWork.title }}</h3><p class="muted">{{ infoWork.artist_name }}</p><p class="muted">{{ infoWork.bookmark_count ?? '—' }} 收藏 · {{ infoWork.view_count ?? '—' }} 浏览</p><div class="tags"><span v-for="tag in infoWork.tags" :key="tag"><TagLabel :name="tag" :details="infoWork.tag_details" /></span></div><div class="info-actions"><BookmarkButton v-if="mode === 'feed'" :work-id="infoWork.id" :initial="infoWork.bookmark" @message="message($event, true)" /><button @click="bookmarkSync"><Icon name="refresh" />同步收藏状态</button><button @click="task('metadata', infoWork.id)"><Icon name="refresh" />刷新作品信息</button><button @click="hidden">{{ infoWork.is_hidden ? '取消隐藏' : '隐藏此作品' }}</button><label>本地评分 <select :value="infoWork.rating" @change="rate(Number(($event.target as HTMLSelectElement).value))"><option :value="0">未评分</option><option v-for="value in [1,2,3,4,5]" :key="value" :value="value">{{ value }} 星</option></select></label><button @click="task('reconcile', infoWork.id)">检查并补齐此作品</button><button @click="task('compare', infoWork.id)" title="重新取得这件作品的原图并比较内容；保留已有存档">重新比对原图</button><button v-if="infoWork.type === 'ugoira'" @click="task('backfill', infoWork.id)">补回动图原包</button><button class="danger" @click="removePrompt = true">永久删除</button></div><details v-if="infoWork.legacy_variants?.length" class="asset-detail"><summary>旧档案的同页文件</summary><div v-for="group in infoWork.legacy_variants" :key="group.page"><p>第 {{ group.page + 1 }} 页</p><p v-for="file in group.files" :key="file.name"><a v-if="file.src" :href="file.src" target="_blank" rel="noopener noreferrer">{{ file.default ? '默认显示' : '另一份' }}：{{ file.name }}</a><span v-else>{{ file.name }}（不可用）</span></p></div></details><details class="asset-detail"><summary>存档状态</summary><p>{{ archiveStatus(infoWork.archive) }}</p><p v-for="asset in infoWork.assets" :key="asset.id">{{ asset.role === 'image' ? `第 ${asset.page + 1} 页` : asset.role === 'ugoira_zip' ? '动图原包' : '播放时序' }}：{{ asset.state === 'verified' ? '已核验' : localAssetReason(asset.error) || '待核验或缺失' }}</p></details></template><p v-else>正在读取作品信息…</p></aside>
        <footer v-if="!readingEnded" class="reading-toolbar" :class="{'is-detail': mode === 'reader'}"><div class="reading-toolbar-inner"><template v-if="mode === 'reader'"><button class="softbtn reader-nav-button" aria-label="上一件作品" :disabled="desiredIndex <= 0" @click="navigate(-1)"><Icon name="left" /><span class="reader-nav-label">上一件</span></button><div class="reading-counter"><strong>{{ currentWork?.title }}</strong><small>{{ ordinaryIndex + 1 }} / {{ total }} 件 · 第 {{ (activePage?.page ?? 0) + 1 }} / {{ currentWork?.display_page_count || 1 }} 页</small></div><BookmarkButton v-if="currentWork" :key="currentWork.id" :work-id="currentWork.id" :initial="currentWork.bookmark" @message="message($event, true)" /><button class="primary reader-nav-button" aria-label="下一件作品" :disabled="desiredIndex >= total - 1" @click="navigate(1)"><span class="reader-nav-label">下一件</span><Icon name="right" /></button></template><template v-else><button class="softbtn reader-nav-button" aria-label="上一件作品" :disabled="!activePage || activePage.previous_work_index == null" @click="navigate(-1)"><Icon name="left" /><span class="reader-nav-label">上一件</span></button><button class="reading-counter reading-work-info" aria-label="作品信息与整理" title="作品信息与整理" :disabled="!activePage" @click="activePage && managePage(activePage)"><strong>{{ activePage?.title || '流式阅览' }}</strong><small>{{ (activePage?.work_index ?? 0) + 1 }} / {{ total }} 件 · 第 {{ (activePage?.page ?? 0) + 1 }} / {{ activePage?.page_count || 1 }} 页</small></button><BookmarkButton v-if="activePage" :key="activePage.work_id" :work-id="activePage.work_id" @message="message($event, true)" /><button class="primary reader-nav-button" aria-label="下一件作品" :disabled="!activePage || readingOrder === 'sequential' && activePage.work_index >= total - 1 || total <= 1" @click="navigate(1)"><span class="reader-nav-label">下一件</span><Icon name="right" /></button></template></div></footer>
      </main></template>
    </template>
    <ArtistsView v-else-if="section === 'artists'" :inert="mobileMenuOpen ? true : undefined" :artist-id="artistId" @works="artistWorks" @work="artistWork" @message="message" @task-queued="taskPanel?.showTask($event)" />
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
