import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, stat, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

test('standalone hooks preserve settings, survive deleted logs, deduplicate sessions, and retain subagents without Git or Entire',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hdyai-hooks-'));process.env.AI_SESSION_DATA_DIR=join(root,'data');process.env.CODEX_HOME=join(root,'codex');process.env.CLAUDE_CONFIG_DIR=join(root,'claude');
 const {installCaptureHooks,captureStatus,mergeCaptureHooks}=await import('../lib/capture-hooks');
 const db=await import('../lib/db');const {collect}=await import('../lib/collector');
 const native=join(root,'claude','projects','work');await mkdir(native,{recursive:true});await mkdir(join(root,'codex','sessions'),{recursive:true});
 const existing={theme:'dark',hooks:{Stop:[{hooks:[{type:'command',command:'my-existing-hook'}]}]}};
 await writeFile(join(root,'claude','settings.json'),JSON.stringify(existing));
 const transcript=join(native,'main.jsonl'),child=join(native,'subagents','child.jsonl');await mkdir(join(native,'subagents'));
 const row=(id:string,text:string)=>JSON.stringify({type:'user',sessionId:id,uuid:id+'-'+text.slice(0,16),timestamp:'2026-10-14T15:00:00Z',message:{content:text}})+'\n';
 await writeFile(transcript,row('main','Compare the sources. Bearer abcdefghijklmnopqrstuvwxyz'));
 await writeFile(child,row('child','Check the counterexample.'));
 const run=(payload:unknown)=>new Promise<string>((resolve,reject)=>{const p=spawn(process.execPath,[join(root,'data','collector','hook.mjs'),'claude','--hdyai-capture']);let output='';p.stdout.on('data',c=>output+=c);p.once('error',reject);p.once('exit',code=>code===0?resolve(output):reject(new Error('Hook failed')));p.stdin.end(JSON.stringify(payload));});
 try{
  assert.throws(()=>mergeCaptureHooks({hooks:{Stop:'malformed'}},'test'),/left unchanged/);
  const results=await installCaptureHooks(['codex','claude']);assert.ok(results.every(r=>r.installed));
  const once=await readFile(join(root,'claude','settings.json'),'utf8');await installCaptureHooks(['codex','claude']);assert.equal(await readFile(join(root,'claude','settings.json'),'utf8'),once);
  const settings=JSON.parse(once);assert.equal(settings.theme,'dark');assert.equal(settings.hooks.Stop[0].hooks[0].command,'my-existing-hook');assert.equal(settings.hooks.Stop.length,2);
  assert.ok((await captureStatus()).every(s=>s.installed));
  await collect();const original=db.listSessions().find(s=>s.nativeId==='main')!;const evidence=db.getSession(original.id)!.events[0].id;
  assert.equal(await run({hook_event_name:'SubagentStop',session_id:'main',transcript_path:transcript,agent_id:'child',agent_transcript_path:child}),'{}\n');
  await collect();assert.equal(db.listSessions().length,2);const captured=db.getSession(original.id)!;assert.equal(captured.capture?.provider,'hooks');assert.equal(captured.events[0].id,evidence);assert.equal(captured.events[0].text.includes('abcdefghijklmnopqrstuvwxyz'),false);
  assert.equal(db.listSessions().find(s=>s.nativeId==='child')!.parentId,'main');
  const folders=await readdir(join(root,'data','collector','sessions'));assert.equal(folders.length,2);for(const folder of folders){const file=join(root,'data','collector','sessions',folder,'full.jsonl');assert.equal((await stat(file)).mode&0o777,0o600);assert.ok(!(await readFile(file,'utf8')).includes('abcdefghijklmnopqrstuvwxyz'));}
  // Live fallback must include text flushed after the last hook snapshot.
  await writeFile(transcript,row('main','First record.')+row('main','Newly flushed work.'));await collect();assert.equal(db.getSession(original.id)!.userCount,2);assert.equal(db.getSession(original.id)!.capture?.storage,'live');
  await run({hook_event_name:'Stop',session_id:'main',transcript_path:transcript});await rm(transcript);await rm(child);await collect();assert.equal(db.listSessions().length,2);assert.equal(db.getSession(original.id)!.userCount,2);
  const outside=join(root,'not-a-transcript.jsonl');await writeFile(outside,row('outside','Private unrelated text.'));await run({hook_event_name:'Stop',session_id:'outside',transcript_path:outside});assert.equal((await readdir(join(root,'data','collector','sessions'))).length,2);
 }finally{db.db().close();await rm(root,{recursive:true,force:true});}
});
