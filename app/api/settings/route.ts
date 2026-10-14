import { hostedApi } from '@/lib/hosted';
import { getState, setState } from '@/lib/db';
import { assertLocal, failure } from '@/lib/http';
import { isAbsolute } from 'node:path';
export async function GET(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);return Response.json(getState('settings')||{entireRepos:[]});}catch(e){return failure(e);}}
export async function POST(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const b=await request.json();if(!Array.isArray(b.entireRepos)||b.entireRepos.length>20||b.entireRepos.some((p:unknown)=>typeof p!=='string'||!isAbsolute(p)))throw new Error('Provide absolute repository paths, one per line.');setState('settings',{...(getState('settings')||{}),entireRepos:b.entireRepos,...(typeof b.nativeFallback==='boolean'?{nativeFallback:b.nativeFallback}:{})});return Response.json({ok:true});}catch(e){return failure(e);}}
