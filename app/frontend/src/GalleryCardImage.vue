<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RecoveryState } from './types'

const props = defineProps<{
  thumbnail: string | null
  preview: string | null
  previewStatus: 'ready' | 'pending' | 'failed' | 'unavailable'
  animation?: boolean
  label: string
  recovery?: RecoveryState | null
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
  if (props.recovery?.status === 'queued') return '已排队补齐'
  if (props.recovery?.status === 'waiting') return '等待自动重试'
  if (props.recovery?.status === 'attention') return '主站暂时无法补齐'
  if (props.animation) {
    if (props.previewStatus === 'failed') return '动图预览生成失败'
    if (props.previewStatus === 'unavailable') return '动图预览不可用'
    if (props.previewStatus === 'pending') return '动图预览尚未生成'
  }
  if (props.previewStatus === 'failed') return '原图下载失败'
  if (props.previewStatus === 'unavailable') return '本地原图不可用'
  if (props.previewStatus === 'pending') return '原图尚未下载'
  if (thumbnailFailed.value || previewFailed.value) return '本地图片暂时无法显示'
  return '缩略图待生成'
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
    <div v-else class="media-placeholder"><span>{{ placeholder }}</span><span v-if="recovery?.status === 'attention' && recovery.reason" class="placeholder-reason">{{ recovery.reason }}</span></div>
  </div>
</template>
