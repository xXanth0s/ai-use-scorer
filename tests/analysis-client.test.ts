import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
test('activation keeps device credentials out of browser status and revokes them on disconnect',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'hdyai-connection-'));process.env.AI_SESSION_DATA_DIR=dir;
 const client=await import('../lib/analysis-client');let revoked=false;
 const token=`hdyai_${'a'.repeat(43)}`;
 const original=globalThis.fetch;
 globalThis.fetch=async(input,init)=>{if(String(input).endsWith('/api/activate'))return Response.json({deviceToken:token,userId:'approved-user',expiresAt:new Date(Date.now()+86400000).toISOString()});assert.equal((init?.headers as Record<string,string>).authorization,`Bearer ${token}`);assert.equal(init?.redirect,'error');revoked=true;return Response.json({revoked:true});};
 try{
  assert.throws(()=>client.serviceUrl('http://remote.example'),/HTTPS/);assert.throws(()=>client.serviceUrl('https://username:password@example.com'),/credentials/);
  const status=await client.connectAnalysis('https://analysis.example.com',`invite_${'b'.repeat(43)}`);assert.equal(status.connected,true);assert.equal(JSON.stringify(status).includes(token),false);
  assert.equal((await stat(join(dir,'analysis-connection.json'))).mode&0o777,0o600);assert.ok((await readFile(join(dir,'analysis-connection.json'),'utf8')).includes(token));
  await client.disconnectAnalysis();assert.equal(revoked,true);assert.equal((await client.connectionStatus()).connected,false);
 }finally{globalThis.fetch=original;await rm(dir,{recursive:true,force:true});}
});
