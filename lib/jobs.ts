import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { getReport, saveReport } from './db';
export function startReportWorker(id:string){
 const worker=spawn(process.execPath,['--import','tsx',join(process.cwd(),'scripts/report-worker.ts'),id],{cwd:process.cwd(),detached:true,stdio:'ignore'});
 const fail=()=>{const r=getReport(id);if(r&&(r.status==='queued'||r.status==='running')){r.status='failed';r.error='The report worker stopped unexpectedly. Please retry.';saveReport(r);}};
 worker.on('error',fail);worker.on('exit',code=>{if(code!==0)fail();});worker.unref();
}
