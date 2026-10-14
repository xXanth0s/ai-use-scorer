import { hostedApi } from '@/lib/hosted';
import { assertLocal, failure } from '@/lib/http';
import { connectAnalysis, connectionStatus, disconnectAnalysis } from '@/lib/analysis-client';
import { getState, setState } from '@/lib/db';
export const runtime='nodejs';
export async function GET(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);return Response.json({...await connectionStatus(),reportName:getState('candidateName')||'Candidate'},{headers:{'cache-control':'no-store'}});}catch(e){return failure(e);}}
export async function PATCH(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const b=await request.json();if(typeof b.reportName!=='string'||b.reportName.length>100)throw new Error('Enter a report name of up to 100 characters.');setState('candidateName',b.reportName.trim()||'Candidate');return Response.json({reportName:b.reportName.trim()||'Candidate'});}catch(e){return failure(e);}}
export async function POST(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const b=await request.json();if(typeof b.url!=='string'||b.url.length>500||typeof b.activationCode!=='string'||b.activationCode.length>100)throw new Error('Enter the service URL and activation code.');return Response.json(await connectAnalysis(b.url,b.activationCode));}catch(e){return failure(e);}}
export async function DELETE(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);await disconnectAnalysis();return Response.json({connected:false});}catch(e){return failure(e);}}
