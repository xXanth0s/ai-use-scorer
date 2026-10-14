import { hostedApi } from '@/lib/hosted';
import { metrics } from '@/lib/metrics';
import { getReport } from '@/lib/db';
import { exportMarkdown } from '@/lib/report';
import { evidenceIndex } from '@/lib/report-view';
import { assertLocal, failure } from '@/lib/http';
export async function GET(request:Request,{params}:{params:Promise<{id:string}>}) {
  try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const {id}=await params;const report=getReport(id);if(!report)return failure(new Error('Report not found.'),404);
    if(new URL(request.url).searchParams.has('download'))return new Response(exportMarkdown(report),{headers:{'Content-Type':'text/markdown; charset=utf-8','Content-Disposition':`attachment; filename="ai-report-${id.slice(0,8)}.md"`}});
    const {snapshot:_,...data}=report;return Response.json({...data,metrics:metrics(report.snapshot),timezone:process.env.REPORT_TIMEZONE||'America/Los_Angeles',activity:report.snapshot.map(s=>({id:s.id,agent:s.agent,title:s.title,tasks:s.tasks,userCount:s.userCount,toolCount:s.toolCount})),evidenceIndex:evidenceIndex(report)});
  }catch(e){return failure(e);}
}
