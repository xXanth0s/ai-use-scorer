import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
test('hook copies reuse native session and evidence IDs; Entire-only mode scopes daily selection',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hdyai-collection-'));process.env.AI_SESSION_DATA_DIR=join(root,'data');process.env.CODEX_HOME=join(root,'codex');process.env.CLAUDE_CONFIG_DIR=join(root,'claude');
 const db=await import('../lib/db');const {collect}=await import('../lib/collector');const {dailySessions}=await import('../lib/daily');
 try{
  const repo=join(root,'repo'),nativeDir=join(root,'claude','projects','work');await mkdir(nativeDir,{recursive:true});await promisify(execFile)('git',['init',repo]);
  const row=(id:string)=>JSON.stringify({type:'user',sessionId:id,uuid:id+'-user',timestamp:'2026-10-14T15:00:00Z',cwd:repo,message:{content:'Compare this conclusion to the source.'}})+'\n';
  const original=join(nativeDir,'tracked.jsonl');await writeFile(original,row('tracked'));await writeFile(join(nativeDir,'outside.jsonl'),row('outside'));await collect();
  const before=db.listSessions().find(s=>s.nativeId==='tracked')!,evidenceId=db.getSession(before.id)!.events[0].id;assert.equal(db.listSessions().length,2);
  await mkdir(join(repo,'.git','entire-sessions'));await mkdir(join(repo,'.entire','metadata','tracked'),{recursive:true});await writeFile(join(repo,'.entire','metadata','tracked','full.jsonl'),row('tracked'));
  await writeFile(join(repo,'.git','entire-sessions','tracked.json'),JSON.stringify({session_id:'tracked',agent_type:'Claude Code',transcript_path:original,phase:'idle'}));
  db.setState('settings',{entireRepos:[repo],nativeFallback:true});await collect();const sessions=db.listSessions();assert.equal(sessions.length,2);const after=sessions.find(s=>s.nativeId==='tracked')!;assert.equal(after.id,before.id);assert.equal(after.capture?.storage,'staged');assert.equal(db.getSession(after.id)!.events[0].id,evidenceId);
  db.setState('settings',{entireRepos:[repo],nativeFallback:false});assert.equal(db.listSessions().length,1);assert.equal(db.listSessions({allSources:true}).length,2);assert.deepEqual(dailySessions('2026-10-14').map(s=>s.id),[before.id]);
 }finally{db.db().close();await rm(root,{recursive:true,force:true});}
});
