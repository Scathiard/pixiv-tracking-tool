<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, ref, watch} from 'vue'
import {api} from '../api'
import {workDetail} from '../state/workCache'
import type {Artist, PageData, WorkDetail} from '../types'
import Icon from './Icon.vue'
import BookmarkButton from './BookmarkButton.vue'
import TagLabel from './TagLabel.vue'
import RestrictionBadge from './RestrictionBadge.vue'

const props = defineProps<{
  page: PageData | null; order: string; total: number; totalExact: boolean; ordinalKnown: boolean
  activity: {phase: string; error: string}; ended: boolean; inactive?: boolean
}>()
const emit = defineEmits<{
  back: []; order: [value: string]; artist: [id: number]; manage: []; tag: [tag: string]
  navigate: [delta: number]; message: [text: string]; retry: []; cancel: []; panel: [open: boolean]
}>()
const detail = ref<WorkDetail | null>(null), artist = ref<Artist | null>(null)
const metadataError = ref(''), opened = ref(false), avatarFailed = ref(false)
const sheet = ref<HTMLElement | null>(null), trigger = ref<HTMLButtonElement | null>(null)
const presented = ref(false), dragging = ref(false), sheetOffset = ref(0), sheetHeight = ref(1)
const overlayStyle = computed(() => ({backgroundColor: `rgb(0 0 0 / ${.4 * Math.max(0, 1 - sheetOffset.value / sheetHeight.value)})`}))
const artists = new Map<number, Artist>()
let settleTimer = 0, motionSerial = 0, ignoreClickUntil = 0
let drag: {id: number; x: number; y: number; distance: number; mode: 'open' | 'close'; target: HTMLElement; started: boolean} | null = null
let request: AbortController | null = null
const caption = computed(() => {
  const html = detail.value?.caption
  if (!html) return metadataError.value ? '简介暂时无法读取，点击重试' : !detail.value ? '正在读取简介…' : detail.value.caption_state === 'empty' ? '作者没有填写简介' : '尚未取得简介'
  const doc = new DOMParser().parseFromString(html, 'text/html')
  return doc.body.textContent?.replace(/\s+/g, ' ').trim() || '查看作品简介'
})
const counter = computed(() => !props.page ? '正在打开…' : `${props.ordinalKnown ? props.page.work_index + 1 : '当前'}${props.totalExact ? ` / ${props.total}` : ''} 件 · ${props.page.page + 1} / ${props.page.page_count} 页`)
async function metadata() {
  request?.abort()
  const page = props.page
  if (!page) { detail.value = null; artist.value = null; return }
  const controller = request = new AbortController()
  metadataError.value = ''
  artist.value = artists.get(page.artist_id) || null
  const info = workDetail(page.work_id, page.revision_id, controller.signal)
    .then(value => { if (!controller.signal.aborted) detail.value = value })
    .catch(error => { if (!controller.signal.aborted) metadataError.value = error instanceof Error ? error.message : '作品信息暂时无法读取' })
  const profile = artist.value ? Promise.resolve() : api<Artist>(`/api/artists/${page.artist_id}`, {signal: controller.signal})
    .then(value => {
      if (controller.signal.aborted) return
      artist.value = value; artists.set(page.artist_id, value)
      if (artists.size > 32) artists.delete(artists.keys().next().value!)
    }).catch(() => { /* Avatar is optional; the local artist link remains usable. */ })
  await Promise.all([info, profile])
}
watch([() => props.page?.work_id, () => props.page?.revision_id], () => {
  detail.value = null; avatarFailed.value = false; close(false); void metadata()
}, {immediate: true})
watch(() => props.inactive, inactive => { if (inactive) close(false) })
function releaseDrag() {
  const previous = drag; drag = null; dragging.value = false
  if (previous?.target.hasPointerCapture?.(previous.id)) previous.target.releasePointerCapture(previous.id)
}
function measureSheet() { sheetHeight.value = Math.max(1, sheet.value?.getBoundingClientRect().height || 1) }
function finishSheet(target: boolean, restore = true) {
  clearTimeout(settleTimer)
  opened.value = target; sheetOffset.value = target ? 0 : sheetHeight.value
  if (target) {
    emit('panel', true)
    void nextTick(() => sheet.value?.querySelector<HTMLButtonElement>('button')?.focus({preventScroll: true}))
  } else {
    settleTimer = window.setTimeout(() => {
      presented.value = false; emit('panel', false)
      if (restore) void nextTick(() => trigger.value?.focus({preventScroll: true}))
    }, window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180)
  }
}
async function open(event?: MouseEvent) {
  if (event && event.detail !== 0 && performance.now() < ignoreClickUntil) { event.preventDefault(); return }
  if (props.inactive || !props.page || opened.value || drag) return
  const serial = ++motionSerial
  clearTimeout(settleTimer); presented.value = true; dragging.value = true; emit('panel', true)
  await nextTick()
  if (serial !== motionSerial) return
  measureSheet(); sheetOffset.value = sheetHeight.value
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  if (serial !== motionSerial) return
  // Commit the hidden starting pose before enabling the short settle transition.
  sheet.value?.getBoundingClientRect()
  dragging.value = false; finishSheet(true)
}
function close(restore = true) {
  ++motionSerial; releaseDrag(); clearTimeout(settleTimer)
  if (!restore) { opened.value = false; presented.value = false; emit('panel', false); return }
  if (presented.value) finishSheet(false, restore)
}
function startDrag(event: PointerEvent, mode: 'open' | 'close') {
  if (props.inactive || !props.page || drag || event.button !== 0 || !event.isPrimary) return
  ++motionSerial; clearTimeout(settleTimer)
  const target = event.currentTarget as HTMLElement
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY, distance: 0, mode, target, started: false}
  target.setPointerCapture?.(event.pointerId)
  event.stopPropagation()
}
async function moveDrag(event: PointerEvent) {
  const current = drag
  if (!current || current.id !== event.pointerId) return
  const delta = event.clientY - current.y
  current.distance = Math.max(0, current.mode === 'open' ? -delta : delta)
  if (!current.started) {
    if (current.distance < 6 || current.distance < Math.abs(event.clientX - current.x)) return
    current.started = true; dragging.value = true; presented.value = true; emit('panel', true)
    event.preventDefault(); event.stopPropagation()
    await nextTick()
    if (drag !== current) return
    measureSheet()
  }
  if (event.cancelable) event.preventDefault()
  event.stopPropagation()
  sheetOffset.value = current.mode === 'open' ? Math.max(0, sheetHeight.value - current.distance) : Math.min(sheetHeight.value, current.distance)
}
function endDrag(event: PointerEvent) {
  const current = drag
  if (!current || current.id !== event.pointerId) return
  const cancelled = event.type === 'pointercancel' || event.type === 'lostpointercapture'
  releaseDrag()
  if (!current.started) return
  event.preventDefault(); event.stopPropagation(); ignoreClickUntil = performance.now() + 400
  const crossed = !cancelled && current.distance >= Math.min(96, sheetHeight.value * .22)
  finishSheet(current.mode === 'open' ? crossed : !crossed)
}
function handleClick(event: MouseEvent) {
  if (event.detail !== 0 && performance.now() < ignoreClickUntil) { event.preventDefault(); return }
  close()
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
  if (event.key !== 'Tab') return
  const elements = [...(sheet.value?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],select') || [])]
  const first = elements[0], last = elements.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onBeforeUnmount(() => { ++motionSerial; releaseDrag(); clearTimeout(settleTimer); request?.abort(); emit('panel', false) })
</script>
<template>
  <section class="stream-shell">
    <header class="stream-top" :inert="opened || inactive ? true : undefined">
      <button class="stream-back" aria-label="返回图库" @click="emit('back')"><Icon name="left" /><span>图库</span></button>
      <small class="stream-counter">{{ counter }}</small>
      <select :value="order" aria-label="流式阅览顺序" @change="emit('order', ($event.target as HTMLSelectElement).value)"><option value="sequential">顺序</option><option value="random">随机</option></select>
    </header>
    <div class="stream-canvas" :inert="opened || inactive ? true : undefined">
      <slot v-if="!ended" />
      <RestrictionBadge v-if="!ended" class="stream-rating" :value="page?.x_restrict" />
      <div v-else class="stream-finished"><p>已到当前范围末尾</p><button @click="emit('back')">返回图库</button></div>
    </div>
    <div v-if="activity.phase || activity.error" class="stream-activity" role="status">
      <span>{{ activity.error || activity.phase }}</span><button v-if="activity.error" @click="emit('retry')">重试</button><button @click="emit('cancel')">取消</button>
    </div>
    <footer class="stream-dock" :inert="opened || inactive ? true : undefined">
      <button class="stream-avatar" aria-label="查看画师全部作品" :title="page?.artist_name" :disabled="!page" @click="page && emit('artist', page.artist_id)">
        <img v-if="artist?.avatar && !avatarFailed" :src="artist.avatar" alt="" @error="avatarFailed = true"><Icon v-else name="artists" />
      </button>
      <button ref="trigger" class="stream-caption-trigger" aria-label="展开作品简介" :aria-expanded="opened" aria-controls="stream-caption-sheet" :disabled="!page" @click="open"
        @pointerdown="startDrag($event, 'open')" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" @lostpointercapture="endDrag">
        <strong>{{ page?.artist_name || '流式阅览' }}<Icon name="down" /></strong><span>{{ page?.title }}<template v-if="page"> · {{ caption }}</template></span>
      </button>
      <BookmarkButton v-if="page" :key="page.work_id" :work-id="page.work_id" @message="emit('message', $event)" />
    </footer>
      <div v-if="presented" class="stream-sheet-overlay" :class="{'is-dragging': dragging}" :style="overlayStyle" @click.self="close()">
        <section id="stream-caption-sheet" ref="sheet" class="stream-sheet" :style="{transform: `translateY(${sheetOffset}px)`}" role="dialog" aria-modal="true" aria-label="作品简介" data-no-navigation @keydown="key">
          <button class="stream-sheet-handle" aria-label="拖动或点击收起作品简介" @click="handleClick"
            @pointerdown="startDrag($event, 'close')" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" @lostpointercapture="endDrag"><span /></button>
          <header><h2>作品简介</h2><button aria-label="关闭作品简介" @click="close()"><Icon name="close" /></button></header>
          <div class="stream-details">
            <h3>{{ page?.title }}</h3><button class="text-link" @click="page && emit('artist', page.artist_id)">{{ page?.artist_name }}</button>
            <div v-if="detail" class="detail-tags"><button v-for="tag in detail.tags" :key="tag" class="tag" @click="emit('tag', tag)"><TagLabel :name="tag" :details="detail.tag_details" /></button></div>
            <p v-if="metadataError" class="error">{{ metadataError }} <button @click="metadata">重试</button></p>
            <div v-if="detail?.caption" class="stream-full-caption" v-html="detail.caption" /><p v-else>{{ caption }}</p>
            <div class="stream-detail-actions"><a v-if="page" :href="`https://www.pixiv.net/artworks/${page.pixiv_id}`" target="_blank" rel="noopener noreferrer">在 Pixiv 查看 <Icon name="external" /></a><button @click="close(false); emit('manage')">整理作品</button></div>
          </div>
        </section>
      </div>
  </section>
</template>
<style scoped>
.stream-shell { position: absolute; inset: 0; display: grid; grid-template-rows: 48px minmax(0,1fr) auto; min-width: 0; background: var(--art-bg); }
.stream-top { display: flex; align-items: center; gap: 12px; padding: 0 20px; background: var(--bg); }
.stream-top button, .stream-top select, .stream-sheet header button { background: transparent; color: var(--text); border: 0; box-shadow: none; }
.stream-back { display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 4px; }
.stream-counter { margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; font-size: 11px; }
.stream-top select { padding: 6px; min-height: 40px; font-size: 12px; }
.stream-canvas { position: relative; min-height: 0; min-width: 0; overflow: hidden; }
.stream-rating { position: absolute; top: 12px; left: 28px; z-index: 2; pointer-events: none; }
.stream-finished { height: 100%; display: grid; place-content: center; text-align: center; gap: 16px; }
.stream-dock { display: flex; gap: 12px; align-items: center; padding: 12px max(20px,calc((100% - 900px)/2)) calc(12px + var(--bottom-safe-inset)); background: var(--bg); min-width: 0; }
.stream-avatar { width: 42px; height: 42px; border: 0; border-radius: 50%; flex-shrink: 0; padding: 0; display: grid; place-items: center; background: var(--surface2); color: var(--muted); overflow: hidden; }
.stream-avatar img { width: 100%; height: 100%; object-fit: cover; }
.stream-caption-trigger { touch-action: none; user-select: none; display: block; flex: 1; min-width: 0; padding: 2px 0; text-align: left; border: 0; background: transparent; box-shadow: none; color: var(--text); }
.stream-caption-trigger strong { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; }
.stream-caption-trigger strong .icon { width: 12px; height: 12px; }
.stream-caption-trigger > span { display: block; margin-top: 5px; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stream-dock :deep(.bookmark-button) { width: 44px; height: 44px; flex-shrink: 0; border: 0; background: transparent; }
.stream-activity { position: absolute; z-index: 4; bottom: calc(80px + var(--bottom-safe-inset)); left: 50%; transform: translateX(-50%); max-width: min(600px,calc(100% - 32px)); padding: 8px 12px; display: flex; gap: 8px; align-items: center; border-radius: 12px; background: var(--surface); box-shadow: 0 2px 16px #0002; font-size: 12px; }
.stream-activity span { min-width: 0; overflow-wrap: anywhere; }
.stream-activity button { background: transparent; color: var(--accent); border: 0; padding: 8px; flex-shrink: 0; }
.stream-sheet-overlay { transition: background-color .18s ease; position: absolute; z-index: 15; inset: 0; background: #0006; display: flex; align-items: flex-end; justify-content: center; }
.stream-sheet { transition: transform .18s cubic-bezier(.22,.7,.2,1); width: min(760px,100%); max-height: 78%; display: flex; flex-direction: column; background: var(--surface); border-radius: 20px 20px 0 0; padding-bottom: var(--bottom-safe-inset); box-shadow: 0 -8px 40px #0002; }
.stream-sheet header { display: flex; align-items: center; justify-content: space-between; padding: 10px 20px; flex-shrink: 0; border-bottom: 1px solid var(--line); }
.stream-sheet h2 { font-size: 14px; margin: 0; }
.stream-sheet header button { width: 44px; height: 44px; padding: 12px; }
.stream-details { padding: 20px 24px 28px; overflow-y: auto; overscroll-behavior: contain; min-height: 0; overflow-wrap: anywhere; line-height: 1.7; }
.stream-details h3 { margin: 0 0 6px; font-size: 20px; line-height: 1.4; }
.stream-details .detail-tags { margin: 20px 0; }
.stream-full-caption { white-space: normal; font-size: 14px; }
.stream-full-caption :deep(img) { max-width: 100%; }
.stream-detail-actions { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; border-top: 1px solid var(--line); padding-top: 20px; margin-top: 28px; font-size: 13px; }
.stream-detail-actions a { display: inline-flex; gap: 6px; align-items: center; }
.stream-detail-actions .icon { width: 14px; height: 14px; }
.stream-sheet-handle { flex-shrink: 0; display: grid; place-items: center; width: 100%; height: 44px; padding: 0; border: 0; box-shadow: none; background: transparent; touch-action: none; cursor: grab; }
.stream-sheet-handle span { width: 38px; height: 4px; border-radius: 4px; background: var(--muted); opacity: .5; }
.is-dragging, .is-dragging .stream-sheet { transition: none; }
.is-dragging .stream-sheet-handle { cursor: grabbing; }
@media(max-width:760px) {
  .stream-top { padding: 0 16px; gap: 6px; }
  .stream-top select { max-width: 76px; }
  .stream-back { font-size: 12px; gap: 3px; }
  .stream-dock { padding: 10px 16px calc(10px + var(--bottom-safe-inset)); gap: 10px; }
  .stream-details { padding: 16px 20px 24px; }
}
@media(prefers-reduced-motion:reduce) { .stream-sheet-overlay, .stream-sheet { transition: none; } }
</style>
