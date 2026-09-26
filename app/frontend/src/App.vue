<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, prepareImage, url, write } from './api'
import { LatestIntent } from './interaction'
import { positions, savePosition } from './positions'
import type { Anchor, GalleryBatch, Mode, PageBatch, PageData, PageLoader, WorkDetail } from './types'
import FilterPanel from './FilterPanel.vue'
import GalleryView from './GalleryView.vue'
import PageReader from './PageReader.vue'
import TaskPanel from './TaskPanel.vue'
import ZoomView from './ZoomView.vue'

const mode = ref<Mode>('gallery'), scope = ref(''), ready = ref(false), pending = ref(false), total = ref(0)
const filterOpen = ref(false), infoOpen = ref(false), notification = ref(''), error = ref(''), newContent = ref(false)
const galleryAnchor = ref(0), pageSize = ref(100), ordinaryIndex = ref(0), desiredIndex = ref(0)
const continuous = ref(false)
const currentWork = ref<WorkDetail | null>(null), infoWork = ref<WorkDetail | null>(null), activePage = ref<PageData | null>(null)
const initialAnchor = ref<Anchor | null>(null), currentAnchor = ref<Anchor | null>(null), zoomPage = ref<PageData | null>(null)
const reader = ref<InstanceType<typeof PageReader> | null>(null), taskPanel = ref<InstanceType<typeof TaskPanel> | null>(null)
const navigation = new LatestIntent(), infoRequest = new LatestIntent()
let saveTime = 0, toastTimer = 0, lastKeyTime = 0, failedNavigation: (() => void) | null = null
const sessionKey = computed(() => mode.value + ':' + (mode.value === 'reader' ? currentWork.value?.id : '') + ':' + scope.value)
const subtitle = computed(() => mode.value === 'gallery' ? `${total.value} 件作品，保存在本地` :
  activePage.value ? `${activePage.value.artist_name} · 第 ${activePage.value.page + 1} / ${activePage.value.page_count} 页` : '正在读取图片')
const heading = computed(() => mode.value === 'gallery' ? '本地图库' : activePage.value?.title || currentWork.value?.title || '阅览')
function message(text: string, persistent = false) { notification.value = text; clearTimeout(toastTimer); if (!persistent) toastTimer = window.setTimeout(() => { notification.value = '' }, 6500) }
function remember() {
  if (mode.value === 'gallery') { savePosition(scope.value, 'gallery', galleryAnchor.value); return }
  const anchor = reader.value?.readAnchor() || currentAnchor.value
  if (anchor) { savePosition(scope.value, mode.value, anchor); galleryAnchor.value = anchor.work_index; savePosition(scope.value, 'gallery', anchor.work_index) }
}
function route(push: boolean) {
  const path = mode.value === 'gallery' ? '/' : mode.value === 'feed' ? '/flow' : '/illust/' + currentWork.value!.id
  const params = new URLSearchParams(scope.value)
  if (mode.value === 'gallery') params.set('page', String(Math.floor(galleryAnchor.value / pageSize.value) + 1))
  history[push ? 'pushState' : 'replaceState']({pixivLocal: true}, '', path + '?' + params.toString())
}
async function initialize(query: string, path = '/', push = false, restoreHistory = false) {
  const ticket = navigation.begin(); pending.value = true; error.value = ''
  try {
    const params = new URLSearchParams(query)
    const explicitPage = params.get('page')
    for (const name of ['page', 'offset', 'limit', 'page_offset', 'mode']) params.delete(name)
    const batch = await api<GalleryBatch>(url('/api/library', params.toString(), {limit: 1}), {signal: ticket.signal})
    if (!ticket.current()) return
    params.set('ceiling', String(batch.ceiling)); params.set('as_of', batch.as_of)
    scope.value = params.toString(); total.value = batch.total
    const pageAnchor = explicitPage ? Math.max(0, Number(explicitPage) - 1) * pageSize.value : 0
    const wantedAnchor = restoreHistory || !explicitPage ? positions(scope.value).gallery ?? pageAnchor : pageAnchor
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
    initialAnchor.value = {work_id: work.id, work_index: index, page: Math.min(saved?.page || 0, detail.display_page_count - 1),
      index: Math.min(saved?.page || 0, detail.display_page_count - 1), offset: saved?.offset || 0}
    activePage.value = null; currentAnchor.value = initialAnchor.value
    mode.value = 'reader'; filterOpen.value = false; galleryAnchor.value = index
    route(push && previousMode !== 'reader')
    await nextTick()
  } catch (failure) {
    if (ticket.current()) { error.value = failure instanceof Error ? failure.message : '下一件作品未能载入'; desiredIndex.value = ordinaryIndex.value }
  } finally { if (ticket.current()) pending.value = false }
}
function navigate(delta: number) { if (!zoomPage.value) void openWork(desiredIndex.value + delta) }
async function continueReading() {
  if (mode.value === 'reader') return
  const ticket = navigation.begin()
  const saved = positions(scope.value).reader
  if (saved) {
    try {
      const found = await api<Anchor>(url('/api/library/locate', scope.value, {work_id: saved.work_id, page: saved.page}), {signal: ticket.signal})
      if (ticket.current()) await openWork(found.work_index, saved)
      return
    } catch { if (!ticket.current()) return; message('上次位置已不在当前范围，将从目前的图库位置开始') }
  }
  if (ticket.current()) await openWork(galleryAnchor.value)
}
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
    mode.value = 'feed'; route(push)
  } catch (failure) { if (ticket.current()) error.value = failure instanceof Error ? failure.message : '流式阅览未能打开' }
  finally { if (ticket.current()) pending.value = false }
}
function gallery(push = true) {
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
      artist_id: detail.artist_id, pixiv_id: detail.pixiv_id, type: detail.type, next_work_index: detail.display_page_count}))}
  }
})
function focused(page: PageData, anchor: Anchor) {
  activePage.value = page; currentAnchor.value = anchor
  if (Date.now() - saveTime > 400) { saveTime = Date.now(); savePosition(scope.value, mode.value === 'feed' ? 'feed' : 'reader', anchor) }
  if (infoOpen.value && infoWork.value?.id !== page.work_id) void loadInfo(page.work_id)
}
async function loadInfo(id: number) {
  const ticket = infoRequest.begin()
  try { const work = await api<WorkDetail>(`/api/works/${id}?page_limit=1`, {signal: ticket.signal}); if (ticket.current()) infoWork.value = work }
  catch (failure) { if (ticket.current()) message(failure instanceof Error ? failure.message : '信息未能载入', true) }
}
function toggleInfo() { infoOpen.value = !infoOpen.value; if (infoOpen.value && activePage.value) void loadInfo(activePage.value.work_id) }
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
async function bookmark() {
  if (!infoWork.value) return
  try {
    const result = await write<{is_bookmarked: boolean}>(`/api/works/${infoWork.value.id}/bookmark`, {desired: !infoWork.value.is_bookmarked})
    infoWork.value.is_bookmarked = result.is_bookmarked; message('Pixiv 收藏状态已确认')
  } catch (failure) { message(failure instanceof Error ? failure.message : '收藏操作未成功', true) }
}
async function remove() {
  if (!infoWork.value || !window.confirm('永久删除这件作品及本地文件，并阻止旧任务将它下载回来？')) return
  try {
    const result = await write<{operation_id: string}>('/api/works/delete', {work_ids: [infoWork.value.id], confirmed: true})
    gallery(); await taskPanel.value?.showTask(result.operation_id)
  } catch (failure) { message(failure instanceof Error ? failure.message : '删除未成功', true) }
}
function key(event: KeyboardEvent) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || zoomPage.value ||
      (event.target as Element).closest('input,textarea,select,[contenteditable],[role="dialog"],[data-no-navigation]')) return
  if (event.key === 'Escape') { if (infoOpen.value) infoOpen.value = false; else if (filterOpen.value) filterOpen.value = false; else if (mode.value !== 'gallery') gallery(); return }
  if (mode.value === 'reader' && ['ArrowLeft', 'ArrowRight'].includes(event.key)) {
    event.preventDefault(); if (event.repeat && Date.now() - lastKeyTime < 160) return
    lastKeyTime = Date.now(); navigate(event.key === 'ArrowLeft' ? -1 : 1)
  } else if (mode.value !== 'gallery' && ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown'].includes(event.key)) {
    event.preventDefault(); reader.value?.scrollBy((event.key.includes('Up') ? -1 : 1) * (event.key.startsWith('Page') ? window.innerHeight * 0.7 : 130))
  }
}
function pop() { remember(); void initialize(location.search, location.pathname, false, true) }
function filter(query: string) { remember(); void initialize(query, '/', true) }
function refresh() { const params = new URLSearchParams(scope.value); params.delete('ceiling'); params.delete('as_of'); void initialize(params.toString()) }
function retryNavigation() { if (failedNavigation) failedNavigation(); else refresh() }
onMounted(async () => {
  try {
    const defaults = await api<{page_size: number; pagination_mode: string}>('/api/status')
    if ([50, 100, 200, 500].includes(defaults.page_size)) pageSize.value = defaults.page_size
    continuous.value = defaults.pagination_mode === 'scroll'
    const savedSize = localStorage.getItem('pixiv-local-page-size')
    if (savedSize === 'scroll') continuous.value = true
    else if (savedSize && [50, 100, 200, 500].includes(Number(savedSize))) { continuous.value = false; pageSize.value = Number(savedSize) }
  } catch { /* Defaults do not block local browsing. */ }
  void initialize(location.search, location.pathname, false, !!history.state?.pixivLocal)
  window.addEventListener('keydown', key); window.addEventListener('popstate', pop); window.addEventListener('pagehide', remember)
})
onBeforeUnmount(() => { remember(); navigation.cancel(); infoRequest.cancel(); clearTimeout(toastTimer); window.removeEventListener('keydown', key); window.removeEventListener('popstate', pop); window.removeEventListener('pagehide', remember) })
function changePageSize(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  continuous.value = value === 'scroll'
  if (!continuous.value) pageSize.value = Number(value)
  try { localStorage.setItem('pixiv-local-page-size', value) } catch { /* optional preference */ }
}
function updated() { newContent.value = true; window.dispatchEvent(new Event('archive-updated')) }
</script>
<template>
  <div class="app-shell" :inert="zoomPage ? true : undefined">
    <header class="app-header"><button class="brand" @click="gallery()" aria-label="返回本地图库"><span class="brand-mark">▧</span><span>Pixiv <b>本地图库</b></span></button><nav aria-label="主导航"><button :class="{selected: mode === 'gallery'}" @click="gallery()">图库</button><button :class="{selected: mode === 'reader'}" :disabled="!total" @click="continueReading">普通阅览</button><button :class="{selected: mode === 'feed'}" :disabled="!total" @click="enterFeed()">流式阅览</button><a href="/artists">画师</a><a href="/settings">设置</a></nav></header>
    <TaskPanel ref="taskPanel" @updated="updated" />
    <div class="view-heading"><div><h1>{{ heading }}</h1><p>{{ subtitle }}</p></div><div v-if="mode === 'gallery'" class="heading-actions"><button @click="filterOpen = !filterOpen" :aria-expanded="filterOpen">筛选与排序</button><button @click="task('check')">检查更新</button><select aria-label="图库浏览方式" :value="continuous ? 'scroll' : pageSize" @change="changePageSize"><option value="scroll">连续滚动</option><option :value="50">50 件 / 页</option><option :value="100">100 件 / 页</option><option :value="200">200 件 / 页</option><option :value="500">500 件 / 页</option></select></div><button v-else class="quiet" @click="gallery()">返回图库</button></div>
    <div v-if="newContent && mode === 'gallery'" class="update-notice"><span>后台任务已结束，当前浏览范围保持原位。</span><button @click="refresh">刷新图库</button><button class="quiet" @click="newContent = false">关闭</button></div>
    <div v-if="error" class="notice error" role="alert">{{ error }} <button @click="retryNavigation">重试</button><button class="quiet" @click="error = ''">关闭</button></div>
    <main class="workspace">
      <FilterPanel v-if="filterOpen && mode === 'gallery'" :key="scope" :query="scope" @apply="filter" @close="filterOpen = false" />
      <GalleryView v-if="ready && mode === 'gallery'" :query="scope" :start-index="galleryAnchor" :page-size="pageSize" :continuous="continuous" @open="openWork" @anchor="galleryAnchor = $event" @total="total = $event" @page="galleryAnchor = $event * pageSize; route(false)" />
      <div v-else-if="!ready" class="empty">正在打开本地图库…</div>
      <div v-else class="reader-stage"><Transition name="work"><PageReader :key="sessionKey" ref="reader" :mode="mode === 'feed' ? 'feed' : 'reader'" :loader="loader" :session-key="sessionKey" :initial="initialAnchor" :inactive="!!zoomPage" @focus="focused" @navigate="navigate" @zoom="zoomPage = $event" /></Transition></div>
      <aside v-if="infoOpen && mode !== 'gallery'" class="info-panel" data-no-navigation aria-label="作品信息与整理"><header><h2>作品信息</h2><button @click="infoOpen = false">收起</button></header><template v-if="infoWork && infoWork.id === activePage?.work_id"><h3>{{ infoWork.title }}</h3><p>{{ infoWork.artist_name }}</p><p class="muted">{{ infoWork.posted_at?.replace('T',' ') || '投稿时间未知' }}</p><p class="muted">{{ infoWork.bookmark_count ?? '—' }} 收藏 · {{ infoWork.view_count ?? '—' }} 浏览</p><div class="caption" v-html="infoWork.caption" /><p v-if="!infoWork.caption" class="muted">{{ infoWork.caption_state === 'empty' ? '作者没有填写简介' : '尚未取得简介' }}</p><div class="tags"><span v-for="tag in infoWork.tags" :key="tag">{{ tag }}</span></div><div class="info-actions"><button @click="bookmark">{{ infoWork.is_bookmarked ? '取消 Pixiv 收藏' : '收藏到 Pixiv' }}</button><button @click="hidden">{{ infoWork.is_hidden ? '取消隐藏' : '隐藏此作品' }}</button><button @click="task('repair', infoWork.id)">补下载此作品</button><button v-if="infoWork.type === 'ugoira'" @click="task('backfill', infoWork.id)">补回动图原包</button><button @click="api(`/illust/${infoWork.id}/open-folder`, {method: 'POST'}).catch(() => message('文件夹未能打开', true))">打开本地文件夹</button><button class="danger" @click="remove">永久删除</button></div><a :href="`https://www.pixiv.net/artworks/${infoWork.pixiv_id}`" target="_blank" rel="noopener noreferrer">在 Pixiv 查看 ↗</a><details class="asset-detail"><summary>存档状态</summary><p v-for="asset in infoWork.assets" :key="asset.id">{{ asset.role === 'image' ? `第 ${asset.page + 1} 页` : asset.role === 'ugoira_zip' ? '动图原包' : '播放时序' }}：{{ asset.state === 'verified' ? '已核验' : asset.error || '待核验或缺失' }}</p></details></template><p v-else>正在读取信息…</p></aside>
    </main>
    <footer v-if="mode !== 'gallery'" class="reading-toolbar"><button v-if="mode === 'reader'" :disabled="desiredIndex <= 0" @click="navigate(-1)">← 上一件</button><div class="reading-counter"><b>{{ (currentAnchor?.work_index ?? ordinaryIndex) + 1 }} <span>/ {{ total }} 件</span></b><small>{{ pending ? '目标准备中，当前画面保留' : mode === 'reader' ? '左右换作品 · 上下阅读' : '自然下滑 · 多页作品连续展开' }}</small></div><button v-if="mode === 'reader'" :disabled="desiredIndex >= total - 1" @click="navigate(1)">下一件 →</button><button v-else :disabled="!activePage || activePage.work_index >= total - 1" @click="reader?.jump(activePage!.next_work_index)">跳过当前作品 ↓</button><button @click="toggleInfo" :aria-expanded="infoOpen">信息与整理</button></footer>
    <div v-if="notification" class="toast" role="status">{{ notification }}<button @click="notification = ''" aria-label="关闭提示">×</button></div>
  </div>
  <ZoomView v-if="zoomPage" :page="zoomPage" @close="zoomPage = null" />
</template>
