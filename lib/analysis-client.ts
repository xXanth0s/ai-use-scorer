import { readFile, writeFile, rename, rm, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { dataDir } from './db';
import type { AnalysisRequest } from '../services/analysis/agent/lib/contract';
type Connection={url:string;deviceToken:string;userId:string;expiresAt:string};
const file=()=>join(dataDir,'analysis-connection.json');
export function serviceUrl(value:string){const u=new URL(value);if(u.username||u.password||u.search||u.hash||u.pathname!=='/')throw new Error('Use the analysis service origin without a path or credentials.');if(u.protocol!=='https:'&&!(u.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(u.hostname)))throw new Error('The analysis service must use HTTPS.');return u.origin;}
async function connection():Promise<Connection|null>{try{return JSON.parse(await readFile(file(),'utf8'));}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return null;throw new Error('The local analysis connection could not be read.');}}
async function request(url:string,path:string,token?:string,init:RequestInit={}){
 const r=await fetch(`${serviceUrl(url)}${path}`,{...init,headers:{'content-type':'application/json',...(token?{authorization:`Bearer ${token}`}:{})},redirect:'error',signal:AbortSignal.timeout(30000)});
 const raw=await r.text();if(raw.length>200000)throw new Error('The analysis response was too large.');let data;try{data=JSON.parse(raw);}catch{throw new Error('The analysis service did not return JSON.');}
 if(!r.ok)throw new Error(typeof data.error==='string'?data.error.slice(0,300):`Analysis service returned ${r.status}.`);return data;
}
export async function connectAnalysis(url:string,activationCode:string){
 url=serviceUrl(url);if(!/^invite_[A-Za-z0-9_-]{43}$/.test(activationCode))throw new Error('Enter the activation code supplied by your platform administrator.');
 const data=await request(url,'/api/activate',undefined,{method:'POST',body:JSON.stringify({activationCode})});
 if(!/^hdyai_[A-Za-z0-9_-]{43}$/.test(data.deviceToken)||typeof data.userId!=='string'||!Number.isFinite(Date.parse(data.expiresAt)))throw new Error('Invalid activation response.');
 const previous=await connection();await mkdir(dataDir,{recursive:true,mode:0o700});
 const temp=`${file()}.${crypto.randomUUID()}.tmp`;await writeFile(temp,JSON.stringify({url,...data}),{mode:0o600,flag:'wx'});await rename(temp,file());
 if(previous)await request(previous.url,'/api/device',previous.deviceToken,{method:'DELETE'}).catch(()=>{});
 return connectionStatus();
}
export async function connectionStatus(){const c=await connection();return c?{connected:Date.parse(c.expiresAt)>Date.now(),url:c.url,userId:c.userId,expiresAt:c.expiresAt,model:'openai/gpt-6.1-sol',reasoning:'low'}:{connected:false,url:'',userId:null,expiresAt:null,model:'openai/gpt-6.1-sol',reasoning:'low'};}
export async function disconnectAnalysis(){const c=await connection();if(c)await request(c.url,'/api/device',c.deviceToken,{method:'DELETE'});await rm(file(),{force:true});}
export async function publishLibrary(payload:unknown){const c=await connection();if(!c)throw new Error('Pair the plugin first.');return request(c.url,'/api/sync',c.deviceToken,{method:'POST',body:JSON.stringify(payload)});}
export async function analyzeRemotely(input:AnalysisRequest){
 const c=await connection();if(!c||Date.parse(c.expiresAt)<=Date.now())throw new Error('Connect how do you ai in Analysis settings before generating a report.');
 const accepted=await request(c.url,'/api/analyze',c.deviceToken,{method:'POST',body:JSON.stringify(input)});
 if(!/^[a-f0-9]{64}$/.test(accepted.jobId))throw new Error('Invalid analysis job response.');
 const deadline=Date.now()+250000;
 while(Date.now()<deadline){const result=await request(c.url,`/api/analyses/${accepted.jobId}`,c.deviceToken);if(result.status==='complete')return result;if(result.status==='failed')throw new Error(result.error||'The hosted assessment failed.');await new Promise(r=>setTimeout(r,2000));}
 throw new Error('Timed out waiting for the hosted assessment. The platform may still be processing it.');
}
