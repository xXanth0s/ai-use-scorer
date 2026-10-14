import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import type { Report, ScorerMetrics, Session } from '../lib/types';
import { evidenceIndex, findEvidence } from '../lib/report-view';
import { layoutDayTimeline, overlapIntervals } from '../lib/day-timeline';

// Session ids from the scorer export are hashes, not uuids; metric evidence ids carry no session prefix.
const sid='3f2a9c0d1e7b44aa9c01de55';
const session:Session={id:sid,nativeId:'3c7c1e5a-652c-4c2a-b306-9d6a96b018c2',agent:'claude',title:'bridge',cwd:'/repo/a',path:'',startedAt:'2026-10-14T10:00:00Z',updatedAt:'2026-10-14T11:00:00Z',parentId:null,source:'scorer',hidden:false,userCount:1,assistantCount:0,toolCount:0,tools:[],malformedLines:0,truncated:false,fingerprint:'f',
 tasks:[{id:`${sid}:t3`,start:'2026-10-14T10:00:00Z',end:'2026-10-14T10:20:00Z',aborted:false,reason:'turn_duration'}],
 events:[{id:`${sid}:3000`,sessionId:sid,line:3000,timestamp:'2026-10-14T10:00:00Z',kind:'user',text:'add --since'},{id:'metric:4711',sessionId:sid,line:9004711,timestamp:'2026-10-14T10:20:00Z',kind:'metric',text:'V = 0.67 share (n=6)'}]};
const scorerMetrics:ScorerMetrics={day:'2026-10-14',tz:'America/Los_Angeles',source:'scorer',demo:true,verdict:'v',breakdown:{cCauses:{},costByModel:[]},flat:{V:.67},
 tiles:[{key:'V',label:'Verified edit batches',value:.67,unit:'share',display:'67%',evidence:[{id:'metric:4711',sessionId:sid,excerpt:'V'}]}],
 lanes:[{sessionId:sid,agent:'claude',repo:'a',branch:'main',cwd:'/repo/a'}],interrupts:[{sessionId:sid,at:'2026-10-14T10:10:00Z',evidenceId:`${sid}:3000`}]};
const report:Report={id:'scorer-demo-2026-10-14',status:'complete',createdAt:'2026-10-14T23:00:00Z',name:'n',role:'self · 2026-10-14',sessionIds:[sid],snapshot:[session],error:null,coverage:[],scorerMetrics,
 content:{title:'t',summary:'s',findings:[{dimension:'Verification judgment',rating:'Effective',confidence:'Moderate',assessment:'a',evidenceIds:[`${sid}:3000`]}],moments:[],opportunities:[],limitations:[],followUps:[]}};

test('evidence resolves by scanning snapshot events, including metric:<id>',()=>{
 assert.equal(findEvidence([session],'metric:4711')?.event.kind,'metric');
 assert.equal(findEvidence([session],'metric:1'),null);
 const ids=evidenceIndex(report).map(e=>e.id).sort();
 assert.deepEqual(ids,[`${sid}:3000`,'metric:4711'].sort());
});

test('report and evidence routes serve scorerMetrics and metric evidence',async()=>{
 const root=await mkdtemp(join(tmpdir(),'scorer-bridge-'));process.env.AI_SESSION_DATA_DIR=root;
 try{
  const db=await import('../lib/db');db.saveReport(report);
  const reports=await import('../app/api/reports/[id]/route');
  const r=await reports.GET(new Request('http://localhost/api/reports/x',{headers:{host:'localhost'}}),{params:Promise.resolve({id:report.id})});
  const view=await r.json();
  assert.equal(view.scorerMetrics.tiles[0].key,'V');
  assert.ok(view.evidenceIndex.some((e:{id:string})=>e.id==='metric:4711'));
  const ev=await import('../app/api/evidence/route');
  const e=await ev.GET(new Request(`http://localhost/api/evidence?id=metric:4711&report=${report.id}`,{headers:{host:'localhost'}}));
  assert.equal((await e.json()).event.text,'V = 0.67 share (n=6)');
 }finally{await rm(root,{recursive:true,force:true});}
});

test('day timeline groups lanes by worktree, shades overlap, places linked interrupts',()=>{
 assert.deepEqual(overlapIntervals([{sessionId:'a',start:0,end:10},{sessionId:'b',start:5,end:20},{sessionId:'a',start:30,end:40}]),[{start:5,end:10}]);
 const L=layoutDayTimeline([{id:sid,agent:'claude',title:'bridge',tasks:session.tasks}],scorerMetrics.lanes,scorerMetrics.interrupts,900)!;
 assert.equal(L.rows[0].type,'repo');
 const lane=L.rows[1];assert.ok(lane.type==='lane');
 if(lane.type==='lane'){assert.equal(lane.label,'main');assert.equal(lane.bars.length,1);assert.equal(lane.ticks[0].evidenceId,`${sid}:3000`);}
 assert.equal(layoutDayTimeline([],[],[],900),null);
});
