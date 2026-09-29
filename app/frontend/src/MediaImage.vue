<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { RecoveryState } from './types'
const props = defineProps<{src: string | null; label: string; thumbnail?: boolean; width?: number; height?: number; recovery?: RecoveryState | null}>()
const emit = defineEmits<{size: [width: number, height: number]; zoom: []}>()
const image = ref<HTMLImageElement | null>(null), failed = ref(false), revision = ref(0)
watch(() => props.src, () => { failed.value = false; revision.value++ })
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
    <div v-else class="media-placeholder" :title="recovery?.status === 'waiting' ? recovery.reason || undefined : undefined">
      <span>{{ src ? '这张图片暂时无法显示' : recovery?.status === 'queued' ? '已排队补齐' : recovery?.status === 'waiting' ? '等待自动重试' : thumbnail ? '暂无预览图' : '本地缺少此页' }}</span>
      <button v-if="src && failed && !thumbnail" type="button" class="quiet" @click.stop="failed = false; revision++">重试图片</button>
    </div>
  </div>
</template>
