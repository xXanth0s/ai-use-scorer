import type { Evidence, Report, Session, TaskSpan, Agent } from './types';
import type { metrics } from './metrics';
type TokenMetricKeys = 'tokenTotals' | 'tokenUsageSessions' | 'finalOutputSessions' | 'finalOutputs' | 'tokenMetricSessions' | 'tokenMetricTotalTokens' | 'tokenMetricFinalOutputs' | 'tokensPerFinalOutput';
export type ReportMetrics = Omit<ReturnType<typeof metrics>, TokenMetricKeys> & Partial<Pick<ReturnType<typeof metrics>, TokenMetricKeys>>;
export type ReportView=Omit<Report,'snapshot'> & {metrics?:ReportMetrics;timezone?:string;activity?:{id:string;agent:Agent;title:string;tasks:TaskSpan[];userCount:number;toolCount:number}[];evidenceIndex?:(Evidence & {sessionTitle:string;agent:Agent})[]};
// Evidence ids are not always `<sessionId>:<line>` (scorer metric evidence is `metric:<id>`), so resolve by scanning.
export function findEvidence(snapshot:Session[],id:string):{session:Session;event:Evidence}|null{for(const session of snapshot){const event=session.events.find(e=>e.id===id);if(event)return {session,event};}return null;}
export function scorerEvidenceIds(report:Pick<Report,'scorerMetrics'>):string[]{return (report.scorerMetrics?.tiles??[]).flatMap(t=>t.evidence.map(e=>e.id));}
export function evidenceIndex(report:Report){const c=report.content;const ids=[...new Set([...(c?[...c.findings,...c.moments,...c.opportunities].flatMap(f=>f.evidenceIds):[]),...scorerEvidenceIds(report)])];return ids.map(id=>{const x=findEvidence(report.snapshot,id);return x?{...x.event,text:x.event.text.slice(0,320),sessionTitle:x.session.title,agent:x.session.agent}:null;}).filter((e):e is NonNullable<typeof e>=>e!==null);}
