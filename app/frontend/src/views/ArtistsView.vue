<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, url, write } from '../api'
import type { Artist } from '../types'
import Icon from '../components/Icon.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import MediaImage from '../MediaImage.vue'
import RestrictionBadge from '../components/RestrictionBadge.vue'
const props = defineProps<{artistId?: number | null}>()
const emit = defineEmits<{works: [id: number]; work: [artistId: number, workId: number]; message: [text: string]}>()
const artists = ref<Artist[]>([]), total = ref(0), offset = ref(0), query = ref(''), draft = ref('')
const layout = ref<'grid' | 'row'>('grid'), loading = ref(false), error = ref(''), busy = ref(false)
const adding = ref(false), newPixivId = ref('')
const deleteTarget = ref<Artist | null>(null)
const drag = {id: -1, x: 0, left: 0, moved: false, target: null as HTMLElement | null}
let timer = 0, generation = 0, suppressClickUntil = 0
const LIMIT = 50
async function load(reset = false) {
  if (reset) offset.value = 0
  const epoch = ++generation
  loading.value = true; error.value = ''
  try {
    if (props.artistId) {
      const artist = await api<Artist>(`/api/artists/${props.artistId}`)
      if (epoch === generation) { artists.value = [artist]; total.value = 1 }
    } else {
      const data = await api<{items: Artist[]; total: number}>(url('/api/artists', '', {offset: offset.value, limit: LIMIT, q: query.value}))
      if (epoch === generation) { artists.value = data.items; total.value = data.total }
    }
  } catch (failure) { if (epoch === generation) error.value = failure instanceof Error ? failure.message : '画师列表暂时无法读取' }
  finally { if (epoch === generation) loading.value = false }
}
function search() { clearTimeout(timer); timer = window.setTimeout(() => { query.value = draft.value; void load(true) }, 280) }
async function addArtist() {
  const pixivId = newPixivId.value.trim()
  if (!/^\d+$/.test(pixivId)) { emit('message', '请输入画师的数字 Pixiv 用户 ID'); return }
  busy.value = true
  try { await write('/api/artists', {pixiv_id: pixivId}); newPixivId.value = ''; adding.value = false; emit('message', '画师已加入，正在更新列表'); await load(true) }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '未能添加画师') }
  finally { busy.value = false }
}
async function track(artist: Artist, mode: Artist['mode']) {
  busy.value = true
  try { await write(`/api/artists/${artist.id}/tracking`, {mode}, 'PATCH'); artist.mode = mode; emit('message', mode === 'active' ? '已恢复追踪' : '已暂停追踪') }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '画师状态未能更改') }
  finally { busy.value = false }
}
async function deleteArtist() {
  const artist = deleteTarget.value
  if (!artist) return
  deleteTarget.value = null; busy.value = true
  try { await api(`/api/artists/${artist.id}?delete_files=true`, {method: 'DELETE'}); emit('message', `${artist.name} 的删除任务已提交，可在任务记录查看`); await load(true) }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '画师存档未能删除') }
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
onMounted(() => { try { layout.value = localStorage.getItem('pixiv-local-artist-layout') === 'row' ? 'row' : 'grid' } catch { /* optional */ }; void load() })
onBeforeUnmount(() => { generation++; clearTimeout(timer) })
function setLayout(next: 'grid' | 'row') { layout.value = next; try { localStorage.setItem('pixiv-local-artist-layout', next) } catch { /* optional */ } }
</script>
<template>
  <section class="subpage artists-page">
    <div class="artists-heading"><div><h1>我的画师</h1><p>管理关注的画师，按最近保存的作品继续阅读。</p></div><button class="primary" :disabled="busy" :aria-expanded="adding" @click="adding = !adding"><Icon name="plus" />添加画师</button></div>
    <form v-if="adding" class="artist-add" @submit.prevent="addArtist"><label>Pixiv 用户 ID <input v-model="newPixivId" inputmode="numeric" pattern="[0-9]+" autocomplete="off" autofocus placeholder="输入数字 ID"></label><button class="primary" type="submit" :disabled="busy">添加</button><button type="button" @click="adding = false">取消</button></form>
    <div class="artists-toolbar"><label class="search-field"><Icon name="search" /><input v-model="draft" type="search" placeholder="搜索画师" aria-label="搜索画师" @input="search" @compositionend="search"></label><span class="muted">{{ total }} 位画师</span><div class="artist-layout-switch" aria-label="画师排列"><button :class="{active: layout === 'grid'}" :aria-pressed="layout === 'grid'" @click="setLayout('grid')"><Icon name="grid" />网格</button><button :class="{active: layout === 'row'}" :aria-pressed="layout === 'row'" @click="setLayout('row')"><Icon name="list" />列表</button></div></div>
    <p v-if="error" class="notice error" role="alert">{{ error }} <button @click="load()">重试</button></p>
    <div v-if="!loading && !artists.length && !error" class="empty"><h2>没有找到画师</h2><p>试试别的名称，或添加一位画师。</p></div>
    <div class="artists-list" :class="layout"><article v-for="artist in artists" :key="artist.id" class="artist-profile">
      <div class="artist-identity"><div class="artist-profile-head"><img v-if="artist.avatar" class="artist-avatar" :src="artist.avatar" :alt="`${artist.name}头像`"><div v-else class="artist-avatar avatar-empty" aria-hidden="true"><Icon name="artists" /></div><div><h2>{{ artist.name }}</h2><small>{{ artist.work_count ?? 0 }} 件作品 · {{ artist.mode === 'active' ? '追踪中' : artist.mode === 'paused' ? '已暂停' : '已停止' }}</small></div></div><p class="artist-bio">{{ artist.biography || '暂无画师简介' }}</p><div class="artist-controls"><button class="text-link" @click="emit('works', artist.id)">查看全部作品 <Icon name="right" /></button><button class="quiet" :disabled="busy" @click="track(artist, artist.mode === 'active' ? 'paused' : 'active')">{{ artist.mode === 'active' ? '暂停追踪' : '恢复追踪' }}</button><button v-if="artist.mode !== 'stopped'" class="quiet" :disabled="busy" @click="track(artist, 'stopped')">停止追踪，保留存档</button><button class="danger-link" :disabled="busy" @click="deleteTarget = artist">删除存档</button></div></div>
      <div class="artist-workarea"><div class="artist-workbar"><span>最近保存</span><div class="strip-nav"><button :aria-label="`向左看 ${artist.name} 的作品`" @click="($event.currentTarget as HTMLElement).closest('.artist-workarea')?.querySelector('.artist-recent-strip')?.scrollBy({left: -250, behavior: 'smooth'})"><Icon name="left" /></button><button :aria-label="`向右看 ${artist.name} 的作品`" @click="($event.currentTarget as HTMLElement).closest('.artist-workarea')?.querySelector('.artist-recent-strip')?.scrollBy({left: 250, behavior: 'smooth'})"><Icon name="right" /></button></div></div><div class="artist-recent-strip" @pointerdown="stripDown" @pointermove="stripMove" @pointerup="stripUp" @pointercancel="stripUp"><button v-for="work in (artist.recent_works || []).slice(0, layout === 'grid' ? 3 : 12)" :key="work.id" class="artist-work" @click="openWork(artist.id, work.id)"><div class="artist-work-image"><MediaImage :src="work.thumbnail" :label="work.title" thumbnail /><RestrictionBadge :value="work.x_restrict" /></div><span>{{ work.title }}</span></button><p v-if="!artist.recent_works?.length" class="muted">还没有本地作品</p></div></div>
    </article></div>
    <nav v-if="!artistId && total > LIMIT" class="pagination" aria-label="画师页码"><button :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - LIMIT); load()">上一页</button><span>第 {{ Math.floor(offset / LIMIT) + 1 }} / {{ Math.ceil(total / LIMIT) }} 页</span><button :disabled="offset + LIMIT >= total || loading" @click="offset += LIMIT; load()">下一页</button></nav>
    <ConfirmDialog v-if="deleteTarget" title="删除画师存档" :description="`将永久删除 ${deleteTarget.name} 的本地作品和文件。这项操作无法撤销。`" action="永久删除" danger @cancel="deleteTarget = null" @confirm="deleteArtist" />
  </section>
</template>
