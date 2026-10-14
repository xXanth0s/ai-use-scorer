import { hostedApi } from '@/lib/hosted';
import { randomUUID } from 'node:crypto';
import { startReportWorker } from '@/lib/jobs';
import { getSession, listReports, saveReport } from '@/lib/db';
import { assertLocal, failure } from '@/lib/http';
import type { Report, Session } from '@/lib/types';
export const runtime='nodejs';
export async function GET(request:Request) {try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);return Response.json(listReports());}catch(e){return failure(e);}}
export async function POST(request:Request) {
  try {
    const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request); const b=await request.json();
    if(!Array.isArray(b.sessionIds)||!b.sessionIds.length||b.sessionIds.length>12||b.sessionIds.some((v:unknown)=>typeof v!=='string'))throw new Error('Select between 1 and 12 sessions.');
    if(typeof b.name!=='string'||b.name.length>100||typeof b.role!=='string'||b.role.length>1000)throw new Error('Invalid report details.');
    const selected=[...new Set<string>(b.sessionIds)]; const sessions=selected.map(id=>getSession(id));
    if(sessions.some(s=>!s||s.hidden))throw new Error('A selected session is unavailable. Sync and try again.');
    if(listReports().filter(r=>r.status==='running'||r.status==='queued').length>=2)throw new Error('Two reports are already processing. Wait for one to finish.');
    const report:Report={id:randomUUID(),status:'queued',createdAt:new Date().toISOString(),name:b.name.trim()||'Candidate',role:b.role.trim(),sessionIds:selected,snapshot:sessions as Session[],content:null,error:null,coverage:[]};
    saveReport(report);
    startReportWorker(report.id);
    return Response.json({id:report.id},{status:202});
  }catch(e){return failure(e);}
}
