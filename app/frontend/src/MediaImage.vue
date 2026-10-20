<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mediaPlaceholder, type ArchiveState, type ProcessingState, type RecoveryState } from './types'
import { ImageTaps } from './state/imageTaps'
import { mediaObservation } from './diagnostics'
import { prepareImage } from './api'
const props = defineProps<{src: string | null; preview?: string | null; state?: string; label: string; thumbnail?: boolean; width?: number; height?: number; recovery?: RecoveryState | null; processing?: ProcessingState; archive?: ArchiveState; animation?: boolean; flowTaps?: boolean}>()
const emit = defineEmits<{size: [width: number, height: number]; zoom: []; bookmark: []}>()
const taps = new ImageTaps(() => emit('zoom'), () => emit('bookmark'))
const image = ref<HTMLImageElement | null>(null), failed = ref(false), revision = ref(0)
const displaySrc = ref<string | null>(null)
let originalRequest: AbortController | null = null
watch([() => props.src, () => props.preview], ([src, preview]) => {
  originalRequest?.abort()
  displaySrc.value = preview || src
  if (preview && src && preview !== src) {
    const controller = originalRequest = new AbortController()
    void prepareImage(src, controller.signal).then(() => {
      if (!controller.signal.aborted) { displaySrc.value = src; failed.value = false }
    }, () => { /* Keep the displayable preview if the original is slow or fails. */ })
  }
}, {immediate: true})
const placeholder = computed(() => mediaPlaceholder(props, props.state === 'failed' ? 'failed' : 'pending', props.animation))
watch(() => props.src, () => { taps.cancel(); failed.value = false; revision.value++ })
watch(() => props.state, (state, previous) => {
  if (state === 'verified' && previous && previous !== 'verified' && failed.value) {
    failed.value = false; revision.value++
  }
})
watch(() => props.recovery?.status, (status, previous) => {
  if (status === 'ready' && previous && previous !== 'ready' && failed.value) {
    failed.value = false; revision.value++
  }
})
let observation: ReturnType<typeof mediaObservation>
watch([displaySrc, revision, failed], () => {
  observation?.cancel('navigation')
  observation = displaySrc.value && !failed.value ? mediaObservation(displaySrc.value) : undefined
}, {immediate: true})
function loaded() {
  if (!image.value) return
  observation?.loaded(image.value); observation?.decoded('unsupported')
  // A cropped thumbnail is not the page's aspect ratio. Keep the original
  // metadata dimensions until the actual original is on screen.
  if (!props.preview || displaySrc.value === props.src) emit('size', image.value.naturalWidth, image.value.naturalHeight)
}
function loadFailed() {
  observation?.error()
  if (displaySrc.value !== props.src && props.src) { displaySrc.value = props.src; failed.value = false }
  else failed.value = true
}
onBeforeUnmount(() => { originalRequest?.abort(); observation?.cancel('unmounted'); taps.cancel(); image.value?.removeAttribute('src') })
</script>
<template>
  <div class="media-image" :class="{'media-thumbnail': thumbnail}">
    <img v-if="displaySrc && !failed" :key="revision" ref="image" :src="displaySrc" :alt="label" :width="width" :height="height" draggable="false" decoding="async"
      @load="loaded" @error="loadFailed" @dblclick.prevent="!flowTaps && emit('zoom')"
      @pointerdown="flowTaps && $event.button === 0 && taps.down($event.pointerId, $event.clientX, $event.clientY)"
      @pointermove="flowTaps && taps.move($event.pointerId, $event.clientX, $event.clientY)"
      @pointerup="flowTaps && taps.up($event.pointerId, $event.clientX, $event.clientY)"
      @pointercancel="taps.cancelPointer($event.pointerId)">
    <div v-else class="media-placeholder">
      <span>{{ src ? '这张图片暂时无法显示' : placeholder.label }}</span>
      <span v-if="!src && placeholder.reason" class="placeholder-reason">{{ placeholder.reason }}</span>
      <button v-if="src && failed && !thumbnail" type="button" class="quiet" @click.stop="failed = false; revision++">重试图片</button>
    </div>
  </div>
</template>
