<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { needsPageRefresh, sourceNotice, type Anchor, type PageData, type PageLoader } from './types'
import { DirectionGesture, LatestIntent } from './interaction'
import { prepareImage } from './api'
import MediaImage from './MediaImage.vue'
import RestrictionBadge from './components/RestrictionBadge.vue'

const props = defineProps<{mode: 'reader' | 'feed'; loader: PageLoader; initial: Anchor | null; sessionKey: string; inactive?: boolean}>()
const emit = defineEmits<{focus: [page: PageData, anchor: Anchor]; navigate: [delta: number]; zoom: [page: PageData]}>()
const viewport = ref<HTMLDivElement | null>(null), pages = ref<PageData[]>([]), total = ref(0)
const before = ref<HTMLDivElement | null>(null), beforeHeight = ref(0)
const size = ref({width: 1000, height: 760}), loading = ref(false), error = ref('')
const gesture = new DirectionGesture(), intent = new LatestIntent()
let observer: ResizeObserver | null = null, restoring = false, frame = 0, lastAnchor: Anchor | null = null
let refreshTimer = 0, refreshController: AbortController | null = null, refreshing = false, forceRefreshPending = false
let failedAction: (() => void) | null = null
const MAX_PAGES = 120, CHUNK = 40
const REFRESH_MS = 12000, REFRESH_LIMIT = 24
function height(page: PageData | undefined) {
  const padding = props.mode === 'feed' ? (size.value.width <= 760 ? 28 : 44) : (size.value.width < 600 ? 16 : 56)
  const availableWidth = Math.max(1, Math.min(props.mode === 'feed' ? 690 : 1100, size.value.width - padding))
  const ratio = page?.width && page.height ? page.height / page.width : 1.35
  const natural = availableWidth * ratio
  return props.mode === 'reader' ? Math.max(120, Math.min(natural, size.value.height - 84)) : Math.max(1, natural)
}
const virtual = useVirtualizer(computed(() => ({
  count: pages.value.length, getScrollElement: () => viewport.value,
  getItemKey: (index: number) => pages.value[index]?.index ?? index,
  estimateSize: (index: number) => height(pages.value[index]) + 48,
  overscan: 2, gap: 16, scrollMargin: beforeHeight.value,
})))
const rows = computed(() => virtual.value.getVirtualItems())
const fullHeight = computed(() => virtual.value.getTotalSize())
function measure(element: unknown) {
  if (element instanceof HTMLElement) virtual.value.measureElement(element)
  // Vue clears refs before removing DOM; prune measurements after that removal.
  else if (element === null) void nextTick(() => virtual.value.measureElement(null))
}
function current(): {page: PageData; anchor: Anchor} | null {
  const top = viewport.value?.scrollTop || 0
  const row = rows.value.find(row => row.end > top + 2) || rows.value[0]
  const page = row && pages.value[row.index]
  if (!page || !row) return null
  return {page, anchor: {work_id: page.work_id, page: page.page, offset: Math.max(0, top - row.start),
    index: page.index, work_index: page.work_index}}
}
function readAnchor() { return current()?.anchor || lastAnchor }
const paint = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
async function restore(anchor: Anchor | null) {
  if (!anchor) return
  const index = pages.value.findIndex(page => page.index === anchor.index)
  if (index < 0) return
  await nextTick()
  virtual.value.measure()
  virtual.value.scrollToIndex(index, {align: 'start'})
  await paint(); await nextTick()
  virtual.value.scrollToIndex(index, {align: 'start'})
  if (anchor.offset) virtual.value.scrollToOffset((viewport.value?.scrollTop || 0) + anchor.offset)
  await paint()
}
async function jump(index: number, offset = 0) {
  stopRefresh()
  const request = intent.begin()
  loading.value = true; restoring = true; error.value = ''
  failedAction = () => { void jump(index, offset) }
  try {
    const batch = await props.loader(Math.max(0, index - 16), 64, request.signal)
    if (!request.current()) return
    const target = batch.items.find(page => page.index === index) || batch.items[0]
    if (target && pages.value.length) await prepareImage(target.src, request.signal)
    if (!request.current()) return
    pages.value = batch.items; total.value = batch.total
    if (target && target.index === 0 && index === 0 && offset === 0) {
      await nextTick(); viewport.value?.scrollTo({top: 0, behavior: 'instant'}); await paint()
    } else if (target) await restore({work_id: target.work_id, page: target.page, offset, index: target.index, work_index: target.work_index})
    else viewport.value?.scrollTo({top: 0})
  } catch (failure) {
    if (request.current()) error.value = failure instanceof Error ? failure.message : '图片页未能载入'
  } finally {
    if (request.current()) { loading.value = false; restoring = false; report(); queueRefresh() }
  }
}
async function extend(direction: number) {
  if (loading.value || restoring || !pages.value.length) return
  const first = pages.value[0]!.index, end = pages.value.at(-1)!.index + 1
  if (direction < 0 && first === 0 || direction > 0 && end >= total.value) return
  stopRefresh()
  const request = intent.begin()
  loading.value = true; error.value = ''
  failedAction = () => { void extend(direction) }
  try {
    const offset = direction > 0 ? end : Math.max(0, first - CHUNK)
    const batch = await props.loader(offset, direction > 0 ? CHUNK : first - offset, request.signal)
    if (!request.current()) return
    const anchor = readAnchor()
    restoring = true
    let next = direction > 0 ? [...pages.value, ...batch.items] : [...batch.items, ...pages.value]
    next = direction > 0 ? next.slice(-MAX_PAGES) : next.slice(0, MAX_PAGES)
    pages.value = next; total.value = batch.total
    await restore(anchor)
  } catch (failure) {
    if (request.current()) error.value = failure instanceof Error ? failure.message : '暂时无法载入更多图片'
  } finally {
    if (request.current()) { loading.value = false; restoring = false; report(); queueRefresh() }
  }
}
function report() {
  if (restoring) return
  const selected = current()
  if (!selected) return
  lastAnchor = selected.anchor
  emit('focus', selected.page, selected.anchor)
}
function scroll() {
  if (frame) return
  frame = requestAnimationFrame(() => {
    frame = 0; report(); queueRefresh()
    if (loading.value || restoring || error.value || !rows.value.length) return
    const visible = current()
    const local = pages.value.findIndex(page => page.index === visible?.anchor.index)
    if (local > pages.value.length - 14) void extend(1)
    else if (local < 8 && (pages.value[0]?.index || 0) > 0) void extend(-1)
  })
}
function visibleMissing() {
  const top = viewport.value?.scrollTop || 0, bottom = top + (viewport.value?.clientHeight || 0)
  const visible = rows.value.filter(row => row.end >= top && row.start <= bottom)
  const indices = visible.length ? visible.map(row => row.index)
    : Array.from({length: Math.min(pages.value.length, 6)}, (_, index) => index)
  return indices.filter(index => {
    const page = pages.value[index]
    return !!page && needsPageRefresh(page)
  })
}
function stopRefresh() {
  clearTimeout(refreshTimer); refreshTimer = 0
  refreshController?.abort(); refreshController = null
}
function queueRefresh() {
  if (forceRefreshPending && document.visibilityState === 'visible' && !loading.value && !restoring && !refreshing) {
    forceRefreshPending = false; void refreshMissing(true); return
  }
  if (document.visibilityState !== 'visible' || !visibleMissing().length) {
    clearTimeout(refreshTimer); refreshTimer = 0; return
  }
  if (!refreshTimer && !refreshing) refreshTimer = window.setTimeout(() => {
    refreshTimer = 0; void refreshMissing()
  }, REFRESH_MS)
}
async function refreshMissing(force = false) {
  if (!pages.value.length) return
  if (force && document.visibilityState !== 'visible') { forceRefreshPending = true; return }
  if (document.visibilityState !== 'visible' || (!force && !visibleMissing().length)) return
  if (loading.value || restoring || refreshing) { if (force) forceRefreshPending = true; else queueRefresh(); return }
  const missing = visibleMissing()
  if (!force && !missing.length) return
  const first = force ? (current()?.page.index ?? pages.value[0]!.index) : pages.value[missing[0]!]!.index
  const offset = Math.max(pages.value[0]!.index, first - 2)
  const limit = Math.min(REFRESH_LIMIT, pages.value.at(-1)!.index - offset + 1)
  const session = props.sessionKey
  const controller = new AbortController()
  refreshController = controller; refreshing = true
  try {
    const batch = await props.loader(offset, limit, controller.signal)
    if (controller.signal.aborted || session !== props.sessionKey) return
    const latest = new Map(batch.items.map(page => [page.index, page]))
    const anchor = readAnchor()
    const previousTop = viewport.value?.scrollTop || 0
    const inHeader = previousTop < beforeHeight.value
    let changed = false
    const updated = pages.value.map(page => {
      const replacement = latest.get(page.index)
      if (!replacement || replacement.work_id !== page.work_id) return page
      if (replacement.src !== page.src || replacement.state !== page.state ||
          replacement.recovery?.status !== page.recovery?.status ||
          replacement.recovery?.next_retry_at !== page.recovery?.next_retry_at ||
          replacement.recovery?.reason !== page.recovery?.reason ||
          replacement.availability?.state !== page.availability?.state ||
          replacement.availability?.reason !== page.availability?.reason) {
        changed = true; return replacement
      }
      return page
    })
    if (changed) {
      restoring = true
      try {
        pages.value = updated
        if (inHeader) { await nextTick(); if (viewport.value) viewport.value.scrollTop = previousTop }
        else await restore(anchor)
      } finally { restoring = false }
      report()
    }
  } catch {
    // The background repair may still be offline; keep the current pages visible.
  } finally {
    if (refreshController === controller) refreshController = null
    refreshing = false
    queueRefresh()
  }
}
function visibilityChanged() {
  if (document.visibilityState === 'visible' && (forceRefreshPending || visibleMissing().length)) {
    clearTimeout(refreshTimer); refreshTimer = 0
    const force = forceRefreshPending; forceRefreshPending = false; void refreshMissing(force)
  } else stopRefresh()
}
function archiveUpdated() { if (pages.value.length) void refreshMissing(true) }
function down(event: PointerEvent) {
  const blocked = props.mode === 'feed' || props.inactive || event.pointerType === 'mouse' ||
    !!(event.target as Element).closest('button,a,input,select,textarea,[contenteditable]') || (window.visualViewport?.scale || 1) > 1.01
  gesture.down(event.pointerId, event.clientX, event.clientY, !!blocked)
}
function move(event: PointerEvent) { if (gesture.move(event.pointerId, event.clientX, event.clientY) && event.cancelable) event.preventDefault() }
function up(event: PointerEvent) { const direction = gesture.up(event.pointerId); if (direction && !props.inactive) emit('navigate', direction) }
function dimensions(page: PageData, width: number, height: number) { page.width = width; page.height = height }
function retry() { failedAction?.() }
watch(() => props.sessionKey, () => { lastAnchor = null; void jump(props.initial?.index || 0, props.initial?.offset || 0) })
onMounted(() => {
  observer = new ResizeObserver(entries => { for (const entry of entries) { const rect = entry.contentRect; if (entry.target === viewport.value) size.value = {width: rect.width, height: rect.height}; else if (entry.target === before.value) beforeHeight.value = rect.height } })
  if (viewport.value) observer.observe(viewport.value)
  if (before.value) observer.observe(before.value)
  document.addEventListener('visibilitychange', visibilityChanged)
  window.addEventListener('archive-updated', archiveUpdated)
  void jump(props.initial?.index || 0, props.initial?.offset || 0)
})
onBeforeUnmount(() => { intent.cancel(); stopRefresh(); observer?.disconnect(); cancelAnimationFrame(frame); document.removeEventListener('visibilitychange', visibilityChanged); window.removeEventListener('archive-updated', archiveUpdated) })
defineExpose({readAnchor, jump, scrollBy: (delta: number) => viewport.value?.scrollBy({top: delta, behavior: 'auto'})})
</script>
<template>
  <div class="reading-surface">
    <div ref="viewport" class="page-viewport" :class="mode" tabindex="0" aria-label="图片阅读区"
      @scroll.passive="scroll" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="gesture.cancel($event.pointerId)">
      <div ref="before" class="reader-before"><slot name="before" /></div>
      <div class="page-window" :style="{height: `${fullHeight}px`}" :data-window-size="pages.length">
        <figure v-for="row in rows" :key="String(row.key)" :ref="measure" class="image-page" :data-index="row.index"
          :data-global-index="pages[row.index]!.index" :data-work-id="pages[row.index]!.work_id" :data-page="pages[row.index]!.page"
          :style="{transform: `translateY(${row.start - beforeHeight}px)`}">
          <figcaption><span v-if="mode === 'feed' && pages[row.index]!.page === 0" class="page-title">{{ pages[row.index]!.title }} <small>{{ pages[row.index]!.artist_name }}</small></span>
            <RestrictionBadge v-if="mode === 'feed' && pages[row.index]!.page === 0" :value="pages[row.index]!.x_restrict" />
            <span class="page-number">{{ pages[row.index]!.page + 1 }} / {{ pages[row.index]!.page_count }}</span>
            <button class="quiet" :disabled="!pages[row.index]!.src" @click="emit('zoom', pages[row.index]!)" aria-label="放大此页">放大</button>
            <span v-if="mode === 'feed' && pages[row.index]!.page === 0 && sourceNotice(pages[row.index]!.availability, pages[row.index]!.recovery)" class="page-source">{{ sourceNotice(pages[row.index]!.availability, pages[row.index]!.recovery) }}</span>
          </figcaption>
          <div class="page-picture" :style="mode === 'reader' ? {height: `${height(pages[row.index])}px`} : undefined">
            <MediaImage :src="pages[row.index]!.src" :state="pages[row.index]!.state" :recovery="pages[row.index]!.recovery" :label="`${pages[row.index]!.title} · 第 ${pages[row.index]!.page + 1} 页`"
              :width="mode === 'feed' ? pages[row.index]!.width || 1000 : undefined"
              :height="mode === 'feed' ? pages[row.index]!.height || 1350 : undefined"
              @size="(width, height) => dimensions(pages[row.index]!, width, height)" @zoom="emit('zoom', pages[row.index]!)" />
          </div>
        </figure>
      </div>
      <div v-if="pages.length && pages.at(-1)!.index + 1 === total" class="reader-after"><slot name="after" /></div>
      <p v-if="!loading && !pages.length" class="empty">当前范围没有可阅读的作品。</p>
      <p v-if="pages.length && pages.at(-1)!.index + 1 === total" class="end-mark">已到{{ mode === 'feed' ? '当前图库范围' : '这件作品' }}末尾</p>
    </div>
    <div v-if="loading || error" class="reader-notice" role="status">{{ error || '正在载入图片页…' }} <button v-if="error" @click="retry">重试</button></div>
  </div>
</template>
