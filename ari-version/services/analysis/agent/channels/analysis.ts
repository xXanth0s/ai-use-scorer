import { defineChannel, GET, POST, DELETE, type ChannelSendOptions } from 'eve/channels';
import { z } from 'zod';
import { sessions } from 'eve/server';
import { analysisRequest, buildPrompt, MODEL, reportSchema, validateContent } from '../lib/contract';
import { admin, body, device, failJob, failure, hash, HttpError, job, response, secret, sql, type Job } from '../lib/store';
import { evidenceDigest, createPairing, readLibrary, readPublished, syncLibrary } from '../lib/library';

type State={jobId:string;evidenceIds:string[];text:string;started:number;usage:{inputTokens?:number;outputTokens?:number;costUsd?:number}};
export default defineChannel<State,{state:State}>({
 state:{jobId:'',evidenceIds:[] as string[],text:'',started:0,usage:{} as {inputTokens?:number;outputTokens?:number;costUsd?:number}},
 context:state=>({state}),
 audience:()=> 'private',
 routes:[
  GET('/api/health',async()=>response({platform:'how do you ai',service:'Eve analysis',model:MODEL,reasoning:'low'})),
  GET('/api/library',readLibrary),
  GET('/api/published/:id',(request,{params})=>readPublished(request,params.id)),
  POST('/api/sync',syncLibrary),
  POST('/api/pairings',createPairing),
  POST('/api/admin/invites',async request=>{try{
   admin(request);const b=z.object({userId:z.string().min(1).max(100),dailyLimit:z.number().int().min(1).max(20).default(6)}).strict().parse(await body(request,2000));
   const invite=`invite_${secret()}`;const db=sql();
   await db.transaction([db`INSERT INTO platform_users(id,daily_limit) VALUES(${b.userId},${b.dailyLimit}) ON CONFLICT(id) DO UPDATE SET daily_limit=excluded.daily_limit`,db`INSERT INTO activation_invites(hash,user_id,expires_at) VALUES(${hash(invite)},${b.userId},now()+interval '15 minutes')`]);
   return response({activationCode:invite,expiresIn:900});
  }catch(e){return failure(e);}}),
  DELETE('/api/admin/users/:userId',async(request,{params})=>{try{admin(request);await sql()`UPDATE platform_users SET active=false WHERE id=${params.userId}`;return response({revoked:true});}catch(e){return failure(e);}}),
  POST('/api/activate',async request=>{try{
   const b=z.object({activationCode:z.string().regex(/^invite_[A-Za-z0-9_-]{43}$/)}).strict().parse(await body(request,2000));
   const token=`hdyai_${secret()}`;const [row]=await sql()`SELECT activate_device(${hash(b.activationCode)},${hash(token)}) AS user_id`;
   return response({deviceToken:token,userId:row.user_id,expiresAt:new Date(Date.now()+30*86400000).toISOString()});
  }catch(e){return failure(e);}}),
  GET('/api/device',async request=>{try{const d=await device(request);return response({userId:d.userId,dailyLimit:d.dailyLimit,model:MODEL,reasoning:'low'});}catch(e){return failure(e);}}),
  DELETE('/api/device',async request=>{try{const d=await device(request);await sql()`UPDATE device_credentials SET revoked=true WHERE hash=${d.hash}`;return response({revoked:true});}catch(e){return failure(e);}}),
  POST<State>('/api/analyze',async(request,{from})=>{try{
   const d=await device(request);
   if(!process.env.AI_GATEWAY_API_KEY)throw new HttpError(503,'The server model credential is not configured.');
   const parsed=analysisRequest.safeParse(await body(request));if(!parsed.success)throw new HttpError(400,'Invalid evidence payload.');const b=parsed.data;
   const prompt=buildPrompt(b);if(prompt.length>125000)throw new HttpError(413,'The evidence sample is too large.');
   const evidenceIds=b.input.flatMap(s=>s.evidence.map(e=>e.id));if(new Set(evidenceIds).size!==evidenceIds.length)throw new HttpError(400,'Evidence IDs must be unique.');
   const id=hash(`${d.userId}:${b.requestId}`);const [row]=await sql()`SELECT * FROM reserve_analysis(${d.hash},${id},${hash(prompt)},${JSON.stringify(evidenceIds)}::jsonb)`;const reserved=row as Job;
   // Exactly one caller wins the transition, even when identical HTTP requests race.
   const digests=Object.fromEntries(b.input.flatMap(s=>s.evidence.map(e=>[e.id,evidenceDigest(e)])));
   const claimed=await sql()`UPDATE analysis_jobs SET status='running',evidence_digests=${JSON.stringify(digests)}::jsonb WHERE id=${id} AND status='queued' RETURNING id`;
   if(claimed.length){try{
    const session=await from(id).send(prompt,{auth:{authenticator:'platform-device',principalId:d.userId,principalType:'user',attributes:{}},outputSchema:z.toJSONSchema(reportSchema) as ChannelSendOptions<State>['outputSchema'],state:{jobId:id,evidenceIds,text:'',started:Date.now(),usage:{}}});
    await sql()`UPDATE analysis_jobs SET session_id=${session.id} WHERE id=${id}`;
   }catch{await failJob(id,'Eve could not start the assessment. Retry with a new report.');throw new HttpError(503,'Eve could not start the assessment.');}}
   return response({jobId:id,status:claimed.length?'running':reserved.status,model:MODEL,reasoning:'low'},202);
  }catch(e){return failure(e);}}),
  GET('/api/analyses/:jobId',async(request,{params,attachSession})=>{try{
   const d=await device(request);const found=await job(params.jobId,d.userId);
   if((found.status==='running'||found.status==='queued')&&Date.now()-Date.parse(found.created_at)>240000){
    if(found.session_id)await attachSession(found.session_id).cancel().catch(()=>{});
    await failJob(found.id,'The assessment timed out. Retry with fewer sessions.');found.status='failed';found.error='The assessment timed out. Retry with fewer sessions.';
   }
   const t=found.telemetry;
   return response({jobId:found.id,status:found.status,content:found.content,usage:t?{inputTokens:t.inputTokens,outputTokens:t.outputTokens,costUsd:t.costUsd,agentMs:t.agentMs}:null,error:found.error,model:MODEL,reasoning:'low'});
  }catch(e){return failure(e);}}),
 ],
 events:{
  'message.completed'(event,channel){channel.state.text=event.message;},
  'step.completed'(event,channel){if(event.usage){const previous=channel.state.usage;channel.state.usage={inputTokens:(previous.inputTokens||0)+(event.usage.inputTokens||0),outputTokens:(previous.outputTokens||0)+(event.usage.outputTokens||0),costUsd:previous.costUsd!==undefined||event.usage.costUsd!==undefined?(previous.costUsd||0)+(event.usage.costUsd||0):undefined};}},
  async 'turn.completed'(_event,channel,ctx){const state=channel.state;try{
   for await(const event of sessions.attach(ctx.session.id).stream({follow:false})){if(event.type==='result.completed')state.text=JSON.stringify(event.data.result);}
   const text=state.text.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
   const content=validateContent(JSON.parse(text),state.evidenceIds);
   await sql()`UPDATE analysis_jobs SET status='complete',content=${JSON.stringify(content)}::jsonb,telemetry=${JSON.stringify({...state.usage,agentMs:Date.now()-state.started})}::jsonb WHERE id=${state.jobId} AND status='running'`;
  }catch{await failJob(state.jobId,'The assessment did not pass output and evidence validation. Retry with a new report.');}},
  async 'turn.failed'(event,channel){
   const code=event.code.replace(/[^A-Z0-9_:-]/gi,'').slice(0,100);
   let detail=event.message;for(const value of [process.env.AI_GATEWAY_API_KEY,process.env.PLATFORM_ADMIN_KEY,process.env.DATABASE_URL])if(value)detail=detail.replaceAll(value,'[redacted]');
   await sql()`UPDATE analysis_jobs SET telemetry=${JSON.stringify({failureCode:code,failureDetail:detail.slice(0,500)})}::jsonb WHERE id=${channel.state.jobId}`;
   await failJob(channel.state.jobId,`The model assessment failed (${code}). Check platform availability and retry.`);
  },
  async 'session.failed'(_event,channel){await failJob(channel.state.jobId,'The Eve analysis session failed. Retry with a new report.');},
 },
});
