import type { Session, SessionSummary, SessionTokenUsage, TokenTotals } from './types';
export function filterTokenUsage(usage: SessionTokenUsage | undefined, include: (timestamp:string)=>boolean): SessionTokenUsage | undefined {
  return usage ? {...usage,records:usage.records.filter(r=>include(r.timestamp)),finalOutputs:usage.finalOutputs.filter(r=>include(r.timestamp))} : undefined;
}
function sumTokens(sessions: (Session | SessionSummary)[]): TokenTotals | null {
  const records=sessions.flatMap(s=>s.tokenUsage?.records??[]);
  return records.length ? records.reduce((sum,r)=>({inputTokens:sum.inputTokens+r.inputTokens,outputTokens:sum.outputTokens+r.outputTokens,cachedInputTokens:sum.cachedInputTokens+r.cachedInputTokens,cacheWriteInputTokens:sum.cacheWriteInputTokens+r.cacheWriteInputTokens,totalTokens:sum.totalTokens+r.totalTokens}),{inputTokens:0,outputTokens:0,cachedInputTokens:0,cacheWriteInputTokens:0,totalTokens:0}) : null;
}
export function metrics(sessions: (Session | SessionSummary)[]) {
  const measured=sessions.filter(s=>s.tokenUsage?.records.length);
  const knownFinals=sessions.filter(s=>s.tokenUsage?.finalOutputCountKnown);
  const matched=measured.filter(s=>s.tokenUsage?.finalOutputCountKnown);
  const tokenTotals=sumTokens(measured), matchedTotals=sumTokens(matched);
  const tokenMetricFinalOutputs=matched.length?matched.reduce((n,s)=>n+s.tokenUsage!.finalOutputs.length,0):null;
  const tokenMetrics={tokenTotals,tokenUsageSessions:measured.length,finalOutputSessions:knownFinals.length,finalOutputs:knownFinals.length?knownFinals.reduce((n,s)=>n+s.tokenUsage!.finalOutputs.length,0):null,tokenMetricSessions:matched.length,tokenMetricTotalTokens:matchedTotals?.totalTokens??null,tokenMetricFinalOutputs,tokensPerFinalOutput:matchedTotals && tokenMetricFinalOutputs ? matchedTotals.totalTokens/tokenMetricFinalOutputs : null};
  const tasks=sessions.flatMap(s=>(s.tasks ?? []).filter(t=>t.end).map(t=>({...t,session:s.id,startMs:Date.parse(t.start),endMs:Date.parse(t.end!)}))).filter(t=>Number.isFinite(t.startMs)&&Number.isFinite(t.endMs)&&t.endMs>=t.startMs);
  const boundaries=tasks.flatMap(t=>[{time:t.startMs,session:t.session,delta:1},{time:t.endMs,session:t.session,delta:-1}]).sort((a,b)=>a.time-b.time||a.delta-b.delta);
  const active=new Map<string,number>();let peak=0;const parallel=new Set<string>();
  for(const b of boundaries){active.set(b.session,(active.get(b.session)||0)+b.delta);if(active.get(b.session)===0)active.delete(b.session);peak=Math.max(peak,active.size);if(active.size>1)for(const id of active.keys())parallel.add(id);}
  const complete=tasks.filter(t=>!t.aborted);
  const noInterventions=complete.filter(t=>{
    const s=sessions.find(s=>s.id===t.session);
    if(!s||!('events' in s))return true;
    const firstActivity=s.events.find(e=>e.kind!=='user'&&Date.parse(e.timestamp)>=t.startMs&&Date.parse(e.timestamp)<t.endMs);
    return !firstActivity||!s.events.some(e=>e.kind==='user'&&Date.parse(e.timestamp)>Date.parse(firstActivity.timestamp)&&Date.parse(e.timestamp)<t.endMs);
  });
  return {...tokenMetrics,sessions:sessions.length,candidateMessages:sessions.reduce((n,s)=>n+s.userCount,0),toolCalls:sessions.reduce((n,s)=>n+s.toolCount,0),uniqueTools:new Set(sessions.flatMap(s=>s.tools)).size,interruptions:tasks.filter(t=>t.aborted).length,completedTasks:complete.length,parallelSessions:parallel.size,peakConcurrency:peak,longestTaskMs:noInterventions.length?Math.max(...noInterventions.map(t=>t.endMs-t.startMs)):null,timingSessions:new Set(tasks.map(t=>t.session)).size};
}
export function duration(ms:number|null){if(ms===null)return '—';const sec=Math.round(ms/1000);return sec>=60?`${Math.floor(sec/60)}m ${sec%60}s`:`${sec}s`;}
