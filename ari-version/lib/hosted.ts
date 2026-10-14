import { defaultAnalysisService } from './onboarding';
import type { ReportContent } from './types';
import { webIdentity, sealWebCredential, unsealWebCredential } from './web-auth';
import { createHmac } from 'node:crypto';

export type HostedAuth = {identity:()=>Promise<{userId:string;sessionId?:string}|null>;seal:(userId:string,credential:string)=>string;unseal:(userId:string,value:string)=>string|null};
const webAuth:HostedAuth={identity:webIdentity,seal:sealWebCredential,unseal:unsealWebCredential};
async function identity(auth:HostedAuth){try{return await auth.identity();}catch{throw new HostedError(503,'Sign-in is temporarily unavailable.');}}

export const isHosted = () => process.env.HDYAI_HOSTED === '1';
const cookieName = 'hdyai_web';
class HostedError extends Error {
 constructor(public status:number,message:string){super(message);}
}
function service() {
 const url=new URL(process.env.HDYAI_ANALYSIS_URL||defaultAnalysisService);
 if(url.username||url.password||url.search||url.hash||url.pathname!=='/'||(url.protocol!=='https:'&&!(url.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(url.hostname))))throw new HostedError(503,'The hosted service is unavailable.');
 return url.origin;
}
function token(request:Request){return request.headers.get('cookie')?.split(';').map(s=>s.trim()).find(s=>s.startsWith(`${cookieName}=`))?.slice(cookieName.length+1)||'';}
function sameOrigin(request:Request){
 const origin=request.headers.get('origin'),site=request.headers.get('sec-fetch-site');
 const denied=()=>{throw new HostedError(403,'Cross-origin requests are not allowed.');};
 if(site==='cross-site'||site==='same-site')denied();
 if(origin){
  let parsed:URL;try{parsed=new URL(origin);}catch{ return denied(); }
  const expected=new URL(request.url);expected.host=request.headers.get('host')||expected.host;
  // Production requests may arrive over HTTP from Vercel's trusted reverse proxy.
  if(process.env.NODE_ENV==='production')expected.protocol='https:';
  if(parsed.origin!==expected.origin||origin!==parsed.origin)denied();
 }else if(!['GET','HEAD'].includes(request.method)&&site!=='same-origin')denied();
}
const upstreamMessage=(status:number)=>status===401?'Open setup to reconnect your account.':status===403?'Account access has been disabled or this connection was revoked. Contact the platform administrator.':status===404?'Report not found.':status===429?'The account limit has been reached. Try again later.':status===400?'The hosted service could not accept this request.':'The hosted service is temporarily unavailable.';
async function upstream(path:string,init:RequestInit={}){
 let r:Response;
 try{r=await fetch(`${service()}${path}`,{...init,cache:'no-store',redirect:'error',signal:AbortSignal.timeout(30000)});}catch{throw new HostedError(503,'The hosted service is temporarily unavailable.');}
 if(!r.ok)throw new HostedError([400,401,403,404,409,413,415,429].includes(r.status)?r.status:503,upstreamMessage(r.status));
 try{return await r.json();}catch{throw new HostedError(503,'The hosted service returned an invalid response.');}
}
export async function hostedRequest(request:Request,path:string,init:RequestInit={},auth:HostedAuth=webAuth){
 sameOrigin(request);
 // Credentials may only be forwarded to the service's explicit device API.
 const method=init.method||'GET';
 let published=false;try{published=path.startsWith('/api/published/')&&/^[A-Za-z0-9:_-]{1,180}$/.test(decodeURIComponent(path.slice('/api/published/'.length)));}catch{/* Invalid encoding is not a supported path. */}
 if(!((method==='GET'&&(/^\/api\/(device|library)$/.test(path)||published))||(method==='DELETE'&&path==='/api/device')||(method==='POST'&&path==='/api/pairings')))throw new HostedError(400,'Unsupported hosted request.');
 const signedIn=await identity(auth);if(!signedIn)throw new HostedError(401,'Sign in to connect this browser.');
 const credential=auth.unseal(signedIn.userId,token(request))||'';
 if(!/^hdyai_[A-Za-z0-9_-]{43}$/.test(credential))throw new HostedError(401,'Open setup to connect your account.');
 return upstream(path,{...init,headers:{'content-type':'application/json',authorization:`Bearer ${credential}`}});
}
const json=(value:unknown,status=200,extra:Record<string,string>={})=>Response.json(value,{status,headers:{'cache-control':'no-store','x-content-type-options':'nosniff',...extra}});
const cookie=(value:string,age:number)=>`${cookieName}=${value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${age}${process.env.NODE_ENV==='production'?'; Secure':''}`;
async function activationBody(request:Request){
 if(!request.headers.get('content-type')?.startsWith('application/json'))throw new HostedError(415,'Use JSON.');
 const reader=request.body?.getReader();if(!reader)throw new HostedError(400,'Missing invitation.');
 const chunks:Uint8Array[]=[];let size=0;
 try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>2048){await reader.cancel();throw new HostedError(413,'The invitation payload is too large.');}chunks.push(value);}}finally{reader.releaseLock();}
 try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw new HostedError(400,'Invalid invitation.');}
}
type Published={name:string;role:string;coverage:string[];content:ReportContent|null;evidenceIndex:{id:string;agent:string;sessionTitle:string;line:number;kind:string;text:string}[]};
export function hostedMarkdown(report:Published){
 const c=report.content;if(!c)return '# Daily report\n\nAnalysis in progress.\n';
 const anchor=(id:string)=>`evidence-${Buffer.from(id).toString('hex')}`;
 const cite=(ids:string[])=>ids.map(id=>`[${id}](#${anchor(id)})`).join(', ');
 const used=new Set([...c.findings,...c.moments,...c.opportunities].flatMap(x=>x.evidenceIds));
 const fenced=(text:string)=>{const fence='`'.repeat(Math.max(3,...Array.from(text.matchAll(/`+/g),match=>match[0].length+1)));return `${fence}text\n${text}\n${fence}`;};
 return `# ${c.title}\n\n${c.summary}\n\nPrepared for ${report.name}; context: ${report.role||'General AI collaboration'}.\n\n## Assessment\n\n${c.findings.map(f=>`### ${f.dimension} — ${f.rating}\n\nEvidence strength: ${f.confidence}. ${f.assessment}\n\n${cite(f.evidenceIds)}`).join('\n\n')}\n\n## Key moments\n\n${c.moments.map(m=>`### ${m.title}\n\nAttribution: ${m.attribution}. ${m.observation}\n\n${cite(m.evidenceIds)}`).join('\n\n')}\n\n## Development opportunities\n\n${c.opportunities.map(o=>`- **${o.title}:** ${o.suggestion} ${cite(o.evidenceIds)}`).join('\n')}\n\n## Limitations\n\n${[...c.limitations,...report.coverage].map(x=>`- ${x}`).join('\n')}\n\n## Follow-up questions\n\n${c.followUps.map(x=>`- ${x}`).join('\n')}\n\n## Evidence\n\n${report.evidenceIndex.filter(e=>used.has(e.id)).map(e=>`<a id="${anchor(e.id)}"></a>\n\n### ${e.id}\n\n${e.agent} · ${e.sessionTitle} · source line ${e.line} · ${e.kind}\n\n${fenced(e.text)}`).join('\n\n')}\n`;
}
export async function hostedApi(request:Request,auth:HostedAuth=webAuth):Promise<Response|null>{
 if(!isHosted())return null;
 try{
  sameOrigin(request);const url=new URL(request.url),path=url.pathname,method=request.method;
  const signedIn=await identity(auth),authStatus={authEnabled:true,signedIn:!!signedIn};
  if(!signedIn){
   if(method==='GET'&&path==='/api/setup')return json({...authStatus,hosted:true,connection:{connected:false},configured:false,collector:null,sessionCount:0,reportCount:0});
   if(method==='GET'&&path==='/api/analysis-connection')return json({...authStatus,connected:false});
   throw new HostedError(401,'Sign in to connect this browser.');
  }
  const authenticatedRequest=(request:Request,path:string,init:RequestInit={})=>hostedRequest(request,path,init,auth);
  const methods=path==='/api/analysis-connection'?['GET','POST','DELETE','PATCH']:path==='/api/setup'?['GET','POST']:path==='/api/reports'?['GET','POST']:['/api/settings','/api/entire','/api/collect','/api/daily'].includes(path)?['GET','POST']:['GET'];
  if(!methods.includes(method))return json({error:'Method not allowed.'},405,{allow:methods.join(', ')});
  if(path==='/api/analysis-connection'){
   if(method==='POST'){
    const b=await activationBody(request);if(!/^invite_[A-Za-z0-9_-]{43}$/.test(b?.activationCode))throw new HostedError(400,'Open a valid invitation.');
    const d=await upstream('/api/activate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({activationCode:b.activationCode})});
    if(!/^hdyai_[A-Za-z0-9_-]{43}$/.test(d.deviceToken))throw new HostedError(503,'Could not connect this browser.');
    return json({...authStatus,connected:true,userId:d.userId,expiresAt:d.expiresAt},200,{'set-cookie':cookie(auth.seal(signedIn.userId,d.deviceToken),2592000)});
   }
   if(method==='DELETE'){
    try{await authenticatedRequest(request,'/api/device',{method:'DELETE'});}catch(e){if(!(e instanceof HostedError)||![401,403].includes(e.status))throw e;}
    return json({connected:false},200,{'set-cookie':cookie('',0)});
   }
   if(method==='PATCH')throw new HostedError(409,'Change the report name through your local collector for now.');
   try{const d=await authenticatedRequest(request,'/api/device');return json({...authStatus,connected:true,userId:d.userId,url:service(),reportName:'',expiresAt:null});}catch(e){if(e instanceof HostedError&&[401,403].includes(e.status))return json({...authStatus,connected:false});throw e;}
  }
  if(path==='/api/setup'){
   if(method==='POST'){
    // Keep an existing invited account connected; new signups get their own restricted tenant.
    if(auth.unseal(signedIn.userId,token(request))){
     try{const d=await authenticatedRequest(request,'/api/device');return json({connected:true,userId:d.userId,dailyLimit:d.dailyLimit});}
     catch(e){if(!(e instanceof HostedError)||e.status!==401)throw e;}
    }
    const key=process.env.WEB_PROVISIONING_KEY;
    if(!key||key.length<32)throw new HostedError(503,'Account setup is temporarily unavailable.');
    if(!/^user_[A-Za-z0-9_-]{1,180}$/.test(signedIn.userId)||!/^sess_[A-Za-z0-9_-]{1,180}$/.test(signedIn.sessionId||''))throw new HostedError(401,'Sign in again to finish account setup.');
    // IDs come exclusively from the verified Clerk session, never the request body.
    const payload=JSON.stringify({userId:signedIn.userId,sessionId:signedIn.sessionId});
    const timestamp=String(Math.floor(Date.now()/1000));
    const signature=createHmac('sha256',key).update(`${timestamp}.${payload}`).digest('hex');
    const d=await upstream('/api/web/provision',{method:'POST',headers:{'content-type':'application/json','x-hdyai-timestamp':timestamp,'x-hdyai-signature':signature},body:payload});
    if(!/^hdyai_[A-Za-z0-9_-]{43}$/.test(d.deviceToken)||d.userId!==`clerk:${signedIn.userId}`||!Number.isInteger(d.dailyLimit)||d.dailyLimit<1||d.dailyLimit>20)throw new HostedError(503,'Could not finish account setup.');
    return json({...authStatus,connected:true,userId:d.userId,dailyLimit:d.dailyLimit,expiresAt:d.expiresAt},200,{'set-cookie':cookie(auth.seal(signedIn.userId,d.deviceToken),2592000)});
   }
   try{const [d,library]=await Promise.all([authenticatedRequest(request,'/api/device'),authenticatedRequest(request,'/api/library')]);return json({...authStatus,hosted:true,connection:{connected:true,userId:d.userId,dailyLimit:d.dailyLimit,usedToday:d.usedToday,collectorLimit:d.collectorLimit||3},configured:!!library.collector,collector:library.collector,sessionCount:library.sessions.length,reportCount:library.reports.length});}catch(e){if(e instanceof HostedError&&[401,403].includes(e.status))return json({...authStatus,hosted:true,connection:{connected:false},configured:false,collector:null,sessionCount:0,reportCount:0});throw e;}
  }
  if(['/api/settings','/api/entire','/api/collect'].includes(path)&&method==='POST')throw new HostedError(409,'The collector manages capture settings on your computer. Keep it running to receive new sessions.');
  if(path==='/api/settings')return json({entireRepos:[],nativeFallback:true});
  if(path==='/api/entire')return json({available:false,version:null,workspaces:[],nativeFallback:true,currentWorkspace:''});
  if(path==='/api/reports'&&method==='POST')throw new HostedError(409,'Daily reports are generated automatically through the collector.');
  if(path.startsWith('/api/reports/')){
   const id=decodeURIComponent(path.slice('/api/reports/'.length));if(!/^[A-Za-z0-9:_-]{1,180}$/.test(id))throw new HostedError(400,'Invalid report ID.');
   const report=await authenticatedRequest(request,`/api/published/${encodeURIComponent(id)}`);
   if(url.searchParams.has('download'))return new Response(hostedMarkdown(report),{headers:{'content-type':'text/markdown; charset=utf-8','content-disposition':'attachment; filename="daily-report.md"','cache-control':'no-store','x-content-type-options':'nosniff'}});
   return json(report);
  }
  if(path==='/api/evidence'){
   const id=url.searchParams.get('id'),reportId=url.searchParams.get('report');
   if(!reportId||!/^[A-Za-z0-9:_-]{1,180}$/.test(reportId))throw new HostedError(400,'A valid report ID is required.');
   const report=await authenticatedRequest(request,`/api/published/${encodeURIComponent(reportId)}`);const evidence=report.evidenceIndex?.find((e:{id:string})=>e.id===id);
   if(evidence)return json({event:evidence,session:{title:evidence.sessionTitle,agent:evidence.agent,cwd:'Synced evidence excerpt'}});
   throw new HostedError(404,'Only report-cited excerpts are synced. Full transcripts stay on your computer.');
  }
  if(!['/api/status','/api/sessions','/api/reports','/api/daily'].includes(path))return json({error:'Unavailable in hosted mode.'},404);
  const library=await authenticatedRequest(request,'/api/library');
  if(path==='/api/status')return json({collector:library.collector,hosted:true});
  if(path==='/api/sessions')return json(library.sessions.map((s:Record<string,unknown>)=>({...s,cwd:'Synced from your computer'})));
  if(path==='/api/reports')return json(library.reports);
  return json({id:library.reports[0]?.id||null,date:library.reports[0]?.role?.match(/\d{4}-\d{2}-\d{2}/)?.[0]||null,timezone:library.reports[0]?.timezone||'America/Los_Angeles',fresh:false,waiting:!library.reports.length});
 }catch(e){return json({error:e instanceof HostedError?e.message:'Hosted request failed.'},e instanceof HostedError?e.status:400);}
}
