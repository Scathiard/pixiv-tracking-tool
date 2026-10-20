<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {useVirtualizer} from '@tanstack/vue-virtual'
import {isOlderCatalogue, needsPageRefresh, type Anchor, type PageData, type PageLoader} from './types'
import {LatestIntent} from './interaction'
import {discardedSignal} from './diagnostics'
import {ApiReadError, preparePageImage} from './api'
import {observeCatalogueData} from './state/catalogue'
import {sameStreamWork, streamPageWindow, streamTouchDirection, StreamPositions, StreamWheel, STREAM_PULL_DISTANCE} from './state/streamReader'
import InlineZoomMedia from './components/InlineZoomMedia.vue'

const props = defineProps<{loader: PageLoader; initial: Anchor | null; sessionKey: string; inactive?: boolean}>()
const emit = defineEmits<{
  focus: [page: PageData, anchor: Anchor]; navigate: [delta: number]
  activity: [state: {phase: string; error: string}]; recover: [page: PageData | null]
}>()
const viewport = ref<HTMLDivElement | null>(null), pages = ref<PageData[]>([]), total = ref(0)
const size = ref({width: 1000, height: 760}), loading = ref(false), phase = ref(''), error = ref('')
const expired = ref(false), pull = ref(0), enter = ref(0)
const intent = new LatestIntent(), positions = new StreamPositions(), wheelGesture = new StreamWheel()
let observer: ResizeObserver | null = null, frame = 0, restoring = false, lastAnchor: Anchor | null = null
let targetIndex: number | null = null, catalogueRevision: number | undefined
let installedSession = ''
let adjacentPreload: AbortController | null = null, preloadIndex: number | null = null
let failedAction: (() => void) | null = null
let refreshTimer = 0, refreshController: AbortController | null = null, forceRefreshPending = false
let touch: {id: number; x: number; y: number; delta: number; top: boolean; bottom: boolean; blocked: boolean} | null = null
const CHUNK = 40, REFRESH_MS = 12000, REFRESH_LIMIT = 24
watch([loading, phase, error], () => emit('activity', {phase: loading.value ? phase.value : '', error: error.value}))

function height(page: PageData | undefined) {
  const width = Math.max(1, Math.min(1100, size.value.width - 32))
  const ratio = page?.width && page.height ? page.height / page.width : 1.35
  if (page?.page_count === 1 && ratio < 2.2) return Math.max(1, size.value.height)
  return Math.max(1, width * ratio)
}
const virtual = useVirtualizer(computed(() => ({
  count: pages.value.length, getScrollElement: () => viewport.value,
  getItemKey: (index: number) => `${pages.value[index]?.work_id}:${pages.value[index]?.revision_id}:${pages.value[index]?.page}`,
  estimateSize: (index: number) => height(pages.value[index]), overscan: 2, gap: 16,
})))
const rows = computed(() => virtual.value.getVirtualItems())
const fullHeight = computed(() => virtual.value.getTotalSize())
function measure(element: unknown) {
  if (element instanceof HTMLElement) virtual.value.measureElement(element)
  else if (element === null) void nextTick(() => virtual.value.measureElement(null))
}
function current(): {page: PageData; anchor: Anchor} | null {
  const top = viewport.value?.scrollTop || 0
  const row = rows.value.find(row => row.end > top + 2) || rows.value[0]
  const page = row && pages.value[row.index]
  if (!page || !row) return null
  return {page, anchor: {work_id: page.work_id, page: page.page, offset: Math.max(0, top - row.start), index: page.index, work_index: page.work_index}}
}
function readAnchor() { return current()?.anchor || lastAnchor }
const paint = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
async function restore(anchor: Anchor | null, ownsView: () => boolean) {
  if (!anchor) return
  const index = pages.value.findIndex(page => page.index === anchor.index)
  if (index < 0) return
  await nextTick()
  if (!ownsView()) return
  virtual.value.measure()
  virtual.value.scrollToIndex(index, {align: 'start'})
  await paint(); await nextTick()
  if (!ownsView()) return
  virtual.value.scrollToIndex(index, {align: 'start'})
  if (anchor.offset) virtual.value.scrollToOffset((viewport.value?.scrollTop || 0) + anchor.offset)
  await paint()
}
function remember() {
  if (installedSession !== props.sessionKey) return
  const selected = current()
  if (selected) positions.remember(selected.page, selected.anchor)
}
function report() {
  if (restoring || props.inactive) return
  const selected = current()
  if (!selected) return
  lastAnchor = selected.anchor; positions.remember(selected.page, selected.anchor)
  emit('focus', selected.page, selected.anchor)
  prefetch(selected.page)
}
function prefetch(page: PageData) {
  if (loading.value || preloadIndex === page.index) return
  adjacentPreload?.abort(); preloadIndex = page.index
  const controller = adjacentPreload = new AbortController()
  for (const neighbor of pages.value.filter(candidate => Math.abs(candidate.index - page.index) === 1)) {
    void preparePageImage(neighbor, controller.signal).catch(() => {})
  }
}
function fail(failure: unknown, fallback: string) {
  expired.value = failure instanceof ApiReadError && failure.code === 'cursor_expired'
  error.value = failure instanceof Error ? failure.message : fallback
}
async function jump(index: number, offset = 0, showIntro = true) {
  if (props.inactive) return
  index = Math.max(0, index)
  if (loading.value && targetIndex === index) return
  remember(); stopRefresh(); adjacentPreload?.abort(); preloadIndex = null; resetTouch()
  const request = intent.begin()
  targetIndex = index; phase.value = '正在定位作品…'; loading.value = true; restoring = true
  error.value = ''; expired.value = false
  failedAction = () => { void jump(index, offset, showIntro) }
  try {
    const cached = installedSession === props.sessionKey ? pages.value.find(page => page.index === index) : undefined
    let batch = cached ? {items: pages.value, total: total.value, catalogue_revision: catalogueRevision}
      : await props.loader(Math.max(0, index - 16), 64, request.signal)
    if (!request.current() || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) { discardedSignal(request.signal, 'other'); return }
    let target = batch.items.find(page => page.index === index)
    if (!target && batch.total) throw new Error('目标作品已不在当前范围内，请重试')
    const changing = !!target && !sameStreamWork(target, pages.value[0] || target)
    const saved = target && changing && showIntro && !offset ? positions.read(target) : null
    if (saved && target && saved.page < target.page_count) {
      index = saved.index; offset = saved.offset
      if (!batch.items.some(page => page.index === index && sameStreamWork(page, target!))) {
        batch = await props.loader(Math.max(target.index - target.page, index - 16), 64, request.signal)
        if (!request.current() || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
      }
      target = batch.items.find(page => page.index === index && sameStreamWork(page, target!))
      if (!target) throw new Error('上次阅读位置暂时无法载入，请重试')
    }
    if (target) {
      phase.value = '正在准备图片，当前画面会保留…'
      await preparePageImage(target, request.signal)
    }
    if (!request.current()) return
    catalogueRevision = batch.catalogue_revision ?? catalogueRevision
    pages.value = target ? streamPageWindow(batch.items, target) : []; total.value = batch.total
    installedSession = props.sessionKey
    observeCatalogueData(batch.catalogue_revision)
    if (changing) enter.value++
    if (target) await restore({work_id: target.work_id, page: target.page, offset, index: target.index, work_index: target.work_index}, request.current)
    else viewport.value?.scrollTo({top: 0})
  } catch (failure) { if (request.current()) fail(failure, '作品暂时无法载入') }
  finally {
    if (request.current()) {
      targetIndex = null; loading.value = false; restoring = false; report(); queueRefresh()
      if (!error.value) queueMicrotask(extendIfNeeded)
    }
  }
}
function stepPage(direction: number) {
  if (props.inactive || !pages.value.length) return
  const selected = targetIndex ?? readAnchor()?.index
  if (selected === undefined) return
  const page = pages.value.find(page => page.index === selected) || current()?.page
  if (!page) return
  if (page.page + Math.sign(direction) < 0 || page.page + Math.sign(direction) >= page.page_count) emit('navigate', Math.sign(direction))
  else void jump(selected + Math.sign(direction), 0, false)
}
async function extend(direction: number) {
  if (props.inactive || loading.value || restoring || !pages.value.length || refreshController) return
  const first = pages.value[0]!, last = pages.value.at(-1)!
  if (direction < 0 && first.page === 0 || direction > 0 && last.page + 1 >= last.page_count) return
  stopRefresh()
  const request = intent.begin(), selected = first
  phase.value = '正在读取本作品的相邻页…'; loading.value = true; error.value = ''; expired.value = false
  failedAction = () => { void extend(direction) }
  try {
    const offset = direction > 0 ? last.index + 1 : Math.max(first.index - first.page, first.index - CHUNK)
    const limit = direction > 0 ? Math.min(CHUNK, last.page_count - last.page - 1) : first.index - offset
    const batch = await props.loader(offset, limit, request.signal)
    if (!request.current() || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
    const incoming = batch.items.filter(page => sameStreamWork(page, selected))
    if (!incoming.length) throw new Error('本作品的相邻页暂时无法载入，请重试')
    const anchor = readAnchor(); restoring = true
    pages.value = streamPageWindow([...pages.value, ...incoming], selected, direction)
    total.value = batch.total; catalogueRevision = batch.catalogue_revision ?? catalogueRevision
    observeCatalogueData(batch.catalogue_revision)
    await restore(anchor, request.current)
  } catch (failure) { if (request.current()) fail(failure, '本作品的相邻页暂时无法载入') }
  finally { if (request.current()) { loading.value = false; restoring = false; report(); queueRefresh() } }
}
function extendIfNeeded() {
  if (props.inactive || loading.value || restoring || error.value || !rows.value.length) return
  const selected = current()
  const local = pages.value.findIndex(page => page.index === selected?.page.index)
  const first = pages.value[0], last = pages.value.at(-1)
  // Direction preference avoids alternating opposite trims when both edges are near.
  if (local > pages.value.length - 14 && last && last.page + 1 < last.page_count) void extend(1)
  else if (local < 8 && first && first.page > 0) void extend(-1)
}
function scroll() {
  if (frame) return
  frame = requestAnimationFrame(() => { frame = 0; report(); queueRefresh(); extendIfNeeded() })
}
function visibleMissing() {
  const top = viewport.value?.scrollTop || 0, bottom = top + (viewport.value?.clientHeight || 0)
  return rows.value.filter(row => row.end >= top && row.start <= bottom && pages.value[row.index] && needsPageRefresh(pages.value[row.index]!)).map(row => row.index)
}
function stopRefresh() {
  clearTimeout(refreshTimer); refreshTimer = 0
  refreshController?.abort(); refreshController = null
  if (!loading.value) restoring = false
}
function queueRefresh() {
  if (props.inactive || document.visibilityState !== 'visible') { clearTimeout(refreshTimer); refreshTimer = 0; return }
  if (loading.value || restoring || refreshController) return
  if (forceRefreshPending) { forceRefreshPending = false; void refreshMissing(true); return }
  if (!visibleMissing().length) { clearTimeout(refreshTimer); refreshTimer = 0; return }
  if (!refreshTimer) refreshTimer = window.setTimeout(() => { refreshTimer = 0; void refreshMissing() }, REFRESH_MS)
}
async function refreshMissing(force = false) {
  if (!pages.value.length) return
  if (props.inactive || document.visibilityState !== 'visible' || loading.value || restoring || refreshController) {
    if (force) forceRefreshPending = true
    return
  }
  const missing = visibleMissing()
  if (!force && !missing.length) return
  const selected = pages.value[0]!, anchor = readAnchor()
  const first = force ? (anchor?.index ?? selected.index) : pages.value[missing[0]!]!.index
  const offset = Math.max(selected.index, first - 2)
  const limit = Math.min(REFRESH_LIMIT, pages.value.at(-1)!.index - offset + 1)
  const session = props.sessionKey, controller = refreshController = new AbortController()
  try {
    const batch = await props.loader(offset, limit, controller.signal)
    if (controller.signal.aborted || session !== props.sessionKey || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
    const latest = new Map(batch.items.filter(page => sameStreamWork(page, selected)).map(page => [page.index, page]))
    const updated = pages.value.map(page => {
      const replacement = latest.get(page.index)
      return replacement ? {...replacement, width: replacement.width || page.width, height: replacement.height || page.height} : page
    })
    const changed = updated.some((page, index) => JSON.stringify(page) !== JSON.stringify(pages.value[index]))
    catalogueRevision = batch.catalogue_revision ?? catalogueRevision
    observeCatalogueData(batch.catalogue_revision)
    if (changed) {
      restoring = true; pages.value = updated
      await restore(anchor, () => !controller.signal.aborted && session === props.sessionKey)
      if (!controller.signal.aborted) { restoring = false; preloadIndex = null; report() }
    }
  } catch { /* Background repair keeps the current display and retries later. */ }
  finally {
    if (refreshController === controller) { refreshController = null; queueRefresh(); extendIfNeeded() }
  }
}
function visibilityChanged() {
  if (document.visibilityState === 'visible') queueRefresh()
  else stopRefresh()
}
function archiveUpdated() { if (pages.value.length) { forceRefreshPending = true; queueRefresh() } }
function atEdge(direction: number) {
  const element = viewport.value, first = pages.value[0], last = pages.value.at(-1)
  if (!element || !first || !last) return false
  return direction < 0 ? first.page === 0 && element.scrollTop <= 2
    : last.page + 1 === last.page_count && element.scrollTop + element.clientHeight >= element.scrollHeight - 2
}
function blocked(target: EventTarget | null) {
  return props.inactive || loading.value || restoring || !!(target instanceof Element && target.closest('button,a,input,select,textarea,[contenteditable],[data-inline-zoom-active="true"],[data-inline-zoom-consumed="true"]')) || (window.visualViewport?.scale || 1) > 1.01
}
function resetTouch() { touch = null; pull.value = 0 }
function touchStart(event: TouchEvent) {
  if (event.touches.length !== 1) { if (touch) touch.blocked = true; pull.value = 0; return }
  const point = event.touches[0]!
  touch = {id: point.identifier, x: point.clientX, y: point.clientY, delta: 0, top: atEdge(-1), bottom: atEdge(1), blocked: blocked(event.target) || event.defaultPrevented}
}
function touchMove(event: TouchEvent) {
  if (!touch || touch.blocked) return
  if (event.touches.length !== 1 || blocked(event.target) || event.defaultPrevented) { touch.blocked = true; pull.value = 0; return }
  const point = [...event.touches].find(point => point.identifier === touch!.id)
  if (!point) return
  const delta = point.clientY - touch.y, horizontal = Math.abs(point.clientX - touch.x)
  if (horizontal > Math.abs(delta) * 1.3 && horizontal > 18) { touch.blocked = true; return }
  touch.delta = delta
  const direction = delta > 0 ? -1 : 1
  if (!(direction < 0 ? touch.top : touch.bottom) || !atEdge(direction)) { pull.value = 0; return }
  if (event.cancelable) event.preventDefault()
  pull.value = Math.sign(delta) * Math.min(80, Math.abs(delta) * .45)
}
function touchEnd(event: TouchEvent) {
  if (event.touches.length) return
  const selected = touch; resetTouch()
  if (!selected || selected.blocked || blocked(event.target) || event.defaultPrevented) return
  const direction = streamTouchDirection(selected.delta, selected.top, selected.bottom)
  if (direction && atEdge(direction)) navigate(direction)
}
function navigate(direction: number) {
  if (props.inactive || loading.value || restoring) return
  remember(); emit('navigate', direction)
}
function wheel(event: WheelEvent) {
  const now = performance.now()
  if (blocked(event.target) || event.defaultPrevented || event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) { wheelGesture.block(now); return }
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? size.value.height : 1)
  const direction = wheelGesture.feed(delta, now, atEdge)
  if (atEdge(Math.sign(delta)) && event.cancelable) event.preventDefault()
  if (direction) navigate(direction)
}
function zoomOwned() { resetTouch(); wheelGesture.block(performance.now()) }
function dimensions(page: PageData, width: number, imageHeight: number) {
  if (page.width === width && page.height === imageHeight) return
  const anchor = readAnchor()
  page.width = width; page.height = imageHeight
  if (restoring) return
  const identity = pages.value[0]
  void nextTick(async () => {
    if (loading.value || restoring || identity !== pages.value[0]) return
    restoring = true
    try { await restore(anchor, () => identity === pages.value[0] && !loading.value) }
    finally { if (!loading.value) { restoring = false; report() } }
  })
}
function retry() { if (expired.value) emit('recover', current()?.page || pages.value[0] || null); else failedAction?.() }
function cancel() {
  intent.cancel(); stopRefresh(); adjacentPreload?.abort(); targetIndex = null; loading.value = false; restoring = false
  error.value = ''; resetTouch(); report(); queueRefresh()
}
watch(() => props.sessionKey, () => {
  positions.clear(); lastAnchor = null; catalogueRevision = undefined; wheelGesture.reset()
  void jump(props.initial?.index || 0, props.initial?.offset || 0)
})
watch(() => props.inactive, inactive => { if (inactive) { resetTouch(); wheelGesture.block(performance.now()); stopRefresh() } else { report(); queueRefresh() } })
watch(size, () => { virtual.value.measure() })
onMounted(() => {
  observer = new ResizeObserver(entries => {
    for (const entry of entries) size.value = {width: entry.contentRect.width, height: entry.contentRect.height}
  })
  if (viewport.value) observer.observe(viewport.value)
  document.addEventListener('visibilitychange', visibilityChanged)
  window.addEventListener('archive-updated', archiveUpdated)
  void jump(props.initial?.index || 0, props.initial?.offset || 0)
})
onBeforeUnmount(() => {
  adjacentPreload?.abort(); intent.cancel(); stopRefresh(); observer?.disconnect(); cancelAnimationFrame(frame)
  document.removeEventListener('visibilitychange', visibilityChanged); window.removeEventListener('archive-updated', archiveUpdated)
})
defineExpose({readAnchor, jump, stepPage, cancel, retry, scrollBy: (delta: number) => viewport.value?.scrollBy({top: delta, behavior: 'auto'})})
</script>
<template>
  <div class="stream-reader" :data-stream-work="pages[0]?.work_id" :data-stream-entry="enter">
    <div ref="viewport" class="stream-viewport" tabindex="0" aria-label="流式图片阅读区" :aria-busy="loading"
      @scroll.passive="scroll" @touchstart="touchStart" @touchmove="touchMove" @touchend="touchEnd" @touchcancel="resetTouch"
      @wheel="wheel" @inline-zoom-gesture="zoomOwned">
      <div :key="`${pages[0]?.work_id}:${pages[0]?.revision_id}`" class="stream-window" :data-window-size="pages.length" :style="{height: `${fullHeight}px`, transform: `translateY(${pull}px)`}">
        <figure v-for="row in rows" :key="String(row.key)" :ref="measure" class="stream-page" :data-index="row.index"
          :data-global-index="pages[row.index]!.index" :data-work-id="pages[row.index]!.work_id" :data-page="pages[row.index]!.page"
          :style="{height: `${height(pages[row.index])}px`, transform: `translateY(${row.start}px)`}">
          <div class="stream-picture" :class="{'stream-single': pages[row.index]!.page_count === 1}">
            <InlineZoomMedia :page="pages[row.index]!" :inactive="inactive || loading" @size="(width, imageHeight) => dimensions(pages[row.index]!, width, imageHeight)" />
          </div>
        </figure>
      </div>
      <p v-if="!loading && !pages.length" class="stream-empty">当前范围没有可阅读的作品。</p>
    </div>
    <div v-if="pull" class="stream-pull" :class="{'stream-pull-bottom': pull < 0}" aria-hidden="true">{{ Math.abs(pull) >= STREAM_PULL_DISTANCE * .45 ? '松手切换作品' : pull > 0 ? '下拉查看上一件' : '上拉查看下一件' }}</div>
  </div>
</template>
<style scoped>
.stream-reader { position: absolute; inset: 0; min-width: 0; }
.stream-viewport { height: 100%; overflow: auto; overflow-anchor: none; overscroll-behavior: contain; touch-action: pan-y pinch-zoom; scrollbar-width: thin; }
.stream-window { position: relative; width: 100%; animation: stream-work-in 160ms ease-out; }
@keyframes stream-work-in { from { opacity: .45; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .stream-window { animation: none; } }
.stream-page { position: absolute; left: 0; top: 0; width: 100%; margin: 0; padding: 0 16px; }
.stream-picture { width: 100%; height: 100%; max-width: 1100px; margin: auto; overflow: hidden; background: var(--art-bg); }
.stream-single { background: transparent; }
.stream-empty { padding: 32px 16px; text-align: center; color: var(--muted); }
.stream-pull { position: absolute; left: 50%; top: 12px; transform: translateX(-50%); padding: 6px 12px; border-radius: 20px; background: var(--surface); color: var(--muted); font-size: 12px; pointer-events: none; white-space: nowrap; }
.stream-pull-bottom { top: auto; bottom: 12px; }
</style>
