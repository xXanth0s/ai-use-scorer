// Headless plugin runtime. No web server or global hook/config modification.
import { mkdir, rm, stat, writeFile, utimes } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { resolve, join } from 'node:path';
import { collect } from '../lib/collector';
import { connectAnalysis, connectionStatus, disconnectAnalysis, publishLibrary } from '../lib/analysis-client';
import { dataDir, getReport, getSession, getState, listSessions, setState, saveReport } from '../lib/db';
import { dayOf, prepareDaily } from '../lib/daily';
import { generateReport } from '../lib/report';
import { publishedView, sessionSummaries } from '../lib/publish';
import { captureRoots } from '../lib/capture-hooks';
import { defaultAnalysisService } from '../lib/onboarding';
const [action,arg]=process.argv.slice(2);
try {
await mkdir(join(dataDir,'collector'),{recursive:true,mode:0o700});
if(action==='pair'){
 if(process.argv.length>4)throw new Error('Pairing codes must be supplied over stdin.');
 let input='';for await(const chunk of process.stdin){input+=chunk;if(input.length>200)throw new Error('Invalid pairing input.');}const code=input.trim();
 await connectAnalysis(arg||defaultAnalysisService,code);
 await writeFile(join(dataDir,'collector','config.json'),JSON.stringify({roots:captureRoots(),active:true}),{mode:0o600});
 setState('plugin:paused',false);
 console.log('Plugin paired. Capture is local; summaries and bounded report evidence sync to your account.');
}else if(action==='disconnect'){
 setState('plugin:paused',true);
 await writeFile(join(dataDir,'collector','config.json'),JSON.stringify({roots:captureRoots(),active:false}),{mode:0o600});await disconnectAnalysis();console.log('Plugin disconnected.');
}else if(action==='status'){
 const c=await connectionStatus();console.log(JSON.stringify({connected:c.connected,lastSync:getState('plugin:lastSync'),lastError:getState('plugin:lastError')}));
}else if(action==='hook'){
 const agent=arg;if(!['codex','claude'].includes(agent))process.exit(0);
 const child=spawn(process.execPath,[resolve('scripts/capture-hook.mjs'),agent],{stdio:['pipe','ignore','ignore'],env:{...process.env,HDYAI_CAPTURE_DIR:join(dataDir,'collector')}});
 child.stdin.on('error',()=>{});process.stdin.pipe(child.stdin);
 const budget=setTimeout(()=>child.kill('SIGKILL'),1500);
 await new Promise(r=>{child.once('exit',r);child.once('error',r);});clearTimeout(budget);process.stdin.unpipe(child.stdin);process.stdin.pause();
 const background=spawn(process.execPath,[process.argv[1],'sync'],{detached:true,stdio:'ignore',env:process.env});background.unref();console.log('{}');
}else if(action==='sync'){
 if(getState('plugin:paused')===true)process.exit(0);
 const lock=join(dataDir,'sync-lock');
 try{const old=await stat(lock);if(Date.now()-old.mtimeMs>10*60*1000)await rm(lock,{recursive:true,force:true});}catch{}
 try{await mkdir(lock,{mode:0o700});}catch{process.exit(0);}
 const lease=setInterval(()=>{const now=new Date();void utimes(lock,now,now).catch(()=>{});},30000);lease.unref();
 try{
  if(!(await connectionStatus()).connected)process.exitCode=0;
  else if(Date.now()-Date.parse(getState('plugin:lastSync')||'1970-01-01')>=15000){
   await collect();const sessions=sessionSummaries(listSessions()),reportName=String(getState('candidateName')||'Candidate');
   const sync=async(report?:ReturnType<typeof publishedView>)=>{const payload={sessions,reportName,...(report?{report}:{})};if(Buffer.byteLength(JSON.stringify(payload))>950000)throw new Error('Sync exceeds the current size limit.');await publishLibrary(payload);setState('plugin:lastSync',new Date().toISOString());};
   await sync();
   const today=dayOf(new Date().toISOString());
   const date=listSessions().filter(s=>!s.parentId).flatMap(s=>getSession(s.id)!.events.filter(e=>e.kind==='user'&&e.timestamp).map(e=>dayOf(e.timestamp))).filter(day=>day<today).sort().at(-1)||null;
   // Resume every unfinished publication, including older days after a newer day arrives.
   const pending=(getState('plugin:pendingReports')||[]) as string[];
   if(date&&!getState(`plugin:analysis:${date}`)){
    const prepared=prepareDaily(date);
    if(prepared){
     if(!pending.includes(prepared.report.id))pending.push(prepared.report.id);
     // Persist the queue first: a crash here cannot lose the prepared report.
     setState('plugin:pendingReports',pending);
     setState(`plugin:analysis:${date}`,{attemptedAt:new Date().toISOString(),id:prepared.report.id});
    }
   }
   // Migrate a first-attempt marker from the original plugin prototype.
   const previous=date?getState(`plugin:analysis:${date}`):null;
   if(previous?.id&&!previous.publishedAt&&!getState(`plugin:published:${previous.id}`)&&!pending.includes(previous.id))pending.push(previous.id);
   setState('plugin:pendingReports',pending);
   for(const id of [...pending]){
    let report=getReport(id);if(!report)throw new Error('Pending report unavailable.');
    await sync(publishedView(report));
    if(report.status==='running'||report.status==='failed'){
     // The sync lock guarantees no local analyzer is still working. Hosted retries
     // use the original requestId and snapshot; they do not reserve another job.
     report.status='queued';saveReport(report);
    }
    if(report.status==='queued')await generateReport(id);
    report=getReport(id)!;
    await sync(publishedView(report));
    if(report.status!=='complete')throw new Error('Assessment has not completed.');
    setState(`plugin:published:${id}`,new Date().toISOString());
    pending.splice(pending.indexOf(id),1);setState('plugin:pendingReports',pending);
    if(previous?.id===id)setState(`plugin:analysis:${date}`,{...previous,publishedAt:new Date().toISOString()});
   }
   setState('plugin:lastError',null);
  }
 }catch{setState('plugin:lastError','Sync could not finish. Run plugin status, check the connection, and retry.');}
 finally{clearInterval(lease);await rm(lock,{recursive:true,force:true});}
}else{console.error('Use pair, status, sync, disconnect or hook.');process.exitCode=1;}

}catch{if(action==='hook'){console.log('{}');}else{console.error('Plugin action could not finish. Check the connection and runtime, then retry.');process.exitCode=1;}}
