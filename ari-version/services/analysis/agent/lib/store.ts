import { neon } from '@neondatabase/serverless';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import type { z } from 'zod';
import type { reportSchema } from './contract';
type ReportContent=z.infer<typeof reportSchema>;

export function sql(){if(!process.env.DATABASE_URL)throw new HttpError(503,'Analysis storage is unavailable.');return neon(process.env.DATABASE_URL);}
export class HttpError extends Error {constructor(public status:number,message:string){super(message);}}
export const hash=(value:string)=>createHash('sha256').update(value).digest('hex');
export const secret=()=>randomBytes(32).toString('base64url');
export async function body(request:Request,max=250000){
 if(!request.headers.get('content-type')?.startsWith('application/json'))throw new HttpError(415,'Use JSON.');
 const reader=request.body?.getReader();if(!reader)throw new HttpError(400,'Missing request body.');
 const chunks:Uint8Array[]=[];let size=0;
 try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>max){await reader.cancel();throw new HttpError(413,'The evidence sample is too large.');}chunks.push(value);}}finally{reader.releaseLock();}
 try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw new HttpError(400,'Invalid JSON.');}
}
export function admin(request:Request){
 const configured=process.env.PLATFORM_ADMIN_KEY;if(!configured||configured.length<32)throw new HttpError(503,'Administration is unavailable.');
 const actual=hash(request.headers.get('authorization')||'');const expected=hash(`Bearer ${configured}`);
 if(!timingSafeEqual(Buffer.from(actual),Buffer.from(expected)))throw new HttpError(401,'Authentication required.');
}
export async function device(request:Request){
 const token=request.headers.get('authorization')?.match(/^Bearer (hdyai_[A-Za-z0-9_-]{43})$/)?.[1];
 if(!token)throw new HttpError(401,'Activate this device to analyze sessions.');
 const digest=hash(token);const [row]=await sql()`SELECT d.user_id,u.daily_limit FROM device_credentials d JOIN platform_users u ON u.id=d.user_id WHERE d.hash=${digest} AND NOT d.revoked AND d.expires_at>now() AND u.active`;
 if(!row)throw new HttpError(401,'Device access has expired or been revoked. Activate again.');
 return {userId:String(row.user_id),hash:digest,dailyLimit:Number(row.daily_limit)};
}
export type Job={id:string;user_id:string;digest:string;status:'queued'|'running'|'complete'|'failed';created_at:string;session_id:string|null;evidence_ids:string[];content:ReportContent|null;telemetry:{inputTokens?:number;outputTokens?:number;costUsd?:number;agentMs?:number}|null;error:string|null};
export async function job(id:string,userId:string){const [row]=await sql()`SELECT * FROM analysis_jobs WHERE id=${id} AND user_id=${userId}`;if(!row)throw new HttpError(404,'Analysis not found.');return row as Job;}
export async function failJob(id:string,message:string){await sql()`UPDATE analysis_jobs SET status='failed',error=${message} WHERE id=${id} AND status IN ('queued','running')`;}
export function response(data:unknown,status=200){return Response.json(data,{status,headers:{'cache-control':'no-store','x-content-type-options':'nosniff'}});}
export function failure(error:unknown){
 if(error instanceof HttpError)return response({error:error.message},error.status);
 const message=error instanceof Error?error.message:'';
 if(/USER_QUOTA|PLATFORM_QUOTA/.test(message))return response({error:'The daily analysis limit has been reached. Try again tomorrow.'},429);
 if(message.includes('ALREADY_RUNNING'))return response({error:'An analysis is already running for this account.'},429);
 if(message.includes('IDEMPOTENCY_CONFLICT'))return response({error:'This request ID already belongs to different evidence.'},409);
 if(/INVALID_INVITE|ACCESS_DISABLED|DEVICE_LIMIT/.test(message))return response({error:'Activation was not accepted. Ask for a new invitation or revoke an old device.'},403);
 // Never send upstream errors: they can contain query parameters or secrets.
 return response({error:'Analysis service is temporarily unavailable.'},503);
}
