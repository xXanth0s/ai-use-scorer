import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createPairing,evidenceDigest,publishedReport,readLibrary,readPublished,syncLibrary,validatePublishedReport} from '../services/analysis/agent/lib/library';
import {hostedMarkdown} from '../lib/hosted';
const evidence={id:'session:1',sessionId:'session',line:1,timestamp:'2026-10-14T00:00:00Z',kind:'user' as const,text:'Synthetic evidence only.',agent:'codex' as const,sessionTitle:'Synthetic session'};
const content={title:'Synthetic report',summary:'Summary',findings:[{dimension:'Verification judgment',rating:'Effective',confidence:'High',assessment:'Checked results.',evidenceIds:[evidence.id]}],moments:[{title:'Check',observation:'Observed check',attribution:'Candidate',evidenceIds:[evidence.id]}],opportunities:[{title:'Repeat',suggestion:'Check again',evidenceIds:[evidence.id]}],limitations:['Sample only'],followUps:['What changed?']};
function report(){return publishedReport.parse({id:'report',status:'complete',createdAt:'2026-10-14T00:00:00.000Z',name:'Synthetic',role:'General',sessionIds:['session'],content,error:null,coverage:[],metrics:{sessions:1,candidateMessages:1,toolCalls:0,uniqueTools:0,interruptions:0,completedTasks:0,parallelSessions:0,peakConcurrency:1,longestTaskMs:null,timingSessions:0},timezone:'UTC',activity:[],evidenceIndex:[evidence]});}
const validated=()=>({content:report().content,evidence_ids:[evidence.id],evidence_digests:{[evidence.id]:evidenceDigest(evidence)}});
test('completed publication binds tenant analysis content and every exact cited excerpt',()=>{
 assert.doesNotThrow(()=>validatePublishedReport(report(),validated()));
 assert.throws(()=>validatePublishedReport(report()),/validated analysis/);
 const changed=report();changed.content!.summary='Forged';assert.throws(()=>validatePublishedReport(changed,validated()),/validated analysis/);
 const altered=report();altered.evidenceIndex[0].text='Forged excerpt';assert.throws(()=>validatePublishedReport(altered,validated()),/match the cited/);
 const missing=report();missing.evidenceIndex=[];assert.throws(()=>validatePublishedReport(missing,validated()),/Every report citation/);
 const duplicate=report();duplicate.evidenceIndex.push(evidence);assert.throws(()=>validatePublishedReport(duplicate,validated()),/match the cited/);
 const uncited=report();uncited.evidenceIndex.push({...evidence,id:'session:2'});assert.throws(()=>validatePublishedReport(uncited,validated()),/match the cited/);
 const wrongSession=report();wrongSession.sessionIds=['other'];assert.throws(()=>validatePublishedReport(wrongSession,validated()),/match the cited/);
 assert.throws(()=>validatePublishedReport(report(),{...validated(),evidence_digests:null}),/match the cited/);
 const pending=report();pending.status='running';assert.throws(()=>validatePublishedReport(pending),/Only completed/);pending.content=null;pending.evidenceIndex=[];assert.doesNotThrow(()=>validatePublishedReport(pending));
 const stripped=publishedReport.parse({...report(),snapshot:[{secret:'local transcript'}],path:'/private/local'});assert.ok(!('snapshot'in stripped)&&!('path'in stripped));
});
test('published token metrics preserve optional counts above one hundred million',()=>{
 const value=report();const totals={inputTokens:2e9,outputTokens:3e8,cachedInputTokens:1e9,cacheWriteInputTokens:0,totalTokens:2.3e9};
 const parsed=publishedReport.parse({...value,metrics:{...value.metrics,tokenTotals:totals,tokenUsageSessions:1,finalOutputSessions:1,finalOutputs:3,tokenMetricSessions:1,tokenMetricTotalTokens:2.3e9,tokenMetricFinalOutputs:3,tokensPerFinalOutput:2.3e9/3}});
 assert.deepEqual(parsed.metrics.tokenTotals,totals);assert.equal(parsed.metrics.tokenMetricFinalOutputs,3);
 assert.equal(publishedReport.safeParse({...value,metrics:{...value.metrics,tokenTotals:{...totals,inputTokens:-1}}}).success,false);
});
test('hosted Markdown includes ratings, citations, moments, followups and exact evidence',()=>{
 const value=report();value.evidenceIndex[0].text+=' Code `quoted` and ```fenced```.';const markdown=hostedMarkdown(value);
 assert.ok(markdown.includes(value.evidenceIndex[0].text));
 for(const text of ['Effective','High','Observed check','Check again','What changed?','Synthetic evidence only.','source line 1','[session:1](#evidence-73657373696f6e3a31)','id="evidence-73657373696f6e3a31"'])assert.ok(markdown.includes(text));
});
type Query={query:string;params:string[]};
test('library endpoints authenticate, scope SQL to the device tenant, bound payloads and hash pairings',async()=>{
 const previousFetch=globalThis.fetch,previousDb=process.env.DATABASE_URL;process.env.DATABASE_URL='postgresql://synthetic:synthetic@unit.example/synthetic';
 const queries:Query[]=[];let authenticated=true,pairing=true;
 const result=(rows:Record<string,unknown>[])=>{const names=Object.keys(rows[0]||{});return {fields:names.map(name=>({name,dataTypeID:25})),rows:rows.map(row=>names.map(name=>typeof row[name]==='string'?row[name]:JSON.stringify(row[name]))),rowCount:rows.length};};
 globalThis.fetch=async(_url,init)=>{
  const payload=JSON.parse(String(init?.body));const items:Query[]=payload.queries||[payload];
  const results=items.map(q=>{queries.push(q);if(q.query.includes('device_credentials'))return result(authenticated?[{user_id:'tenant-a',daily_limit:'6'}]:[]);if(q.query.includes('create_collector_pairing'))return {fields:[{name:'accepted',dataTypeID:16}],rows:[[pairing?'t':'f']],rowCount:1};return result([]);});
  return Response.json(payload.queries?{results}:results[0]);
 };
 const req=(value?:unknown)=>new Request('https://service.example/api/sync',{method:value===undefined?'GET':'POST',headers:{authorization:`Bearer hdyai_${'a'.repeat(43)}`,'content-type':'application/json'},body:value===undefined?undefined:JSON.stringify(value)});
 try{
  assert.equal((await readLibrary(new Request('https://service.example/api/library'))).status,401);assert.equal(queries.length,0);
  authenticated=false;assert.equal((await readPublished(req(),'report')).status,401);authenticated=true;
  assert.equal((await readPublished(req(),'other-tenant-report')).status,404);
  const lookup=queries.at(-1)!;assert.match(lookup.query,/user_id=\$1 AND id=\$2/);assert.deepEqual(lookup.params,['tenant-a','other-tenant-report']);
  assert.equal((await syncLibrary(req({sessions:[],reportName:'Synthetic',userId:'tenant-b'}))).status,400);
  assert.equal((await syncLibrary(req({sessions:[],reportName:'Synthetic'}))).status,200);
  const writes=queries.filter(q=>q.query.startsWith('INSERT INTO collector_library')||q.query.startsWith('DELETE FROM published_reports'));assert.ok(writes.length===2);for(const q of writes)assert.equal(q.params[0],'tenant-a');
  assert.equal((await syncLibrary(req({sessions:[],reportName:'Synthetic',report:report()}))).status,400);
  const completedLookup=queries.at(-1)!;assert.match(completedLookup.query,/user_id=\$2 AND status='complete'/);assert.equal(completedLookup.params[1],'tenant-a');
  const oversized=new Request('https://service.example/api/sync',{method:'POST',headers:{authorization:`Bearer hdyai_${'a'.repeat(43)}`,'content-type':'application/json'},body:'x'.repeat(1000001)});assert.equal((await syncLibrary(oversized)).status,413);
  const paired=await createPairing(req({}));assert.equal(paired.status,200);const data=await paired.json();assert.match(data.activationCode,/^invite_[\w-]{43}$/);assert.match(queries.at(-1)!.params[1],/^[0-9a-f]{64}$/);assert.ok(!JSON.stringify(queries).includes(data.activationCode));
  pairing=false;assert.equal((await createPairing(req({}))).status,429);
  globalThis.fetch=async()=>{throw new Error('postgresql://sensitive-credential');};const failure=await readLibrary(req());assert.equal(failure.status,503);assert.doesNotMatch(await failure.text(),/sensitive/);
 }finally{globalThis.fetch=previousFetch;if(previousDb===undefined)delete process.env.DATABASE_URL;else process.env.DATABASE_URL=previousDb;}
});
