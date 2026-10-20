const fs = require('fs');
const vm = require('vm');
const path = require('path');
const root = process.cwd();
const ts = require(path.join(root, '.worktrees/v0.2.0-build20261004.3/app/frontend/node_modules/typescript'));
const vue = require(path.join(root, '.worktrees/v0.2.0-build20261004.3/app/frontend/node_modules/vue'));
const original = fs.readFileSync('app/frontend/src/App.vue', 'utf8').split('<script setup lang="ts">')[1].split('</script>')[0];
const source = original.replace(/^import .*$/gm, '') + '\nreturn {openWork,navigate,retryNavigation,mode,total,scope,currentWork,ordinaryIndex,desiredIndex,ordinaryNeighbors,ordinaryCursor,pending,error,cursorExpired};';
const code = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS}}).outputText;
const interactionSource=fs.readFileSync('app/frontend/src/interaction.ts','utf8');
const intentSource=interactionSource.slice(interactionSource.indexOf('export class LatestIntent'),interactionSource.indexOf('/** A view owns')).replace('export class','class');
const LatestIntent=new Function('discardedSignal',ts.transpileModule(intentSource,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText+';return LatestIntent')(()=>{});
let stallImage=false,pendingImages=[];
class SyntheticImage {
 set src(value){queueMicrotask(()=>this.onload?.())} removeAttribute(){} decode(){return stallImage?new Promise(resolve=>pendingImages.push(resolve)):Promise.resolve()}
}
const imageSource=fs.readFileSync('app/frontend/src/api.ts','utf8').split('export function prepareImage')[1];
const prepareImage=new Function('mediaObservation','Image',ts.transpileModule('function prepareImage'+imageSource,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText+';return prepareImage')(()=>undefined,SyntheticImage);
class ApiReadError extends Error {constructor(message,code){super(message);this.code=code}}
let gen=1, stall=false, calls=[];
const cards=[3,2,1].map(id=>({id,title:`Synthetic ${id}`,revision_id:null,cursor:`${id}:${gen}`}));
const token=id=>`${id}:${gen}`;
const url=(p,q,e={})=>{const x=new URLSearchParams(q);Object.entries(e).forEach(([k,v])=>x.set(k,String(v)));return p+'?'+x};
async function api(p,opts={}){calls.push(p);if(stall) return new Promise((resolve,reject)=>{opts.signal?.addEventListener('abort',()=>reject(opts.signal.reason),{once:true})});const u=new URL(p,'http://synthetic');const t=u.searchParams.get('cursor');if(t&&Number(t.split(':')[1])!==gen)throw new ApiReadError('图库已变化，当前位置仍保留；重新定位后可继续','cursor_expired');const id=Number(u.searchParams.get('anchor_work_id'));
 if(u.pathname==='/api/library/cursor'){const i=cards.findIndex(c=>c.id===id),d=u.searchParams.get('direction');return {items:(d==='previous'?cards.slice(Math.max(0,i-1),i+1):cards.slice(i,i+2)).map(c=>({...c,cursor:token(c.id)}))}}
 const w=Number(u.pathname.match(/works\/(\d+)/)?.[1]);const i=cards.findIndex(c=>c.id===w);return {anchor_cursor:token(w),generation:gen,previous:cards[i-1]?{work_id:cards[i-1].id,cursor:token(cards[i-1].id)}:null,next:cards[i+1]?{work_id:cards[i+1].id,cursor:token(cards[i+1].id)}:null}; }
const router={resolve:()=>({fullPath:'/synthetic'}),push:async()=>{},replace:async()=>{}};
const env={...vue,watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},useRoute:()=>({fullPath:'',path:'/'}),useRouter:()=>router,LatestIntent,ApiReadError,api,url,prepareImage,workDetail:async id=>({...cards.find(c=>c.id===id),pages:[{src:`/synthetic/${id}.png`}],display_page_count:1}),observeCatalogueData:()=>{},ingestBookmark:()=>{},refreshBookmark:()=>{},invalidateWorkCache:()=>{},window:{setTimeout},localStorage:{getItem:()=>null},console,AbortController,URLSearchParams,clearTimeout};
const app=new Function(...Object.keys(env),code)(...Object.values(env));
const out=[];const snapshot=label=>out.push({label,id:app.currentWork.value?.id,index:app.ordinaryIndex.value,pending:app.pending.value,error:app.error.value,expired:app.cursorExpired.value,calls:calls.length});
(async()=>{app.total.value=3;app.mode.value='reader';await app.openWork(0,false,undefined,{id:3});await app.openWork(1);snapshot('normal-next');await app.openWork(0);snapshot('normal-previous');gen++;await app.openWork(1);snapshot('stale-cursor');app.retryNavigation();await new Promise(r=>setImmediate(r));snapshot('explicit-recovery');await app.openWork(1);snapshot('after-recovery-next');gen++;await app.openWork(2);snapshot('second-background-write');stall=true;app.navigate(1);await new Promise(r=>setImmediate(r));snapshot('next-read-stalled');app.navigate(-1);await new Promise(r=>setImmediate(r));snapshot('another-arrow-while-stalled');
 stall=false;app.retryNavigation();await new Promise(r=>setImmediate(r));stallImage=true;const imagePending=app.openWork(2);await new Promise(r=>setImmediate(r));snapshot('image-decode-stalled-after-api-success');stallImage=false;pendingImages.forEach(resolve=>resolve());await imagePending;snapshot('image-decode-released');
 const template=fs.readFileSync('app/frontend/src/App.vue','utf8').split('<template>')[1];out.push({pendingReferencedInTemplate:/\bpending\b/.test(template.replaceAll(':navigation-pending="pending"', ''))});fs.writeFileSync('work/tmp/reading-ux-20261019/navigation/sfc-replay-result.json',JSON.stringify(out,null,2));console.log(JSON.stringify(out,null,2));
 if(out.find(x=>x.label==='next-read-stalled').pending && !out.find(x=>x.label==='next-read-stalled').error && !out.at(-1).pendingReferencedInTemplate){console.log('REPRODUCED: pending navigation clears prior expiry message and has no rendered pending feedback');process.exitCode=1}
})();

