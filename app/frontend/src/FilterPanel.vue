<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from './api'
import type { Artist } from './types'
const props = defineProps<{query: string}>()
const emit = defineEmits<{apply: [query: string]; close: []}>()
const current = new URLSearchParams(props.query)
const artists = ref<Artist[]>([]), allArtists = ref(!current.has('artist_ids'))
const selected = ref((current.get('artist_ids') || '').split(',').filter(Boolean)), tags = ref(current.get('tags') || '')
const types = ref(current.has('types') ? (current.get('types') || '').split(',').filter(Boolean) : ['illust', 'manga', 'ugoira'])
const hidden = ref(current.get('show_hidden') === '1'), sort = ref(current.get('sort') || 'newest')
const period = ref(current.get('period') || ''), after = ref(current.get('posted_after') || ''), before = ref(current.get('posted_before') || '')
const error = ref('')
onMounted(async () => { try { artists.value = (await api<{items: Artist[]}>('/api/artists')).items } catch { error.value = '画师列表暂时无法读取，仍可调整其他筛选' } })
function apply() {
  const query = new URLSearchParams()
  if (!allArtists.value) query.set('artist_ids', selected.value.join(','))
  if (types.value.length !== 3) query.set('types', types.value.join(','))
  if (tags.value.trim()) query.set('tags', tags.value.split(/[,，]/).map(value => value.trim()).filter(Boolean).join(','))
  if (hidden.value) query.set('show_hidden', '1')
  query.set('sort', sort.value)
  if (period.value) query.set('period', period.value)
  if (period.value === 'custom') { if (after.value) query.set('posted_after', after.value); if (before.value) query.set('posted_before', before.value) }
  emit('apply', query.toString())
}
</script>
<template>
  <aside class="filter-panel" aria-label="图库筛选"><header><h2>筛选图库</h2><button class="quiet" @click="emit('close')">收起</button></header>
    <form @submit.prevent="apply"><label>排序<select v-model="sort"><option value="newest">投稿时间 · 从新到旧</option><option value="oldest">投稿时间 · 从旧到新</option><option value="added">存档时间 · 从新到旧</option></select></label>
      <fieldset><legend>作品类型</legend><label v-for="[value, name] in [['illust','插画'],['manga','漫画'],['ugoira','动图']]" :key="value" class="check"><input v-model="types" type="checkbox" :value="value">{{ name }}</label></fieldset>
      <fieldset><legend>画师</legend><label class="check"><input v-model="allArtists" type="checkbox">所有画师（包括暂停和停止追踪）</label><select v-if="!allArtists" v-model="selected" multiple size="6" aria-label="选择画师"><option v-for="artist in artists" :key="artist.id" :value="String(artist.id)">{{ artist.name }}</option></select><small v-if="!allArtists">可以多选；不选择画师时显示空图库。</small></fieldset>
      <label>标签<input v-model="tags" placeholder="多个标签用逗号分开"><small>匹配其中任意一个标签</small></label>
      <label>投稿日期<select v-model="period"><option value="">不限</option><option value="24h">24 小时内</option><option value="week">一周内</option><option value="month">一个月内</option><option value="half_year">半年内</option><option value="year">一年内</option><option value="custom">指定期间</option></select></label>
      <div v-if="period === 'custom'" class="date-range"><label>从<input v-model="after" type="date"></label><label>到<input v-model="before" type="date"></label></div>
      <label class="check"><input v-model="hidden" type="checkbox">显示已隐藏作品</label>
      <p v-if="error" class="error">{{ error }}</p><button class="primary" type="submit">应用筛选</button><button type="button" class="quiet" @click="emit('apply', '')">重置</button>
    </form>
  </aside>
</template>
