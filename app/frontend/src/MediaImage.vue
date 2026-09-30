<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mediaPlaceholder, type ArchiveState, type ProcessingState, type RecoveryState } from './types'
const props = defineProps<{src: string | null; state?: string; label: string; thumbnail?: boolean; width?: number; height?: number; recovery?: RecoveryState | null; processing?: ProcessingState; archive?: ArchiveState; animation?: boolean}>()
const emit = defineEmits<{size: [width: number, height: number]; zoom: []}>()
const image = ref<HTMLImageElement | null>(null), failed = ref(false), revision = ref(0)
const placeholder = computed(() => mediaPlaceholder(props, props.state === 'failed' ? 'failed' : 'pending', props.animation))
watch(() => props.src, () => { failed.value = false; revision.value++ })
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
onBeforeUnmount(() => { image.value?.removeAttribute('src') })
</script>
<template>
  <div class="media-image" :class="{'media-thumbnail': thumbnail}">
    <img v-if="src && !failed" :key="revision" ref="image" :src="src" :alt="label" :width="width" :height="height" draggable="false" decoding="async"
      @load="loaded" @error="failed = true" @dblclick="emit('zoom')">
    <div v-else class="media-placeholder">
      <span>{{ src ? '这张图片暂时无法显示' : placeholder.label }}</span>
      <span v-if="!src && placeholder.reason" class="placeholder-reason">{{ placeholder.reason }}</span>
      <button v-if="src && failed && !thumbnail" type="button" class="quiet" @click.stop="failed = false; revision++">重试图片</button>
    </div>
  </div>
</template>
