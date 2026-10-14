import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm, readdir, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

const rootDir=resolve('.');
function run(cmd:string,args:string[],env:NodeJS.ProcessEnv,cwd:string,input='',closeInput=true){
 return new Promise<{code:number|null;out:string;err:string}>((done,reject)=>{
  const child=spawn(cmd,args,{env,cwd,stdio:'pipe'});let out='',err='';
  const timeout=setTimeout(()=>{child.kill('SIGKILL');reject(new Error('Process budget exceeded'));},15000);
  child.stdout.on('data',c=>out+=c);child.stderr.on('data',c=>err+=c);child.once('error',reject);
  child.once('exit',code=>{clearTimeout(timeout);done({code,out,err});});if(closeInput)child.stdin.end(input);else child.stdin.write(input);
 });
}
async function unpack(file:string,dir:string){
 const b=await readFile(file);let offset=0;const names:string[]=[];
 while(b.readUInt32LE(offset)===0x04034b50){
  const size=b.readUInt32LE(offset+18),length=b.readUInt16LE(offset+26),extra=b.readUInt16LE(offset+28);
  const name=b.subarray(offset+30,offset+30+length).toString();assert.ok(!name.includes('..'));
  const start=offset+30+length+extra,path=join(dir,name);await mkdir(resolve(path,'..'),{recursive:true});
  await writeFile(path,b.subarray(start,start+size));names.push(name);offset=start+size;
 }return names;
}
test('extracted package runs without npm, tsx, Git or Entire; hooks, sync and interrupted reports recover privately',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hdyai-package-'));
 const env={...process.env,HDYAI_DATA_DIR:join(root,'data'),AI_SESSION_DATA_DIR:join(root,'data'),CODEX_HOME:join(root,'codex'),CLAUDE_CONFIG_DIR:join(root,'claude'),REPORT_TIMEZONE:'America/Los_Angeles',NODE_NO_WARNINGS:'1'};
 const data=join(root,'data'),native=join(root,'claude','projects','work');
 let server:ReturnType<typeof createServer>|undefined;
 try{
  const built=await run(process.execPath,['--import','tsx','scripts/build-plugin.ts',join(root,'release')],env,rootDir);assert.equal(built.code,0,built.err);
  const archive=join(root,'release','how-do-you-ai.zip');const release=JSON.parse(await readFile(join(root,'release','release.json'),'utf8'));
  assert.equal(release.sha256,createHash('sha256').update(await readFile(archive)).digest('hex'));
  const names=await unpack(archive,root);assert.ok(names.includes('plugins/how-do-you-ai/scripts/worker.mjs'));
  assert.equal(names.some(n=>n.includes('node_modules')||n.endsWith('.sqlite')),false);
  const plugin=join(root,'plugins','how-do-you-ai');const invoke=(action:string,input='',arg?:string)=>run('/bin/bash',[join(plugin,'scripts/run.sh'),action,...(arg?[arg]:[])],env,root,input);
  assert.equal((await invoke('status')).out.trim(),'{"connected":false,"lastSync":null,"lastError":null}');
  await mkdir(native,{recursive:true});await mkdir(data,{recursive:true});
  const transcript=join(native,'main.jsonl');const day='2020-01-02';
  const row={type:'user',sessionId:'main',uuid:'one',timestamp:`${day}T15:00:00Z`,message:{content:'Synthetic verification. Bearer abcdefghijklmnopqrstuvwxyz'}};
  await writeFile(transcript,JSON.stringify(row));
  await writeFile(join(data,'collector','config.json'),JSON.stringify({active:true,roots:{codex:[join(root,'codex','sessions')],claude:[join(root,'claude','projects')]}}));
  const hook=await invoke('hook',JSON.stringify({hook_event_name:'Stop',session_id:'main',transcript_path:transcript}),'claude');assert.equal(hook.code,0);assert.equal(hook.out,'{}\n');
  const hooks=JSON.parse(await readFile(join(plugin,'hooks','hooks.json'),'utf8'));
  const stopBudgetMs=hooks.hooks.Stop[0].hooks[0].timeout*1000;
  const stalledAt=Date.now();const stalled=await run('/bin/bash',[join(plugin,'scripts/run.sh'),'hook','claude'],env,root,'{}',false);assert.equal(stalled.code,0);assert.equal(stalled.out,'{}\n');assert.ok(Date.now()-stalledAt<stopBudgetMs,'Stop hook must exit within its declared lifecycle budget even without stdin EOF');
  const folders=await readdir(join(data,'collector','sessions'));assert.equal(folders.length,1);
  const snapshot=join(data,'collector','sessions',folders[0],'full.jsonl');assert.equal((await stat(snapshot)).mode&0o777,0o600);assert.ok(!(await readFile(snapshot,'utf8')).includes('abcdefghijklmnopqrstuvwxyz'));
  // Reject paths outside the configured roots and retain a fully written final line.
  const outside=join(root,'outside.jsonl');await writeFile(outside,JSON.stringify(row));
  await invoke('hook',JSON.stringify({hook_event_name:'Stop',session_id:'outside',transcript_path:outside}),'claude');assert.equal((await readdir(join(data,'collector','sessions'))).length,1);
  const token='hdyai_'+'x'.repeat(43),invitation='invite_'+'i'.repeat(43);let activation=0,analyze=0,failUpload=false;const uploads:any[]=[];
  server=createServer(async(req,res)=>{
   let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;
   res.setHeader('content-type','application/json');
   if(req.url==='/api/activate'){activation++;assert.equal(body.activationCode,invitation);res.end(JSON.stringify({deviceToken:token,userId:'synthetic',expiresAt:'2099-01-01T00:00:00Z'}));}
   else if(req.url==='/api/sync'){assert.equal(req.headers.authorization,`Bearer ${token}`);uploads.push(body);if(failUpload&&body.report?.status==='complete'){res.statusCode=503;res.end('{"error":"Synthetic temporary failure"}');}else res.end('{}');}
   else if(req.url==='/api/analyze'){analyze++;res.end(JSON.stringify({jobId:'a'.repeat(64)}));}
   else if(req.url?.startsWith('/api/analyses/'))res.end(JSON.stringify({status:'complete',content:{title:'Synthetic',summary:'Test',findings:[],moments:[],opportunities:[],limitations:[],followUps:[]}}));
   else res.end('{}');
  });await new Promise<void>(r=>server!.listen(0,'127.0.0.1',r));const address=server.address() as {port:number};const origin=`http://127.0.0.1:${address.port}`;
  const paired=await invoke('pair',invitation,origin);assert.equal(paired.code,0,paired.err);assert.equal(activation,1);assert.ok(!paired.out.includes(token)&&!paired.err.includes(invitation));
  assert.equal((await stat(join(data,'analysis-connection.json'))).mode&0o777,0o600);
  const db=new DatabaseSync(join(data,'sessions.sqlite'));
  const state=(key:string,value:any)=>db.prepare('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key,JSON.stringify(value));
  failUpload=true;state('plugin:lastSync',null);assert.equal((await invoke('sync')).code,0);
  assert.equal(analyze,1);const id=JSON.parse(String(db.prepare('SELECT value FROM state WHERE key=?').get(`plugin:analysis:${day}`)!.value)).id;
  assert.ok(JSON.parse(String(db.prepare('SELECT value FROM state WHERE key=?').get('plugin:pendingReports')!.value)).includes(id));
  assert.ok(uploads.every(p=>!JSON.stringify(p).includes('abcdefghijklmnopqrstuvwxyz')&&!JSON.stringify(p).includes(transcript)));
  failUpload=false;state('plugin:lastSync',null);await invoke('sync');assert.equal(analyze,1,'Completed report publication retries without another analysis');
  state('plugin:lastSync',null);await invoke('sync');assert.equal(analyze,1,'Repeated sync does not create a second daily assessment');
  // Simulate a killed analyzer: original report id is reused.
  const report=JSON.parse(String(db.prepare('SELECT data FROM reports WHERE id=?').get(id)!.data));report.status='running';report.content=null;
  db.prepare('UPDATE reports SET data=? WHERE id=?').run(JSON.stringify(report),id);state('plugin:pendingReports',[id]);state('plugin:lastSync',null);
  await mkdir(join(data,'sync-lock'));const {utimes}=await import('node:fs/promises');await utimes(join(data,'sync-lock'),new Date(0),new Date(0));
  await invoke('sync');assert.equal(analyze,2);assert.equal(JSON.parse(String(db.prepare('SELECT data FROM reports WHERE id=?').get(id)!.data)).status,'complete');
  db.close();assert.equal((await invoke('disconnect')).code,0);assert.equal(JSON.parse(await readFile(join(data,'collector','config.json'),'utf8')).active,false);
  await rm(join(plugin,'scripts','worker.mjs'));
  const missingWorker=await invoke('hook','{}','claude');assert.equal(missingWorker.code,0);assert.equal(missingWorker.out,'{}\n');assert.equal(missingWorker.err,'');
  const noNode={...env,PATH:join(root,'empty')};await mkdir(noNode.PATH);
  // Bash is invoked by absolute path; helper gracefully returns a hook result.
  const missing=await run('/bin/bash',[join(plugin,'scripts/run.sh'),'hook','claude'],noNode,root,'{}');assert.equal(missing.code,0);assert.equal(missing.out,'{}\n');
 }finally{if(server)await new Promise<void>(r=>server!.close(()=>r()));await rm(root,{recursive:true,force:true});}
});
