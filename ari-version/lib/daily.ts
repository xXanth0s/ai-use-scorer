import { createHash } from 'node:crypto';
import { getSession, getState, listSessions, saveReport, setState } from './db';
import type { Report, Session } from './types';
import { filterTokenUsage } from './metrics';
export const reportTimezone=process.env.REPORT_TIMEZONE||'America/Los_Angeles';
const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:reportTimezone,year:'numeric',month:'2-digit',day:'2-digit'});
export const dayOf=(timestamp:string)=>calendar.format(new Date(timestamp));
export function latestDay(){
 const dates=listSessions().filter(s=>!s.parentId&&s.userCount>0).flatMap(s=>getSession(s.id)!.events.filter(e=>e.kind==='user'&&e.timestamp).map(e=>dayOf(e.timestamp)));
 return dates.sort().at(-1)||null;
}
export function dailySessions(date:string):Session[]{
 return listSessions().filter(s=>!s.parentId&&s.userCount>0).map(s=>getSession(s.id)!).map(s=>{
  const events=s.events.filter(e=>e.timestamp&&dayOf(e.timestamp)===date);
  return {...s,events,tokenUsage:filterTokenUsage(s.tokenUsage,t=>!!t&&dayOf(t)===date),userCount:events.filter(e=>e.kind==='user').length,assistantCount:events.filter(e=>e.kind==='assistant').length,toolCount:events.filter(e=>e.kind==='tool_call').length,tasks:(s.tasks||[]).filter(t=>t.start&&dayOf(t.start)===date).map(t=>({...t,end:t.end&&dayOf(t.end)===date?t.end:null}))};
 }).filter(s=>s.userCount>0);
}
export function prepareDaily(date:string,force=false):{report:Report;fresh:boolean}|null{
 const sessions=dailySessions(date);if(!sessions.length)return null;
 const key=`daily-auto:${reportTimezone}:${date}`;
 const candidateName=String(getState('candidateName')||'Candidate');
 const signature=createHash('sha256').update(JSON.stringify({analyzer:'eve-gpt-6.1-sol-low-v1',candidateName,sessions:sessions.map(s=>({id:s.id,events:s.events,tasks:s.tasks,tokenUsage:s.tokenUsage}))})).digest('hex').slice(0,16);
 const state=getState(key) as {id:string;signature:string}|null;
 // A running snapshot finishes first. A later sync can analyze newly arrived evidence.
 if(state){const r=getStoredReport(state.id);if(r&&(r.status==='queued'||r.status==='running'||(!force&&r.status==='complete'&&state.signature===signature)))return {report:r,fresh:false};}
 const id=`daily-${date}-${signature}${force?`-${Date.now()}`:''}`;
 const report:Report={id,status:'queued',createdAt:new Date().toISOString(),name:candidateName,role:`Daily AI collaboration review · ${date} (${reportTimezone})`,sessionIds:sessions.map(s=>s.id),snapshot:sessions,content:null,error:null,coverage:[]};
 saveReport(report);setState(key,{id,signature});return {report,fresh:true};
}
import { getReport as getStoredReport } from './db';
