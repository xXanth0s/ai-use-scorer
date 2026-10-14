import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { sql, hash, secret, device } from '../services/analysis/agent/lib/store';
const db=sql(),id=`test-${randomUUID()}`,token=`hdyai_${secret()}`,invite=`invite_${secret()}`;
let admitted=0;
try{
 await db`INSERT INTO platform_users(id,daily_limit) VALUES(${id},2)`;
 await db`INSERT INTO activation_invites VALUES(${hash(invite)},${id},now()+interval '1 minute',false)`;
 const [activated]=await db`SELECT activate_device(${hash(invite)},${hash(token)}) AS user_id`;assert.equal(activated.user_id,id);
 await assert.rejects(db`SELECT activate_device(${hash(invite)},${hash(`hdyai_${secret()}`)})`,/INVALID_INVITE/);
 const auth=await device(new Request('https://analysis.test',{headers:{authorization:`Bearer ${token}`}}));assert.equal(auth.userId,id);
 await assert.rejects(device(new Request('https://analysis.test')),/Activate/);
 const job1=`${id}-one`;
 const [a,b]=await Promise.all([db`SELECT * FROM reserve_analysis(${hash(token)},${job1},'same','[]'::jsonb)`,db`SELECT * FROM reserve_analysis(${hash(token)},${job1},'same','[]'::jsonb)`]);admitted++;assert.equal(a[0].id,b[0].id);
 await assert.rejects(db`SELECT * FROM reserve_analysis(${hash(token)},${job1},'different','[]'::jsonb)`,/IDEMPOTENCY_CONFLICT/);
 await assert.rejects(db`SELECT * FROM reserve_analysis(${hash(token)},${id+'-blocked'},'same','[]'::jsonb)`,/ALREADY_RUNNING/);
 await db`UPDATE analysis_jobs SET status='complete' WHERE id=${job1}`;
 await db`SELECT * FROM reserve_analysis(${hash(token)},${id+'-two'},'same','[]'::jsonb)`;admitted++;
 await assert.rejects(db`SELECT * FROM reserve_analysis(${hash(token)},${id+'-three'},'same','[]'::jsonb)`,/USER_QUOTA/);
 await db`UPDATE device_credentials SET revoked=true WHERE hash=${hash(token)}`;
 await assert.rejects(device(new Request('https://analysis.test',{headers:{authorization:`Bearer ${token}`}})),/revoked/);
 await assert.rejects(db`SELECT * FROM reserve_analysis(${hash(token)},${id+'-four'},'same','[]'::jsonb)`,/ACCESS_DISABLED/);
 console.log('Neon checks passed: one-use activation, anonymous rejection, idempotency under concurrent calls, concurrency limit, daily quota, and device revocation.');
}finally{
 await db.transaction([db`DELETE FROM analysis_jobs WHERE user_id=${id}`,db`DELETE FROM activation_invites WHERE user_id=${id}`,db`DELETE FROM device_credentials WHERE user_id=${id}`,db`DELETE FROM platform_users WHERE id=${id}`,db`UPDATE daily_usage SET requests=greatest(0,requests-${admitted}) WHERE day=(now() AT TIME ZONE 'UTC')::date`]);
}
