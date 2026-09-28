const path=require('node:path'), fs=require('node:fs');
const root=process.cwd();
const artifact=path.join(root,'.devtools/test-runs/audit-cache-20260928');
const deps=path.join(root,'.worktrees/v0.2.4/app/frontend/node_modules');
const ts=require(path.join(deps,'typescript'));
require(path.join(deps,'vue'));
const Module=require('node:module');
const modules=new Map();
function sourceModule(filename){
 if(modules.has(filename))return modules.get(filename).exports;
 const item=new Module(filename); item.filename=filename; item.paths=[deps];
 const requireOriginal=item.require.bind(item);
 item.require=(request)=>request.startsWith('.') ? sourceModule(path.resolve(path.dirname(filename),request+'.ts')) : requireOriginal(request);
 modules.set(filename,item);
 const compiled=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 item._compile(compiled,filename); return item.exports;
}
let id=0; const timers=new Map(); const delays=[];
global.window={setTimeout(callback,delay){timers.set(++id,{callback,delay});return id;}};
global.clearTimeout=(value)=>timers.delete(value);
global.document={hidden:false};
const snapshot={work_id:1,status:'pending',operation_id:'synthetic-operation',version:1,value:true,cached:true,confirmed_at:'2026-09-28T00:00:00Z'};
global.fetch=async()=>new Response(JSON.stringify(snapshot),{status:200,headers:{'content-type':'application/json'}});
const bookmarks=sourceModule(path.join(root,'app/frontend/src/state/bookmarks.ts'));
(async()=>{
 bookmarks.ingestBookmark(1,snapshot);
 for(let step=0;step<6;step++){
  const [timerId,timer]=timers.entries().next().value;
  timers.delete(timerId); delays.push(timer.delay); await timer.callback();
 }
 const result={pending_poll_delays_ms:delays,expected_schedule_ms:[900,1600,2300,3000,3700,4400],uses_actual_frontend_module:true,real_account_used:false};
 fs.writeFileSync(path.join(artifact,'polling.json'),JSON.stringify(result,null,2)+'\n');
 console.log(JSON.stringify(result,null,2));
 if(delays.every(delay=>delay===900)) process.exitCode=1;
})();
