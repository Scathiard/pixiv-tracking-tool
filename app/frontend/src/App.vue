<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, prepareImage, url, write } from './api'
import { LatestIntent } from './interaction'
import { positions, savePosition } from './positions'
import type { Anchor, GalleryBatch, Mode, PageBatch, PageData, PageLoader, WorkDetail } from './types'
import FilterPanel from './FilterPanel.vue'
import GalleryView from './GalleryView.vue'
import PageReader from './PageReader.vue'
import TaskPanel from './TaskPanel.vue'
import ZoomView from './ZoomView.vue'
import ArtistsView from './views/ArtistsView.vue'
import SettingsView from './views/SettingsView.vue'
import BookmarkButton from './components/BookmarkButton.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'
import Icon from './components/Icon.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import { ingestBookmark, refreshBookmark, resumeBookmarkPolling, syncBookmark } from './state/bookmarks'

const mode = ref<Mode>('gallery'), scope = ref(''), ready = ref(false), pending = ref(false), total = ref(0)
const section = ref<'library' | 'artists' | 'settings'>('library'), artistId = ref<number | null>(null)
const detailOrigin = ref<'gallery' | 'feed' | 'artists'>('gallery'), originArtistId = ref<number | null>(null)
const homeScope = ref(''), searchText = ref(''), sortChoice = ref('newest')
const railCounts = ref({all: 0, recent: 0, favorites: 0})
const router = useRouter(), browserRoute = useRoute()
let internalRoute = '', searchTimer = 0
const composing = ref(false)
const filterOpen = ref(false), infoOpen = ref(false), notification = ref(''), error = ref(''), newContent = ref(false)
const removePrompt = ref(false)
const galleryAnchor = ref(0), galleryWorkId = ref<number | null>(null), pageSize = ref(100), ordinaryIndex = ref(0), desiredIndex = ref(0)
const continuous = ref(false)
const currentWork = ref<WorkDetail | null>(null), infoWork = ref<WorkDetail | null>(null), activePage = ref<PageData | null>(null)
const initialAnchor = ref<Anchor | null>(null), currentAnchor = ref<Anchor | null>(null), zoomPage = ref<PageData | null>(null)
const reader = ref<InstanceType<typeof PageReader> | null>(null), taskPanel = ref<InstanceType<typeof TaskPanel> | null>(null)
const navigation = new LatestIntent(), infoRequest = new LatestIntent()
let saveTime = 0, toastTimer = 0, lastKeyTime = 0, failedNavigation: (() => void) | null = null
const sessionKey = computed(() => mode.value + ':' + (mode.value === 'reader' ? currentWork.value?.id : '') + ':' + scope.value)
const heading = computed(() => mode.value === 'gallery' ? new URLSearchParams(scope.value).has('favorites') ? '收藏作品' : new URLSearchParams(scope.value).has('recent') ? '最近存入' : '我的图库' : activePage.value?.title || currentWork.value?.title || '阅览')
const filterSummary = computed(() => {
  const params = new URLSearchParams(scope.value)
  const labels = [params.has('artist_ids') ? `${params.get('artist_ids')?.split(',').filter(Boolean).length || 0} 位画师` : '全部画师',
    params.has('types') ? `${params.get('types')?.split(',').filter(Boolean).length || 0} 种类型` : '全部类型']
  if (params.has('tags')) labels.push('标签已筛选')
  if (params.has('q')) labels.push(`搜索：${params.get('q')}`)
  return labels.join(' · ')
})
async function updateRailCounts(query: string, ticket: ReturnType<LatestIntent['begin']>) {
  const base = new URLSearchParams(query)
  base.delete('favorites'); base.delete('recent')
  try {
    const batches = await Promise.all(['all', 'recent', 'favorites'].map(kind => {
      const params = new URLSearchParams(base)
      if (kind !== 'all') params.set(kind, '1')
      return api<GalleryBatch>(url('/api/library', params.toString(), {limit: 1}), {signal: ticket.signal})
    }))
    if (ticket.current()) railCounts.value = {all: batches[0]!.total, recent: batches[1]!.total, favorites: batches[2]!.total}
  } catch { /* Counters are optional while the library itself remains usable. */ }
}
function message(text: string, persistent = false) { notification.value = text; clearTimeout(toastTimer); if (!persistent) toastTimer = window.setTimeout(() => { notification.value = '' }, 6500) }
function remember() {
  if (mode.value === 'gallery') { savePosition(scope.value, 'gallery', galleryWorkId.value ? {work_id: galleryWorkId.value, index: galleryAnchor.value} : galleryAnchor.value); return }
  const anchor = reader.value?.readAnchor() || currentAnchor.value
  if (anchor) { savePosition(scope.value, mode.value, anchor); galleryAnchor.value = anchor.work_index; galleryWorkId.value = anchor.work_id; savePosition(scope.value, 'gallery', {work_id: anchor.work_id, index: anchor.work_index}) }
}
function route(push: boolean) {
  stopThemeTransition()
  const path = section.value === 'settings' ? '/settings' : section.value === 'artists' ? artistId.value ? `/artist/${artistId.value}` : '/artists' :
    mode.value === 'gallery' ? '/' : mode.value === 'feed' ? '/flow' : '/illust/' + currentWork.value!.id
  const params = section.value === 'library' ? new URLSearchParams(scope.value) : new URLSearchParams()
  if (section.value === 'library' && mode.value === 'reader' && detailOrigin.value !== 'gallery') {
    params.set('origin', detailOrigin.value)
    if (detailOrigin.value === 'artists' && originArtistId.value) params.set('origin_artist', String(originArtistId.value))
  }
  if (section.value === 'library' && mode.value === 'gallery') params.set('page', String(Math.floor(galleryAnchor.value / pageSize.value) + 1))
  const target = router.resolve({path, query: Object.fromEntries(params)})
  internalRoute = target.fullPath
  void router[push ? 'push' : 'replace'](target).catch(() => { internalRoute = '' })
}
async function initialize(query: string, path = '/', push = false, restoreHistory = false) {
  if (path === '/settings' || path === '/artists' || /^\/artist\/\d+$/.test(path)) {
    navigation.cancel(); infoRequest.cancel()
    section.value = path === '/settings' ? 'settings' : 'artists'
    artistId.value = path.startsWith('/artist/') ? Number(path.split('/').at(-1)) : null
    ready.value = true; pending.value = false; error.value = ''
    if (push) route(true)
    return
  }
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  try {
    const params = new URLSearchParams(query)
    if (/^\/illust\/\d+$/.test(path)) {
      detailOrigin.value = params.get('origin') === 'artists' ? 'artists' : params.get('origin') === 'feed' ? 'feed' : 'gallery'
      originArtistId.value = Number(params.get('origin_artist')) || null
    }
    const explicitPage = params.get('page')
    for (const name of ['page', 'offset', 'limit', 'page_offset', 'mode', 'origin', 'origin_artist']) params.delete(name)
    const batch = await api<GalleryBatch>(url('/api/library', params.toString(), {limit: 1}), {signal: ticket.signal})
    if (!ticket.current()) return
    params.set('ceiling', String(batch.ceiling)); params.set('as_of', batch.as_of)
    scope.value = params.toString(); total.value = batch.total
    void updateRailCounts(scope.value, ticket)
    section.value = 'library'
    searchText.value = params.get('q') || ''; sortChoice.value = params.get('sort') || 'newest'
    const pageAnchor = explicitPage ? Math.max(0, Number(explicitPage) - 1) * pageSize.value : 0
    const savedGallery = positions(scope.value).gallery
    const savedIndex = typeof savedGallery === 'number' ? savedGallery : savedGallery?.index
    let wantedAnchor = restoreHistory || !explicitPage ? savedIndex ?? pageAnchor : pageAnchor
    if (restoreHistory && typeof savedGallery === 'object' && savedGallery.work_id) {
      try { const found = await api<Anchor>(url('/api/library/locate', params.toString(), {work_id: savedGallery.work_id}), {signal: ticket.signal}); wantedAnchor = found.work_index; galleryWorkId.value = savedGallery.work_id }
      catch { /* A removed work falls back to its former position. */ }
    }
    if (!ticket.current()) return
    galleryAnchor.value = Math.min(Math.max(0, batch.total - 1), wantedAnchor)
    mode.value = 'gallery'; ready.value = true; pending.value = false; newContent.value = false
    filterOpen.value = false; infoOpen.value = false; activePage.value = null; currentAnchor.value = null
    const match = path.match(/^\/illust\/(\d+)$/)
    if (match) {
      const id = Number(match[1]), saved = positions(scope.value).reader
      const position = await api<Anchor>(url('/api/library/locate', scope.value, {work_id: id}), {signal: ticket.signal})
      if (ticket.current()) await openWork(position.work_index, saved?.work_id === id ? saved : null, false)
    } else if (path === '/flow') await enterFeed(false)
    else route(push)
  } catch (failure) {
    if (ticket.current()) { error.value = failure instanceof Error ? failure.message : '图库未能载入'; pending.value = false; ready.value = true }
  }
}
async function openWork(index: number, saved: Anchor | null = null, push = true) {
  if (!total.value) return
  remember()
  index = Math.max(0, Math.min(total.value - 1, index))
  desiredIndex.value = index
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void openWork(index, saved, push) }
  try {
    const batch = await api<GalleryBatch>(url('/api/library', scope.value, {offset: index, limit: 1}), {signal: ticket.signal})
    const work = batch.items[0]
    if (!work) throw new Error('目标作品已不在这个范围内，请刷新图库')
    const detail = await api<WorkDetail>(`/api/works/${work.id}?page_limit=1&page_offset=${saved?.page || 0}`, {signal: ticket.signal})
    await prepareImage(detail.pages[0]?.src || null, ticket.signal)
    if (!ticket.current()) return
    const previousMode = mode.value
    currentWork.value = infoWork.value = detail; ordinaryIndex.value = index; total.value = batch.total
    ingestBookmark(detail.id, detail.bookmark)
    refreshBookmark(detail.id)
    initialAnchor.value = {work_id: work.id, work_index: index, page: Math.min(saved?.page || 0, detail.display_page_count - 1),
      index: Math.min(saved?.page || 0, detail.display_page_count - 1), offset: saved?.offset || 0}
    activePage.value = null; currentAnchor.value = initialAnchor.value
    if (previousMode === 'feed') detailOrigin.value = 'feed'
    mode.value = 'reader'; section.value = 'library'; filterOpen.value = false; galleryAnchor.value = index; galleryWorkId.value = work.id
    route(push && previousMode !== 'reader')
    await nextTick()
  } catch (failure) {
    if (ticket.current()) { error.value = failure instanceof Error ? failure.message : '下一件作品未能载入'; desiredIndex.value = ordinaryIndex.value }
  } finally { if (ticket.current()) pending.value = false }
}
function navigate(delta: number) { if (!zoomPage.value) void openWork(desiredIndex.value + delta) }
async function enterFeed(push = true) {
  if (mode.value === 'feed') return
  if (!total.value) return
  remember()
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  failedNavigation = () => { void enterFeed(push) }
  try {
    let saved = positions(scope.value).feed
    let found: Anchor | null = null
    if (saved) {
      try { found = await api<Anchor>(url('/api/library/locate', scope.value, {work_id: saved.work_id, page: saved.page}), {signal: ticket.signal}) }
      catch (failure) { if (ticket.signal.aborted) throw failure; saved = undefined }
    }
    if (!found) {
      const batch = await api<GalleryBatch>(url('/api/library', scope.value, {offset: galleryAnchor.value, limit: 1}), {signal: ticket.signal})
      if (!batch.items[0]) throw new Error('当前范围已变化，请刷新图库')
      found = await api<Anchor>(url('/api/library/locate', scope.value, {work_id: batch.items[0].id}), {signal: ticket.signal})
    }
    if (!ticket.current()) return
    initialAnchor.value = {...found, offset: saved?.offset || 0}; currentAnchor.value = initialAnchor.value
    activePage.value = null; infoWork.value = null; infoOpen.value = false; filterOpen.value = false
    mode.value = 'feed'; section.value = 'library'; route(push)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '流式阅览未能打开' }
  finally { if (ticket.current()) pending.value = false }
}
function gallery(push = true) {
  if (section.value !== 'library') { void initialize(homeScope.value, '/', push, true); return }
  remember(); navigation.cancel(); infoRequest.cancel(); pending.value = false; error.value = ''
  mode.value = 'gallery'; infoOpen.value = false; zoomPage.value = null; route(push)
}
const loader = computed<PageLoader>(() => {
  const query = scope.value
  if (mode.value === 'feed') return (offset, limit, signal) => api<PageBatch>(url('/api/library/feed', query, {offset, limit}), {signal})
  const work = currentWork.value!, workIndex = ordinaryIndex.value
  return async (offset, limit, signal) => {
    const detail = await api<WorkDetail>(`/api/works/${work.id}?page_offset=${offset}&page_limit=${limit}`, {signal})
    return {offset, total: detail.display_page_count, items: detail.pages.map(page => ({...page, work_id: work.id, work_index: workIndex,
      page: page.index, page_count: detail.display_page_count, title: detail.title, artist_name: detail.artist_name,
      artist_id: detail.artist_id, pixiv_id: detail.pixiv_id, type: detail.type, x_restrict: detail.x_restrict, next_work_index: detail.display_page_count}))}
  }
})
function focused(page: PageData, anchor: Anchor) {
  const changedWork = activePage.value?.work_id !== page.work_id
  activePage.value = page; currentAnchor.value = anchor
  if (changedWork) refreshBookmark(page.work_id)
  if (Date.now() - saveTime > 400) { saveTime = Date.now(); savePosition(scope.value, mode.value === 'feed' ? 'feed' : 'reader', anchor) }
  if (infoOpen.value && infoWork.value?.id !== page.work_id) void loadInfo(page.work_id)
}
async function loadInfo(id: number) {
  const ticket = infoRequest.begin()
  try { const work = await api<WorkDetail>(`/api/works/${id}?page_limit=1`, {signal: ticket.signal}); if (ticket.current()) { infoWork.value = work; ingestBookmark(id, work.bookmark); refreshBookmark(id) } }
  catch (failure) { if (ticket.current()) message(failure instanceof Error ? failure.message : '信息未能载入', true) }
}
async function task(kind: string, workId?: number) {
  try {
    const result = await write<{operation_id: string; created: boolean}>('/api/tasks', {kind, ...(workId ? {work_ids: [workId]} : {})})
    message(result.created ? '操作已排队，可在任务记录中查看' : '已有同类任务，已定位对应记录')
    await taskPanel.value?.showTask(result.operation_id)
  } catch (failure) { message(failure instanceof Error ? failure.message : '任务未能提交', true) }
}
async function hidden() {
  if (!infoWork.value) return
  try { await write(`/api/works/${infoWork.value.id}/preferences`, {hidden: !infoWork.value.is_hidden}, 'PATCH'); const text = infoWork.value.is_hidden ? '已取消隐藏' : '已隐藏作品'; gallery(); message(text + '，已回到对应图库位置') }
  catch (failure) { message(failure instanceof Error ? failure.message : '操作失败', true) }
}
async function bookmarkSync() { if (infoWork.value) try { await syncBookmark(infoWork.value.id) } catch (failure) { message(failure instanceof Error ? failure.message : '收藏状态未能核对', true) } }
async function remove() {
  if (!infoWork.value) return
  removePrompt.value = false
  try {
    const result = await write<{operation_id: string}>('/api/works/delete', {work_ids: [infoWork.value.id], confirmed: true})
    gallery(); await taskPanel.value?.showTask(result.operation_id)
  } catch (failure) { message(failure instanceof Error ? failure.message : '删除未成功', true) }
}
function key(event: KeyboardEvent) {
  if (section.value !== 'library' || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || zoomPage.value ||
      (event.target as Element).closest('input,textarea,select,[contenteditable],[role="dialog"],[data-no-navigation]')) return
  if (event.key === 'Escape') { if (infoOpen.value) infoOpen.value = false; else if (filterOpen.value) filterOpen.value = false; else if (mode.value !== 'gallery') gallery(); return }
  if (mode.value === 'reader' && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    event.preventDefault(); if (event.repeat && Date.now() - lastKeyTime < 160) return
    lastKeyTime = Date.now(); navigate(event.key === 'ArrowLeft' ? -1 : 1)
  } else if (mode.value !== 'gallery' && ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown'].includes(event.key)) {
    event.preventDefault(); reader.value?.scrollBy((event.key.includes('Up') ? -1 : 1) * (event.key.startsWith('Page') ? window.innerHeight * 0.7 : 130))
  }
}
function setHomeScope(query: string) {
  const params = new URLSearchParams(query)
  for (const name of ['ceiling', 'as_of', 'page', 'offset', 'limit', 'page_offset', 'mode', 'origin', 'origin_artist']) params.delete(name)
  homeScope.value = params.toString()
  try { localStorage.setItem('pixiv-local-home-scope', homeScope.value) } catch { /* optional */ }
}
function galleryFocused(index: number, workId: number) { galleryAnchor.value = index; galleryWorkId.value = workId }
function filter(query: string) { remember(); setHomeScope(query); void initialize(query, '/', true) }
function refresh() { const params = new URLSearchParams(scope.value); params.delete('ceiling'); params.delete('as_of'); void initialize(params.toString()) }
function retryNavigation() { if (failedNavigation) failedNavigation(); else refresh() }
function changeRail(rail: 'all' | 'recent' | 'favorites') {
  const params = new URLSearchParams(scope.value)
  for (const name of ['ceiling', 'as_of', 'page', 'favorites', 'recent']) params.delete(name)
  if (rail !== 'all') params.set(rail, '1')
  setHomeScope(params.toString()); void initialize(homeScope.value, '/', true)
}
function openFromGallery(index: number) { detailOrigin.value = 'gallery'; void openWork(index) }
function tagFilter(tag: string) {
  const params = new URLSearchParams(homeScope.value)
  params.delete('ceiling'); params.delete('as_of'); params.delete('favorites'); params.delete('recent'); params.set('tags', tag)
  setHomeScope(params.toString()); void initialize(homeScope.value, '/', true)
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
    setHomeScope(params.toString()); void initialize(homeScope.value, '/', false)
  }, 280)
}
function sort() {
  const params = new URLSearchParams(scope.value)
  for (const name of ['ceiling', 'as_of', 'page']) params.delete(name)
  params.set('sort', sortChoice.value); setHomeScope(params.toString()); void initialize(homeScope.value, '/', true)
}
function showArtists(id: number | null = null) { remember(); navigation.cancel(); infoRequest.cancel(); pending.value = false; section.value = 'artists'; artistId.value = id; filterOpen.value = false; infoOpen.value = false; route(true) }
function showSettings() { remember(); navigation.cancel(); infoRequest.cancel(); pending.value = false; section.value = 'settings'; filterOpen.value = false; infoOpen.value = false; route(true) }
async function artistWorks(id: number) {
  detailOrigin.value = 'artists'; originArtistId.value = id
  const params = new URLSearchParams(homeScope.value); params.delete('ceiling'); params.delete('as_of'); params.delete('favorites'); params.delete('recent'); params.set('artist_ids', String(id))
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
function brandHome() { remember(); detailOrigin.value = 'gallery'; void initialize(homeScope.value, '/', true, true) }
function foreground() { resumeBookmarkPolling(); if (section.value === 'library' && mode.value !== 'gallery') { const id = activePage.value?.work_id || currentWork.value?.id; if (id) refreshBookmark(id) } }
let themeOverlay: HTMLElement | null = null
function stopThemeTransition() { themeOverlay?.remove(); themeOverlay = null }
function chooseTheme(theme: 'dark' | 'light', event: MouseEvent) {
  const old = document.body.dataset.theme === 'light' ? 'light' : 'dark'
  if (old === theme) return
  stopThemeTransition()
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!reduce) {
    const panel = document.querySelector('.settings-page')
    if (panel instanceof HTMLElement) {
      const overlay = document.createElement('div')
      overlay.className = 'theme-snapshot'; overlay.dataset.theme = old; overlay.inert = true
      overlay.setAttribute('aria-hidden', 'true')
      const clone = panel.cloneNode(true) as HTMLElement
      clone.style.marginTop = `${panel.getBoundingClientRect().top}px`
      for (const element of [clone, ...clone.querySelectorAll<HTMLElement>('*')]) {
        for (const attribute of [...element.attributes]) if (attribute.name === 'id' || attribute.name.startsWith('data-') || attribute.name === 'href') element.removeAttribute(attribute.name)
        if (element instanceof HTMLInputElement) { element.value = ''; element.removeAttribute('value') }
      }
      overlay.append(clone)
      const target = event.currentTarget as HTMLElement
      const rect = target.getBoundingClientRect()
      overlay.style.setProperty('--theme-x', `${rect.left + rect.width / 2}px`)
      overlay.style.setProperty('--theme-y', `${rect.top + rect.height / 2}px`)
      document.body.append(overlay); themeOverlay = overlay
      const diagonal = Math.hypot(innerWidth, innerHeight) * 1.5
      const animation = overlay.animate([{'--theme-radius': '0px'}, {'--theme-radius': `${diagonal}px`}], {duration: 280, easing: 'ease-out'})
      animation.onfinish = () => { overlay.remove(); if (themeOverlay === overlay) themeOverlay = null }
    }
  }
  document.body.dataset.theme = theme
  try { localStorage.setItem('pixiv-local-theme', theme) } catch { /* optional */ }
}
watch(() => browserRoute.fullPath, path => {
  if (path === internalRoute) { internalRoute = ''; return }
  remember(); void initialize(browserRoute.fullPath.slice(browserRoute.path.length).replace(/^\?/, ''), browserRoute.path, false, true)
})
onMounted(async () => {
  try { document.body.dataset.theme = localStorage.getItem('pixiv-local-theme') === 'light' ? 'light' : 'dark' } catch { document.body.dataset.theme = 'dark' }
  try { setHomeScope(localStorage.getItem('pixiv-local-home-scope') || '') } catch { /* optional */ }
  if (location.pathname === '/') setHomeScope(location.search.replace(/^\?/, ''))
  try {
    const defaults = await api<{page_size: number; pagination_mode: string}>('/api/status')
    if ([50, 100, 200].includes(defaults.page_size)) pageSize.value = defaults.page_size
    continuous.value = defaults.pagination_mode === 'scroll'
    const savedSize = localStorage.getItem('pixiv-local-page-size')
    if (savedSize === 'scroll') continuous.value = true
    else if (savedSize && [50, 100, 200].includes(Number(savedSize))) { continuous.value = false; pageSize.value = Number(savedSize) }
  } catch { /* Defaults do not block local browsing. */ }
  void initialize(location.search, location.pathname, false, true)
  window.addEventListener('keydown', key); window.addEventListener('pagehide', remember)
  window.addEventListener('focus', foreground)
  window.addEventListener('scroll', stopThemeTransition, true); window.addEventListener('resize', stopThemeTransition)
})
onBeforeUnmount(() => { remember(); navigation.cancel(); infoRequest.cancel(); clearTimeout(toastTimer); clearTimeout(searchTimer); stopThemeTransition(); window.removeEventListener('keydown', key); window.removeEventListener('pagehide', remember); window.removeEventListener('focus', foreground); window.removeEventListener('scroll', stopThemeTransition, true); window.removeEventListener('resize', stopThemeTransition) })
function changePageSize(value: string) {
  continuous.value = value === 'scroll'
  if (!continuous.value) pageSize.value = Number(value)
  try { localStorage.setItem('pixiv-local-page-size', value) } catch { /* optional preference */ }
}
function updated() { newContent.value = true; window.dispatchEvent(new Event('archive-updated')) }
</script>
<template>
  <div class="app-shell" :inert="zoomPage ? true : undefined">
    <header class="app-header"><button class="brand" @click="brandHome" aria-label="返回我的图库"><span class="brand-mark">P</span><span><span class="brand-pixiv">Pixiv</span>Tracker</span></button><nav aria-label="主导航"><button :class="{selected: section === 'library' && mode !== 'feed'}" @click="brandHome"><Icon name="grid" />作品</button><button :class="{selected: section === 'artists'}" @click="showArtists()"><Icon name="artists" />画师</button><button :class="{selected: section === 'library' && mode === 'feed'}" :disabled="!total" @click="enterFeed()"><Icon name="flow" />流式</button><button :class="{selected: section === 'settings'}" @click="showSettings"><Icon name="settings" />设置</button></nav></header>
    <TaskPanel ref="taskPanel" @updated="updated" />
    <template v-if="section === 'library'">
      <template v-if="mode === 'gallery'">
        <div class="gallery-layout"><aside class="gallery-rail"><div class="rail-overline">我的图库</div><nav aria-label="图库范围"><button :class="{active: currentRail() === 'all'}" :aria-pressed="currentRail() === 'all'" @click="changeRail('all')">全部作品 <span>{{ railCounts.all }}</span></button><button :class="{active: currentRail() === 'recent'}" :aria-pressed="currentRail() === 'recent'" @click="changeRail('recent')">最近存入 <span>{{ railCounts.recent }}</span></button><button :class="{active: currentRail() === 'favorites'}" :aria-pressed="currentRail() === 'favorites'" @click="changeRail('favorites')">收藏 <span>{{ railCounts.favorites }}</span></button></nav><p>作品保存在本地<br>离线也能继续阅读</p></aside>
          <main class="gallery-main"><div class="gallery-intro"><div><h1>{{ heading }}</h1><p>本地存档 · {{ total }} 件作品</p></div></div>
            <div class="library-tabs" aria-label="图库范围"><button :class="{active: currentRail() === 'all'}" @click="changeRail('all')">全部</button><button :class="{active: currentRail() === 'recent'}" @click="changeRail('recent')">最近</button><button :class="{active: currentRail() === 'favorites'}" @click="changeRail('favorites')">收藏</button></div>
            <div class="gallery-controls"><label class="search-field"><Icon name="search" /><input v-model="searchText" type="search" placeholder="搜索作品、画师或标签" aria-label="搜索作品、画师或标签" @compositionstart="composing = true" @compositionend="composing = false; search()" @input="search"></label><select v-model="sortChoice" aria-label="图库排序" @change="sort"><option value="newest">最新投稿</option><option value="oldest">最早投稿</option><option value="added">最近存入</option><option value="title">作品名称</option></select><button class="softbtn" :aria-expanded="filterOpen" @click="filterOpen = true"><Icon name="filter" />筛选</button></div>
            <div class="filter-summary-line"><span>{{ filterSummary }}</span><span>{{ total }} 件作品</span></div>
            <div v-if="newContent" class="update-notice"><span>后台任务已结束，当前位置已保留。</span><button @click="refresh">刷新图库</button><button class="quiet" @click="newContent = false">关闭</button></div>
            <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">重试</button></div>
            <GalleryView v-if="ready" :query="scope" :start-index="galleryAnchor" :page-size="pageSize" :continuous="continuous" @open="openFromGallery" @artist="showArtists" @message="message($event, true)" @anchor="galleryFocused" @total="total = $event" @page="galleryAnchor = $event * pageSize; route(false)" /><div v-else class="empty">正在打开本地图库…</div>
          </main></div>
      </template>
      <template v-else><main class="reader-layout">
        <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">重试</button><button class="quiet" @click="error = ''">关闭</button></div>
        <div class="reader-stage"><Transition name="work"><PageReader :key="sessionKey" ref="reader" :mode="mode === 'feed' ? 'feed' : 'reader'" :loader="loader" :session-key="sessionKey" :initial="initialAnchor" :inactive="!!zoomPage" @focus="focused" @navigate="navigate" @zoom="zoomPage = $event">
          <template #before><div v-if="mode === 'reader' && currentWork" class="detail-head"><div class="detail-nav"><button class="detail-back" @click="backFromDetail"><Icon name="left" />{{ detailOrigin === 'artists' ? '返回画师' : detailOrigin === 'feed' ? '返回流式阅览' : '返回图库' }}</button><span>{{ ordinaryIndex + 1 }} / {{ total }}</span></div><div class="detail-title-row"><h1>{{ currentWork.title }}</h1><RestrictionBadge :value="currentWork.x_restrict" /><span class="detail-badge">{{ currentWork.type === 'ugoira' ? '动图' : currentWork.type === 'manga' ? '漫画' : '插画' }}</span><span v-if="currentWork.display_page_count > 1" class="detail-badge">{{ currentWork.display_page_count }} 页</span></div><div class="detail-meta"><span>画师：<button class="text-link" @click="showArtists(currentWork.artist_id)">{{ currentWork.artist_name }}</button></span><span v-if="currentWork.posted_at">发布于 {{ currentWork.posted_at.slice(0, 10) }}</span></div><div class="detail-tags"><button v-for="tag in currentWork.tags" :key="tag" class="tag" @click="tagFilter(tag)">#{{ tag }}</button></div><div class="detail-actions"><a class="text-link" :href="`https://www.pixiv.net/artworks/${currentWork.pixiv_id}`" target="_blank" rel="noopener noreferrer">在 Pixiv 查看 <Icon name="external" /></a><button class="softbtn" @click="openFolder(currentWork.id)"><Icon name="folder" />本地文件</button><div class="detail-action-right"><button class="softbtn" @click="infoOpen = true; loadInfo(currentWork.id)">整理作品</button></div></div></div><div v-else class="stream-head"><div><h1>流式阅览</h1><p>当前图库范围 · {{ total }} 件作品</p></div><button class="softbtn" @click="gallery()"><Icon name="grid" />返回图库</button></div></template>
          <template #after><template v-if="mode === 'reader' && currentWork"><div class="detail-bottom-actions"><BookmarkButton :work-id="currentWork.id" :initial="currentWork.bookmark" @message="message($event, true)" /></div><section class="detail-caption" aria-label="作品简介"><h2>作品简介</h2><div v-if="currentWork.caption" v-html="currentWork.caption" /><p v-else>{{ currentWork.caption_state === 'empty' ? '作者没有填写简介' : '尚未取得简介' }}</p></section></template><div v-else class="stream-end"><p>已到当前范围末尾</p><button @click="gallery()">返回图库</button></div></template>
        </PageReader></Transition></div>
        <aside v-if="infoOpen" class="info-panel" data-no-navigation aria-label="作品整理"><header><h2>作品整理</h2><button @click="infoOpen = false" aria-label="关闭作品整理"><Icon name="close" /></button></header><template v-if="infoWork && (mode === 'reader' || infoWork.id === activePage?.work_id)"><h3>{{ infoWork.title }}</h3><p class="muted">{{ infoWork.artist_name }}</p><p class="muted">{{ infoWork.bookmark_count ?? '—' }} 收藏 · {{ infoWork.view_count ?? '—' }} 浏览</p><div class="tags"><span v-for="tag in infoWork.tags" :key="tag">{{ tag }}</span></div><div class="info-actions"><BookmarkButton :work-id="infoWork.id" :initial="infoWork.bookmark" @message="message($event, true)" /><button @click="bookmarkSync"><Icon name="refresh" />同步收藏状态</button><button @click="hidden">{{ infoWork.is_hidden ? '取消隐藏' : '隐藏此作品' }}</button><label>本地评分 <select :value="infoWork.rating" @change="rate(Number(($event.target as HTMLSelectElement).value))"><option :value="0">未评分</option><option v-for="value in [1,2,3,4,5]" :key="value" :value="value">{{ value }} 星</option></select></label><button @click="task('repair', infoWork.id)">补下载此作品</button><button v-if="infoWork.type === 'ugoira'" @click="task('backfill', infoWork.id)">补回动图原包</button><button class="danger" @click="removePrompt = true">永久删除</button></div><details v-if="infoWork.legacy_variants?.length" class="asset-detail"><summary>旧档案的同页文件</summary><div v-for="group in infoWork.legacy_variants" :key="group.page"><p>第 {{ group.page + 1 }} 页</p><p v-for="file in group.files" :key="file.name"><a v-if="file.src" :href="file.src" target="_blank" rel="noopener noreferrer">{{ file.default ? '默认显示' : '另一份' }}：{{ file.name }}</a><span v-else>{{ file.name }}（不可用）</span></p></div></details><details class="asset-detail"><summary>存档状态</summary><p v-for="asset in infoWork.assets" :key="asset.id">{{ asset.role === 'image' ? `第 ${asset.page + 1} 页` : asset.role === 'ugoira_zip' ? '动图原包' : '播放时序' }}：{{ asset.state === 'verified' ? '已核验' : asset.error || '待核验或缺失' }}</p></details></template><p v-else>正在读取作品信息…</p></aside>
        <footer class="reading-toolbar"><div class="reading-toolbar-inner"><template v-if="mode === 'reader'"><button class="softbtn" :disabled="desiredIndex <= 0" @click="navigate(-1)"><Icon name="left" />上一件</button><div class="reading-counter"><strong>{{ currentWork?.title }}</strong><small>{{ ordinaryIndex + 1 }} / {{ total }} 件 · 第 {{ (activePage?.page ?? 0) + 1 }} / {{ currentWork?.display_page_count || 1 }} 页</small></div><button class="primary" :disabled="desiredIndex >= total - 1" @click="navigate(1)">下一件<Icon name="right" /></button></template><template v-else><div class="reading-counter"><strong>{{ activePage?.title || '流式阅览' }}</strong><small>{{ (activePage?.work_index ?? 0) + 1 }} / {{ total }} 件 · 第 {{ (activePage?.page ?? 0) + 1 }} / {{ activePage?.page_count || 1 }} 页</small></div><BookmarkButton v-if="activePage" :key="activePage.work_id" :work-id="activePage.work_id" @message="message($event, true)" /><button class="primary" :disabled="!activePage || activePage.work_index >= total - 1" @click="reader?.jump(activePage!.next_work_index)">跳过作品<Icon name="down" /></button></template></div></footer>
      </main></template>
    </template>
    <ArtistsView v-else-if="section === 'artists'" :artist-id="artistId" @works="artistWorks" @work="artistWork" @message="message($event, true)" />
    <SettingsView v-else :gallery-mode="continuous ? 'scroll' : String(pageSize)" @gallery-mode="changePageSize" @task="task" @history="taskPanel?.openHistory()" @theme="chooseTheme" @message="message($event, true)" />
    <FilterPanel v-if="filterOpen && section === 'library' && mode === 'gallery'" :key="scope" :query="scope" @apply="filter" @close="filterOpen = false" />
    <div v-if="notification" class="toast" role="status">{{ notification }}<button @click="notification = ''" aria-label="关闭提示"><Icon name="close" /></button></div>
  </div>
  <ZoomView v-if="zoomPage" :page="zoomPage" @close="zoomPage = null" />
  <ConfirmDialog v-if="removePrompt" title="永久删除作品" description="这会删除作品和本地文件，并阻止旧任务再次下载。" action="永久删除" danger @cancel="removePrompt = false" @confirm="remove" />
</template>
