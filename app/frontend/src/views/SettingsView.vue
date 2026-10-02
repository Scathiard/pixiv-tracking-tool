<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, write } from '../api'
import SourceStatusOverview from '../components/SourceStatusOverview.vue'
import Icon from '../components/Icon.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
defineProps<{galleryMode: string}>()
interface Settings {
  login: {status: 'logged_in' | 'offline' | 'pending'; account_id: string | null}
  automatic_paused: boolean; current_port: number; next_port: number; data_root: string
  check_interval_minutes: number; stats: {artists: number; works: number; downloaded_pages: number}
  version?: string; build?: string
}
const emit = defineEmits<{task: [kind: string, workId?: number]; theme: [theme: 'dark' | 'light', event: MouseEvent]; galleryMode: [value: string]; message: [text: string, isError?: boolean]}>()
const settings = ref<Settings | null>(null), loading = ref(true), error = ref(''), port = ref(''), pendingPath = ref(''), busy = ref(false)
const theme = ref<'dark' | 'light'>('dark')
const auth = ref<{logged_in: boolean; login_url: string | null; login_error: string | null} | null>(null)
const oauthCode = ref(''), confirmPath = ref(false)
const logsOpen = ref(false), logsLoading = ref(false), logsError = ref(''), logs = ref<{lines: string[]; truncated: boolean} | null>(null)
let statusTimer = 0, stopped = false
async function loadLogs() {
  logsLoading.value = true
  logsError.value = ''
  try { logs.value = await api<{lines: string[]; truncated: boolean}>('/api/logs') }
  catch (failure) { logsError.value = failure instanceof Error ? failure.message : '处理日志暂时无法读取' }
  finally { logsLoading.value = false }
}
function toggleLogs() {
  logsOpen.value = !logsOpen.value
  if (logsOpen.value && !logs.value) void loadLogs()
}
async function load() {
  loading.value = true
  try { settings.value = await api<Settings>('/api/settings'); port.value = String(settings.value.next_port); error.value = '' }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '设置暂时无法读取' }
  finally { loading.value = false }
}
async function pause() {
  if (!settings.value) return
  busy.value = true
  try { settings.value = await write<Settings>('/api/settings', {automatic_paused: !settings.value.automatic_paused}, 'PATCH'); emit('message', settings.value.automatic_paused ? '后续自动检查已暂停' : '已恢复自动检查') }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '设置未能保存', true) }
  finally { busy.value = false }
}
async function savePort() {
  const next = Number(port.value)
  if (!Number.isInteger(next) || next < 1024 || next > 65535) { error.value = '请输入 1024 到 65535 之间的端口号'; emit('message', error.value, true); return }
  busy.value = true
  try { settings.value = await write<Settings>('/api/settings', {port: next}, 'PATCH'); error.value = ''; emit('message', `端口 ${next} 已保存，重启后生效`) }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '端口未能保存' }
  finally { busy.value = false }
}
async function login() {
  busy.value = true
  try { await api('/api/login', {method: 'POST'}); emit('message', '已开始连接 Pixiv，可继续使用本地图库'); await pollStatus(); await load() }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '无法开始登录', true) }
  finally { busy.value = false }
}
async function submitCode() {
  if (!oauthCode.value.trim()) return
  busy.value = true
  try { await write('/api/submit-code', {code: oauthCode.value.trim()}); oauthCode.value = ''; emit('message', '已提交授权回调，正在确认登录'); await pollStatus() }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '授权回调未能提交', true) }
  finally { busy.value = false }
}
async function pollStatus() {
  clearTimeout(statusTimer)
  try {
    const previous = auth.value?.logged_in
    auth.value = await api<{logged_in: boolean; login_url: string | null; login_error: string | null}>('/api/status')
    if (auth.value.logged_in && !previous) await load()
  } catch { /* Settings remain usable while status is unavailable. */ }
  if (!stopped) statusTimer = window.setTimeout(pollStatus, 3000)
}
async function browsePath() {
  try { const result = await api<{path: string}>('/settings/browse-path', {method: 'POST'}); if (result.path) pendingPath.value = result.path }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '未能打开目录选择器', true) }
}
async function changePath() {
  if (!pendingPath.value || pendingPath.value === settings.value?.data_root) return
  confirmPath.value = false
  busy.value = true
  try {
    const body = new URLSearchParams({new_path: pendingPath.value})
    const result = await api<{message?: string}>('/settings/change-path', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body})
    emit('message', result.message || '数据目录已更新'); pendingPath.value = ''; await load()
  } catch (failure) { emit('message', failure instanceof Error ? failure.message : '数据目录未能更新', true) }
  finally { busy.value = false }
}
onMounted(() => { theme.value = document.body.dataset.theme === 'light' ? 'light' : 'dark'; void load(); void pollStatus() })
onBeforeUnmount(() => { stopped = true; clearTimeout(statusTimer) })
</script>
<template>
  <section class="settings-page"><div class="settings-heading"><h1>设置</h1><p class="sublead">管理连接、自动检查和本地存储。</p></div>
    <p v-if="error" class="notice error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <div v-if="loading && !settings" class="empty">正在读取设置…</div>
    <template v-if="settings">
      <h2 class="settings-section-title">账号与检查</h2>
      <div class="settings-card settings-stack"><div class="setting-top"><div><h3>Pixiv 连接</h3><p>{{ settings.login.status === 'logged_in' || auth?.logged_in ? `已连接${settings.login.account_id ? ` · ${settings.login.account_id}` : ''}` : settings.login.status === 'pending' || auth?.login_url ? '登录进行中' : '离线，本地图库仍可阅读' }}</p></div><button v-if="settings.login.status !== 'logged_in' && !auth?.logged_in" :disabled="busy" @click="login">{{ auth?.login_url ? '重新连接' : '连接 Pixiv' }}</button></div><div v-if="auth?.login_url" class="auth-flow"><a class="softbtn" :href="auth.login_url" target="_blank" rel="noopener noreferrer">打开 Pixiv 授权页面 <Icon name="external" /></a><p>授权后复制浏览器中的 callback 地址，粘贴到下面完成登录。</p><form @submit.prevent="submitCode"><input v-model="oauthCode" type="text" autocomplete="off" spellcheck="false" aria-label="授权回调地址或 code" placeholder="粘贴 callback 地址或 code"><button class="primary" :disabled="busy || !oauthCode.trim()" type="submit">提交回调</button></form></div><p v-if="auth?.login_error" class="error">{{ auth.login_error }}</p></div>
      <div class="settings-card"><div><h3>自动检查</h3><p>{{ settings.automatic_paused ? '已暂停后续自动检查；当前任务会继续。' : `每 ${settings.check_interval_minutes} 分钟检查一次；当前任务不会中断。` }}</p></div><button :disabled="busy" :aria-pressed="settings.automatic_paused" @click="pause"><Icon :name="settings.automatic_paused ? 'check' : 'pause'" />{{ settings.automatic_paused ? '恢复自动检查' : '暂停自动检查' }}</button></div>
      <div class="settings-card settings-stack"><div><h3>手动管理</h3><p>检查已有存档、更新资料与收藏状态，并补齐可取得的缺项；已存内容继续保留。</p></div><div class="settings-controls wrap"><button class="primary" @click="emit('task', 'reconcile')">检查并补齐图库</button><button @click="emit('task', 'check')">检查新作品</button></div></div>
      <SourceStatusOverview />
      <div class="settings-card settings-stack"><div class="setting-top"><div><h3>处理日志</h3><p>查看最近的后台处理与失败原因，内容只读。</p></div><button :aria-expanded="logsOpen" @click="toggleLogs">{{ logsOpen ? '收起日志' : '查看日志' }}</button></div><div v-if="logsOpen" class="settings-log"><div class="settings-controls"><button :disabled="logsLoading" @click="loadLogs">{{ logsLoading ? '正在读取…' : '刷新日志' }}</button></div><p v-if="logsError" class="error" role="alert">{{ logsError }} <button @click="loadLogs">重试</button></p><p v-else-if="!logs && logsLoading" class="muted">正在读取处理日志…</p><template v-else-if="logs"><pre v-if="logs.lines.length">{{ logs.lines.join('\n') }}</pre><p v-else class="muted">暂无处理记录。</p><p v-if="logs.truncated" class="muted">仅显示最近的记录。</p></template></div></div>
      <h2 class="settings-section-title">本地存储</h2>
      <div class="settings-card settings-stack"><div><h3>图库位置</h3><p>{{ settings.stats.artists }} 位画师 · {{ settings.stats.works }} 件作品 · {{ settings.stats.downloaded_pages }} 页已下载</p></div><div class="path-display">{{ settings.data_root }}</div><div class="settings-controls"><button @click="browsePath"><Icon name="folder" />选择目录</button><span v-if="pendingPath" class="muted">{{ pendingPath }}</span><button v-if="pendingPath" :disabled="busy" @click="confirmPath = true">迁移到这里</button></div></div>
      <div class="settings-card"><div><h3>访问端口</h3><p>当前使用 {{ settings.current_port }}；修改后重启程序才会生效。</p></div><div class="settings-controls"><input v-model="port" type="number" min="1024" max="65535" aria-label="下次启动端口"><button :disabled="busy" @click="savePort">保存端口</button></div></div>
      <h2 class="settings-section-title">界面</h2>
      <div class="settings-card"><div><h3>图库浏览方式</h3><p>连续滚动会按需读取作品；页码模式可直接跳页。</p></div><select :value="galleryMode" aria-label="图库浏览方式" @change="emit('galleryMode', ($event.target as HTMLSelectElement).value)"><option value="scroll">连续滚动</option><option value="50">50 件 / 页</option><option value="100">100 件 / 页</option><option value="200">200 件 / 页</option></select></div>
      <div class="settings-card"><div><h3>外观</h3><p>选择适合当前环境的阅读配色。</p></div><div class="theme-choices"><button :class="{active: theme === 'dark'}" :aria-pressed="theme === 'dark'" @click="theme = 'dark'; emit('theme', 'dark', $event)">深色</button><button :class="{active: theme === 'light'}" :aria-pressed="theme === 'light'" @click="theme = 'light'; emit('theme', 'light', $event)">浅色</button></div></div>
      <p v-if="settings.version" class="settings-version muted">版本 {{ settings.version }}<template v-if="settings.build"> · 内部构建 {{ settings.build }}</template></p>
    </template>
    <ConfirmDialog v-if="confirmPath" title="迁移本地图库" :description="`将图库从 ${settings?.data_root} 迁移到 ${pendingPath}。现有文件会移动到新位置，请确认当前没有运行中的任务。`" action="开始迁移" @cancel="confirmPath = false" @confirm="changePath" />
  </section>
</template>
