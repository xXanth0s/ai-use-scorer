import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { isAbsolute } from 'node:path';
import { realpath } from 'node:fs/promises';
const exec=promisify(execFile);
export const entireBinary=()=>process.env.ENTIRE_BIN||'entire';
export async function entireStatus(repo:string){
 try{const {stdout}=await exec(entireBinary(),['status','--json'],{cwd:repo,timeout:15000,maxBuffer:1024*1024});const s=JSON.parse(stdout);return {repository:repo,enabled:s.enabled===true,agents:Array.isArray(s.agents)?s.agents:[],codexTrust:s.codex_hooks?.state||null,pushDisabled:s.checkpoint_push_disabled===true};}
 catch{return {repository:repo,enabled:false,agents:[],codexTrust:null,pushDisabled:false};}
}
export async function entireAvailability(){try{const {stdout}=await exec(entireBinary(),['version'],{timeout:5000,maxBuffer:10000});return {available:true,version:stdout.split('\n')[0]};}catch{return {available:false,version:null};}}
export async function enableEntire(repo:string,agents:('codex'|'claude-code')[],initialize=false){
 if(!isAbsolute(repo)||!agents.length||agents.some(a=>!['codex','claude-code'].includes(a)))throw new Error('Choose an absolute workspace path and at least one supported agent.');
 const root=await realpath(repo);
 try{await exec('git',['-C',root,'rev-parse','--show-toplevel'],{timeout:5000});}catch{if(!initialize)throw new Error('This folder is not a Git repository. Enable “Create a local Git workspace” or choose an existing repository.');}
 const args=['enable','--agent',agents[0],'--local','--telemetry=false','--skip-push-sessions','--absolute-git-hook-path',initialize?'--init-repo':'--no-init-repo'];
 // Never import old history, choose a remote, or request generated summaries.
 await exec(entireBinary(),args,{cwd:root,timeout:60000,maxBuffer:1024*1024});
 for(const agent of agents.slice(1))await exec(entireBinary(),['agent','add',agent],{cwd:root,timeout:30000,maxBuffer:1024*1024});
 return entireStatus(root);
}
