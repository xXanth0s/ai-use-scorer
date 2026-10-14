import { hostedApi } from '@/lib/hosted';
import { collect } from '@/lib/collector';
import { assertLocal, failure } from '@/lib/http';
export const runtime='nodejs';
let active: Promise<Awaited<ReturnType<typeof collect>>> | null = null;
export async function POST(request:Request) {try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);active ??= collect().finally(()=>{active=null;});return Response.json(await active);}catch(e){return failure(e);}}
