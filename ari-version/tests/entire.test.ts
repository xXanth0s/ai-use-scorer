import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, mkdir, writeFile, rm, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { entireTranscripts } from '../lib/entire';
import { parseSession, sessionKey } from '../lib/parser';
const exec=promisify(execFile);
test('Entire state discovery resolves Git common dir and recognizes native agent paths',async()=>{
 const root=await mkdtemp(join(tmpdir(),'entire-adapter-'));
 try{
  await exec('git',['init',root]);
  const transcript=join(root,'native.jsonl');await writeFile(transcript,'{}\n');
  await mkdir(join(root,'.git','entire-sessions'));
  await writeFile(join(root,'.git','entire-sessions','session.json'),JSON.stringify({session_id:'one',agent_type:'Claude Code',transcript_path:transcript}));
  const r=await entireTranscripts([root]);assert.equal(r.errors.length,0);assert.equal(r.entries.length,1);assert.equal(r.entries[0].agent,'claude');assert.ok(r.entries[0].path.endsWith('/native.jsonl'));
 }finally{await rm(root,{recursive:true,force:true});}
});
test('missing Entire session store reports a coverage error without failing direct collection',async()=>{const r=await entireTranscripts(['/does-not-exist']);assert.equal(r.entries.length,0);assert.equal(r.errors.length,1);});

test('Entire staging is preferred while stable source identity is preserved',async()=>{
 const root=await mkdtemp(join(tmpdir(),'entire-staging-'));
 try{
  await exec('git',['init',root]);const original=join(root,'native.jsonl'),id='stable-session';
  const text=JSON.stringify({type:'user',sessionId:id,uuid:'user-1',timestamp:'2026-10-14T12:00:00Z',message:{content:'Check the source.'}})+'\n';
  await writeFile(original,text);await mkdir(join(root,'.git','entire-sessions'));await mkdir(join(root,'.entire','metadata',id),{recursive:true});await writeFile(join(root,'.entire','metadata',id,'full.jsonl'),text);
  await writeFile(join(root,'.git','entire-sessions',id+'.json'),JSON.stringify({session_id:id,agent_type:'Claude Code',phase:'idle',transcript_path:original}));
  const r=await entireTranscripts([root],join(root,'cache'));assert.equal(r.errors.length,0);assert.equal(r.entries[0].storage,'staged');
  const parsed=await parseSession('claude',r.entries[0].path,'fixture',sessionKey('claude',original));assert.equal(parsed.id,sessionKey('claude',original));assert.equal(parsed.events[0].sessionId,parsed.id);assert.equal(parsed.nativeId,id);
 }finally{await rm(root,{recursive:true,force:true});}
});

test('local Entire checkpoint survives missing native files and repairs missing cache files',async()=>{
 const root=await mkdtemp(join(tmpdir(),'entire-checkpoint-'));
 try{
  await exec('git',['init',root]);await mkdir(join(root,'0'));
  const text=JSON.stringify({type:'user',sessionId:'archived',uuid:'u1',timestamp:'2026-10-14T12:00:00Z',message:{content:'Inspect this reference.'}})+'\n';
  await writeFile(join(root,'metadata.json'),JSON.stringify({sessions:[{metadata:'/0/metadata.json',transcript:'/0/full.jsonl'}]}));await writeFile(join(root,'0','metadata.json'),JSON.stringify({session_id:'archived',agent:'Claude Code'}));await writeFile(join(root,'0','full.jsonl'),text);
  await exec('git',['-C',root,'add','metadata.json','0']);await exec('git',['-C',root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','Checkpoint fixture']);await exec('git',['-C',root,'update-ref','refs/entire/checkpoints/00/01ARCHIVED00000000000000000','HEAD']);
  await rm(join(root,'0'),{recursive:true});await rm(join(root,'metadata.json'));
  const cache=join(root,'private-cache'),first=await entireTranscripts([root],cache);assert.equal(first.errors.length,0);assert.equal(first.entries.length,1);assert.equal(first.entries[0].storage,'checkpoint');assert.equal(await readFile(first.entries[0].path,'utf8'),text);assert.equal((await stat(first.entries[0].path)).mode&0o777,0o600);
  await rm(first.entries[0].path);const recovered=await entireTranscripts([root],cache);assert.equal(recovered.errors.length,0);assert.equal(await readFile(recovered.entries[0].path,'utf8'),text);
 }finally{await rm(root,{recursive:true,force:true});}
});
