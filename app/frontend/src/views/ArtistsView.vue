<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, url, write } from '../api'
import { sourceInaccessible, sourceStatusLabel, type Artist } from '../types'
import { LatestIntent } from '../interaction'
import Icon from '../components/Icon.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import MediaImage from '../MediaImage.vue'
import RestrictionBadge from '../components/RestrictionBadge.vue'
const props = defineProps<{artistId?: number | null}>()
const emit = defineEmits<{works: [id: number]; work: [artistId: number, workId: number]; message: [text: string, isError?: boolean]; taskQueued: [id: string]}>()
const artists = ref<Artist[]>([]), total = ref(0), offset = ref(0), query = ref(''), draft = ref('')
const layout = ref<'grid' | 'row'>('grid'), loading = ref(false), error = ref(''), busy = ref(false)
interface Identity {user_id: string; name: string; biography?: string | null; avatar_url?: string | null; artist_id?: number | null}
interface CheckStatus {checked_at: string | null; operation: {id: string; state: string; result: {outcome?: string}; finished_at: string | null} | null}
const adding = ref(false), newPixivId = ref(''), previews = ref<Identity[]>([]), selectedIdentity = ref<Identity | null>(null), previewError = ref(''), previewLoading = ref(false)
const previewRequest = new LatestIntent()
const checkStatuses = ref(new Map<number, CheckStatus>())
const artistList = ref<HTMLElement | null>(null), previewWidth = ref(130)
const previewStyle = computed(() => ({'--artist-preview-width': `${previewWidth.value}px`, '--artist-preview-height': `${previewWidth.value * 2 / 3}px`}))
let resize: ResizeObserver | null = null
function sizePreviews() {
  const width = artistList.value?.clientWidth
  if (!width) return
  // Derive both layouts from the same hypothetical grid, including card
  // borders, padding and two gaps. Three full cards fit without shrinking art
  // further when changing to the list layout.
  const columns = window.matchMedia('(max-width:680px)').matches ? 1 : 2
  previewWidth.value = Math.max(36, ((width - (columns - 1) * 18) / columns - 42 - 20) / 3)
}
const deleteTarget = ref<Artist | null>(null)
const checks = ref(new Map<number, string>())
const request = new LatestIntent()
const drag = {id: -1, x: 0, left: 0, moved: false, target: null as HTMLElement | null}
let timer = 0, generation = 0, suppressClickUntil = 0
const LIMIT = 50
async function load(reset = false) {
  if (reset) offset.value = 0
  const epoch = ++generation, ticket = request.begin()
  loading.value = true; error.value = ''
  try {
    if (props.artistId) {
      const artist = await api<Artist>(`/api/artists/${props.artistId}`, {signal: ticket.signal})
      if (epoch === generation) { artists.value = [artist]; total.value = 1 }
    } else {
      const data = await api<{items: Artist[]; total: number}>(url('/api/artists', '', {offset: offset.value, limit: LIMIT, q: query.value}), {signal: ticket.signal})
      if (epoch === generation) { artists.value = data.items; total.value = data.total }
    }
  } catch (failure) { if (epoch === generation && ticket.current()) error.value = failure instanceof Error ? failure.message : '画师列表暂时无法读取' }
  finally { if (epoch === generation) { loading.value = false; void nextTick(sizePreviews); void loadCheckStatuses(epoch) } }
}
async function checkSource(artist: Artist) {
  if (checks.value.has(artist.id)) return
  checks.value.set(artist.id, '')
  try {
    const data = await api<{operation?: {id: string}; id?: string; operation_id?: string}>(`/api/artists/${artist.id}/check-source`, {method: 'POST'})
    const operationId = data.operation?.id || data.operation_id || data.id
    if (!operationId) throw new Error('未能确认核对任务，请重试')
    checks.value.set(artist.id, operationId)
    emit('taskQueued', operationId)
    emit('message', `已排队核对 ${artist.name} 的主站主页，进度见任务提示`)
  } catch (failure) {
    checks.value.delete(artist.id)
    emit('message', failure instanceof Error ? failure.message : '主页核对未能提交', true)
  }
}
function refreshArtists() { if (document.visibilityState === 'visible') void load() }
function taskCompleted(event: Event) {
  const id = (event as CustomEvent<{id: string}>).detail?.id
  for (const [artistId, operationId] of checks.value) if (operationId === id) checks.value.delete(artistId)
  refreshArtists()
}
function visibilityChanged() {
  if (document.visibilityState === 'visible') refreshArtists()
  else { request.cancel(); generation++; loading.value = false }
}
function search() { clearTimeout(timer); timer = window.setTimeout(() => { query.value = draft.value; void load(true) }, 280) }
async function lookupIdentity() {
  const query = newPixivId.value.trim()
  if (!query) return
  const ticket = previewRequest.begin(); previewLoading.value = true; previewError.value = ''; selectedIdentity.value = null; previews.value = []
  try {
    const result = await api<{items: Identity[]; identity: boolean}>(url('/api/artists/preview', '', {query}), {signal: ticket.signal})
    if (!ticket.current()) return
    previews.value = result.items
    if (result.identity) selectedIdentity.value = result.items[0] || null
    if (!result.items.length) previewError.value = '没有找到画师，请换个名称或使用用户 ID / 主页地址'
  } catch (failure) { if (ticket.current()) previewError.value = failure instanceof Error ? failure.message : '身份查询未完成，请检查连接后重试' }
  finally { if (ticket.current()) previewLoading.value = false }
}
async function chooseIdentity(identity: Identity) {
  const ticket = previewRequest.begin(); previewLoading.value = true; previewError.value = ''
  try { const result = await api<Identity>(`/api/artists/preview/${identity.user_id}`, {signal: ticket.signal}); if (ticket.current()) selectedIdentity.value = result }
  catch (failure) { if (ticket.current()) previewError.value = failure instanceof Error ? failure.message : '无法确认画师身份，请重试' }
  finally { if (ticket.current()) previewLoading.value = false }
}
function identityInput() { previewRequest.cancel(); selectedIdentity.value = null; previews.value = []; previewLoading.value = false; previewError.value = '' }
async function addArtist() {
  const identity = selectedIdentity.value
  if (!identity || previewLoading.value) return
  if (identity.artist_id) { adding.value = false; emit('works', identity.artist_id); emit('message', '这位画师已在本地，已打开现有存档'); return }
  busy.value = true
  try { const result = await write<{operation_id?: string}>('/api/artists', {pixiv_id: identity.user_id}); if (result.operation_id) emit('taskQueued', result.operation_id); newPixivId.value = ''; identityInput(); adding.value = false; emit('message', '画师已加入，正在检查新作'); await load(true) }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '未能添加画师', true) }
  finally { busy.value = false }
}
async function checkNewWorks(artist: Artist) {
  if (checkStatuses.value.get(artist.id)?.operation && ['queued', 'planning', 'ready', 'running', 'paused', 'interrupted'].includes(checkStatuses.value.get(artist.id)!.operation!.state)) return
  try { const result = await write<{operation_id: string}>('/api/tasks', {kind:'check', artist_ids:[artist.id]}); emit('taskQueued', result.operation_id); emit('message', `已排队检查 ${artist.name} 的新作品`); await loadCheckStatuses(generation) }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '检查未能提交', true) }
}
async function loadCheckStatuses(epoch: number) {
  if (!artists.value.length) return
  try {
    const data = await api<{items: Record<string, CheckStatus>}>(url('/api/artists/check-status', '', {artist_ids: artists.value.map(artist => artist.id).join(',')}))
    if (epoch === generation) for (const [id, status] of Object.entries(data.items)) checkStatuses.value.set(Number(id), status)
  } catch { /* A failed local status read must not invent a successful check. */ }
}
function checkLabel(artist: Artist) {
  const status = checkStatuses.value.get(artist.id), operation = status?.operation
  if (operation && ['queued', 'planning', 'ready', 'running', 'paused', 'interrupted'].includes(operation.state)) return operation.state === 'running' ? '正在检查新作' : operation.state === 'paused' ? '检查已暂停' : '已排队检查'
  const checked = status?.checked_at || (artist.last_checked_at ? artist.last_checked_at + 'Z' : null)
  const latestAttempt = operation?.finished_at && (!checked || new Date(operation.finished_at) >= new Date(checked))
  const date = latestAttempt ? operation!.finished_at : checked
  const time = date ? new Date(date).toLocaleString('zh-CN', {hour12:false}) : '尚未完成检查'
  return `${time}${latestAttempt && operation?.result.outcome === 'failed' ? ' · 上次未完成，请查看日志' : latestAttempt && operation?.result.outcome === 'partial_failure' ? ' · 上次部分未完成，请查看日志' : checked ? ' · 上次检查完成' : ''}`
}
async function track(artist: Artist, mode: Artist['mode']) {
  busy.value = true
  try { await write(`/api/artists/${artist.id}/tracking`, {mode}, 'PATCH'); artist.mode = mode; emit('message', mode === 'active' ? '已恢复追踪' : '已暂停追踪') }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '画师状态未能更改', true) }
  finally { busy.value = false }
}
async function deleteArtist() {
  const artist = deleteTarget.value
  if (!artist) return
  deleteTarget.value = null; busy.value = true
  try { await api(`/api/artists/${artist.id}?delete_files=true`, {method: 'DELETE'}); emit('message', `${artist.name} 的删除任务已提交，结果可在设置的处理日志查看`); await load(true) }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '画师存档未能删除', true) }
  finally { busy.value = false }
}
function stripDown(event: PointerEvent) {
  if (event.pointerType === 'touch' || event.button !== 0) return
  const target = event.currentTarget as HTMLElement
  drag.id = event.pointerId; drag.x = event.clientX; drag.left = target.scrollLeft; drag.moved = false; drag.target = target
}
function stripMove(event: PointerEvent) {
  if (event.pointerId !== drag.id || !drag.target) return
  const dx = event.clientX - drag.x
  if (Math.abs(dx) > 6) { drag.moved = true; drag.target.classList.add('dragging') }
  if (drag.moved) { drag.target.scrollLeft = drag.left - dx; event.preventDefault() }
}
function stripUp(event: PointerEvent) {
  if (event.pointerId !== drag.id) return
  if (drag.moved) suppressClickUntil = Date.now() + 400
  drag.target?.classList.remove('dragging'); drag.id = -1; drag.target = null
}
function openWork(artistId: number, workId: number) { if (Date.now() >= suppressClickUntil) emit('work', artistId, workId) }
watch(() => props.artistId, () => { void load(true) })
onMounted(() => { resize = new ResizeObserver(sizePreviews); if (artistList.value) resize.observe(artistList.value); try { layout.value = localStorage.getItem('pixiv-local-artist-layout') === 'row' ? 'row' : 'grid' } catch { /* optional */ }; void load(); window.addEventListener('archive-updated', refreshArtists); window.addEventListener('task-completed', taskCompleted); document.addEventListener('visibilitychange', visibilityChanged) })
onBeforeUnmount(() => { resize?.disconnect(); previewRequest.cancel(); generation++; request.cancel(); clearTimeout(timer); window.removeEventListener('archive-updated', refreshArtists); window.removeEventListener('task-completed', taskCompleted); document.removeEventListener('visibilitychange', visibilityChanged) })
function setLayout(next: 'grid' | 'row') { layout.value = next; try { localStorage.setItem('pixiv-local-artist-layout', next) } catch { /* optional */ } }
</script>
<template>
  <section class="subpage artists-page">
    <div class="artists-heading"><div><h1>我的画师</h1><p>管理关注的画师，按最近保存的作品继续阅读。</p></div><button class="primary" :disabled="busy" :aria-expanded="adding" @click="adding = !adding"><Icon name="plus" />添加画师</button></div>
    <div v-if="adding" class="artist-add-panel"><form class="identity-query" @submit.prevent="lookupIdentity"><label>名称、Pixiv 用户 ID 或主页地址<input v-model="newPixivId" autocomplete="off" autofocus placeholder="名称 / 123 / https://www.pixiv.net/users/123" @input="identityInput"></label><button class="primary" type="submit" :disabled="previewLoading || !newPixivId.trim()">{{ previewLoading ? '正在查询…' : '查询身份' }}</button><button type="button" @click="adding = false; identityInput()">取消</button></form><p v-if="previewError" class="error" role="alert">{{ previewError }}</p><div v-if="!selectedIdentity" class="identity-results"><button v-for="identity in previews" :key="identity.user_id" class="identity-choice" :disabled="previewLoading" @click="chooseIdentity(identity)"><img v-if="identity.avatar_url" :src="identity.avatar_url" alt="" referrerpolicy="no-referrer"><span>{{ identity.name }}<small>ID {{ identity.user_id }}{{ identity.artist_id ? ' · 已添加' : '' }}</small></span></button></div><article v-if="selectedIdentity" class="identity-confirm"><img v-if="selectedIdentity.avatar_url" :src="selectedIdentity.avatar_url" :alt="`${selectedIdentity.name}头像`" referrerpolicy="no-referrer"><div><h2>{{ selectedIdentity.name }}</h2><p>Pixiv ID {{ selectedIdentity.user_id }}</p><p class="identity-biography">{{ selectedIdentity.biography || '暂无画师简介' }}</p><a :href="`https://www.pixiv.net/users/${selectedIdentity.user_id}`" target="_blank" rel="noopener noreferrer">打开 Pixiv 主页</a><button class="primary" :disabled="busy || previewLoading" @click="addArtist">{{ selectedIdentity.artist_id ? '查看现有画师' : '确认身份并添加' }}</button></div></article></div>
    <div class="artists-toolbar"><label class="search-field"><Icon name="search" /><input v-model="draft" type="search" placeholder="搜索画师" aria-label="搜索画师" @input="search" @compositionend="search"></label><span class="muted">{{ total }} 位画师</span><div class="artist-layout-switch" aria-label="画师排列"><button :class="{active: layout === 'grid'}" :aria-pressed="layout === 'grid'" @click="setLayout('grid')"><Icon name="grid" />网格</button><button :class="{active: layout === 'row'}" :aria-pressed="layout === 'row'" @click="setLayout('row')"><Icon name="list" />列表</button></div></div>
    <p v-if="error" class="notice error" role="alert">{{ error }} <button @click="load()">重试</button></p>
    <div v-if="!loading && !artists.length && !error" class="empty"><h2>没有找到画师</h2><p>试试别的名称，或添加一位画师。</p></div>
    <div ref="artistList" class="artists-list" :class="layout" :style="previewStyle"><article v-for="artist in artists" :key="artist.id" class="artist-profile">
      <div class="artist-identity"><div class="artist-profile-head"><img v-if="artist.avatar" class="artist-avatar" :class="{'source-deleted': sourceInaccessible(artist.availability)}" :src="artist.avatar" :alt="`${artist.name}头像`"><div v-else class="artist-avatar avatar-empty" :class="{'source-deleted': sourceInaccessible(artist.availability)}" aria-hidden="true"><Icon name="artists" /></div><div><h2>{{ artist.name }}</h2><small>{{ artist.work_count ?? 0 }} 件作品 · {{ artist.mode === 'active' ? '追踪中' : artist.mode === 'paused' ? '已暂停' : '已停止' }}</small><a class="artist-pixiv-link" :href="`https://www.pixiv.net/users/${encodeURIComponent(artist.pixiv_id)}`" target="_blank" rel="noopener noreferrer" :aria-label="`在 Pixiv 查看 ${artist.name}`">Pixiv 主页 <Icon name="external" /></a></div></div><p v-if="sourceStatusLabel(artist.availability, 'artist')" class="artist-source-status">{{ sourceStatusLabel(artist.availability, 'artist') }}<span v-if="artist.availability?.reason"> · {{ artist.availability.reason }}</span></p><p class="artist-last-check">{{ checkLabel(artist) }}</p><p class="artist-bio">{{ artist.biography || '暂无画师简介' }}</p><div class="artist-controls"><button class="text-link" @click="emit('works', artist.id)">查看全部作品 <Icon name="right" /></button><button class="quiet" :disabled="busy" @click="track(artist, artist.mode === 'active' ? 'paused' : 'active')">{{ artist.mode === 'active' ? '暂停追踪' : '恢复追踪' }}</button><button v-if="artist.mode !== 'stopped'" class="quiet" :disabled="busy" @click="track(artist, 'stopped')">停止追踪，保留存档</button><button class="quiet artist-check-new" :disabled="busy || !!checkStatuses.get(artist.id)?.operation && ['queued', 'planning', 'ready', 'running', 'paused', 'interrupted'].includes(checkStatuses.get(artist.id)!.operation!.state)" @click="checkNewWorks(artist)">检查这位画师新作</button><button class="quiet artist-check-source" :disabled="checks.has(artist.id)" @click="checkSource(artist)">{{ checks.has(artist.id) ? '已排队核对' : '核对主站主页' }}</button><button class="danger-link" :disabled="busy" @click="deleteTarget = artist">删除存档</button></div></div>
      <div class="artist-workarea"><div class="artist-workbar"><span>最近保存</span><div class="strip-nav"><button :aria-label="`向左看 ${artist.name} 的作品`" @click="($event.currentTarget as HTMLElement).closest('.artist-workarea')?.querySelector('.artist-recent-strip')?.scrollBy({left: -250, behavior: 'smooth'})"><Icon name="left" /></button><button :aria-label="`向右看 ${artist.name} 的作品`" @click="($event.currentTarget as HTMLElement).closest('.artist-workarea')?.querySelector('.artist-recent-strip')?.scrollBy({left: 250, behavior: 'smooth'})"><Icon name="right" /></button></div></div><div class="artist-recent-strip" @pointerdown="stripDown" @pointermove="stripMove" @pointerup="stripUp" @pointercancel="stripUp"><button v-for="work in (artist.recent_works || []).slice(0, 12)" :key="work.id" class="artist-work" @click="openWork(artist.id, work.id)"><div class="artist-work-image"><MediaImage :src="work.thumbnail" :label="work.title" thumbnail /><RestrictionBadge :value="work.x_restrict" /></div><span>{{ work.title }}</span></button><p v-if="!artist.recent_works?.length" class="muted">还没有本地作品</p></div></div>
    </article></div>
    <nav v-if="!artistId && total > LIMIT" class="pagination" aria-label="画师页码"><button :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - LIMIT); load()">上一页</button><span>第 {{ Math.floor(offset / LIMIT) + 1 }} / {{ Math.ceil(total / LIMIT) }} 页</span><button :disabled="offset + LIMIT >= total || loading" @click="offset += LIMIT; load()">下一页</button></nav>
    <ConfirmDialog v-if="deleteTarget" title="删除画师存档" :description="`将永久删除 ${deleteTarget.name} 的本地作品和文件。这项操作无法撤销。`" action="永久删除" danger @cancel="deleteTarget = null" @confirm="deleteArtist" />
  </section>
</template>

<style scoped>
.artist-add-panel{padding:16px;border:1px solid var(--line);background:var(--surface);border-radius:10px;margin-bottom:18px}.identity-query{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.identity-query label{flex:1;min-width:180px;font-size:12px;display:grid;gap:7px;color:var(--muted)}.identity-query input{width:100%}.identity-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:12px}.identity-choice{justify-content:start;white-space:normal;text-align:left}.identity-choice img,.identity-confirm>img{width:48px;height:48px;border-radius:50%;object-fit:cover;flex:none}.identity-choice small{display:block;color:var(--muted);font-size:11px}.identity-confirm{display:flex;gap:15px;margin-top:18px}.identity-confirm h2{font-size:17px}.identity-confirm p{font-size:12px;color:var(--muted)}.identity-confirm button{margin-top:12px;display:flex}.identity-biography{white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}.artist-last-check{font-size:11px;color:var(--muted);line-height:1.6;margin-top:9px}.artist-work{width:var(--artist-preview-width)}.artist-work-image{height:var(--artist-preview-height)}
.artists-list{container-type:inline-size}
.artists-list.row .artist-profile{grid-template-columns:250px minmax(0,1fr)}
@container(max-width:780px){.artists-list.row .artist-profile{display:block}.artists-list.row .artist-identity{border:0;padding:0}.artists-list.row .artist-workarea{margin-top:14px}}
@media(max-width:680px){.identity-confirm>div{min-width:0}.artist-recent-strip{gap:10px}.artist-profile{padding:20px}}
</style>
