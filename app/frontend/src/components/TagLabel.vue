<script setup lang="ts">
import { computed } from 'vue'
import type { TagDetail } from '../types'

const props = defineProps<{name: string; details?: TagDetail[]}>()
const detail = computed(() => props.details?.find(tag => tag.name === props.name))
const translation = computed(() => {
  const tag = detail.value
  return tag?.source === 'pixiv' && tag.language === 'zh-cn' &&
    tag.translated_name?.trim() && tag.translated_name !== props.name ? tag.translated_name : null
})
</script>

<template><span class="tag-label"><span class="tag-original">#{{ name }}</span><span v-if="translation" class="tag-translation" data-source="pixiv" title="Pixiv 中文译名">{{ translation }}</span></span></template>
