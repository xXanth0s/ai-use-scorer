import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { provisionWebAccount, verifyProvisioning } from '../services/analysis/agent/lib/provision';
import { hash, HttpError } from '../services/analysis/agent/lib/store';

const key='synthetic-provisioning-key-for-unit-tests-only';
const now=1792015200000;
const value={userId:'user_Synthetic123',sessionId:'sess_Synthetic123'};
const text=JSON.stringify(value);
function request(payload=text,timestamp=String(now/1000),signature?:string) {
 return new Request('https://service.example/api/web/provision',{method:'POST',headers:{'content-type':'application/json','x-hdyai-timestamp':timestamp,'x-hdyai-signature':signature??createHmac('sha256',key).update(`${timestamp}.${payload}`).digest('hex')},body:payload});
}
function throwsStatus(action:()=>unknown,status:number) {assert.throws(action,(error:unknown)=>error instanceof HttpError&&error.status===status);}

test('provisioning authenticates the exact bounded body and binds credential to Clerk user and session',()=>{
 const previous=process.env.WEB_PROVISIONING_KEY;process.env.WEB_PROVISIONING_KEY=key;
 try{
  const result=verifyProvisioning(request(),text,now);
  assert.equal(result.userId,'clerk:user_Synthetic123');
  assert.match(result.deviceToken,/^hdyai_[A-Za-z0-9_-]{43}$/);
  assert.deepEqual(verifyProvisioning(request(),text,now),result);
  const other=JSON.stringify({...value,sessionId:'sess_Other'});
  assert.notEqual(verifyProvisioning(request(other),other,now).deviceToken,result.deviceToken);
  const otherUser=JSON.stringify({...value,userId:'user_Other'});
  assert.notEqual(verifyProvisioning(request(otherUser),otherUser,now).deviceToken,result.deviceToken);
  throwsStatus(()=>verifyProvisioning(request(),text+' ',now),401);
  throwsStatus(()=>verifyProvisioning(request(text,undefined,'a'.repeat(64)),text,now),401);
  throwsStatus(()=>verifyProvisioning(request(text,undefined,'invalid'),text,now),401);
  throwsStatus(()=>verifyProvisioning(new Request('https://service.example'),text,now),401);
  for(const offset of [-61,61])throwsStatus(()=>verifyProvisioning(request(text,String(now/1000+offset)),text,now),401);
  assert.doesNotThrow(()=>verifyProvisioning(request(text,String(now/1000-60)),text,now));
  for(const invalid of ['{',JSON.stringify({...value,dailyLimit:20}),JSON.stringify({...value,userId:'ari'}),JSON.stringify({...value,sessionId:''}),JSON.stringify({...value,userId:'user_a:other'})])throwsStatus(()=>verifyProvisioning(request(invalid),invalid,now),400);
  process.env.WEB_PROVISIONING_KEY='short';throwsStatus(()=>verifyProvisioning(request(),text,now),503);
 }finally{if(previous===undefined)delete process.env.WEB_PROVISIONING_KEY;else process.env.WEB_PROVISIONING_KEY=previous;}
});

test('provisioning uses atomic database admission, preserves stored quota and denies disabled credentials',async()=>{
 const oldKey=process.env.WEB_PROVISIONING_KEY,oldDb=process.env.DATABASE_URL,oldFetch=globalThis.fetch;
 process.env.WEB_PROVISIONING_KEY=key;process.env.DATABASE_URL='postgresql://synthetic:synthetic@unit.example/synthetic';
 const calls:{query:string;params:string[]}[]=[];let rejection='';
 globalThis.fetch=async(_url,init)=>{
  if(rejection)return Response.json({message:rejection,code:'P0001'},{status:400});
  calls.push(JSON.parse(String(init?.body)));
  return Response.json({fields:[{name:'provisioned_user_id',dataTypeID:25},{name:'provisioned_daily_limit',dataTypeID:23},{name:'credential_expires_at',dataTypeID:25}],rows:[['clerk:user_Synthetic123','4','2026-11-14T00:00:00.000Z']],rowCount:1});
 };
 const currentRequest=(payload=text)=>request(payload,String(Math.floor(Date.now()/1000)));
 try{
  const response=await provisionWebAccount(currentRequest());assert.equal(response.status,200);
  const result=await response.json();assert.equal(result.dailyLimit,4);assert.equal(result.expiresAt,'2026-11-14T00:00:00.000Z');
  assert.equal(calls.length,1);assert.match(calls[0].query,/SELECT \* FROM provision_web_account\(\$1,\$2\)/);
  assert.deepEqual(calls[0].params,['clerk:user_Synthetic123',hash(result.deviceToken)]);
  assert.ok(!JSON.stringify(calls).includes(result.deviceToken));
  rejection='BROWSER_ACCESS_DISABLED';assert.equal((await provisionWebAccount(currentRequest())).status,403);
  rejection='BROWSER_LIMIT';assert.equal((await provisionWebAccount(currentRequest())).status,403);
  rejection='postgresql://private-secret';const failed=await provisionWebAccount(currentRequest());assert.equal(failed.status,503);assert.doesNotMatch(await failed.text(),/private-secret/);
  rejection='';const count=calls.length;assert.equal((await provisionWebAccount(currentRequest(JSON.stringify({...value,userId:'attacker'})))).status,400);assert.equal(calls.length,count);
  assert.equal((await provisionWebAccount(currentRequest('x'.repeat(2001)))).status,413);assert.equal(calls.length,count);
  // Migration guards are part of the atomic admission contract; real transaction
  // tests can exercise the same function in a disposable database.
  const schema=readFileSync(new URL('../services/analysis/schema.sql',import.meta.url),'utf8');
  const fn=schema.slice(schema.indexOf('CREATE OR REPLACE FUNCTION provision_web_account'),schema.indexOf('-- Row locks'));
  assert.match(fn,/ON CONFLICT\(id\) DO NOTHING/);assert.match(fn,/FOR UPDATE/);
  assert.match(fn,/credential\.revoked OR credential\.expires_at<=now\(\)/);
  assert.match(fn,/kind='browser'.*>=5/);assert.doesNotMatch(fn,/UPDATE platform_users|daily_usage|DELETE FROM/);
  assert.match(schema,/kind='collector'.*>=3/);
 }finally{globalThis.fetch=oldFetch;if(oldKey===undefined)delete process.env.WEB_PROVISIONING_KEY;else process.env.WEB_PROVISIONING_KEY=oldKey;if(oldDb===undefined)delete process.env.DATABASE_URL;else process.env.DATABASE_URL=oldDb;}
});
