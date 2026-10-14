import { hostedApi } from '@/lib/hosted';
import { getReport, getSession } from '@/lib/db';
import { assertLocal, failure } from '@/lib/http';
import { findEvidence } from '@/lib/report-view';
export async function GET(request:Request) {
  try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const q=new URL(request.url).searchParams;const id=q.get('id')||'';const reportId=q.get('report');
    const snapshot=reportId?getReport(reportId)?.snapshot:undefined;const s=snapshot?(findEvidence(snapshot,id)?.session??snapshot.find(s=>s.id===id.split(':')[0])):getSession(id.split(':')[0]);
    if(!s||s.hidden)return failure(new Error('Evidence not found.'),404);
    if(q.has('session'))return Response.json({session:{title:s.title,agent:s.agent,cwd:s.cwd},events:s.events});
    const event=s.events.find(e=>e.id===id);if(!event)return failure(new Error('Evidence not found.'),404);
    return Response.json({session:{title:s.title,agent:s.agent,cwd:s.cwd},event});
  }catch(e){return failure(e);}
}
