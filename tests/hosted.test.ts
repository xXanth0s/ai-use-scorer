import {test} from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import {hostedApi as api,hostedRequest as proxy,type HostedAuth} from '../lib/hosted';
const auth:HostedAuth={identity:async()=>({userId:'clerk-synthetic'}),seal:(_userId,value)=>value,unseal:(_userId,value)=>value};
const hostedApi=(request:Request)=>api(request,auth);
const hostedRequest=(request:Request,path:string,init:RequestInit={})=>proxy(request,path,init,auth);
const credential=`hdyai_${'a'.repeat(43)}`;
function request(path:string,method='GET',headers:Record<string,string>={},body?:string){return new Request(`https://dashboard.example${path}`,{method,headers:{host:'dashboard.example',cookie:`hdyai_web=${credential}`,origin:'https://dashboard.example',...headers},body});}
test('automatic setup uses verified Clerk IDs, signs server requests and hides provisioned credentials',async()=>{
 const saved={HDYAI_HOSTED:process.env.HDYAI_HOSTED,WEB_PROVISIONING_KEY:process.env.WEB_PROVISIONING_KEY,HDYAI_ANALYSIS_URL:process.env.HDYAI_ANALYSIS_URL};
 const oldFetch=globalThis.fetch;
 const key='synthetic-web-provisioning-key-for-tests-only';
 Object.assign(process.env,{HDYAI_HOSTED:'1',WEB_PROVISIONING_KEY:key,HDYAI_ANALYSIS_URL:'https://analysis.example'});
 const verified:HostedAuth={...auth,identity:async()=>({userId:'user_Synthetic',sessionId:'sess_Synthetic'}),unseal:()=>null,seal:(id,value)=>{assert.equal(id,'user_Synthetic');assert.equal(value,credential);return 'encrypted-test-value';}};
 let calls=0;
 globalThis.fetch=async(input,init)=>{
  calls++;assert.equal(String(input),'https://analysis.example/api/web/provision');
  const headers=init?.headers as Record<string,string>,payload=String(init?.body);
  assert.deepEqual(JSON.parse(payload),{userId:'user_Synthetic',sessionId:'sess_Synthetic'});
  assert.equal(headers['x-hdyai-signature'],createHmac('sha256',key).update(`${headers['x-hdyai-timestamp']}.${payload}`).digest('hex'));
  assert.equal(headers.authorization,undefined);assert.equal(init?.redirect,'error');
  return Response.json({deviceToken:credential,userId:'clerk:user_Synthetic',dailyLimit:2,expiresAt:'2026-11-14T00:00:00Z'});
 };
 try{
  const response=await api(request('/api/setup','POST',{'content-type':'application/json'},JSON.stringify({userId:'user_Attacker',dailyLimit:20})),verified);
  assert.equal(response?.status,200);assert.match(response!.headers.get('set-cookie')||'',/HttpOnly/);
  const value=await response!.json();assert.equal(value.dailyLimit,2);assert.doesNotMatch(JSON.stringify(value),/hdyai_|invite_|encrypted-test/);assert.equal(calls,1);
  const before=calls;
  assert.equal((await api(request('/api/setup','POST',{'sec-fetch-site':'cross-site'}),verified))?.status,403);
  assert.equal((await api(request('/api/setup','POST'),{...verified,identity:async()=>null}))?.status,401);
  assert.equal((await api(request('/api/setup','POST'),{...verified,identity:async()=>({userId:'user_Synthetic'})}))?.status,401);
  delete process.env.WEB_PROVISIONING_KEY;
  assert.equal((await api(request('/api/setup','POST'),verified))?.status,503);assert.equal(calls,before);
 }finally{globalThis.fetch=oldFetch;for(const [name,value] of Object.entries(saved)){if(value===undefined)delete process.env[name];else process.env[name]=value;}}
});
test('hosted facade protects credentials, CSRF, errors and fixed proxy paths',async()=>{
 const oldFetch=globalThis.fetch,oldHosted=process.env.HDYAI_HOSTED,oldUrl=process.env.HDYAI_ANALYSIS_URL;
 process.env.HDYAI_HOSTED='1';process.env.HDYAI_ANALYSIS_URL='https://analysis.example';
 let calls=0;
 globalThis.fetch=async(input,init)=>{calls++;assert.equal(String(input),'https://analysis.example/api/library');assert.equal(init?.redirect,'error');assert.equal(init?.cache,'no-store');assert.equal((init?.headers as Record<string,string>).authorization,`Bearer ${credential}`);return Response.json({sessions:[],reports:[],collector:null});};
 try{
  for(const headers of ([{origin:'https://attacker.example'},{origin:'http://dashboard.example'},{origin:'null'},{'sec-fetch-site':'same-site'},{'sec-fetch-site':'cross-site'}] as Record<string,string>[]))assert.equal((await hostedApi(request('/api/setup','POST',headers)))?.status,403);
  assert.equal(calls,0);
  assert.equal((await hostedApi(new Request('https://dashboard.example/api/setup',{method:'POST',headers:{host:'dashboard.example',cookie:`hdyai_web=${credential}`}})))?.status,403);
  const unauth=await hostedApi(request('/api/sessions','GET',{cookie:''}));assert.equal(unauth?.status,401);assert.equal(calls,0);
  assert.equal((await hostedApi(request('/api/sessions')))?.status,200);assert.equal(calls,1);
  await assert.rejects(()=>hostedRequest(request('/api/setup'),'/api/admin/invites',{method:'POST'}),/Unsupported/);
  assert.equal((await hostedApi(request('/api/reports/%2e%2e%2fadmin')))?.status,400);
  await assert.rejects(()=>hostedRequest(request('/api/setup'),'/api/published/%2e%2e%2fadmin'),/Unsupported/);
  assert.equal((await hostedApi(request('/api/reports','DELETE')))?.status,405);
  globalThis.fetch=async()=>Response.json({error:`database postgres://secret ${credential}`},{status:429});
  const limited=await hostedApi(request('/api/sessions'));assert.equal(limited?.status,429);assert.doesNotMatch(await limited!.text(),/postgres|hdyai_/);
  globalThis.fetch=async()=>{throw new Error(`gateway ${credential}`);};
  const unavailable=await hostedApi(request('/api/setup'));assert.equal(unavailable?.status,503);assert.doesNotMatch(await unavailable!.text(),/hdyai_/);
  const logout=await hostedApi(request('/api/analysis-connection','DELETE'));assert.equal(logout?.status,503);assert.equal(logout?.headers.get('set-cookie'),null);
  process.env.HDYAI_ANALYSIS_URL='https://username:password@analysis.example';
  const invalid=await hostedApi(request('/api/sessions'));assert.equal(invalid?.status,503);assert.doesNotMatch(await invalid!.text(),/password/);
 }finally{globalThis.fetch=oldFetch;if(oldHosted===undefined)delete process.env.HDYAI_HOSTED;else process.env.HDYAI_HOSTED=oldHosted;if(oldUrl===undefined)delete process.env.HDYAI_ANALYSIS_URL;else process.env.HDYAI_ANALYSIS_URL=oldUrl;}
});
test('browser activation uses a private cookie and disconnect revokes that device',async()=>{
 const oldFetch=globalThis.fetch,oldHosted=process.env.HDYAI_HOSTED,oldEnv=process.env.NODE_ENV;
 process.env.HDYAI_HOSTED='1';Object.assign(process.env,{NODE_ENV:'production'});
 let revoked=false;
 globalThis.fetch=async(input,init)=>{
  if(String(input).endsWith('/api/activate'))return Response.json({deviceToken:credential,userId:'approved',expiresAt:'2026-11-01T00:00:00.000Z'});
  assert.equal((init?.headers as Record<string,string>).authorization,`Bearer ${credential}`);assert.equal(init?.method,'DELETE');revoked=true;return Response.json({revoked:true});
 };
 try{
  const response=await hostedApi(request('/api/analysis-connection','POST',{'content-type':'application/json'},JSON.stringify({activationCode:`invite_${'b'.repeat(43)}`})));
  assert.equal(response?.status,200);const cookie=response?.headers.get('set-cookie')||'';for(const flag of ['HttpOnly','SameSite=Strict','Secure','Path=/','Max-Age=2592000'])assert.ok(cookie.includes(flag));
  assert.doesNotMatch(await response!.text(),/hdyai_|invite_/);
  const logout=await hostedApi(request('/api/analysis-connection','DELETE'));assert.equal(logout?.status,200);assert.ok(revoked);assert.match(logout?.headers.get('set-cookie')||'',/Max-Age=0/);
  assert.equal((await hostedApi(request('/api/analysis-connection','POST',{'content-type':'application/json'},'x'.repeat(3000))))?.status,413);
 }finally{globalThis.fetch=oldFetch;if(oldHosted===undefined)delete process.env.HDYAI_HOSTED;else process.env.HDYAI_HOSTED=oldHosted;if(oldEnv===undefined)Reflect.deleteProperty(process.env,'NODE_ENV');else Object.assign(process.env,{NODE_ENV:oldEnv});}
});

test('every hosted route requires Clerk identity while setup distinguishes signed-out and unlinked states',async()=>{
 const before=process.env.HDYAI_HOSTED;process.env.HDYAI_HOSTED='1';
 const signedOut:HostedAuth={...auth,identity:async()=>null};
 try{
  for(const path of ['/api/settings','/api/entire','/api/status','/api/sessions','/api/reports','/api/evidence?id=synthetic&report=report'])assert.equal((await api(request(path),signedOut))?.status,401);
  assert.equal((await api(request('/api/analysis-connection','POST',{'content-type':'application/json'},JSON.stringify({activationCode:`invite_${'b'.repeat(43)}`})),signedOut))?.status,401);
  for(const path of ['/api/setup','/api/analysis-connection']){const r=await api(request(path),signedOut);assert.equal(r?.status,200);assert.equal((await r!.json()).signedIn,false);}
  const unlinked=await api(request('/api/setup'),{...auth,unseal:()=>null});assert.equal(unlinked?.status,200);const state=await unlinked!.json();assert.equal(state.signedIn,true);assert.equal(state.connection.connected,false);
  const down=await api(request('/api/setup'),{...auth,identity:async()=>{throw new Error('CLERK_SECRET_KEY sensitive');}});assert.equal(down?.status,503);assert.doesNotMatch(await down!.text(),/sensitive/);
 }finally{if(before===undefined)delete process.env.HDYAI_HOSTED;else process.env.HDYAI_HOSTED=before;}
});

test('hosted activation encrypts the service token and account switching cannot reuse it',async()=>{
 const {sealWebCredential,unsealWebCredential}=await import('../lib/web-auth');
 const saved={HDYAI_HOSTED:process.env.HDYAI_HOSTED,CLERK_SECRET_KEY:process.env.CLERK_SECRET_KEY,NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY};
 Object.assign(process.env,{HDYAI_HOSTED:'1',CLERK_SECRET_KEY:'sk_test_synthetic_only_no_live_credential',NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:'pk_test_synthetic_only'});
 const oldFetch=globalThis.fetch;let current='user_syntheticA',calls=0;
 const verified:HostedAuth={identity:async()=>({userId:current}),seal:sealWebCredential,unseal:unsealWebCredential};
 globalThis.fetch=async(input,init)=>{calls++;if(String(input).endsWith('/api/activate'))return Response.json({deviceToken:credential,userId:'approved'});assert.equal((init?.headers as Record<string,string>).authorization,`Bearer ${credential}`);return Response.json({sessions:[],reports:[],collector:null});};
 try{
  const activated=await api(request('/api/analysis-connection','POST',{'content-type':'application/json'},JSON.stringify({activationCode:`invite_${'b'.repeat(43)}`})),verified);
  assert.equal(activated?.status,200);const sealed=activated!.headers.get('set-cookie')!.split(';')[0];assert.doesNotMatch(sealed,/hdyai_a/);assert.match(sealed,/hdyai_web=v1\./);
  assert.equal((await api(request('/api/sessions','GET',{cookie:sealed}),verified))?.status,200);const beforeSwitch=calls;
  current='user_syntheticB';assert.equal((await api(request('/api/sessions','GET',{cookie:sealed}),verified))?.status,401);assert.equal(calls,beforeSwitch);
  current='user_syntheticA';assert.equal((await api(request('/api/sessions'),verified))?.status,401);assert.equal(calls,beforeSwitch);
 }finally{globalThis.fetch=oldFetch;for(const [key,value] of Object.entries(saved)){if(value===undefined)delete process.env[key];else process.env[key]=value;}}
});
