<script setup lang="ts">
import { computed, nextTick, onUnmounted, onMounted, ref } from 'vue'
import type { PageData } from './types'
import MediaImage from './MediaImage.vue'
defineProps<{page: PageData}>()
const emit = defineEmits<{close: []}>()
const dialog = ref<HTMLDivElement | null>(null), scale = ref(1), x = ref(0), y = ref(0)
const points = new Map<number, {x: number; y: number}>()
let distance = 0, midpoint = {x: 0, y: 0}, lastFocus: HTMLElement | null = null
const transform = computed(() => ({transform: `translate(${x.value}px, ${y.value}px) scale(${scale.value})`}))
function reset() { scale.value = 1; x.value = y.value = 0 }
function clamp() {
  const width = dialog.value?.clientWidth || 1, height = dialog.value?.clientHeight || 1
  x.value = Math.max(-width * (scale.value - 1) / 2, Math.min(width * (scale.value - 1) / 2, x.value))
  y.value = Math.max(-height * (scale.value - 1) / 2, Math.min(height * (scale.value - 1) / 2, y.value))
}
function zoom(factor: number) { scale.value = Math.max(1, Math.min(8, scale.value * factor)); clamp() }
function metrics() {
  const [a, b] = [...points.values()]
  return a && b ? {d: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2} : {d: 0, x: a?.x || 0, y: a?.y || 0}
}
function down(event: PointerEvent) {
  if ((event.target as Element).closest('button')) return
  event.preventDefault(); dialog.value?.setPointerCapture(event.pointerId)
  points.set(event.pointerId, {x: event.clientX, y: event.clientY})
  const m = metrics(); distance = m.d; midpoint = m
}
function move(event: PointerEvent) {
  if (!points.has(event.pointerId)) return
  event.preventDefault(); points.set(event.pointerId, {x: event.clientX, y: event.clientY})
  const m = metrics()
  if (m.d && distance) zoom(m.d / distance)
  if (scale.value > 1) { x.value += m.x - midpoint.x; y.value += m.y - midpoint.y; clamp() }
  distance = m.d; midpoint = m
}
function up(event: PointerEvent) { points.delete(event.pointerId); const m = metrics(); distance = m.d; midpoint = m }
function key(event: KeyboardEvent) {
  event.stopPropagation()
  if (event.key === 'Escape') emit('close')
  if (event.key === '+' || event.key === '=') zoom(1.25)
  if (event.key === '-') zoom(0.8)
  if (event.key === 'Tab') {
    const buttons = [...(dialog.value?.querySelectorAll<HTMLButtonElement>('button') || [])]
    if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons.at(-1)?.focus() }
    else if (!event.shiftKey && document.activeElement === buttons.at(-1)) { event.preventDefault(); buttons[0]?.focus() }
  }
}
onMounted(async () => { lastFocus = document.activeElement as HTMLElement; await nextTick(); dialog.value?.querySelector('button')?.focus() })
onUnmounted(() => { void nextTick(() => lastFocus?.focus({preventScroll: true})) })
</script>
<template>
  <div ref="dialog" class="zoom-view" role="dialog" aria-modal="true" aria-label="放大查看" tabindex="-1"
    @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @keydown="key"
    @wheel.prevent="zoom($event.deltaY < 0 ? 1.12 : 0.89)">
    <div class="zoom-canvas" :style="transform"><MediaImage :src="page.src" :label="page.title" /></div>
    <div class="zoom-toolbar"><button @click="zoom(0.8)" aria-label="缩小">−</button><button @click="reset">{{ Math.round(scale * 100) }}%</button><button @click="zoom(1.25)" aria-label="放大">＋</button><button @click="emit('close')">关闭放大 <kbd>Esc</kbd></button></div>
    <p class="zoom-hint">滚轮或双指缩放 · 拖动画面</p>
  </div>
</template>
