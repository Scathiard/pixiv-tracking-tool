<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, write } from '../api'
import { ReadLifecycle } from '../interaction'
import SourceStatusOverview from '../components/SourceStatusOverview.vue'
import Icon from '../components/Icon.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
defineProps<{galleryMode: string}>()
interface Settings {
  login: {status: 'logged_in' | 'offline' | 'pending'; account_id: string | null}
  automatic_paused: boolean; current_port: number; next_port: number; data_root: string
  check_interval_minutes: number; next_check_at?: string | null; lan_urls?: string[]; stats: {artists: number; works: number; downloaded_pages: number; registered_originals?: number; verified_originals?: number; missing_originals?: number; unverified_originals?: number}
  version?: string; build?: string
}
const emit = defineEmits<{task: [kind: string, workId?: number]; theme: [theme: 'dark' | 'light', event: MouseEvent]; galleryMode: [value: string]; message: [text: string, isError?: boolean]}>()
const settings = ref<Settings | null>(null), loading = ref(true), error = ref(''), port = ref(''), pendingPath = ref(''), busy = ref(false)
const sourcesOpen = ref(false), interval = ref(''), intervalUnit = ref<'minutes' | 'hours'>('minutes')
function openSources(open: boolean) { sourcesOpen.value = open; void nextTick(() => document.querySelector('.settings-page')?.scrollTo({top:0})) }
const theme = ref<'dark' | 'light'>('dark')
interface AuthStatus {logged_in: boolean; login_url: string | null; login_error: string | null}
interface Logs {lines: string[]; truncated: boolean; details_complete: boolean; next_cursor: string | null}
const auth = ref<AuthStatus | null>(null), authError = ref('')
const oauthCode = ref(''), confirmPath = ref(false)
const logsOpen = ref(false), logsLoading = ref(false), logsError = ref(''), logs = ref<Logs | null>(null)
const settingsReads = new ReadLifecycle(), logReads = new ReadLifecycle(), statusReads = new ReadLifecycle()
let retryLogContinuation = false
let statusTimer = 0, stopped = false
function loadLogs(continuation = false) {
  if (stopped || !logsOpen.value) return Promise.resolve()
  const cursor = continuation ? logs.value?.next_cursor : null
  if (continuation && !cursor) return Promise.resolve()
  const previous = logs.value
  retryLogContinuation = continuation
  logsLoading.value = true
  logsError.value = ''
  return logReads.run(`logs:${cursor || 'initial'}`, signal => api<Logs>(cursor ? `/api/logs?cursor=${encodeURIComponent(cursor)}` : '/api/logs', {signal}), value => {
    logs.value = cursor && previous ? {...value, lines: [...previous.lines, ...value.lines], truncated: previous.truncated || value.truncated} : value
  }, failure => { logsError.value = failure instanceof Error ? failure.message : '处理日志暂时无法读取' }, () => { logsLoading.value = false })
}
function toggleLogs() {
  logsOpen.value = !logsOpen.value
  if (logsOpen.value && !logs.value) void loadLogs()
  else if (!logsOpen.value) { logReads.cancel(); logsLoading.value = false; logsError.value = '' }
}
function load() {
  if (stopped) return Promise.resolve()
  loading.value = true
  return settingsReads.run('settings', signal => api<Settings>('/api/settings', {signal}), value => {
    settings.value = value; port.value = String(value.next_port); interval.value = String(value.check_interval_minutes); intervalUnit.value = 'minutes'; error.value = ''
  }, failure => { error.value = failure instanceof Error ? failure.message : '设置暂时无法读取' }, () => { loading.value = false })
}
function beginSettingsWrite() { settingsReads.cancel(); loading.value = false; busy.value = true }
function applySettings(value: Settings) {
  settingsReads.cancel()
  if (!stopped) { settings.value = value; loading.value = false }
}
async function pause() {
  if (!settings.value) return
  beginSettingsWrite()
  try { const value = await write<Settings>('/api/settings', {automatic_paused: !settings.value.automatic_paused}, 'PATCH'); applySettings(value); if (!stopped) emit('message', value.automatic_paused ? '后续自动检查已暂停' : '已恢复自动检查') }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '设置未能保存', true) }
  finally { busy.value = false }
}
async function savePort() {
  const next = Number(port.value)
  if (!Number.isInteger(next) || next < 1024 || next > 65535) { error.value = '请输入 1024 到 65535 之间的端口号'; emit('message', error.value, true); return }
  beginSettingsWrite()
  try { const value = await write<Settings>('/api/settings', {port: next}, 'PATCH'); applySettings(value); if (!stopped) { error.value = ''; emit('message', `端口 ${next} 已保存，重启后生效`) } }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '端口未能保存' }
  finally { busy.value = false }
}
async function saveInterval() {
  const minutes = Number(interval.value) * (intervalUnit.value === 'hours' ? 60 : 1)
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 43200) { emit('message', '请输入 1 到 43200 分钟（最多 720 小时）的周期', true); return }
  beginSettingsWrite()
  try { const value = await write<Settings>('/api/settings', {check_interval_minutes: minutes}, 'PATCH'); applySettings(value); if (!stopped) emit('message', '周期已保存，从现在重新计时；当前任务及暂停设置保持原状') }
  catch (failure) { emit('message', failure instanceof Error ? failure.message : '周期未能保存，原周期保持不变', true) }
  finally { busy.value = false }
}
async function copyAddress(value: string) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value)
    else { const input = document.createElement('textarea'); input.value = value; document.body.append(input); input.select(); const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error('请长按地址手动复制') }
    emit('message', '手机访问地址已复制')
  } catch { emit('message', '复制不可用，请选择地址手动复制', true) }
}
function nextCheck(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '保存周期后显示' }
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
function pollStatus() {
  if (stopped) return Promise.resolve()
  clearTimeout(statusTimer)
  return statusReads.run('status', signal => api<AuthStatus>('/api/status', {signal}), value => {
    const previous = auth.value?.logged_in
    auth.value = value; authError.value = ''
    if (value.logged_in && !previous && !busy.value) void load()
  }, failure => { authError.value = failure instanceof Error ? failure.message : '连接状态暂时无法读取' }, () => {
    if (!stopped) statusTimer = window.setTimeout(pollStatus, 3000)
  })
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
onBeforeUnmount(() => { stopped = true; clearTimeout(statusTimer); settingsReads.dispose(); logReads.dispose(); statusReads.dispose() })
</script>
<template>
  <section class="settings-page"><template v-if="sourcesOpen"><button class="text-link" @click="openSources(false)">← 返回设置</button><h1 class="sources-title">主站已无法访问</h1><SourceStatusOverview /></template><template v-else><div class="settings-heading"><h1>设置</h1><p class="sublead">管理连接、自动检查和本地存储。</p></div>
    <p v-if="error" class="notice error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <div v-if="loading && !settings" class="empty">正在读取设置…</div>
    <template v-if="settings">
      <h2 class="settings-section-title">账号与检查</h2>
      <div class="settings-card settings-stack"><div class="setting-top"><div><h3>Pixiv 连接</h3><p>{{ settings.login.status === 'logged_in' || auth?.logged_in ? `已连接${settings.login.account_id ? ` · ${settings.login.account_id}` : ''}` : settings.login.status === 'pending' || auth?.login_url ? '登录进行中' : '离线，本地图库仍可阅读' }}</p></div><button v-if="settings.login.status !== 'logged_in' && !auth?.logged_in" :disabled="busy" @click="login">{{ auth?.login_url ? '重新连接' : '连接 Pixiv' }}</button></div><div v-if="auth?.login_url" class="auth-flow"><a class="softbtn" :href="auth.login_url" target="_blank" rel="noopener noreferrer">打开 Pixiv 授权页面 <Icon name="external" /></a><p>授权后复制浏览器中的 callback 地址，粘贴到下面完成登录。</p><form @submit.prevent="submitCode"><input v-model="oauthCode" type="text" autocomplete="off" spellcheck="false" aria-label="授权回调地址或 code" placeholder="粘贴 callback 地址或 code"><button class="primary" :disabled="busy || !oauthCode.trim()" type="submit">提交回调</button></form></div><p v-if="auth?.login_error" class="error">{{ auth.login_error }}</p><p v-if="authError" class="muted" role="status">连接状态暂不可得：{{ authError }}；稍后会继续核对。</p></div>
      <div class="settings-card settings-stack"><div class="setting-top"><div><h3>自动检查</h3><p>{{ settings.automatic_paused ? '后续自动检查已暂停；当前批次继续。' : `每 ${settings.check_interval_minutes} 分钟检查一次。` }}</p><p>下次计划：{{ nextCheck(settings.next_check_at) }}{{ settings.automatic_paused ? '（暂停期间不启动）' : '' }}</p></div><button :disabled="busy" :aria-pressed="settings.automatic_paused" @click="pause">{{ settings.automatic_paused ? '恢复自动检查' : '暂停自动检查' }}</button></div><form class="settings-controls interval-controls" @submit.prevent="saveInterval"><label>检查周期 <input v-model="interval" type="number" min="1" :max="intervalUnit === 'hours' ? 720 : 43200" aria-label="检查周期"></label><select v-model="intervalUnit" aria-label="检查周期单位"><option value="minutes">分钟</option><option value="hours">小时</option></select><button :disabled="busy" type="submit">保存周期</button></form></div>
      <div class="settings-card settings-stack"><div><h3>手动管理</h3><p>核对画师主页和作品来源，检查已有存档、更新资料与收藏状态，并补齐可取得的缺项；已存内容继续保留。</p></div><div class="settings-controls wrap"><button class="primary" @click="emit('task', 'reconcile')">检查并补齐图库</button><button @click="emit('task', 'check')">检查新作品</button></div></div>
      <SourceStatusOverview summary @open="openSources(true)" />
      <div class="settings-card settings-stack"><div class="setting-top"><div><h3>处理日志</h3><p>查看最近的后台处理与失败原因，内容只读。</p></div><button :aria-expanded="logsOpen" @click="toggleLogs">{{ logsOpen ? '收起日志' : '查看日志' }}</button></div><div v-if="logsOpen" class="settings-log"><div class="settings-controls"><button :disabled="logsLoading" @click="loadLogs()">{{ logsLoading ? '正在读取…' : '刷新日志' }}</button></div><p v-if="logsError" class="error" role="alert">{{ logsError }} <button :disabled="logsLoading" @click="loadLogs(retryLogContinuation)">重试</button></p><p v-else-if="!logs && logsLoading" class="muted">正在读取处理日志…</p><template v-else-if="logs"><pre v-if="logs.lines.length">{{ logs.lines.join('\n') }}</pre><p v-else class="muted">暂无处理记录。</p><p v-if="logs.truncated" class="muted">仅显示最近任务的记录。</p><p v-if="!logs.details_complete" class="muted">详细记录尚未读完，可继续读取。</p><button v-if="logs.next_cursor" :disabled="logsLoading" @click="loadLogs(true)">{{ logsLoading ? '正在读取…' : '继续读取' }}</button></template></div></div>
      <h2 class="settings-section-title">本地存储</h2>
      <div class="settings-card settings-stack"><div><h3>图库位置</h3><p>{{ settings.stats.artists }} 位画师 · {{ settings.stats.works }} 件作品 · {{ settings.stats.registered_originals ?? settings.stats.downloaded_pages }} 项原件已登记</p></div><p class="stats-detail">原件已核验 {{ settings.stats.verified_originals ?? settings.stats.downloaded_pages }} 项 · 缺失 {{ settings.stats.missing_originals ?? 0 }} 项 · 待核验 {{ settings.stats.unverified_originals ?? 0 }} 项</p><p class="stats-detail">统计当前默认版本：登记表示已有本地路径；核验表示文件已经检查。历史版本和动图预览不重复计数。</p><div class="path-display">{{ settings.data_root }}</div><div class="settings-controls"><button @click="browsePath"><Icon name="folder" />选择目录</button><span v-if="pendingPath" class="muted">{{ pendingPath }}</span><button v-if="pendingPath" :disabled="busy" @click="confirmPath = true">迁移到这里</button></div></div>
      <div class="settings-card"><div><h3>访问端口</h3><p>当前使用 {{ settings.current_port }}；修改后重启程序才会生效。</p></div><div class="settings-controls"><input v-model="port" type="number" min="1024" max="65535" aria-label="下次启动端口"><button :disabled="busy" @click="savePort">保存端口</button></div></div>
      <div class="settings-card settings-stack"><div><h3>手机访问</h3><p>电脑和手机连接同一局域网，在手机浏览器打开以下当前端口地址。</p></div><div v-for="address in settings.lan_urls || []" :key="address" class="lan-address"><a :href="address">{{ address }}</a><button @click="copyAddress(address)">复制地址</button></div><p v-if="!settings.lan_urls?.length">暂未发现可用局域网地址，请检查电脑的网络连接。</p></div>
      <h2 class="settings-section-title">界面</h2>
      <div class="settings-card"><div><h3>图库浏览方式</h3><p>连续滚动会按需读取作品；页码模式可直接跳页。</p></div><select :value="galleryMode" aria-label="图库浏览方式" @change="emit('galleryMode', ($event.target as HTMLSelectElement).value)"><option value="scroll">连续滚动</option><option value="50">50 件 / 页</option><option value="100">100 件 / 页</option><option value="200">200 件 / 页</option><option value="500">500 件 / 页</option></select></div>
      <div class="settings-card"><div><h3>外观</h3><p>选择适合当前环境的阅读配色。</p></div><div class="theme-choices"><button :class="{active: theme === 'dark'}" :aria-pressed="theme === 'dark'" @click="theme = 'dark'; emit('theme', 'dark', $event)">深色</button><button :class="{active: theme === 'light'}" :aria-pressed="theme === 'light'" @click="theme = 'light'; emit('theme', 'light', $event)">浅色</button></div></div>
      <p v-if="settings.version" class="settings-version muted">版本 {{ settings.version }}<template v-if="settings.build"> · 内部构建 {{ settings.build }}</template></p>
    </template>
    </template><ConfirmDialog v-if="confirmPath" title="迁移本地图库" :description="`将图库从 ${settings?.data_root} 迁移到 ${pendingPath}。现有文件会移动到新位置，请确认当前没有运行中的任务。`" action="开始迁移" @cancel="confirmPath = false" @confirm="changePath" />
  </section>
</template>

<style scoped>
.sources-title{margin:15px 0 20px}.interval-controls{flex-wrap:wrap;margin-top:14px}.interval-controls label{display:flex;align-items:center;gap:8px;font-size:12px}.interval-controls select{width:80px}.stats-detail{margin-top:8px}.lan-address{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:12px}.lan-address a{overflow-wrap:anywhere;font-size:13px}.lan-address button{font-size:12px}
</style>
