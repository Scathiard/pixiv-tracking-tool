<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {useVirtualizer} from '@tanstack/vue-virtual'
import {isOlderCatalogue, needsPageRefresh, type Anchor, type PageBatch, type PageData, type PageLoader} from '../types'
import {LatestIntent} from '../interaction'
import {ApiReadError, preparePageImage} from '../api'
import {observeCatalogueData} from '../state/catalogue'
import {sameStreamWork, streamPageWindow} from '../state/streamReader'
import InlineZoomMedia from './InlineZoomMedia.vue'

const props = defineProps<{loader: PageLoader; opening: PageBatch; initial: Anchor; tail?: boolean; active: boolean; inactive?: boolean}>()
const emit = defineEmits<{
  focus: [page: PageData, anchor: Anchor]; navigate: [delta: number]; ready: []
  activity: [state: {phase: string; error: string}]; recover: [page: PageData | null]
}>()
const viewport = ref<HTMLDivElement | null>(null), pages = ref<PageData[]>([]), total = ref(0)
const size = ref({width: 1000, height: 760}), loading = ref(false), switching = ref(false), phase = ref(''), error = ref('')
const intent = new LatestIntent()
let observer: ResizeObserver | null = null, frame = 0, restoring = false, destroyed = false
let lastAnchor: Anchor | null = null, catalogueRevision: number | undefined, targetIndex: number | null = null
let adjacentPreload: AbortController | null = null, preloadIndex: number | null = null
let refreshTimer = 0, refreshController: AbortController | null = null, forceRefreshPending = false
let failedAction: (() => void) | null = null, expired = false
const paint = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
watch([loading, phase, error, () => props.active], () => emit('activity', {phase: loading.value ? phase.value : '', error: error.value}))
function height(page: PageData | undefined) {
  const width = Math.max(1, Math.min(1100, size.value.width - 32))
  const ratio = page?.width && page.height ? page.height / page.width : 1.35
  return page?.page_count === 1 && ratio < 2.2 ? Math.max(1, size.value.height) : Math.max(1, width * ratio)
}
const virtual = useVirtualizer(computed(() => ({
  count: pages.value.length, getScrollElement: () => viewport.value,
  getItemKey: (index: number) => pages.value[index]!.work_id + ':' + pages.value[index]!.revision_id + ':' + pages.value[index]!.page,
  estimateSize: (index: number) => height(pages.value[index]), overscan: 2, gap: 16,
})))
const rows = computed(() => virtual.value.getVirtualItems())
const fullHeight = computed(() => virtual.value.getTotalSize())
function measure(element: unknown) {
  if (element instanceof HTMLElement) virtual.value.measureElement(element)
  else if (element === null) void nextTick(() => virtual.value.measureElement(null))
}
function current() {
  const top = viewport.value?.scrollTop || 0
  const row = rows.value.find(row => row.end > top + 2) || rows.value[0]
  const page = row && pages.value[row.index]
  if (!page || !row) return null
  return {page, anchor: {work_id: page.work_id, page: page.page, offset: Math.max(0, top - row.start), index: page.index, work_index: page.work_index}}
}
function readAnchor() { return current()?.anchor || lastAnchor }
function snapshot() {
  const selected = current()
  return selected ? {page: {...selected.page}, anchor: {...selected.anchor}} : null
}
async function restore(anchor: Anchor | null, owns: () => boolean, tail = false) {
  if (!anchor) return
  const index = pages.value.findIndex(page => page.index === anchor.index)
  if (index < 0) return
  await nextTick()
  if (!owns()) return
  virtual.value.measure(); virtual.value.scrollToIndex(index, {align: tail ? 'end' : 'start'})
  await paint(); await nextTick()
  if (!owns()) return
  virtual.value.scrollToIndex(index, {align: tail ? 'end' : 'start'})
  if (anchor.offset && !tail) virtual.value.scrollToOffset((viewport.value?.scrollTop || 0) + anchor.offset)
  await paint()
}
function report() {
  if (restoring || !props.active || props.inactive) return
  const selected = current()
  if (!selected) return
  lastAnchor = selected.anchor; emit('focus', selected.page, selected.anchor)
  if (preloadIndex === selected.page.index || loading.value) return
  adjacentPreload?.abort(); preloadIndex = selected.page.index
  const controller = adjacentPreload = new AbortController()
  for (const neighbor of pages.value.filter(page => Math.abs(page.index - selected.page.index) === 1)) {
    void preparePageImage(neighbor, controller.signal).catch(() => {})
  }
}
function fail(failure: unknown, fallback: string) {
  expired = failure instanceof ApiReadError && failure.code === 'cursor_expired'
  error.value = failure instanceof Error ? failure.message : fallback
}
function stopRefresh() {
  clearTimeout(refreshTimer); refreshTimer = 0
  refreshController?.abort(); refreshController = null
}
async function install(batch: PageBatch, anchor: Anchor, owns: () => boolean, tail = false) {
  const target = batch.items.find(page => page.index === anchor.index)
  if (!target) throw new Error('目标作品已不在当前范围内，请重试')
  pages.value = streamPageWindow(batch.items, target); total.value = batch.total
  catalogueRevision = batch.catalogue_revision ?? catalogueRevision
  observeCatalogueData(batch.catalogue_revision)
  await restore(anchor, owns, tail)
}
async function jump(index: number, offset = 0, initial = false) {
  if (loading.value && targetIndex === index) return
  stopRefresh(); adjacentPreload?.abort(); preloadIndex = null
  const request = intent.begin()
  targetIndex = index; phase.value = '正在准备原图，当前画面会保留…'; loading.value = true; switching.value = true; restoring = true
  error.value = ''; expired = false; failedAction = () => { void jump(index, offset, initial) }
  try {
    const cached = pages.value.find(page => page.index === index)
    const opening = initial && props.opening.items.some(page => page.index === index) ? props.opening : null
    const batch = cached ? {items: pages.value, total: total.value, offset: pages.value[0]!.index, catalogue_revision: catalogueRevision}
      : opening || await props.loader(Math.max(0, index - 16), 64, request.signal)
    if (!request.current() || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
    const target = batch.items.find(page => page.index === index)
    if (!target) throw new Error('目标作品已不在当前范围内，请重试')
    await preparePageImage(target, request.signal)
    if (!request.current()) return
    await install(batch, {work_id: target.work_id, page: target.page, offset, index: target.index, work_index: target.work_index}, request.current, initial && props.tail)
    if (request.current()) emit('ready')
  } catch (failure) { if (request.current()) fail(failure, '作品暂时无法载入') }
  finally {
    if (request.current()) {
      targetIndex = null; loading.value = false; switching.value = false; restoring = false; report(); queueRefresh()
      if (!error.value) queueMicrotask(extendIfNeeded)
    }
  }
}
function stepPage(direction: number) {
  if (props.inactive || !props.active) return
  const selected = current()?.page
  if (!selected) return
  if (selected.page + direction < 0 || selected.page + direction >= selected.page_count) emit('navigate', Math.sign(direction))
  else void jump(selected.index + Math.sign(direction))
}
async function extend(direction: number) {
  if (!props.active || props.inactive || loading.value || restoring || !pages.value.length || refreshController) return
  const first = pages.value[0]!, last = pages.value.at(-1)!
  if (direction < 0 && first.page === 0 || direction > 0 && last.page + 1 >= last.page_count) return
  stopRefresh()
  const request = intent.begin()
  loading.value = true; phase.value = '正在读取本作品的相邻页…'; error.value = ''; failedAction = () => { void extend(direction) }
  try {
    const offset = direction > 0 ? last.index + 1 : Math.max(first.index - first.page, first.index - 40)
    const limit = direction > 0 ? Math.min(40, last.page_count - last.page - 1) : first.index - offset
    const batch = await props.loader(offset, limit, request.signal)
    if (!request.current() || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
    const incoming = batch.items.filter(page => sameStreamWork(page, first))
    if (!incoming.length) throw new Error('本作品的相邻页暂时无法载入，请重试')
    const anchor = readAnchor(); restoring = true
    pages.value = streamPageWindow([...pages.value, ...incoming], first, direction)
    total.value = batch.total; catalogueRevision = batch.catalogue_revision ?? catalogueRevision
    observeCatalogueData(batch.catalogue_revision); await restore(anchor, request.current)
  } catch (failure) { if (request.current()) fail(failure, '本作品的相邻页暂时无法载入') }
  finally { if (request.current()) { loading.value = false; restoring = false; report(); queueRefresh() } }
}
function extendIfNeeded() {
  if (!props.active || props.inactive || loading.value || restoring || error.value || !rows.value.length) return
  const selected = current(), first = pages.value[0], last = pages.value.at(-1)
  const local = pages.value.findIndex(page => page.index === selected?.page.index)
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
function queueRefresh() {
  if (!props.active || props.inactive || document.visibilityState !== 'visible') { clearTimeout(refreshTimer); refreshTimer = 0; return }
  if (loading.value || restoring || refreshController) return
  if (forceRefreshPending) { forceRefreshPending = false; void refreshMissing(true); return }
  if (!visibleMissing().length) { clearTimeout(refreshTimer); refreshTimer = 0; return }
  if (!refreshTimer) refreshTimer = window.setTimeout(() => { refreshTimer = 0; void refreshMissing() }, 12000)
}
async function refreshMissing(force = false) {
  if (!props.active || props.inactive || loading.value || restoring || refreshController || !pages.value.length || document.hidden) {
    if (force) forceRefreshPending = true
    return
  }
  const missing = visibleMissing()
  if (!force && !missing.length) return
  const selected = pages.value[0]!, anchor = readAnchor()
  const first = force ? anchor?.index ?? selected.index : pages.value[missing[0]!]!.index
  const offset = Math.max(selected.index, first - 2), limit = Math.min(24, pages.value.at(-1)!.index - offset + 1)
  const controller = refreshController = new AbortController()
  try {
    const batch = await props.loader(offset, limit, controller.signal)
    if (controller.signal.aborted || isOlderCatalogue(batch.catalogue_revision, catalogueRevision)) return
    const latest = new Map(batch.items.filter(page => sameStreamWork(page, selected)).map(page => [page.index, page]))
    const updated = pages.value.map(page => {
      const replacement = latest.get(page.index)
      return replacement ? {...replacement, width: replacement.width || page.width, height: replacement.height || page.height} : page
    })
    catalogueRevision = batch.catalogue_revision ?? catalogueRevision; observeCatalogueData(batch.catalogue_revision)
    if (updated.some((page, index) => JSON.stringify(page) !== JSON.stringify(pages.value[index]))) {
      restoring = true; pages.value = updated
      await restore(anchor, () => !controller.signal.aborted)
      if (!controller.signal.aborted) { restoring = false; preloadIndex = null; report() }
    }
  } catch { /* Keep current media; the visible repair state and timer remain available. */ }
  finally { if (refreshController === controller) { refreshController = null; restoring = false; report(); queueRefresh(); extendIfNeeded() } }
}
function atEdge(direction: number) {
  const element = viewport.value, first = pages.value[0], last = pages.value.at(-1)
  if (!element || !first || !last || loading.value || restoring) return false
  return direction < 0 ? first.page === 0 && element.scrollTop <= 2
    : last.page + 1 === last.page_count && element.scrollTop + element.clientHeight >= element.scrollHeight - 2
}
function dimensions(page: PageData, width: number, imageHeight: number) {
  if (page.width === width && page.height === imageHeight) return
  const anchor = readAnchor(); page.width = width; page.height = imageHeight
  if (restoring) return
  void nextTick(async () => {
    if (loading.value || restoring || destroyed) return
    restoring = true
    try { await restore(anchor, () => !destroyed && !loading.value) }
    finally { if (!loading.value) { restoring = false; report() } }
  })
}
function cancel() {
  intent.cancel(); stopRefresh(); adjacentPreload?.abort(); targetIndex = null; loading.value = false; switching.value = false; restoring = false
  error.value = ''; report(); queueRefresh()
}
function retry() { if (expired) emit('recover', current()?.page || pages.value[0] || null); else failedAction?.() }
async function rebase(batch: PageBatch, anchor: Anchor) {
  const target = batch.items.find(page => page.index === anchor.index)
  if (!target) throw new Error('当前作品页已不在此阅读范围内，请返回后重试')
  // A one-page locate response may be shorter than the viewport. Replacing the
  // visible window with it would clamp scrollTop and destroy the saved offset.
  // Retain the bounded physical pages, changing only their sequence coordinates.
  const origin = target.index - target.page
  const mapped = pages.value.filter(page => page.work_id === target.work_id && page.revision_id === target.revision_id && page.page < target.page_count)
    .map(page => ({...page, index: origin + page.page, work_index: target.work_index, page_count: target.page_count,
      next_work_index: target.next_work_index, previous_work_index: target.previous_work_index,
      round: target.round, work_cursor: target.work_cursor, has_next_work: target.has_next_work}))
  cancel(); restoring = true
  try { await install({...batch, items: [...mapped, ...batch.items]}, anchor, () => !destroyed) }
  finally { restoring = false; report(); queueRefresh(); emit('ready') }
}
let inactiveWork: Promise<void> = Promise.resolve()
watch(() => props.active, active => {
  inactiveWork = updateActive(active)
})
async function updateActive(active: boolean) {
  if (!active) {
    intent.cancel(); targetIndex = null; loading.value = false; switching.value = false; restoring = false; stopRefresh(); adjacentPreload?.abort()
    const selected = current()
    if (selected && pages.value.length > 8) {
      const local = pages.value.findIndex(page => page.index === selected.page.index)
      const start = Math.max(0, Math.min(local - 2, pages.value.length - 8))
      restoring = true; pages.value = pages.value.slice(start, start + 8)
      await restore(selected.anchor, () => !props.active && !destroyed); restoring = false
    }
  } else { report(); queueRefresh(); extendIfNeeded() }
}
async function firstScreen() {
  await inactiveWork
  const selected = current()?.page || pages.value[0]
  if (selected) await jump(selected.index - selected.page)
}
watch(() => props.inactive, inactive => { if (inactive) stopRefresh(); else { report(); queueRefresh(); extendIfNeeded() } })
watch(size, () => virtual.value.measure())
function visibilityChanged() {
  if (document.hidden) { stopRefresh(); adjacentPreload?.abort(); preloadIndex = null }
  else { report(); queueRefresh() }
}
function archiveUpdated() { forceRefreshPending = true; queueRefresh() }
onMounted(() => {
  observer = new ResizeObserver(entries => { for (const entry of entries) size.value = {width: entry.contentRect.width, height: entry.contentRect.height} })
  if (viewport.value) observer.observe(viewport.value)
  document.addEventListener('visibilitychange', visibilityChanged); window.addEventListener('archive-updated', archiveUpdated)
  void jump(props.initial.index, props.initial.offset || 0, true)
})
onBeforeUnmount(() => {
  destroyed = true; intent.cancel(); stopRefresh(); adjacentPreload?.abort(); observer?.disconnect(); cancelAnimationFrame(frame)
  document.removeEventListener('visibilitychange', visibilityChanged); window.removeEventListener('archive-updated', archiveUpdated)
})
defineExpose({readAnchor, snapshot, atEdge, jump, rebase, firstScreen, stepPage, cancel, retry, scrollBy: (delta: number) => viewport.value?.scrollBy({top: delta, behavior: 'auto'})})
</script>
<template>
  <div class="stream-work-panel">
    <div ref="viewport" class="stream-viewport" :class="{'is-suspended': inactive || !active}" :tabindex="active ? 0 : -1" aria-label="流式图片阅读区" :aria-busy="loading" @scroll.passive="scroll">
      <div class="stream-window" :data-window-size="pages.length" :style="{height: fullHeight + 'px'}">
        <figure v-for="row in rows" :key="String(row.key)" :ref="measure" class="stream-page" :data-index="row.index"
          :data-global-index="pages[row.index]!.index" :data-work-id="pages[row.index]!.work_id" :data-page="pages[row.index]!.page"
          :style="{height: height(pages[row.index]) + 'px', transform: 'translateY(' + row.start + 'px)'}">
          <div class="stream-picture" :class="{'stream-single': pages[row.index]!.page_count === 1}">
            <InlineZoomMedia :page="pages[row.index]!" :inactive="!active || inactive || switching" @size="(width, imageHeight) => dimensions(pages[row.index]!, width, imageHeight)" />
          </div>
        </figure>
      </div>
    </div>
  </div>
</template>
<style scoped>
.stream-work-panel { position: absolute; inset: 0; min-width: 0; overflow: hidden; background: var(--art-bg); }
.stream-viewport { height: 100%; overflow: auto; overflow-anchor: none; overscroll-behavior: contain; touch-action: pan-y pinch-zoom; scrollbar-width: thin; }
.stream-viewport.is-suspended { overflow: hidden; }
.stream-window { position: relative; width: 100%; }
.stream-page { position: absolute; left: 0; top: 0; width: 100%; margin: 0; padding: 0 16px; }
.stream-picture { width: 100%; height: 100%; max-width: 1100px; margin: auto; overflow: hidden; background: var(--art-bg); }
.stream-single { background: transparent; }
</style>
