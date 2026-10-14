import { hostedApi } from '@/lib/hosted';
import { assertLocal, failure } from '@/lib/http';
import { getState, setState } from '@/lib/db';
import { entireAvailability, entireStatus, enableEntire } from '@/lib/entire-hooks';
export const runtime='nodejs';
export async function GET(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const settings=getState('settings')||{};return Response.json({...await entireAvailability(),workspaces:await Promise.all((settings.entireRepos||[]).map(entireStatus)),nativeFallback:settings.nativeFallback!==false,currentWorkspace:process.cwd()});}catch(e){return failure(e);}}
export async function POST(request:Request){try{const hosted=await hostedApi(request);if(hosted)return hosted;assertLocal(request);const b=await request.json();if(typeof b.repository!=='string'||b.repository.length>1000||!Array.isArray(b.agents)||b.agents.length>2||b.agents.some((a:unknown)=>!['codex','claude-code'].includes(String(a))))throw new Error('Choose a workspace and supported agents.');
 const result=await enableEntire(b.repository,b.agents,b.initialize===true);
 const settings=getState('settings')||{};setState('settings',{...settings,entireRepos:[...new Set([...(settings.entireRepos||[]),result.repository])]});return Response.json(result);
 }catch(e){return failure(e instanceof Error&&'stderr' in e?new Error('Entire could not enable recording. Check that Git has a name/email for an initial commit, and run entire enable in that workspace for details.'):e);}}
