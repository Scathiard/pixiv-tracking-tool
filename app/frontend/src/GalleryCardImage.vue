<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { mediaPlaceholder, type ArchiveState, type ProcessingState, type RecoveryState } from './types'

const props = defineProps<{
  thumbnail: string | null
  preview: string | null
  previewStatus: 'ready' | 'pending' | 'failed' | 'unavailable'
  animation?: boolean
  label: string
  recovery?: RecoveryState | null
  archive?: ArchiveState
  processing?: ProcessingState
}>()
const host = ref<HTMLElement | null>(null), image = ref<HTMLImageElement | null>(null)
const visible = ref(false)
const thumbnailFailed = ref(false), previewFailed = ref(false)
let observer: IntersectionObserver | null = null
watch(() => [props.thumbnail, props.preview], () => {
  thumbnailFailed.value = false
  previewFailed.value = false
})
watch(() => props.previewStatus, (status, previous) => {
  if (status === 'ready' && previous !== 'ready') previewFailed.value = false
})
watch(() => props.recovery?.status, (status, previous) => {
  if (status === 'ready' && previous && previous !== 'ready') {
    if (thumbnailFailed.value) thumbnailFailed.value = false
    if (previewFailed.value) previewFailed.value = false
  }
})
const source = computed(() => {
  if (props.thumbnail && !thumbnailFailed.value) return {url: props.thumbnail, kind: 'thumbnail'}
  if (props.preview && visible.value && !previewFailed.value) return {url: props.preview, kind: 'original'}
  return null
})
const placeholder = computed(() => {
  if (thumbnailFailed.value || previewFailed.value) return {label: '本地图片暂时无法显示', reason: '可打开作品重试图片'}
  if (props.previewStatus === 'ready') return {label: '缩略图待生成', reason: null}
  return mediaPlaceholder(props, props.previewStatus, props.animation)
})
function failed(event: Event) {
  if ((event.currentTarget as HTMLImageElement).dataset.mediaSource === 'thumbnail') thumbnailFailed.value = true
  else if (visible.value) previewFailed.value = true
}
onMounted(() => {
  const root = host.value?.closest('.gallery-viewport')
  if (!host.value || !root) return
  observer = new IntersectionObserver(entries => { visible.value = !!entries[0]?.isIntersecting },
    {root, rootMargin: '0px', threshold: 0})
  observer.observe(host.value)
})
watch(visible, value => { if (!value && image.value?.dataset.mediaSource === 'original') image.value.removeAttribute('src') })
onBeforeUnmount(() => { observer?.disconnect(); image.value?.removeAttribute('src') })
</script>

<template>
  <div ref="host" class="media-image media-thumbnail">
    <img v-if="source" :key="source.kind + source.url" ref="image" :src="source.url" :alt="label"
      :data-media-source="source.kind" draggable="false" decoding="async" loading="lazy" @error="failed">
    <div v-else class="media-placeholder"><span>{{ placeholder.label }}</span><span v-if="placeholder.reason" class="placeholder-reason">{{ placeholder.reason }}</span></div>
  </div>
</template>
