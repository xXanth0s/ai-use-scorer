import { writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { invitationLink } from '../lib/onboarding';
import { serviceUrl } from '../lib/analysis-client';
// Administrator only: load the ignored service env with Node's --env-file flag.
const [action,url,userId,dashboardOrigin]=process.argv.slice(2);
if(!['invite','revoke'].includes(action)||!url||!userId)throw new Error('Usage: npm run platform:admin -- invite|revoke <service-origin> <platform-user-id>');
const admin=process.env.PLATFORM_ADMIN_KEY;if(!admin)throw new Error('Load PLATFORM_ADMIN_KEY from your private server environment.');
const r=await fetch(`${serviceUrl(url)}/api/admin/${action==='invite'?'invites':`users/${encodeURIComponent(userId)}`}`,{method:action==='invite'?'POST':'DELETE',headers:{authorization:`Bearer ${admin}`,'content-type':'application/json'},...(action==='invite'?{body:JSON.stringify({userId})}:{}),redirect:'error'});
if(!r.ok)throw new Error(`Administration request failed (${r.status}).`);
if(action==='invite'){const data=await r.json();await mkdir('.local',{recursive:true,mode:0o700});const file=resolve('.local',`activation-${crypto.randomUUID()}.json`);await writeFile(file,JSON.stringify({url:serviceUrl(url),...data,setupLink:invitationLink(data.activationCode,dashboardOrigin)},null,2),{mode:0o600,flag:'wx'});console.log(`One-time activation saved privately to ${file}. Expires in 15 minutes. Share only with the approved platform user.`);}else console.log('Platform user access revoked.');
