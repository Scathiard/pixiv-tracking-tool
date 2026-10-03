<script setup lang="ts">
import {onBeforeUnmount, onMounted, ref} from 'vue'
import {api, write} from '../api'
export interface StartupStatus {state: 'ready' | 'required' | 'preparing' | 'activating' | 'starting' | 'failed'; stage: string; message: string | null; can_start: boolean; activated: boolean; backup?: string | null; candidate?: string | null}
const props=defineProps<{status: StartupStatus}>()
const emit=defineEmits<{ready: []}>()
const current=ref(props.status), error=ref(''), busy=ref(false)
let timer=0, stopped=false
async function poll() {
  clearTimeout(timer)
  try { current.value=await api<StartupStatus>('/api/startup'); error.value=''; if (current.value.state==='ready') {emit('ready');return} }
  catch {error.value='启动状态暂时无法读取，请检查程序是否仍在运行。'}
  if (!stopped) timer=window.setTimeout(poll,1000)
}
async function start() {
  busy.value=true; error.value=''
  try {current.value=await write<StartupStatus>('/api/startup/upgrade',{confirmed:true});void poll()}
  catch(failure) {error.value=failure instanceof Error?failure.message:'升级未能开始，请重试'}
  finally {busy.value=false}
}
onMounted(()=>void poll())
onBeforeUnmount(()=>{stopped=true;clearTimeout(timer)})
</script>
<template>
  <main class="upgrade-screen"><section class="upgrade-card" aria-live="polite"><span class="upgrade-brand">PixivTracker</span><h1>{{current.stage}}</h1><p>{{current.message || '正在处理本地图库，已有图片和资料会保留。'}}</p><template v-if="current.state==='required'"><ol><li>为旧数据库保存一致的备份。</li><li>在独立候选中转换并核对作品、文件和未完成任务。</li><li>验证通过后启用，原图片路径保持不变。</li></ol><p>升级可能需要一些时间；完成前图库暂不开放。备份和旧程序可用于恢复，升级后新增的记录不能直接被旧备份覆盖。</p></template><p v-if="error" class="error" role="alert">{{error}}</p><div v-if="current.backup" class="upgrade-path"><small>旧库备份</small><span>{{current.backup}}</span></div><div v-if="current.candidate" class="upgrade-path"><small>核验候选</small><span>{{current.candidate}}</span></div><button v-if="current.can_start" class="primary" :disabled="busy" @click="start">{{current.activated ? '重试启动图库' : current.state==='failed' ? '重试备份并升级' : '备份并升级图库'}}</button><p v-else-if="current.state!=='failed'" class="upgrade-progress" role="status">{{current.state==='preparing'?'正在备份与核验…':current.state==='activating'?'正在启用…':'正在启动…'}}</p></section></main>
</template>
<style scoped>
.upgrade-screen{height:100dvh;overflow:auto;display:grid;place-items:center;padding:28px;background:var(--bg);color:var(--text)}.upgrade-card{width:min(620px,100%);border:1px solid var(--line);border-radius:14px;background:var(--surface);padding:30px}.upgrade-brand{font-size:13px;color:var(--accent);font-weight:600}.upgrade-card h1{font-size:24px;margin:15px 0}.upgrade-card p,.upgrade-card li{font-size:13px;color:var(--muted);line-height:1.8}.upgrade-card ol{padding-left:20px;margin:16px 0}.upgrade-card button{margin-top:22px}.upgrade-path{margin-top:15px;padding:12px;border:1px solid var(--line);border-radius:8px;overflow-wrap:anywhere}.upgrade-path small,.upgrade-path span{display:block;font-size:12px}.upgrade-path small{color:var(--muted);margin-bottom:5px}.upgrade-progress{margin-top:22px;color:var(--accent)!important}@media(max-width:400px){.upgrade-screen{padding:15px}.upgrade-card{padding:22px}}
</style>
