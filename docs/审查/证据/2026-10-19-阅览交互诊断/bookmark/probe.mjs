// Diagnostic only: actual current state module, synthetic transport, fake clock.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {stripTypeScriptTypes, createRequire} from 'node:module';
import assert from 'node:assert/strict';
const root = path.resolve('app/frontend/src');
const requireVue = createRequire(path.resolve('.worktrees/v0.2.0-build20261004.3/app/frontend/package.json'));
const vue = requireVue('vue');
const initial = id => ({work_id:id,operation_id:null,status:'confirmed',desired:null,value:false,confirmed_at:'2026-10-19T00:00:00Z',error:null,version:1,cached:true,verified:true,fact_account_id:'synthetic',current_account_id:'synthetic',account_epoch:10});
async function fixture(api, write) {
  let now=100000, timerId=0;
  const timers=new Map();
  const context=vm.createContext({console,AbortController,crypto:globalThis.crypto,performance:{now:()=>now},Date:class extends Date {static now(){return now}},document:{hidden:false}});
  context.window={setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{at:now+delay,fn});return id},clearTimeout:id=>timers.delete(id)};
  context.clearTimeout=context.window.clearTimeout;
  const cache=new Map();
  function synthetic(name,values){const result=new vm.SyntheticModule(Object.keys(values),function(){for(const [key,value] of Object.entries(values))this.setExport(key,value)},{context,identifier:name});cache.set(name,result);return result}
  synthetic('vue',vue); synthetic('api',{api,write});
  function load(file) {
    if(cache.has(file))return cache.get(file);
    const result=new vm.SourceTextModule(stripTypeScriptTypes(fs.readFileSync(file,'utf8')),{context,identifier:file});cache.set(file,result);return result;
  }
  const module=load(path.join(root,'state/bookmarks.ts'));
  await module.link((specifier,ref)=>specifier==='vue'?cache.get('vue'):specifier==='../api'?cache.get('api'):load(path.resolve(path.dirname(ref.identifier),specifier+'.ts')));
  await module.evaluate();
  const drain=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
  async function advance(ms){const target=now+ms;for(;;){const due=[...timers].filter(([,t])=>t.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].fn();await drain()}now=target;await drain()}
  return {s:module.namespace,advance,drain,timers};
}
let failed=0;
async function check(name, fn){try{await fn();console.log('GREEN '+name)}catch(error){failed++;console.log('RED '+name+': '+error.message)}}
await check('background refresh does not discard deliberate heart click',async()=>{
  let writes=0;const f=await fixture(async()=>initial(1),async p=>{writes++;if(p.includes('/refresh'))return new Promise(()=>{});return initial(1)});
  f.s.ingestBookmark(1,initial(1));void f.s.syncBookmark(1,true);await f.drain();
  let error;await f.s.toggleBookmark(1).catch(e=>error=e.message);
  console.log(JSON.stringify({busy:f.s.bookmarkBusy(1),ack:f.s.bookmarkAcknowledging(1),requests:writes,error}));
  assert.equal(f.s.bookmarkAcknowledging(1),true,'click has no acknowledgement; toggle rejects before feedback.start');
});
await check('transport failure reports without waiting indefinitely on recovery GET',async()=>{
  let settled=false;const f=await fixture(async()=>new Promise(()=>{}),async()=>{throw new Error('synthetic write transport failure')});
  f.s.ingestBookmark(2,initial(2));void f.s.toggleBookmark(2).then(()=>settled=true,()=>settled=true);await f.drain();await f.advance(60000);
  console.log(JSON.stringify({busy:f.s.bookmarkBusy(2),ack:f.s.bookmarkAcknowledging(2),toggleSettled:settled,timers:f.timers.size}));
  assert.equal(settled,true,'error feedback is blocked behind unbounded recovery read');
});
await check('polling failure leaves a usable recoverable heart instead of permanent busy',async()=>{
  let reads=0;const f=await fixture(async()=>{reads++;throw new Error('synthetic operation read failure')},async()=>({...initial(3),version:2,status:'pending',operation_id:'op-3',desired:true}));
  f.s.ingestBookmark(3,initial(3));await f.s.toggleBookmark(3);await f.advance(180000);
  console.log(JSON.stringify({reads,busy:f.s.bookmarkBusy(3),ack:f.s.bookmarkAcknowledging(3),timers:f.timers.size,error:f.s.bookmarkState(3).error}));
  assert.equal(f.s.bookmarkBusy(3),false,'9 failed polls stop scheduling but keep pending and acknowledgement forever');
});
process.exitCode=failed?1:0;
