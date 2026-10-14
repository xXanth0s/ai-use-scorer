import { reportInput } from './report';
import { metrics } from './metrics';
import { redact } from './parser';
import type { Report, SessionSummary } from './types';
export function sessionSummaries(sessions:SessionSummary[]){return sessions.slice(0,200).map(s=>({id:s.id,nativeId:s.nativeId,agent:s.agent,title:redact(s.title).slice(0,300),startedAt:s.startedAt,updatedAt:s.updatedAt,parentId:s.parentId,source:s.source,hidden:s.hidden,userCount:s.userCount,assistantCount:s.assistantCount,toolCount:s.toolCount,tools:s.tools.slice(0,200).map(t=>redact(t).slice(0,200)),malformedLines:s.malformedLines,truncated:s.truncated,tasks:s.tasks.slice(0,2000).map(t=>({...t,reason:t.reason?redact(t.reason).slice(0,300):null}))}));}

export function publishedView(report:Report){
 const used=new Set(report.content?[...report.content.findings,...report.content.moments,...report.content.opportunities].flatMap(f=>f.evidenceIds):[]);
 if(used.size>200)throw new Error('Report cites more than the publication evidence limit.');
 const sample=reportInput(report.snapshot).input;
 const {snapshot:_,...data}=report;
 return {...data,error:report.error?redact(report.error).slice(0,1000):null,metrics:metrics(report.snapshot),timezone:process.env.REPORT_TIMEZONE||'America/Los_Angeles',activity:report.snapshot.map(s=>({id:s.id,agent:s.agent,title:redact(s.title).slice(0,300),tasks:s.tasks.slice(0,2000).map(t=>({...t,reason:t.reason?redact(t.reason).slice(0,300):null})),userCount:s.userCount,toolCount:s.toolCount})),evidenceIndex:sample.flatMap(s=>s.evidence.filter(e=>used.has(e.id)).map(e=>({...e,sessionTitle:redact(s.title).slice(0,300),agent:s.agent})))};
}
