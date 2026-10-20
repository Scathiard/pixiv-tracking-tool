<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MediaImage from '../MediaImage.vue'
import type { PageData } from '../types'
import { boundInlineTransform, inlineZoomAt, inlineZoomFrame, INLINE_ZOOM_DURATION, type InlineTransform } from '../state/inlineZoom'

const props = defineProps<{page: PageData; inactive?: boolean}>()
const emit = defineEmits<{size: [width: number, height: number]; engaged: [value: boolean]}>()
const frame = ref<HTMLDivElement | null>(null)
const transform = ref<InlineTransform>({scale: 1, x: 0, y: 0})
const touchOwned = ref(false), mouseOwned = ref(false), wheelOwned = ref(false)
const active = computed(() => transform.value.scale > 1)
const consumed = computed(() => touchOwned.value || mouseOwned.value || wheelOwned.value)
const mediaStyle = computed(() => ({transform: `translate(${transform.value.x}px, ${transform.value.y}px) scale(${transform.value.scale})`}))
let observer: ResizeObserver | null = null, wheelTimer = 0, lastWheel = 0
let engaged = false, ignoreDoubleClickUntil = 0
let motionQuery: MediaQueryList | null = null, animationFrame = 0
let animation: {from: InlineTransform; to: InlineTransform; start: number} | null = null
let mouse: {id: number; x: number; y: number; target: HTMLElement} | null = null
type TouchGesture = {mode: 'tap' | 'pan' | 'pinch' | 'blocked'; x: number; y: number; startX: number; startY: number;
  moved: boolean; distance: number; scale: number; imageX: number; imageY: number}
let touch: TouchGesture | null = null
let lastTap: {time: number; x: number; y: number} | null = null

function dimensions() { return {width: frame.value?.clientWidth || 1, height: frame.value?.clientHeight || 1} }
function local(clientX: number, clientY: number) {
  const box = frame.value?.getBoundingClientRect()
  return {x: clientX - (box?.left || 0), y: clientY - (box?.top || 0)}
}
function imageTarget(target: EventTarget | null) { return target instanceof Element && target.tagName === 'IMG' }
function publish() {
  // Set these synchronously: the reader examines them in the same bubbling event.
  if (frame.value) {
    frame.value.dataset.inlineZoomActive = String(active.value)
    frame.value.dataset.inlineZoomConsumed = String(consumed.value)
    frame.value.dataset.inlineZoomScale = String(transform.value.scale)
  }
  const next = active.value || consumed.value
  if (next !== engaged) { engaged = next; emit('engaged', next) }
}
function claim(kind: 'pointer' | 'wheel') {
  publish()
  frame.value?.dispatchEvent(new CustomEvent('inline-zoom-gesture', {bubbles: true, detail: {kind}}))
}
function consume(event: Event) { if (event.cancelable) event.preventDefault(); event.stopPropagation() }
function stopAnimation() { cancelAnimationFrame(animationFrame); animationFrame = 0; animation = null }
function apply(value: InlineTransform) { transform.value = boundInlineTransform(value, dimensions()); publish() }
function update(value: InlineTransform) { stopAnimation(); apply(value) }
function animate(value: InlineTransform) {
  stopAnimation()
  if (motionQuery?.matches) { apply(value); return }
  animation = {from: {...transform.value}, to: value, start: performance.now()}
  const step = (now: number) => {
    const current = animation
    if (!current) return
    apply(inlineZoomFrame(current.from, current.to, now - current.start))
    if (now - current.start < INLINE_ZOOM_DURATION) animationFrame = requestAnimationFrame(step)
    else { apply(current.to); stopAnimation() }
  }
  animationFrame = requestAnimationFrame(step)
}
function reducedMotionChanged() { if (motionQuery?.matches && animation) update(animation.to) }
function zoomAt(scale: number, clientX: number, clientY: number) {
  update(inlineZoomAt(transform.value, scale, local(clientX, clientY), dimensions()))
}
function toggle(clientX: number, clientY: number) {
  const enlarged = animation ? animation.to.scale > 1 : active.value
  claim('pointer'); animate(inlineZoomAt(transform.value, enlarged ? 1 : 2.25, local(clientX, clientY), dimensions())); lastTap = null
}
function doubleClick(event: MouseEvent) {
  if (props.inactive || !imageTarget(event.target)) return
  consume(event)
  if (performance.now() < ignoreDoubleClickUntil) return
  toggle(event.clientX, event.clientY)
}
function pointerDown(event: PointerEvent) {
  if (props.inactive || event.pointerType === 'touch' || event.button !== 0 || (!active.value && !animation) || !imageTarget(event.target)) return
  stopAnimation()
  consume(event); lastTap = null
  const target = event.target as HTMLElement
  mouse = {id: event.pointerId, x: event.clientX, y: event.clientY, target}
  mouseOwned.value = true; claim('pointer')
  target.setPointerCapture?.(event.pointerId)
  frame.value?.focus({preventScroll: true})
}
function pointerMove(event: PointerEvent) {
  if (!mouse || event.pointerId !== mouse.id) return
  consume(event)
  update({...transform.value, x: transform.value.x + event.clientX - mouse.x, y: transform.value.y + event.clientY - mouse.y})
  mouse.x = event.clientX; mouse.y = event.clientY
}
function releaseMouse() {
  const previous = mouse; mouse = null; mouseOwned.value = false
  if (previous?.target.hasPointerCapture?.(previous.id)) previous.target.releasePointerCapture(previous.id)
  publish()
}
function pointerEnd(event: PointerEvent) {
  if (!mouse || event.pointerId !== mouse.id) return
  consume(event); releaseMouse()
}
function point(touches: TouchList) {
  const a = touches[0]!, b = touches[1]!
  return {x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2,
    distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)}
}
function beginPinch(touches: TouchList) {
  const middle = point(touches), at = local(middle.x, middle.y), value = transform.value
  touch = {mode: 'pinch', x: middle.x, y: middle.y, startX: middle.x, startY: middle.y, moved: true,
    distance: Math.max(1, middle.distance), scale: value.scale, imageX: (at.x - value.x) / value.scale,
    imageY: (at.y - value.y) / value.scale}
  touchOwned.value = true; lastTap = null; claim('pointer')
}
function touchStart(event: TouchEvent) {
  if (props.inactive || !imageTarget(event.target)) return
  stopAnimation()
  if (event.touches.length >= 2) { beginPinch(event.touches); consume(event); return }
  const first = event.touches[0]
  if (!first) return
  // Ownership remains with this frame until all fingers release, even at 100%.
  const owned = touchOwned.value || active.value
  touch = {mode: owned ? (active.value ? 'pan' : 'blocked') : 'tap', x: first.clientX, y: first.clientY,
    startX: first.clientX, startY: first.clientY, moved: false, distance: 0, scale: transform.value.scale, imageX: 0, imageY: 0}
  if (owned) { touchOwned.value = true; claim('pointer'); consume(event) }
}
function touchMove(event: TouchEvent) {
  if (!touch || props.inactive) return
  if (event.touches.length >= 2) {
    if (touch.mode !== 'pinch') beginPinch(event.touches)
    const current = touch!, middle = point(event.touches), at = local(middle.x, middle.y)
    const scale = Math.max(1, Math.min(5, current.scale * middle.distance / current.distance))
    update({scale, x: at.x - current.imageX * scale, y: at.y - current.imageY * scale})
    consume(event); return
  }
  const first = event.touches[0]
  if (!first) return
  if (Math.hypot(first.clientX - touch.startX, first.clientY - touch.startY) > 7) { touch.moved = true; lastTap = null }
  if (touch.mode === 'pan') update({...transform.value,
    x: transform.value.x + first.clientX - touch.x, y: transform.value.y + first.clientY - touch.y})
  touch.x = first.clientX; touch.y = first.clientY
  if (touchOwned.value) consume(event)
}
function touchEnd(event: TouchEvent) {
  if (!touch) return
  const owned = touchOwned.value
  if (owned) consume(event)
  if (event.touches.length) {
    const first = event.touches[0]!
    touch = {...touch, mode: active.value ? 'pan' : 'blocked', x: first.clientX, y: first.clientY,
      startX: first.clientX, startY: first.clientY, moved: true}
    return
  }
  const previous = touch; touch = null; touchOwned.value = false; publish()
  if (event.type === 'touchcancel' || previous.moved || previous.mode === 'blocked' || previous.mode === 'pinch') { lastTap = null; return }
  const now = performance.now()
  if (lastTap && now - lastTap.time < 330 && Math.hypot(previous.x - lastTap.x, previous.y - lastTap.y) < 28) {
    consume(event); ignoreDoubleClickUntil = now + 700; toggle(previous.x, previous.y)
  } else lastTap = {time: now, x: previous.x, y: previous.y}
}
function wheel(event: WheelEvent) {
  if (props.inactive || (!active.value && !wheelOwned.value && !animation)) return
  const wasAnimating = !!animation
  stopAnimation()
  const now = performance.now(), continuing = wasAnimating || (wheelOwned.value && now - lastWheel <= 220)
  if (!active.value && !continuing) { wheelOwned.value = false; publish(); return }
  consume(event)
  wheelOwned.value = true; lastWheel = now; claim('wheel')
  clearTimeout(wheelTimer)
  wheelTimer = window.setTimeout(() => { wheelOwned.value = false; publish() }, 220)
  if (active.value || wasAnimating) {
    const delta = event.deltaY * (event.deltaMode === 1 ? 18 : event.deltaMode === 2 ? dimensions().height : 1)
    zoomAt(transform.value.scale * Math.exp(-delta * .002), event.clientX, event.clientY)
  }
}
function reset() {
  stopAnimation()
  clearTimeout(wheelTimer); wheelTimer = 0; lastWheel = 0; lastTap = null; touch = null
  releaseMouse(); touchOwned.value = false; wheelOwned.value = false
  update({scale: 1, x: 0, y: 0})
}
function keydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !active.value) return
  consume(event); update({scale: 1, x: 0, y: 0})
}
watch([() => props.page.work_id, () => props.page.page, () => props.page.revision_id, () => props.page.src], reset)
watch(() => props.inactive, value => { if (value) reset() })
onMounted(() => {
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionQuery.addEventListener('change', reducedMotionChanged)
  observer = new ResizeObserver(() => update(transform.value))
  if (frame.value) observer.observe(frame.value)
})
onBeforeUnmount(() => { motionQuery?.removeEventListener('change', reducedMotionChanged); observer?.disconnect(); reset() })
</script>

<template>
  <div ref="frame" class="inline-zoom-media" :class="{'inline-zoomed': active, 'inline-dragging': mouseOwned}"
    :data-inline-zoom-active="String(active)" :data-inline-zoom-consumed="String(consumed)" :data-inline-zoom-scale="transform.scale"
    tabindex="0" :aria-label="`${page.title}，第 ${page.page + 1} 页；双击放大，再次双击还原`"
    @dblclick.capture="doubleClick" @pointerdown="pointerDown" @pointermove="pointerMove"
    @pointerup="pointerEnd" @pointercancel="pointerEnd" @lostpointercapture="pointerEnd"
    @touchstart="touchStart" @touchmove="touchMove" @touchend="touchEnd" @touchcancel="touchEnd" @wheel="wheel" @keydown="keydown"
    @error.capture="reset">
    <div class="inline-zoom-content" :style="mediaStyle">
      <MediaImage :src="page.src" :label="page.title" :state="page.state"
        :width="page.width || undefined" :height="page.height || undefined" :recovery="page.recovery"
        :processing="page.processing" :archive="page.archive" :animation="page.type === 'ugoira'"
        @size="(width, height) => emit('size', width, height)" />
    </div>
    <span v-if="active" class="inline-zoom-label" aria-hidden="true">{{ Math.round(transform.scale * 100) }}% · 双击还原</span>
  </div>
</template>

<style scoped>
.inline-zoom-media{position:relative;width:100%;height:100%;overflow:hidden;isolation:isolate;touch-action:pan-y;outline:none}
.inline-zoom-media:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.inline-zoom-content{width:100%;height:100%;transform-origin:0 0}
.inline-zoom-content :deep(.media-image),.inline-zoom-content :deep(.media-image img){width:100%;height:100%}
.inline-zoom-content :deep(.media-image img){object-fit:contain;-webkit-user-drag:none}
.inline-zoom-content :deep(.media-placeholder){height:100%;min-height:0;aspect-ratio:auto}
.inline-zoomed{touch-action:none;cursor:grab}
.inline-dragging{cursor:grabbing}
.inline-zoom-label{position:absolute;left:10px;top:10px;padding:4px 7px;border-radius:4px;background:#10151c99;color:#fff;font-size:10px;pointer-events:none}
</style>
