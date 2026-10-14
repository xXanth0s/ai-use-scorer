import { hostedApi } from '@/lib/hosted';
import { assertLocal, failure } from '@/lib/http';
import { connectionStatus } from '@/lib/analysis-client';
import { captureStatus, installCaptureHooks } from '@/lib/capture-hooks';
import { getState, listSessions, setState } from '@/lib/db';
export const runtime='nodejs';
export async function GET(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const [connection,agents]=await Promise.all([connectionStatus(),captureStatus()]);return Response.json({connection,agents,reportName:getState('candidateName')||'',sessionCount:listSessions().length,configured:getState('setup')?.complete===true},{headers:{'cache-control':'no-store'}});}catch(e){return failure(e);}}
let active:Promise<unknown>|null=null;
export async function POST(request:Request){try{
 const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);if(!(await connectionStatus()).connected)throw new Error('Open your invitation to connect this device first.');
 active??=(async()=>{const agents=(await captureStatus()).filter(a=>a.detected&&!a.installed).map(a=>a.agent);const results=agents.length?await installCaptureHooks(agents).catch(()=>agents.map(agent=>({agent,installed:false,issue:'Native capture is ready. Hook installation is unavailable on this device.'}))):[];
  setState('setup',{complete:true,configuredAt:new Date().toISOString()});return {results};
 })().finally(()=>{active=null;});return Response.json(await active);
}catch(e){return failure(e);}}
