<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { bookmarkBusy, bookmarkState, ingestBookmark, refreshBookmark, toggleBookmark } from '../state/bookmarks'
import type { BookmarkState } from '../types'
import Icon from './Icon.vue'
const props = defineProps<{workId: number; initial?: BookmarkState; compact?: boolean; refreshOnMount?: boolean}>()
const emit = defineEmits<{message: [text: string]}>()
const state = computed(() => bookmarkState(props.workId))
const known = computed(() => typeof state.value?.value === 'boolean')
const lastValue = computed(() => state.value?.value === true)
const on = computed(() => lastValue.value && state.value?.status === 'confirmed' && !state.value.cached)
const pending = computed(() => bookmarkBusy(props.workId))
const problem = computed(() => state.value?.status === 'failed' || state.value?.status === 'unconfirmed')
const confirmedAt = computed(() => state.value?.confirmed_at ? new Date(state.value.confirmed_at).toLocaleString('zh-CN') : '')
const title = computed(() => {
  const current = state.value
  const action = lastValue.value ? '取消 Pixiv 收藏' : '收藏到 Pixiv'
  const status = pending.value ? current?.error || '正在核对 Pixiv 收藏状态' : problem.value ? current?.error || '收藏状态未确认' : current?.cached ? `上次确认${lastValue.value ? '已收藏' : '未收藏'}，当前状态待核对` : !known.value ? '当前收藏状态未知' : ''
  return [status, confirmedAt.value ? `最近核实：${confirmedAt.value}` : '', action].filter(Boolean).join('。')
})
onMounted(() => { if (props.initial) ingestBookmark(props.workId, props.initial); if (props.refreshOnMount) refreshBookmark(props.workId) })
async function click() {
  try {
    await toggleBookmark(props.workId)
  } catch (failure) { emit('message', failure instanceof Error ? failure.message : '收藏状态未能确认') }
}
</script>
<template><button type="button" class="bookmark-button" :class="{on, pending, problem, cached: state?.cached, unknown: !known}" :disabled="pending" :title="title" :aria-label="title" :aria-pressed="known && !state?.cached ? on : undefined" @click.stop="click"><Icon name="heart" /><span v-if="!compact">{{ pending ? '核对中' : problem ? state?.status === 'failed' ? '操作失败' : '待确认' : state?.cached ? lastValue ? '上次已收藏' : '上次未收藏' : !known ? '收藏（状态未知）' : on ? '已收藏' : '收藏' }}</span><small v-if="!compact && confirmedAt" class="bookmark-time">最近核实 {{ confirmedAt }}</small><span v-if="problem" class="bookmark-alert" aria-hidden="true">!</span><span v-else-if="state?.cached" class="cached-dot" aria-label="上次确认的状态" /><span v-else-if="compact && !known && !pending" class="bookmark-unknown" aria-hidden="true">?</span></button></template>
