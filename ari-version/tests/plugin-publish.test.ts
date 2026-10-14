import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import type { Report, Session } from '../lib/types';

test('publication retains exact analyzed citations, excludes snapshots and paths, and rejects silent citation truncation',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hdyai-publication-'));process.env.AI_SESSION_DATA_DIR=root;
 try{
  const {publishedView,sessionSummaries}=await import('../lib/publish');const {reportInput}=await import('../lib/report');
  const session:Session={id:'s',nativeId:'s',agent:'claude',title:'Synthetic',cwd:'/private/work',path:'/private/log.jsonl',startedAt:'2020-01-02T15:00:00Z',updatedAt:'2020-01-02T15:01:00Z',parentId:null,source:'native',hidden:false,userCount:201,assistantCount:0,toolCount:0,tools:['Bearer abcdefghijklmnopqrstuvwxyz'],malformedLines:0,truncated:false,fingerprint:'test',tasks:[{id:'t',start:'2020-01-02T15:00:00Z',end:null,aborted:false,reason:'Bearer abcdefghijklmnopqrstuvwxyz'}],events:Array.from({length:201},(_,i)=>({id:`s:${i}`,sessionId:'s',line:i,timestamp:'2020-01-02T15:00:00Z',kind:'user' as const,text:`Synthetic excerpt ${i}`}))};
  const report:Report={id:'r',status:'complete',createdAt:'2020-01-03T15:00:00Z',name:'Test',role:'Test',sessionIds:['s'],snapshot:[session],content:{title:'Test',summary:'Test',findings:[],moments:[{title:'Test',observation:'Test',attribution:'Candidate',evidenceIds:['s:2','s:0','s:2']}],opportunities:[],limitations:[],followUps:[]},error:null,coverage:[]};
  const view=publishedView(report);assert.equal('snapshot' in view,false);assert.equal(view.evidenceIndex.length,2);
  const original=reportInput([session]).input[0].evidence;
  for(const {sessionTitle,agent,...e} of view.evidenceIndex){assert.deepEqual(e,original.find(item=>item.id===e.id));assert.equal(sessionTitle,session.title);assert.equal(agent,session.agent);}
  const {events:_events,path:_path,fingerprint:_fp,...summary}=session;
  const serialized=JSON.stringify(sessionSummaries([summary]));assert.ok(!serialized.includes('/private/')&&!serialized.includes('abcdefghijklmnopqrstuvwxyz'));
  const defensive=JSON.stringify(sessionSummaries([session]));assert.ok(!defensive.includes('Synthetic excerpt')&&!defensive.includes('fingerprint')&&!defensive.includes('/private/'),'summary transport must whitelist metadata even when passed a full session');
  report.content!.moments[0].evidenceIds=session.events.map(e=>e.id);assert.throws(()=>publishedView(report),/evidence limit/);
 }finally{await rm(root,{recursive:true,force:true});}
});
