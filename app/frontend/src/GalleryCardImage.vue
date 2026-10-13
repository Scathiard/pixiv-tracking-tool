<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { mediaPlaceholder, type ArchiveState, type ProcessingState, type RecoveryState } from './types'
import { galleryMediaSource, mediaRetryDelay, newerMediaRevision, staticMediaRetryDelay } from './state/galleryMedia'
import { mediaObservation } from './diagnostics'

const props = defineProps<{
  thumbnail: string | null
  preview: string | null
  previewStatus: 'ready' | 'pending' | 'failed' | 'unavailable'
  animation?: boolean
  suspended?: boolean
  mediaRevision?: number
  label: string
  recovery?: RecoveryState | null
  archive?: ArchiveState
  processing?: ProcessingState
}>()
const host = ref<HTMLElement | null>(null), image = ref<HTMLImageElement | null>(null), staticImage = ref<HTMLImageElement | null>(null)
const visible = ref(false)
const foreground = ref(document.visibilityState === 'visible')
const active = computed(() => visible.value && foreground.value && (!props.animation || !props.suspended))
// Keep only already-started lightweight previews in the bounded virtual window.
// Scroll/brief exit masks paint; changing src would restart the browser's animation.
const retainedPreview = ref<string | null>(null)
watch([active, foreground, () => props.preview, () => props.animation], () => {
  if (!foreground.value || !props.animation || retainedPreview.value !== props.preview) retainedPreview.value = null
  if (active.value && props.animation && props.preview) retainedPreview.value = props.preview
})
const thumbnailFailed = ref(false), previewFailed = ref(false)
let observer: IntersectionObserver | null = null
let previewFailedAt = 0, retryTimer = 0, pendingMediaUpdate = false
let thumbnailFailedAt = 0, thumbnailFailures = 0, previewFailures = 0, staticRetryTimer = 0
function clearPreviewRetry() { clearTimeout(retryTimer); retryTimer = 0; pendingMediaUpdate = false }
function clearStaticRetry() { clearTimeout(staticRetryTimer); staticRetryTimer = 0 }
function queueStaticRetry() {
  if (!active.value || staticRetryTimer) return
  const thumbnailDelay = thumbnailFailed.value ? staticMediaRetryDelay(thumbnailFailures, thumbnailFailedAt, Date.now()) : null
  const previewDelay = previewFailed.value && !props.animation ? staticMediaRetryDelay(previewFailures, previewFailedAt, Date.now()) : null
  if (thumbnailDelay === null && previewDelay === null) return
  staticRetryTimer = window.setTimeout(() => {
    staticRetryTimer = 0
    if (!active.value) return
    if (thumbnailFailed.value && staticMediaRetryDelay(thumbnailFailures, thumbnailFailedAt, Date.now()) === 0) thumbnailFailed.value = false
    if (previewFailed.value && !props.animation && staticMediaRetryDelay(previewFailures, previewFailedAt, Date.now()) === 0) previewFailed.value = false
    queueStaticRetry()
  }, Math.min(thumbnailDelay ?? Infinity, previewDelay ?? Infinity))
}
function retryUpdatedPreview() {
  if (!props.animation || !pendingMediaUpdate || !previewFailed.value || !active.value || retryTimer) return
  const delay = mediaRetryDelay(previewFailedAt, Date.now())
  if (delay) {
    retryTimer = window.setTimeout(() => { retryTimer = 0; retryUpdatedPreview() }, delay)
  } else { clearPreviewRetry(); previewFailed.value = false }
}
watch(() => [props.thumbnail, props.preview], () => {
  clearPreviewRetry()
  clearStaticRetry(); thumbnailFailures = 0; previewFailures = 0
  thumbnailFailed.value = false
  previewFailed.value = false
})
watch(() => props.previewStatus, (status, previous) => {
  if (status === 'ready' && previous !== 'ready') { clearPreviewRetry(); previewFailures = 0; previewFailed.value = false }
})
watch(() => props.mediaRevision, (revision, previous) => {
  if (props.animation && newerMediaRevision(revision, previous) && previewFailed.value) {
    pendingMediaUpdate = true
    retryUpdatedPreview()
  }
})
watch(() => props.recovery?.status, (status, previous) => {
  if (status === 'ready' && previous && previous !== 'ready') {
    clearPreviewRetry()
    clearStaticRetry(); thumbnailFailures = 0; previewFailures = 0
    if (thumbnailFailed.value) thumbnailFailed.value = false
    if (previewFailed.value) previewFailed.value = false
  }
})
const source = computed(() => galleryMediaSource(props, active.value || !!(foreground.value && props.animation && retainedPreview.value && retainedPreview.value === props.preview), thumbnailFailed.value, previewFailed.value))
const animationMasked = computed(() => source.value?.kind === 'animation' && !active.value)
let observation: ReturnType<typeof mediaObservation>, staticObservation: ReturnType<typeof mediaObservation>
watch(() => source.value ? source.value.kind + source.value.url : null, () => {
  observation?.cancel(foreground.value ? 'navigation' : 'hidden')
  observation = mediaObservation(source.value?.url)
}, {immediate: true})
watch(() => animationMasked.value && !thumbnailFailed.value ? props.thumbnail : null, src => {
  staticObservation?.cancel(foreground.value ? 'navigation' : 'hidden')
  staticObservation = mediaObservation(src)
}, {immediate: true})
const placeholder = computed(() => {
  if (thumbnailFailed.value || previewFailed.value) return {label: '本地图片暂时无法显示', reason: '可打开作品重试图片'}
  if (props.previewStatus === 'ready') return {label: '缩略图待生成', reason: null}
  return mediaPlaceholder(props, props.previewStatus, props.animation)
})
function failed(event: Event) {
  if (event.currentTarget !== image.value && event.currentTarget !== staticImage.value) return
  const observed = event.currentTarget === image.value ? observation : staticObservation
  observed?.error()
  if ((event.currentTarget as HTMLImageElement).dataset.mediaSource === 'thumbnail') {
    thumbnailFailures++; thumbnailFailedAt = Date.now(); thumbnailFailed.value = true
  } else if (active.value || source.value?.kind === 'animation') { clearPreviewRetry(); previewFailures++; previewFailedAt = Date.now(); previewFailed.value = true }
  queueStaticRetry()
}
function loaded(event: Event) {
  if (event.currentTarget !== image.value && event.currentTarget !== staticImage.value) return
  const observed = event.currentTarget === image.value ? observation : staticObservation
  observed?.loaded(event.currentTarget as HTMLImageElement); observed?.decoded('unsupported')
  if ((event.currentTarget as HTMLImageElement).dataset.mediaSource === 'thumbnail') thumbnailFailures = 0
  else previewFailures = 0
}
function visibilityChanged() { foreground.value = document.visibilityState === 'visible' }
watch(foreground, value => {
  if (!value && image.value?.dataset.mediaSource === 'animation') image.value.removeAttribute('src')
})
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  const root = host.value?.closest('.gallery-viewport')
  if (!host.value || !root) return
  observer = new IntersectionObserver(entries => {
    const entry = entries[0]
    visible.value = !!entry?.isIntersecting && entry.intersectionRect.width > 0 && entry.intersectionRect.height > 0
  },
    {root, rootMargin: '0px', threshold: 0})
  observer.observe(host.value)
})
watch(active, value => {
  if (!value && image.value && image.value.dataset.mediaSource === 'original') image.value.removeAttribute('src')
  if (!value) { clearTimeout(retryTimer); retryTimer = 0; clearStaticRetry() }
  else { retryUpdatedPreview(); queueStaticRetry() }
})
onBeforeUnmount(() => { observation?.cancel('unmounted'); staticObservation?.cancel('unmounted'); observer?.disconnect(); clearPreviewRetry(); clearStaticRetry(); document.removeEventListener('visibilitychange', visibilityChanged); image.value?.removeAttribute('src') })
</script>

<template>
  <div ref="host" class="media-image media-thumbnail" :class="{'animation-masked': animationMasked}">
    <img v-if="source" :key="source.kind + source.url" ref="image" :src="source.url" :alt="label"
      :data-media-source="source.kind" draggable="false" decoding="async" loading="lazy" @error="failed" @load="loaded">
    <img v-if="animationMasked && thumbnail && !thumbnailFailed" ref="staticImage" class="animation-static"
      :src="thumbnail" :alt="label" data-media-source="thumbnail" draggable="false" decoding="async" loading="lazy" @error="failed" @load="loaded">
    <div v-if="!source || (animationMasked && (!thumbnail || thumbnailFailed))" class="media-placeholder"><span>{{ placeholder.label }}</span><span v-if="placeholder.reason" class="placeholder-reason">{{ placeholder.reason }}</span></div>
  </div>
</template>

<style scoped>
.animation-masked{position:relative}
.animation-masked>img[data-media-source="animation"]{visibility:hidden}
.animation-static{position:absolute;inset:0}
</style>
