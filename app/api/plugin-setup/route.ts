import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { hostedRequest, isHosted } from '@/lib/hosted';
import { defaultAnalysisService } from '@/lib/onboarding';
export const runtime='nodejs';
export async function POST(request:Request){try{
 if(!isHosted())throw new Error('Plugin pairing is available from the hosted dashboard.');
 const origin=request.headers.get('origin');if(request.headers.get('sec-fetch-site')==='cross-site'||(origin&&new URL(origin).host!==request.headers.get('host')))throw new Error('Cross-origin requests are not allowed.');
 const b=await request.json();if(!['codex','claude'].includes(b.agent))throw new Error('Choose Codex or Claude Code.');
 const release=JSON.parse(await readFile(resolve('public/plugins/release.json'),'utf8'));
 const pair=await hostedRequest(request,'/api/pairings',{method:'POST',body:'{}'});
 const originUrl=new URL(request.url).origin;
 const install=b.agent==='claude'?'Register the extracted marketplace with Claude Code and install how-do-you-ai@how-do-you-ai at user scope using its native plugin commands.':'Register the extracted marketplace with codex plugin marketplace add, then install with codex plugin add how-do-you-ai@how-do-you-ai. If this Codex version lacks plugin add, guide me through its native Plugins interface. Do not pretend registration alone installs it.';
 const message=`Set up the how do you ai plugin for ${b.agent==='codex'?'Codex':'Claude Code'} on this computer. Download ${originUrl}/plugins/how-do-you-ai.zip and verify SHA-256 ${release.sha256} before extracting it into a private user-owned marketplace folder. ${install} Use the plugin’s connect skill to pair it with ${process.env.HDYAI_ANALYSIS_URL||defaultAnalysisService}. Pass this one-time invitation over stdin to the pairing helper, never in command arguments or logs: ${pair.activationCode}. It expires in 15 minutes. I authorize reading supported local Codex/Claude session transcripts and sending session summaries, descriptive metrics, and bounded best-effort-redacted evidence excerpts to how do you ai, Eve and AI Gateway for daily reports. Original transcripts stay local. Preserve existing plugins, configuration and trust approvals. Set up Node.js 24+ from its official distribution if needed. Verify pairing with plugin status; native hook trust review must still be completed. Do not run or install Entire or initialize Git.`;
 return Response.json({message,expiresIn:900,version:release.version},{headers:{'cache-control':'no-store'}});
}catch(e){return Response.json({error:e instanceof Error?e.message:'Could not prepare plugin setup.'},{status:400,headers:{'cache-control':'no-store'}});}}
