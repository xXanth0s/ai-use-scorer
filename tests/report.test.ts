import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import type { Report, Session } from '../lib/types';

test('report pipeline records measured stages and rejects fabricated evidence citations',async()=>{
 const root=await mkdtemp(join(tmpdir(),'trace-report-'));process.env.AI_SESSION_DATA_DIR=root;
 const db=await import('../lib/db');const pipeline=await import('../lib/report');
 const evidenceId='sample:1';
 const snapshot:Session={id:'sample',nativeId:'native',agent:'codex',title:'Verify a result',cwd:'/trial',path:'/trial/log.jsonl',startedAt:'2026-10-14T10:00:00Z',updatedAt:'2026-10-14T10:00:00Z',parentId:null,source:'cli',hidden:false,userCount:1,assistantCount:0,toolCount:0,tools:[],malformedLines:0,truncated:false,fingerprint:'one',tasks:[],events:[{id:evidenceId,sessionId:'sample',line:1,timestamp:'2026-10-14T10:00:00Z',kind:'user',text:'Check this claim against the original source.'}]};
 const result={title:'Assessment',summary:'Observed direction.',findings:[{dimension:'Verification',rating:'Effective',confidence:'Moderate',assessment:'Requested a source check.',evidenceIds:[evidenceId]}],moments:[],opportunities:[],limitations:['Small sample.'],followUps:[]};
 const create=(id:string):Report=>({id,status:'queued',createdAt:new Date(Date.now()-200).toISOString(),name:'Sample',role:'General',sessionIds:['sample'],snapshot:[snapshot],content:null,error:null,coverage:[]});
 try{
  db.saveReport(create('good'));await pipeline.generateReport('good',async payload=>{assert.equal(payload.input[0].evidence[0].id,evidenceId);assert.equal('workspace' in payload.input[0],false);return {content:result,usage:{inputTokens:120,outputTokens:30}};});const good=db.getReport('good')!;
  assert.equal(good.status,'complete');assert.equal(good.analysis?.stage,'complete');assert.equal(good.analysis?.sampledRecords,1);assert.equal(good.analysis?.inputTokens,120);assert.ok((good.analysis?.totalMs||0)>=200);assert.equal(typeof good.analysis?.agentMs,'number');
  db.saveReport(create('bad'));await pipeline.generateReport('bad',async()=>({content:{...result,findings:[{...result.findings[0],evidenceIds:['fabricated:99']}]}}));const bad=db.getReport('bad')!;assert.equal(bad.status,'failed');assert.match(bad.error||'',/invalid evidence/);assert.equal(bad.analysis?.stage,'failed');
 }finally{db.db().close();await rm(root,{recursive:true,force:true});}
});
