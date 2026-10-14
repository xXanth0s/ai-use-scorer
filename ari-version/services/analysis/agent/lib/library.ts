import { z } from 'zod';
import { isDeepStrictEqual } from 'node:util';
import { reportSchema } from './contract';
import { body, device, failure, hash, HttpError, response, secret, sql } from './store';
const id=z.string().regex(/^[A-Za-z0-9:_-]{1,180}$/);
const agent=z.enum(['codex','claude']);
const count=z.number().int().min(0).max(1e8);
const task=z.object({id:z.string().max(200),start:z.string().max(50),end:z.string().max(50).nullable(),aborted:z.boolean(),reason:z.string().max(200).nullable()});
const tokenCount=z.number().int().nonnegative();
const tokens=z.object({inputTokens:tokenCount,outputTokens:tokenCount,cachedInputTokens:tokenCount,cacheWriteInputTokens:tokenCount,totalTokens:tokenCount});
const tokenUsage=z.object({records:z.array(tokens.extend({id:z.string().max(200),timestamp:z.string().max(50)})).max(10000),finalOutputs:z.array(z.object({id:z.string().max(200),timestamp:z.string().max(50)})).max(10000),finalOutputCountKnown:z.boolean()});
const summary=z.object({id,nativeId:z.string().max(180),agent,title:z.string().max(300),startedAt:z.string().max(50),updatedAt:z.string().max(50),parentId:z.string().max(180).nullable(),source:z.string().max(100),hidden:z.boolean(),userCount:count,assistantCount:count,toolCount:count,tools:z.array(z.string().max(200)).max(200),malformedLines:count,truncated:z.boolean(),tasks:z.array(task).max(2000),tokenUsage:tokenUsage.optional()});
const evidence=z.object({id,sessionId:id,line:count,timestamp:z.string().max(50),kind:z.enum(['user','assistant','tool_call','tool_result','metric']),text:z.string().max(1800),tool:z.string().max(300).optional(),callId:z.string().max(200).optional(),error:z.boolean().optional(),truncated:z.boolean().optional(),sessionTitle:z.string().max(300),agent});
// Scorer harness KPIs (docs/spec/bridge-contract.md). Self-contained excerpts; never part of the validated content.
const metricValue=z.number().nonnegative().nullable();
const scorerEvidence=z.object({id,sessionId:id,excerpt:z.string().max(320)}).strict();
const scorerTile=z.object({key:z.string().max(40),label:z.string().max(100),value:z.number().nullable(),unit:z.enum(['share','min','count','tokens_k','usd']),display:z.string().max(40),sub:z.string().max(200).optional(),baseline:z.number().nullable().optional(),evidence:z.array(scorerEvidence).max(8)}).strict();
export const scorerMetrics=z.object({day:z.string().regex(/^\d{4}-\d{2}-\d{2}$/),tz:z.string().max(100),source:z.literal('scorer'),demo:z.boolean(),verdict:z.string().max(500),tiles:z.array(scorerTile).max(12),breakdown:z.object({cCauses:z.record(z.string().max(40),count),costByModel:z.array(z.object({model:z.string().max(100),usd:z.number().nonnegative(),tokens:z.number().nonnegative()}).strict()).max(20)}).strict(),flat:z.record(z.string().max(40),metricValue).refine(r=>Object.keys(r).length<=100,'Too many metrics.'),lanes:z.array(z.object({sessionId:id,agent,repo:z.string().max(300).nullable(),branch:z.string().max(300).nullable(),cwd:z.string().max(300)}).strict()).max(500).optional(),interrupts:z.array(z.object({sessionId:id,at:z.string().max(50),evidenceId:id}).strict()).max(2000).optional()}).strict();
export const publishedReport=z.object({id,status:z.enum(['queued','running','complete','failed']),createdAt:z.string().datetime(),name:z.string().max(100),role:z.string().max(1000),sessionIds:z.array(id).max(500),content:reportSchema.nullable(),error:z.string().max(1000).nullable(),coverage:z.array(z.string().max(1000)).max(500),metrics:z.object({sessions:count,candidateMessages:count,toolCalls:count,uniqueTools:count,interruptions:count,completedTasks:count,parallelSessions:count,peakConcurrency:count,longestTaskMs:z.number().nonnegative().nullable(),timingSessions:count,tokenTotals:tokens.nullable().optional(),tokenUsageSessions:tokenCount.optional(),finalOutputSessions:tokenCount.optional(),finalOutputs:tokenCount.nullable().optional(),tokenMetricSessions:tokenCount.optional(),tokenMetricTotalTokens:tokenCount.nullable().optional(),tokenMetricFinalOutputs:tokenCount.nullable().optional(),tokensPerFinalOutput:z.number().nonnegative().nullable().optional()}),timezone:z.string().max(100),activity:z.array(z.object({id,agent,title:z.string().max(300),tasks:z.array(task).max(2000),userCount:count,toolCount:count})).max(500),evidenceIndex:z.array(evidence).max(200),analysis:z.object({stage:z.enum(['preparing','agent','validating','complete','failed','codex']),startedAt:z.string().max(50),finishedAt:z.string().max(50).optional(),queueMs:count,prepareMs:count.optional(),agentMs:count.optional(),validateMs:count.optional(),totalMs:count.optional(),engine:z.literal('eve').optional(),model:z.string().max(100).optional(),reasoning:z.literal('low').optional(),inputCharacters:count.optional(),sampledRecords:count.optional(),availableRecords:count.optional(),inputTokens:count.optional(),outputTokens:count.optional()}).optional(),scorerMetrics:scorerMetrics.optional()});
// Match only fields supplied to the analysis, in a stable order. Keep raw excerpts out of storage.
export function evidenceDigest(e: {id:string;sessionId:string;line:number;timestamp:string;kind:string;text:string;tool?:string;callId?:string;error?:boolean;truncated?:boolean}) {
 return hash(JSON.stringify([e.id,e.sessionId,e.line,e.timestamp,e.kind,e.text,e.tool??null,e.callId??null,e.error??null,e.truncated??null]));
}
export function validatePublishedReport(report:z.infer<typeof publishedReport>, job?:{content:unknown;evidence_ids:string[];evidence_digests:Record<string,string>|null}) {
 const complete=report.status==='complete';
 if(report.scorerMetrics){const sessions=new Set(report.sessionIds);const m=report.scorerMetrics;if(m.tiles.some(t=>t.evidence.some(e=>!sessions.has(e.sessionId)))||(m.lanes??[]).some(l=>!sessions.has(l.sessionId))||(m.interrupts??[]).some(i=>!sessions.has(i.sessionId)))throw new HttpError(400,'Scorer metric evidence must belong to the report sessions.');}
 if(!complete){
  if(report.content!==null||report.evidenceIndex.length)throw new HttpError(400,'Only completed assessments may publish content and evidence.');
  return;
 }
 if(!report.content||!job||!isDeepStrictEqual(job.content,report.content))throw new HttpError(400,'A completed report must match its validated analysis.');
 const used=new Set([...report.content.findings,...report.content.moments,...report.content.opportunities].flatMap(item=>item.evidenceIds));
 const allowed=new Set(job.evidence_ids),seen=new Set<string>(),sessions=new Set(report.sessionIds);
 for(const e of report.evidenceIndex){
  if(seen.has(e.id)||!used.has(e.id)||!allowed.has(e.id)||!sessions.has(e.sessionId)||job.evidence_digests?.[e.id]!==evidenceDigest(e))throw new HttpError(400,'Published evidence must match the cited analysis excerpts.');
  seen.add(e.id);
 }
 if([...used].some(id=>!seen.has(id)))throw new HttpError(400,'Every report citation must include its evidence excerpt.');
}
const syncSchema=z.object({sessions:z.array(summary).max(200),report:publishedReport.optional(),reportName:z.string().max(100)}).strict();
export async function syncLibrary(request:Request){try{
 const d=await device(request);const b=syncSchema.safeParse(await body(request,1000000));if(!b.success)throw new HttpError(400,'Invalid collector sync.');const data=b.data,db=sql();
 if(data.report){
  let validated;
  if(data.report.status==='complete'){
   const [row]=await db`SELECT content,evidence_ids,evidence_digests FROM analysis_jobs WHERE id=${hash(`${d.userId}:${data.report.id}`)} AND user_id=${d.userId} AND status='complete'`;
   validated=row as {content:unknown;evidence_ids:string[];evidence_digests:Record<string,string>|null}|undefined;
  }
  validatePublishedReport(data.report,validated);
 }
 const queries=[db`INSERT INTO collector_library(user_id,sessions,report_name,last_seen) VALUES(${d.userId},${JSON.stringify(data.sessions)}::jsonb,${data.reportName},now()) ON CONFLICT(user_id) DO UPDATE SET sessions=excluded.sessions,report_name=excluded.report_name,last_seen=now()`];
 if(data.report)queries.push(db`INSERT INTO published_reports(user_id,id,data,created_at) VALUES(${d.userId},${data.report.id},${JSON.stringify(data.report)}::jsonb,${data.report.createdAt}) ON CONFLICT(user_id,id) DO UPDATE SET data=excluded.data WHERE published_reports.data->>'status'<>'complete' AND (published_reports.data->>'status'<>'failed' OR excluded.data->>'status'='complete') AND (published_reports.data->>'status'<>'running' OR excluded.data->>'status'<>'queued')`);
 queries.push(db`DELETE FROM published_reports WHERE user_id=${d.userId} AND id IN (SELECT id FROM published_reports WHERE user_id=${d.userId} ORDER BY created_at DESC OFFSET 100)`);
 await db.transaction(queries);return response({synced:true});
}catch(e){return failure(e);}}
export async function readLibrary(request:Request){try{
 const d=await device(request);const db=sql();const [[library],reports]=await Promise.all([db`SELECT sessions,report_name,last_seen FROM collector_library WHERE user_id=${d.userId}`,db`SELECT (data - 'activity' - 'evidenceIndex' - 'metrics' - 'coverage' - 'analysis' - 'scorerMetrics') || jsonb_build_object('content',CASE WHEN data->'content'='null'::jsonb THEN 'null'::jsonb ELSE jsonb_build_object('title',left(data->'content'->>'title',300)) END) AS data FROM published_reports WHERE user_id=${d.userId} ORDER BY created_at DESC`]);
 return response({sessions:library?.sessions||[],reportName:library?.report_name||'',collector:library?{syncedAt:library.last_seen}:null,reports:reports.map(r=>r.data)});
}catch(e){return failure(e);}}
export async function readPublished(request:Request,reportId:string){try{const d=await device(request);if(!id.safeParse(reportId).success)throw new HttpError(400,'Invalid report ID.');const [row]=await sql()`SELECT data FROM published_reports WHERE user_id=${d.userId} AND id=${reportId}`;if(!row)throw new HttpError(404,'Report not found.');return response(row.data);}catch(e){return failure(e);}}
export async function createPairing(request:Request){try{
 const d=await device(request),code=`invite_${secret()}`,db=sql();
 // Lock account to bound pending pairings even when several browser requests race.
 const result=await db`SELECT create_collector_pairing(${d.userId},${hash(code)}) AS accepted`;
 if(!result[0]?.accepted)throw new HttpError(429,'Use the current download, or wait 15 minutes before requesting another.');
 return response({activationCode:code,expiresIn:900});
}catch(e){return failure(e);}}
