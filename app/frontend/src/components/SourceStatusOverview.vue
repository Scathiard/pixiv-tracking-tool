<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, url } from '../api'
import { LatestIntent } from '../interaction'
import { discardedSignal } from '../diagnostics'
import { isOlderCatalogue, localSourceStatus, sourceStatusLabel, type SourceStatusBatch } from '../types'
import { observeCatalogueData } from '../state/catalogue'

const props = defineProps<{summary?: boolean}>()
const emit = defineEmits<{open: []}>()
const kind = ref<'works' | 'artists'>('works'), group = ref<'all' | 'deleted' | 'unavailable'>('all')
const snapshot = ref<SourceStatusBatch | null>(null), offset = ref(0), loading = ref(false), error = ref('')
const request = new LatestIntent(), LIMIT = 50
let stopped = false, refreshPending = false, revision: number | undefined
async function load() {
  if (stopped || document.visibilityState !== 'visible') return
  const ticket = request.begin(), requestedKind = kind.value, requestedGroup = group.value, requestedOffset = offset.value
  loading.value = true; error.value = ''
  try {
    const data = await api<SourceStatusBatch>(url('/api/library/source-status', '', {kind: requestedKind, group: requestedGroup, offset: requestedOffset, limit: props.summary ? 1 : LIMIT}), {signal: ticket.signal})
    if (!ticket.current() || isOlderCatalogue(data.catalogue_revision, revision)) { discardedSignal(ticket.signal, 'other'); return }
    revision = data.catalogue_revision ?? revision
    if (!data.items.length && requestedOffset > 0 && data.total <= requestedOffset) {
      offset.value = Math.max(0, Math.floor(Math.max(0, data.total - 1) / LIMIT) * LIMIT)
      void load(); return
    }
    snapshot.value = data
    observeCatalogueData(data.catalogue_revision)
  } catch (failure) {
    if (ticket.current()) {
      error.value = failure instanceof Error ? failure.message : '来源一览暂时无法读取'
    }
  } finally {
    if (ticket.current()) {
      loading.value = false
      if (refreshPending) { refreshPending = false; void load() }
    }
  }
}
function select(nextKind: typeof kind.value, nextGroup: typeof group.value) {
  if (kind.value === nextKind && group.value === nextGroup) return
  kind.value = nextKind; group.value = nextGroup; offset.value = 0
  snapshot.value = null; refreshPending = false; void load()
}
function refresh() { if (loading.value) refreshPending = true; else void load() }
function visibilityChanged() {
  if (document.visibilityState === 'visible') refresh()
  else { request.cancel(); loading.value = false; refreshPending = false }
}
function checkedAt(value: string | null) {
  if (!value) return '尚无核对时间'
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12: false}) : '尚无核对时间'
}
onMounted(() => { void load(); window.addEventListener('archive-updated', refresh); window.addEventListener('task-completed', refresh); document.addEventListener('visibilitychange', visibilityChanged) })
onBeforeUnmount(() => { stopped = true; request.cancel(); window.removeEventListener('archive-updated', refresh); window.removeEventListener('task-completed', refresh); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>
<template>
  <div class="settings-card settings-stack source-overview">
    <div class="setting-top"><div><h3>主站已无法访问</h3><p>已有存档继续保留；清单显示本地是否完整及具体缺项。</p><p v-if="snapshot">{{ snapshot.counts.works.deleted + snapshot.counts.works.unavailable }} 件作品 · {{ snapshot.counts.artists.deleted + snapshot.counts.artists.unavailable }} 位画师</p></div><button v-if="summary" @click="emit('open')">查看清单</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}<template v-if="snapshot">；保留上次读取的清单。</template> <button :disabled="loading" @click="load">重试</button></p>
    <template v-if="!summary">
    <div class="source-overview-tabs" aria-label="来源类型"><button :aria-pressed="kind === 'works'" :class="{active: kind === 'works'}" @click="select('works', group)">作品</button><button :aria-pressed="kind === 'artists'" :class="{active: kind === 'artists'}" @click="select('artists', group)">画师</button></div>


    <div v-if="snapshot?.items.length" class="source-status-list" :aria-busy="loading"><article v-for="item in snapshot.items" :key="`${item.kind}-${item.id}`" class="source-status-item" :data-source-id="item.id">
      <img v-if="item.kind === 'artist' && item.avatar" class="source-avatar" :src="item.avatar" :alt="`${item.name}头像`">
      <div class="source-status-copy"><RouterLink :to="item.kind === 'work' ? `/illust/${item.id}` : `/artist/${item.id}`">{{ item.title || item.name }}</RouterLink><p v-if="item.artist_name" class="muted">{{ item.artist_name }}</p><p class="source-status-fact">{{ sourceStatusLabel(item.availability, item.kind) }}</p><p v-if="item.availability.reason">{{ item.availability.reason }}</p><p class="source-local-status">{{ localSourceStatus(item) }}</p><p v-if="item.kind === 'artist'">{{ item.work_count ?? 0 }} 件本地作品<template v-if="item.incomplete_work_count"> · {{ item.incomplete_work_count }} 件尚未齐全</template></p><small>上次核对 {{ checkedAt(item.availability.checked_at) }}</small></div>
    </article></div>
    <p v-else-if="loading && !snapshot" class="muted">正在读取本地来源记录…</p><p v-else-if="snapshot && !error" class="muted">当前范围没有记录。</p>
    <nav v-if="snapshot && (snapshot.total > LIMIT || snapshot.offset)" class="source-pagination" aria-label="来源一览页码"><button :disabled="snapshot.offset === 0 || loading" @click="offset = Math.max(0, snapshot!.offset - LIMIT); load()">上一页</button><span>第 {{ Math.floor(snapshot.offset / LIMIT) + 1 }} / {{ Math.max(1, Math.ceil(snapshot.total / LIMIT)) }} 页 · {{ snapshot.total }} 项</span><button :disabled="snapshot.offset + LIMIT >= snapshot.total || loading" @click="offset = snapshot!.offset + LIMIT; load()">下一页</button></nav>
    </template>
  </div>
</template>
