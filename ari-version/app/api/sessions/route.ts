import { hostedApi } from '@/lib/hosted';
import { listSessions } from '@/lib/db';
import { assertLocal, failure } from '@/lib/http';
export const runtime='nodejs';
export async function GET(request:Request) {try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);return Response.json(listSessions());}catch(e){return failure(e);}}
