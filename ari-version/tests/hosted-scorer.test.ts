import {test} from 'node:test';
import assert from 'node:assert/strict';
import {evidenceDigest,publishedReport,validatePublishedReport} from '../services/analysis/agent/lib/library';
import {analysisRequest,buildPrompt} from '../services/analysis/agent/lib/contract';
const user={id:'demo-a:3000',sessionId:'demo-a',line:3000,timestamp:'2026-10-14T10:00:00Z',kind:'user' as const,text:'Add --since to ingest.',agent:'claude' as const,sessionTitle:'Synthetic session'};
const metric={id:'metric:4711',sessionId:'demo-a',line:9004711,timestamp:'2026-10-14T10:05:00Z',kind:'metric' as const,text:'V = 0.67 share (n=6) · turns demo-a:3000',agent:'claude' as const,sessionTitle:'Synthetic session'};
const content={title:'Synthetic',summary:'Summary',findings:[{dimension:'Verification judgment',rating:'Effective',confidence:'Moderate',assessment:'Verified most batches.',evidenceIds:[metric.id,user.id]}],moments:[],opportunities:[],limitations:[],followUps:[]};
const tile={key:'V',label:'Verified edit batches',value:0.67,unit:'share',display:'67%',sub:'asked by you 15%',baseline:null,evidence:[{id:user.id,sessionId:'demo-a',excerpt:'Add --since to ingest.'}]};
const scorerMetrics={day:'2026-10-14',tz:'Europe/Berlin',source:'scorer',demo:true,verdict:'47 of 144 edit batches never verified',tiles:[tile,{...tile,key:'episodes',label:'Episodes',unit:'count',value:3,display:'3'}],breakdown:{cCauses:{instruction_violated:2},costByModel:[{model:'opus-5-5',usd:12.3,tokens:1200000}]},flat:{V:0.67,V_asked_by_you:0.15,B_min:10.2,ctx_p90:null},lanes:[{sessionId:'demo-a',agent:'claude',repo:'acme',branch:'main',cwd:'/sandbox/acme'}],interrupts:[{sessionId:'demo-a',at:'2026-10-14T10:03:00Z',evidenceId:user.id}]};
function report(extra:Record<string,unknown>={}){return publishedReport.parse({id:'scorer-2026-10-14-0a1b2c3d',status:'complete',createdAt:'2026-10-14T23:00:00.000Z',name:'scorer · 2026-10-14',role:'self · 2026-10-14',sessionIds:['demo-a'],content,error:null,coverage:[],metrics:{sessions:1,candidateMessages:1,toolCalls:0,uniqueTools:0,interruptions:0,completedTasks:0,parallelSessions:0,peakConcurrency:1,longestTaskMs:null,timingSessions:0},timezone:'Europe/Berlin',activity:[],evidenceIndex:[user,metric],scorerMetrics,...extra});}
const job=()=>({content,evidence_ids:[user.id,metric.id],evidence_digests:{[user.id]:evidenceDigest(user),[metric.id]:evidenceDigest(metric)}});
test('published reports keep scorerMetrics and accept cited metric evidence',()=>{
 const r=report();
 assert.deepEqual(r.scorerMetrics,scorerMetrics);
 assert.equal(r.evidenceIndex[1].kind,'metric');
 assert.doesNotThrow(()=>validatePublishedReport(r,job()));
 const without=publishedReport.parse({...r,scorerMetrics:undefined});assert.equal(without.scorerMetrics,undefined);
});
test('scorerMetrics is bounded and id-checked',()=>{
 const bad=(m:Record<string,unknown>)=>publishedReport.safeParse({...report(),scorerMetrics:{...scorerMetrics,...m}}).success;
 assert.equal(bad({tiles:Array.from({length:13},()=>tile)}),false);
 assert.equal(bad({tiles:[{...tile,evidence:Array.from({length:9},()=>tile.evidence[0])}]}),false);
 assert.equal(bad({tiles:[{...tile,evidence:[{...tile.evidence[0],excerpt:'x'.repeat(321)}]}]}),false);
 assert.equal(bad({tiles:[{...tile,evidence:[{...tile.evidence[0],id:'bad id/../x'}]}]}),false);
 assert.equal(bad({flat:{['k'.repeat(41)]:1}}),false);
 assert.equal(bad({flat:{V:-1}}),false);
 assert.equal(bad({source:'other'}),false);
 assert.equal(bad({unknownKey:1}),false);
 assert.equal(bad({lanes:[{...scorerMetrics.lanes[0],cwd:'x'.repeat(301)}]}),false);
 assert.equal(bad({interrupts:[{...scorerMetrics.interrupts[0],sessionId:'bad id'}]}),false);
});
test('scorer evidence, lanes and interrupts must belong to the report sessions',()=>{
 const foreign=(m:Record<string,unknown>)=>()=>validatePublishedReport(report({scorerMetrics:{...scorerMetrics,...m}}),job());
 assert.throws(foreign({tiles:[{...tile,evidence:[{...tile.evidence[0],sessionId:'other'}]}]}),/report sessions/);
 assert.throws(foreign({lanes:[{...scorerMetrics.lanes[0],sessionId:'other'}]}),/report sessions/);
 assert.throws(foreign({interrupts:[{...scorerMetrics.interrupts[0],sessionId:'other'}]}),/report sessions/);
});
test('analysis requests accept metric evidence and the flat scorer metrics record',()=>{
 const {agent:_a,sessionTitle:_t,...m}=metric,{agent:_b,sessionTitle:_u,...u}=user;
 const body={requestId:'scorer-2026-10-14-0a1b2c3d',name:'scorer · 2026-10-14',role:'self · 2026-10-14',metrics:{V:0.67,V_asked_by_you:0.15,C_cause_instruction_violated:2,cost_usd_claude_opus_5_5:104.2,ctx_p90:null},input:[{id:'demo-a',agent:'claude',title:'Synthetic',parentId:null,source:'scorer',startedAt:'2026-10-14T10:00:00Z',updatedAt:'2026-10-14T11:00:00Z',counts:{candidateMessages:1,agentMessages:0,toolCalls:0},evidence:[u,m]}]};
 const parsed=analysisRequest.parse(body);
 assert.ok(buildPrompt(parsed).includes('metric:4711'));
 assert.equal(analysisRequest.safeParse({...body,metrics:{['k'.repeat(41)]:1}}).success,false);
 assert.equal(analysisRequest.safeParse({...body,metrics:{V:-0.1}}).success,false);
});
