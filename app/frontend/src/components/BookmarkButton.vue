<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { bookmarkAcknowledging, bookmarkBusy, bookmarkState, ingestBookmark, refreshBookmark, toggleBookmark } from '../state/bookmarks'
import { showsBookmarkFact } from '../state/bookmarkRules'
import type { BookmarkState } from '../types'
import Icon from './Icon.vue'
const props = defineProps<{workId: number; initial?: BookmarkState; refreshOnMount?: boolean}>()
const emit = defineEmits<{message: [text: string]}>()
const state = computed(() => bookmarkState(props.workId))
const sameAccount = computed(() => !!state.value?.current_account_id &&
  state.value.fact_account_id === state.value.current_account_id)
const known = computed(() => state.value?.verified === true &&
  typeof state.value.value === 'boolean' && (sameAccount.value || !state.value.current_account_id))
const lastValue = computed(() => state.value?.value === true)
const on = computed(() => showsBookmarkFact(state.value))
const pending = computed(() => bookmarkBusy(props.workId))
const acknowledging = computed(() => bookmarkAcknowledging(props.workId))
const problem = computed(() => state.value?.status === 'failed' || state.value?.status === 'unconfirmed')
const confirmedAt = computed(() => state.value?.confirmed_at ? new Date(state.value.confirmed_at).toLocaleString('zh-CN') : '')
const title = computed(() => {
  const current = state.value
  const action = !current?.current_account_id ? '连接 Pixiv 后可操作收藏' :
    sameAccount.value && lastValue.value ? '取消 Pixiv 收藏' : '收藏到 Pixiv'
  const status = pending.value ? current?.error || '正在核对 Pixiv 收藏状态' :
    problem.value ? current?.error || '收藏状态未确认' :
    !current?.current_account_id && known.value ? '上次账号确认的收藏，当前离线' :
    current?.verified && !sameAccount.value ? '当前账号的收藏状态待核对' :
    current?.cached && known.value ? `上次确认${lastValue.value ? '已收藏' : '未收藏'}，当前状态待核对` :
    !known.value ? '当前收藏状态未知' : ''
  return [acknowledging.value ? '已收到点击，红心暂作点击回馈' : '', status,
    confirmedAt.value ? `最近核实：${confirmedAt.value}` : '', action].filter(Boolean).join('。')
})
watch(() => [props.workId, props.initial] as const, ([id, initial]) => {
  if (initial) ingestBookmark(id, initial)
}, {immediate: true})
onMounted(() => { if (props.refreshOnMount) refreshBookmark(props.workId) })
async function click() {
  try {
    await toggleBookmark(props.workId)
  } catch (failure) { emit('message', failure instanceof Error ? failure.message : '收藏状态未能确认') }
}
</script>
<template><button type="button" class="bookmark-button" :class="{on: on || acknowledging, acknowledging, pending: pending && !acknowledging, problem: problem && !acknowledging, cached: state?.cached && !acknowledging, unknown: !known && !acknowledging}" :disabled="pending || acknowledging" :title="title" :aria-label="title" :aria-pressed="sameAccount && known ? on : undefined" :aria-busy="pending" @click.stop="click"><Icon name="heart" /></button></template>
