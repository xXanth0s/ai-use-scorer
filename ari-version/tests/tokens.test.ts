import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseSession } from '../lib/parser';
import { metrics, filterTokenUsage } from '../lib/metrics';
async function fixture(records:unknown[],agent:'codex'|'claude'='codex') {const dir=await mkdtemp(join(tmpdir(),'tokens-test-'));try{const path=join(dir,'session.jsonl');await writeFile(path,records.map(r=>JSON.stringify(r)).join('\n'));return await parseSession(agent,path,'test');}finally{await rm(dir,{recursive:true,force:true});}}
const at=(type:string,payload:unknown,timestamp='2026-10-13T10:00:00Z')=>({type,payload,timestamp});
const native=(input:number,output:number,cached=0)=>({input_tokens:input,output_tokens:output,cached_input_tokens:cached,total_tokens:input+output,reasoning_output_tokens:output});
const count=(total:unknown,last:unknown=total,timestamp?:string)=>at('event_msg',{type:'token_count',info:{total_token_usage:total,last_token_usage:last}},timestamp);
const answer=(phase='final_answer',id='a')=>at('response_item',{type:'message',role:'assistant',id,phase,content:[{type:'output_text',text:'An answer.'}]});
const claude=(id:string,usage:unknown,stop_reason:string|null,content:unknown=[{type:'text',text:'An answer.'}],uuid=id)=>({type:'assistant',uuid,timestamp:'2026-10-13T10:00:00Z',message:{id,usage,stop_reason,content}});
test('Codex cumulative usage counts each increment once, not reasoning or cached tokens again',async()=>{
 const s=await fixture([at('event_msg',{type:'task_started',turn_id:'t'}),count(native(100,20,40),native(100,20,40)),count(native(100,20,40),native(100,20,40)),answer('commentary','working'),count(native(300,50,140),native(200,30,100)),answer(),at('event_msg',{type:'task_complete',turn_id:'t',last_agent_message:'An answer.'}),at('event_msg',{type:'task_complete',turn_id:'t',last_agent_message:'An answer.'})]);
 const m=metrics([s]);assert.equal(m.tokenTotals?.totalTokens,350);assert.equal(m.tokenTotals?.cachedInputTokens,140);assert.equal(m.tokenTotals?.outputTokens,50);assert.equal(m.finalOutputs,1);assert.equal(m.tokensPerFinalOutput,350);assert.equal(s.tokenUsage?.records.length,2);
});
test('Codex resumed baseline counts last request only and handles a counter reset',async()=>{
 const s=await fixture([count(native(1000,200),native(100,20)),count(native(1200,230),native(200,30)),count(native(40,10),native(40,10)),answer()]);assert.equal(metrics([s]).tokenTotals?.totalTokens,400);
});
test('Claude streaming duplicates update usage once and include disjoint cache categories',async()=>{
 const u={input_tokens:10,output_tokens:2,cache_creation_input_tokens:30,cache_read_input_tokens:100,cache_creation:{ephemeral_5m_input_tokens:30}};
 const s=await fixture([claude('tool',u,'tool_use',[{type:'tool_use',id:'tool1',name:'read',input:{}}]),claude('answer',u,null),claude('answer',{...u,output_tokens:20},'end_turn',undefined,'chunk2'),claude('answer',{...u,output_tokens:20},'end_turn',undefined,'chunk3')],'claude');
 const m=metrics([s]);assert.equal(s.tokenUsage?.records.length,2);assert.equal(m.tokenTotals?.inputTokens,280);assert.equal(m.tokenTotals?.outputTokens,22);assert.equal(m.tokenTotals?.cachedInputTokens,200);assert.equal(m.tokenTotals?.cacheWriteInputTokens,60);assert.equal(m.finalOutputs,1);assert.equal(m.tokensPerFinalOutput,302);
});
test('Claude tool use, max-token stops, and commentary are not final outputs',async()=>{
 const s=await fixture([claude('a',{input_tokens:10,output_tokens:2},'tool_use'),claude('b',{input_tokens:10,output_tokens:2},'max_tokens')],'claude');assert.equal(metrics([s]).finalOutputs,0);assert.equal(metrics([s]).tokensPerFinalOutput,null);
});
test('historical sessions and usage without final attribution remain unavailable',async()=>{
 const s=await fixture([count(native(10,2)),at('response_item',{type:'message',role:'assistant',content:[{text:'Unclassified text.'}]})]);assert.equal(metrics([s]).tokensPerFinalOutput,null);assert.equal(metrics([s]).finalOutputs,null);
 const historical={...s,tokenUsage:undefined};assert.equal(metrics([historical]).tokenTotals,null);assert.equal(metrics([historical]).tokenUsageSessions,0);
 const noUsage=await fixture([answer()]);assert.equal(metrics([noUsage]).tokensPerFinalOutput,null);assert.equal(metrics([noUsage]).finalOutputs,1);
});
test('ratio matches numerator and denominator session coverage',async()=>{
 const a=await fixture([count(native(100,20)),answer()]);const b=await fixture([answer()]);const c=await fixture([count(native(800,80))]);
 const m=metrics([a,b,c]);assert.equal(m.tokenTotals?.totalTokens,1000);assert.equal(m.tokensPerFinalOutput,120);assert.equal(m.tokenMetricFinalOutputs,1);assert.equal(m.finalOutputs,2);assert.equal(m.tokenUsageSessions,2);assert.equal(m.tokenMetricSessions,1);
});
test('day filter attributes incremental usage and final answers to their recorded timestamps',async()=>{
 const s=await fixture([count(native(100,10),native(100,10),'2026-10-13T23:59:00Z'),count(native(300,30),native(200,20),'2026-10-14T00:01:00Z'),{...answer(),timestamp:'2026-10-14T00:02:00Z'}]);
 const today={...s,tokenUsage:filterTokenUsage(s.tokenUsage,t=>t.startsWith('2026-10-14'))};assert.equal(metrics([today]).tokenTotals?.totalTokens,220);assert.equal(metrics([today]).tokensPerFinalOutput,220);assert.equal(filterTokenUsage(undefined,()=>true),undefined);
});
test('invalid counters and partial Claude usage are ignored, never estimated',async()=>{
 const s=await fixture([count(native(-10,2)),count({input_tokens:10,output_tokens:'2'}),answer()]);assert.equal(metrics([s]).tokenTotals,null);
 const c=await fixture([claude('a',{input_tokens:10},'end_turn')],'claude');assert.equal(metrics([c]).tokenTotals,null);
});
test('older Codex completed-turn answer fallback is deduplicated',async()=>{
 const s=await fixture([count(native(10,2)),at('event_msg',{type:'task_complete',turn_id:'t',last_agent_message:'An answer.'}),at('event_msg',{type:'task_complete',turn_id:'t',last_agent_message:'An answer.'})]);assert.equal(metrics([s]).finalOutputs,1);assert.equal(metrics([s]).tokensPerFinalOutput,12);
});

test('cumulative-only initial usage establishes a baseline instead of attributing inherited history',async()=>{
 const s=await fixture([count(native(1000,200),null),count(native(1100,220),null),answer()]);assert.equal(metrics([s]).tokenTotals?.totalTokens,120);
 const baselineOnly=await fixture([count(native(1000,200),null),answer()]);assert.equal(metrics([baselineOnly]).tokenTotals,null);assert.equal(metrics([baselineOnly]).tokensPerFinalOutput,null);
});
