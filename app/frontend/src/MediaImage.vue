<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mediaPlaceholder, type ArchiveState, type ProcessingState, type RecoveryState } from './types'
import { ImageTaps } from './state/imageTaps'
const props = defineProps<{src: string | null; state?: string; label: string; thumbnail?: boolean; width?: number; height?: number; recovery?: RecoveryState | null; processing?: ProcessingState; archive?: ArchiveState; animation?: boolean; flowTaps?: boolean}>()
const emit = defineEmits<{size: [width: number, height: number]; zoom: []; bookmark: []}>()
const taps = new ImageTaps(() => emit('zoom'), () => emit('bookmark'))
const image = ref<HTMLImageElement | null>(null), failed = ref(false), revision = ref(0)
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
function loaded() { if (image.value) emit('size', image.value.naturalWidth, image.value.naturalHeight) }
onBeforeUnmount(() => { taps.cancel(); image.value?.removeAttribute('src') })
</script>
<template>
  <div class="media-image" :class="{'media-thumbnail': thumbnail}">
    <img v-if="src && !failed" :key="revision" ref="image" :src="src" :alt="label" :width="width" :height="height" draggable="false" decoding="async"
      @load="loaded" @error="failed = true" @dblclick.prevent="!flowTaps && emit('zoom')"
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
