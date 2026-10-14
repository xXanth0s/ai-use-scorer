import { hostedApi } from '@/lib/hosted';
import { connectionStatus } from '@/lib/analysis-client';
import { collect } from '@/lib/collector';
import { latestDay, prepareDaily, reportTimezone } from '@/lib/daily';
import { startReportWorker } from '@/lib/jobs';
import { assertLocal, failure } from '@/lib/http';
export const runtime='nodejs';
let active:Promise<{id:string|null;date:string|null;timezone:string;fresh:boolean}>|null=null;
export async function POST(request:Request){
 try{
  const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);if(!(await connectionStatus()).connected)return Response.json({error:'Connect this device in Setup & settings first.'},{status:409});const body=await request.json().catch(()=>({}));
  active??=(async()=>{
   await collect();const date=latestDay();if(!date)return {id:null,date:null,timezone:reportTimezone,fresh:false};
   const result=prepareDaily(date,body.force===true);if(!result)return {id:null,date,timezone:reportTimezone,fresh:false};
   if(result.fresh)startReportWorker(result.report.id);
   return {id:result.report.id,date,timezone:reportTimezone,fresh:result.fresh};
  })().finally(()=>{active=null;});
  return Response.json(await active);
 }catch(e){return failure(e);}
}
