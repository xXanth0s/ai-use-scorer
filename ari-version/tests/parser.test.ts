import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseSession, humanText, redact } from '../lib/parser';
import { metrics } from '../lib/metrics';
async function fixture(records:unknown[],agent:'codex'|'claude') {const dir=await mkdtemp(join(tmpdir(),'trace-test-'));try{const path=join(dir,'session.jsonl');await writeFile(path,records.map(r=>typeof r==='string'?r:JSON.stringify(r)).join('\n'));return await parseSession(agent,path,'test');}finally{await rm(dir,{recursive:true,force:true});}}
const at=(time:string,type:string,payload:unknown)=>({timestamp:`2026-10-13T${time}Z`,type,payload});
test('Codex parser preserves candidate attribution, call/result links and explicit aborts',async()=>{
 const s=await fixture([
  at('10:00:00','session_meta',{id:'native',cwd:'/trial',source:'cli'}),
  at('10:00:00','event_msg',{type:'task_started',turn_id:'t1'}),
  at('10:00:00','response_item',{type:'message',role:'user',content:[{text:'Check this calculation.'}]}),
  at('10:00:01','response_item',{type:'function_call',name:'calculator',call_id:'c1',arguments:'{"value":2}'}),
  at('10:00:02','response_item',{type:'function_call_output',call_id:'c1',output:'4'}),
  at('10:00:10','event_msg',{type:'turn_aborted',turn_id:'t1',reason:'interrupted'}),
  '{"incomplete":'
 ],'codex');
 assert.equal(s.userCount,1);assert.equal(s.toolCount,1);assert.equal(s.events[1].callId,s.events[2].callId);assert.equal(s.malformedLines,1);assert.equal(metrics([s]).interruptions,1);assert.equal(metrics([s]).longestTaskMs,null);
});
test('Claude tool results in user records are not counted as candidate prompts',async()=>{
 const s=await fixture([{type:'user',uuid:'u1',timestamp:'2026-10-13T10:00:00Z',sessionId:'s',message:{content:'Find sources.'}},
 {type:'assistant',uuid:'a1',timestamp:'2026-10-13T10:00:01Z',message:{content:[{type:'tool_use',id:'tool1',name:'search',input:{query:'market'}}]}},
 {type:'user',uuid:'u2',timestamp:'2026-10-13T10:00:02Z',message:{content:[{type:'tool_result',tool_use_id:'tool1',content:'Source found',is_error:false}]}}],'claude');
 assert.equal(s.userCount,1);assert.equal(s.toolCount,1);assert.equal(s.events.find(e=>e.kind==='tool_result')?.callId,'tool1');assert.equal(metrics([s]).timingSessions,0);
});
test('system wrappers are excluded and candidate context extracted',()=>{assert.equal(humanText('# AGENTS.md instructions\nsecret'), '');assert.equal(humanText('<in-app-browser-context>ambient</in-app-browser-context>\n## My request:\nFix hover.'),'Fix hover.');assert.equal(redact('Bearer abcdefghijklmnop'),'Bearer [REDACTED]');});
test('guardian threads are marked as excluded',async()=>{const s=await fixture([at('10:00:00','session_meta',{source:{subagent:{other:'guardian'}},thread_source:'guardian_review'})],'codex');assert.equal(s.hidden,true);});
test('concurrency uses execution intervals; touching boundaries do not overlap',async()=>{
 const make=async(start:string,end:string)=>fixture([at(start,'event_msg',{type:'task_started',turn_id:'t'}),at(end,'event_msg',{type:'task_complete',turn_id:'t'})],'codex');
 const a=await make('10:00:00','10:02:00'),b=await make('10:01:00','10:03:00'),c=await make('10:03:00','10:04:00');
 const m=metrics([a,b,c]);assert.equal(m.peakConcurrency,2);assert.equal(m.parallelSessions,2);assert.equal(m.longestTaskMs,120000);
});
test('candidate intervention disqualifies an otherwise completed task from uninterrupted duration',async()=>{const s=await fixture([at('10:00:00','event_msg',{type:'task_started',turn_id:'t'}),at('10:00:01','response_item',{type:'message',role:'assistant',content:[{text:'Working.'}]}),at('10:00:30','response_item',{type:'message',role:'user',content:[{text:'Change direction.'}]}),at('10:01:00','event_msg',{type:'task_complete',turn_id:'t'})],'codex');assert.equal(metrics([s]).longestTaskMs,null);});

test('numeric Unix task timestamps are normalized before KPI calculation',async()=>{const s=await fixture([at('10:00:00','event_msg',{type:'task_started',turn_id:'t',started_at:1791885600}),at('10:01:00','event_msg',{type:'task_complete',turn_id:'t',completed_at:1791885660})],'codex');assert.equal(metrics([s]).longestTaskMs,60000);assert.equal(metrics([s]).peakConcurrency,1);});
