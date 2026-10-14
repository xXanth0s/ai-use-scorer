import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Session } from '../lib/types';

test('daily analysis selects the latest local calendar day, reuses unchanged evidence and versions changed evidence',async()=>{
 const root=await mkdtemp(join(tmpdir(),'trace-daily-'));process.env.AI_SESSION_DATA_DIR=root;process.env.REPORT_TIMEZONE='America/Los_Angeles';
 const db=await import('../lib/db');const daily=await import('../lib/daily');
 try{
  const session:Session={id:'s1',nativeId:'native',agent:'codex',title:'Research',cwd:'/work',path:'/work/session.jsonl',startedAt:'2026-10-14T01:00:00Z',updatedAt:'2026-10-14T08:00:00Z',parentId:null,source:'cli',hidden:false,userCount:2,assistantCount:0,toolCount:0,tools:[],malformedLines:0,truncated:false,fingerprint:'1',tasks:[],events:[{id:'s1:1',sessionId:'s1',line:1,timestamp:'2026-10-14T01:00:00Z',kind:'user',text:'Yesterday in Pacific time.'},{id:'s1:2',sessionId:'s1',line:2,timestamp:'2026-10-14T08:00:00Z',kind:'user',text:'Today in Pacific time.'}]};
  db.saveSession(session);assert.equal(daily.latestDay(),'2026-10-14');assert.equal(daily.dailySessions('2026-10-13')[0].events.length,1);
  const first=daily.prepareDaily('2026-10-14')!;assert.equal(first.fresh,true);
  const running=daily.prepareDaily('2026-10-14')!;assert.equal(running.fresh,false);assert.equal(running.report.id,first.report.id);
  first.report.status='complete';db.saveReport(first.report);
  assert.equal(daily.prepareDaily('2026-10-14')!.fresh,false);
  session.events.push({id:'s1:3',sessionId:'s1',line:3,timestamp:'2026-10-14T09:00:00Z',kind:'user',text:'A new decision.'});db.saveSession(session);
  const next=daily.prepareDaily('2026-10-14')!;assert.equal(next.fresh,true);assert.notEqual(next.report.id,first.report.id);assert.equal(db.getReport(first.report.id)!.snapshot[0].events.length,1);
 }finally{db.db().close();await rm(root,{recursive:true,force:true});}
});
