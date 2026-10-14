import { getReport, saveReport } from './db';
import { metrics } from './metrics';
import type { Report, Session } from './types';
import { analyzeRemotely } from './analysis-client';
import { validateContent } from '../services/analysis/agent/lib/contract';
export { reportSchema } from '../services/analysis/agent/lib/contract';
export function analysisMetrics(sessions:Session[]){
 const {tokenTotals,...flat}=metrics(sessions);
 return {...flat,tokenInputTokens:tokenTotals?.inputTokens??null,tokenOutputTokens:tokenTotals?.outputTokens??null,tokenCachedInputTokens:tokenTotals?.cachedInputTokens??null,tokenCacheWriteInputTokens:tokenTotals?.cacheWriteInputTokens??null,tokenTotalTokens:tokenTotals?.totalTokens??null};
}
export function reportInput(sessions: Session[]) {
  const budget = Math.floor(95000 / sessions.length);
  const coverage: string[] = [];
  const input = sessions.map(s => {
    const candidate = s.events.filter(e=>e.kind==='user');
    const remainder = s.events.filter(e=>e.kind!=='user');
    // Keep candidate interventions first; sample agent activity across the whole session.
    const step = Math.max(1,Math.ceil(remainder.length / 65));
    const prioritized = [...candidate, ...remainder.filter((_,i)=> i % step === 0 || i>=remainder.length-6)];
    const chosen: typeof s.events = []; let size = 0;
    for(const e of prioritized) {
      const text = e.text.slice(0,e.kind==='user'?1800:850);
      if(size+text.length+200>budget) continue;
      chosen.push({...e,text,truncated:e.truncated || text.length<e.text.length});size+=text.length+200;
    }
    chosen.sort((a,b)=>a.line-b.line);
    coverage.push(`${s.title}: ${chosen.length} of ${s.events.length} records included; excerpts capped for analysis.${s.malformedLines ? ` ${s.malformedLines} unreadable records.` : ''}${s.truncated ? ' Some stored content is truncated or compacted.' : ''}`);
    return {id:s.id,agent:s.agent,title:s.title,parentId:s.parentId,source:s.source,startedAt:s.startedAt,updatedAt:s.updatedAt,counts:{candidateMessages:s.userCount,agentMessages:s.assistantCount,toolCalls:s.toolCount},evidence:chosen};
  });
  return {input,coverage};
}
export async function generateReport(id:string,analyze=analyzeRemotely){
 const report=getReport(id);if(!report||report.status!=='queued')return;
 const started=performance.now();report.analysis={stage:'preparing',startedAt:new Date().toISOString(),queueMs:Math.max(0,Date.now()-Date.parse(report.createdAt)),engine:'eve',model:'openai/gpt-6.1-sol',reasoning:'low'};report.status='running';saveReport(report);
 try{
  const {input,coverage}=reportInput(report.snapshot);report.coverage=coverage;
  const payload={requestId:id,name:report.name,role:report.role,metrics:analysisMetrics(report.snapshot),input};
  report.analysis.prepareMs=Math.round(performance.now()-started);report.analysis.inputCharacters=JSON.stringify(payload).length;report.analysis.sampledRecords=input.reduce((n,s)=>n+s.evidence.length,0);report.analysis.availableRecords=report.snapshot.reduce((n,s)=>n+s.events.length,0);report.analysis.stage='agent';saveReport(report);
  const agentStarted=performance.now();const result=await analyze(payload);
  report.analysis.agentMs=Math.round(performance.now()-agentStarted);report.analysis.inputTokens=result.usage?.inputTokens;report.analysis.outputTokens=result.usage?.outputTokens;report.analysis.stage='validating';saveReport(report);
  const validating=performance.now();report.content=validateContent(result.content,input.flatMap(s=>s.evidence.map(e=>e.id)));
  report.analysis.validateMs=Math.round(performance.now()-validating);report.analysis.totalMs=Math.round(performance.now()-started)+report.analysis.queueMs;report.analysis.finishedAt=new Date().toISOString();report.analysis.stage='complete';report.status='complete';report.error=null;saveReport(report);
 }catch(error){report.analysis.stage='failed';report.analysis.totalMs=Math.round(performance.now()-started)+report.analysis.queueMs;report.analysis.finishedAt=new Date().toISOString();report.status='failed';report.error=error instanceof Error?error.message:'Report generation failed.';saveReport(report);}
}
export function exportMarkdown(report: Report) {
  const c=report.content; if(!c)return '';
  const evidence=new Map(report.snapshot.flatMap(s=>s.events.map(e=>[e.id,{e,session:s}] as const)));
  const cite=(ids:string[])=>ids.map(id=>`[${id}](#evidence-${id.replace(/:/g,'-')})`).join(', ');
  const used=new Set([...c.findings,...c.moments,...c.opportunities].flatMap(x=>x.evidenceIds));
  return `# ${c.title}\n\n${c.summary}\n\nPrepared for ${report.name}; context: ${report.role || 'General AI collaboration'}.\n\n## Assessment\n\n${c.findings.map(f=>`### ${f.dimension} — ${f.rating}\n\nEvidence strength: ${f.confidence}. ${f.assessment}\n\n${cite(f.evidenceIds)}`).join('\n\n')}\n\n## Key moments\n\n${c.moments.map(m=>`### ${m.title}\n\nAttribution: ${m.attribution}. ${m.observation}\n\n${cite(m.evidenceIds)}`).join('\n\n')}\n\n## Development opportunities\n\n${c.opportunities.map(o=>`- **${o.title}:** ${o.suggestion} ${cite(o.evidenceIds)}`).join('\n')}\n\n## Limitations\n\n${[...c.limitations,...report.coverage].map(x=>`- ${x}`).join('\n')}\n\n## Follow-up questions\n\n${c.followUps.map(x=>`- ${x}`).join('\n')}\n\n## Evidence\n\n${[...used].map(id=>{const x=evidence.get(id);return x?`<a id="evidence-${id.replace(/:/g,'-')}"></a>\n\n### ${id}\n\n${x.session.agent} · ${x.session.title} · source line ${x.e.line} · ${x.e.kind}\n\n\`\`\`text\n${x.e.text.replace(/```/g,'~~~')}\n\`\`\``:'';}).join('\n\n')}`;
}
