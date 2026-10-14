import { createHmac, timingSafeEqual } from 'node:crypto';
import { z } from 'zod';
import { failure, hash, HttpError, rawBody, response, sql } from './store';

const identity=z.object({
 userId:z.string().regex(/^user_[A-Za-z0-9]{1,100}$/),
 sessionId:z.string().regex(/^sess_[A-Za-z0-9]{1,100}$/),
}).strict();

// Only the trusted Next.js server signs requests after resolving its Clerk session.
// Body identity is never accepted from unsigned browser input.
export function verifyProvisioning(request:Request,text:string,now=Date.now()) {
 const key=process.env.WEB_PROVISIONING_KEY;
 if(!key||key.length<32)throw new HttpError(503,'Account setup is temporarily unavailable.');
 const timestamp=request.headers.get('x-hdyai-timestamp')||'';
 const signature=request.headers.get('x-hdyai-signature')||'';
 if(!/^\d{10}$/.test(timestamp)||Math.abs(now/1000-Number(timestamp))>60||! /^[a-f0-9]{64}$/.test(signature))throw new HttpError(401,'Trusted account authentication required.');
 const expected=createHmac('sha256',key).update(`${timestamp}.${text}`).digest();
 if(!timingSafeEqual(Buffer.from(signature,'hex'),expected))throw new HttpError(401,'Trusted account authentication required.');
 let value:unknown;
 try{value=JSON.parse(text);}catch{throw new HttpError(400,'Invalid account identity.');}
 const parsed=identity.safeParse(value);
 if(!parsed.success)throw new HttpError(400,'Invalid account identity.');
 const {userId,sessionId}=parsed.data;
 const deviceToken=`hdyai_${createHmac('sha256',key).update(`browser:v1:${userId}:${sessionId}`).digest('base64url')}`;
 return {userId:`clerk:${userId}`,deviceToken};
}

export async function provisionWebAccount(request:Request) {try{
 const text=await rawBody(request,2000);
 const verified=verifyProvisioning(request,text);
 // One database function owns account creation, revocation checks and admission.
 // Replays reuse the credential, retain the original expiry and never reset quota.
 const [row]=await sql()`SELECT * FROM provision_web_account(${verified.userId},${hash(verified.deviceToken)})`;
 if(!row)throw new HttpError(503,'Account setup is temporarily unavailable.');
 return response({deviceToken:verified.deviceToken,userId:String(row.provisioned_user_id),dailyLimit:Number(row.provisioned_daily_limit),expiresAt:new Date(String(row.credential_expires_at)).toISOString()});
}catch(error){return failure(error);}}
