import { cookies } from 'next/headers';
import { webIdentity } from '@/lib/web-auth';
import { isHosted } from '@/lib/hosted';
import { redirect } from 'next/navigation';
import { connectionStatus } from '@/lib/analysis-client';
export const dynamic='force-dynamic';
export default async function Page(){if(isHosted()){const identity=await webIdentity().catch(()=>null);redirect(identity&&(await cookies()).has('hdyai_web')?'/daily':'/setup');}redirect((await connectionStatus()).connected?'/daily':'/setup');}
