import { readFile, writeFile, mkdir, rename, lstat, readdir, realpath, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { dataDir } from './db';
import type { Agent } from './types';
export const captureEvents=['SessionStart','SessionEnd','UserPromptSubmit','Stop','PostToolUse','SubagentStart','SubagentStop'];
export function agentHomes(){return {codex:resolve(/* turbopackIgnore: true */ process.env.CODEX_HOME||join(homedir(),'.codex')),claude:resolve(/* turbopackIgnore: true */ process.env.CLAUDE_CONFIG_DIR||join(homedir(),'.claude'))};}
export function captureRoots(){const homes=agentHomes();return {codex:[join(homes.codex,'sessions'),join(homes.codex,'archived_sessions')],claude:[join(homes.claude,'projects')]};}
const folder=()=>join(dataDir,'collector');
const target=(agent:Agent)=>join(agentHomes()[agent],agent==='codex'?'hooks.json':'settings.json');
const quote=(s:string)=>`'${s.replace(/'/g,`'\\''`)}'`;
const command=(agent:Agent)=>`${quote(process.execPath)} ${quote(join(folder(),'hook.mjs'))} ${agent} --hdyai-capture`;
function object(value:unknown):value is Record<string,any>{return !!value&&typeof value==='object'&&!Array.isArray(value);}
export function mergeCaptureHooks(config:unknown,cmd:string,remove=false){
 if(!object(config)||('hooks' in config&&!object(config.hooks)))throw new Error('Existing agent settings are invalid; they have been left unchanged.');
 const result=structuredClone(config);const hooks=result.hooks||{};
 for(const event of captureEvents){
  const groups=hooks[event]||[];if(!Array.isArray(groups)||groups.some(g=>!object(g)||!Array.isArray(g.hooks)))throw new Error('Existing hooks are invalid; they have been left unchanged.');
  const next=groups.map(g=>({...g,hooks:g.hooks.filter((h:any)=>h.command!==cmd)})).filter(g=>g.hooks.length);
  if(!remove)next.push({hooks:[{type:'command',command:cmd,timeout:event==='SessionEnd'?3:5}]});
  if(next.length)hooks[event]=next;else delete hooks[event];
 }
 result.hooks=hooks;return result;
}
async function config(agent:Agent){try{const info=await lstat(target(agent));if(!info.isFile()||info.isSymbolicLink()||info.size>1024*1024)throw new Error('Agent settings must be a regular file under 1 MB.');return JSON.parse(await readFile(target(agent),'utf8'));}catch(e){if((e as NodeJS.ErrnoException).code==='ENOENT')return {};throw e;}}
export async function captureStatus(){
 const worker=await lstat(join(folder(),'hook.mjs')).then(s=>s.isFile()).catch(()=>false);
 return Promise.all((['codex','claude'] as Agent[]).map(async agent=>{
  const detected=await lstat(agentHomes()[agent]).then(s=>s.isDirectory()).catch(()=>false);
  try{const c=await config(agent);const installed=worker&&captureEvents.every(event=>c.hooks?.[event]?.some((g:any)=>g.hooks?.some((h:any)=>h.command===command(agent))));return {agent,detected,installed,issue:null};}
  catch{return {agent,detected,installed:false,issue:'Existing agent settings need attention. Native session reading is still available.'};}
 }));
}
export async function installCaptureHooks(agents:Agent[]){
 if(process.platform==='win32')throw new Error('Capture hooks currently support macOS and Linux. Existing sessions can still be imported.');
 if(!agents.length||agents.some(a=>!['codex','claude'].includes(a)))throw new Error('No supported agents were selected.');
 await mkdir(folder(),{recursive:true,mode:0o700});
 // Package a dependency-free runner; hooks keep working when the web app closes.
 const runner=await readFile(resolve('scripts/capture-hook.mjs'),'utf8');
 await writeFile(join(folder(),'hook.mjs'),runner,{mode:0o600});
 await writeFile(join(folder(),'config.json'),JSON.stringify({roots:captureRoots(),active:true}),{mode:0o600});
 const results=[];
 for(const agent of [...new Set(agents)]){
  try{
   const current=await config(agent);const updated=mergeCaptureHooks(current,command(agent));
   if(JSON.stringify(current)!==JSON.stringify(updated)){
    // Never follow a symlinked config directory or overwrite malformed settings.
    if((await lstat(dirname(target(agent)))).isSymbolicLink())throw new Error('Symlinked agent settings cannot be changed automatically.');
    const old=await readFile(target(agent),'utf8').catch((e:NodeJS.ErrnoException)=>{if(e.code==='ENOENT')return null;throw e;});
    if(old!==null)await writeFile(join(folder(),`${agent}-backup-${crypto.randomUUID()}.json`),old,{mode:0o600,flag:'wx'});
    const temp=`${target(agent)}.${crypto.randomUUID()}.tmp`;await writeFile(temp,JSON.stringify(updated,null,2)+'\n',{mode:0o600,flag:'wx'});await rename(temp,target(agent));
   }
   results.push({agent,installed:true,issue:null});
  }catch{results.push({agent,installed:false,issue:'Could not install capture hooks. Existing settings were preserved; native session reading remains available.'});}
 }
 return results;
}
export async function capturedTranscripts(){
 const entries:{agent:Agent;path:string;identityPath:string;nativeId:string;parentNativeId:string|null;hookSnapshot:true;hookStorage:'live'|'snapshot'}[]=[];const root=await realpath(join(folder(),'sessions')).catch(()=>null);if(!root)return entries;
 for(const name of await readdir(root).catch(()=>[])){
  if(!/^[a-f0-9]{64}$/.test(name))continue;
  try{const meta=JSON.parse(await readFile(join(root,name,'metadata.json'),'utf8'));if(!['codex','claude'].includes(meta.agent)||typeof meta.nativeId!=='string'||typeof meta.originalPath!=='string')continue;
   const path=await realpath(join(root,name,'full.jsonl'));if(path!==join(root,name,'full.jsonl'))continue;
   const current=await stat(meta.originalPath).catch(()=>null);
   const live=!!current&&(current.size!==meta.originalSize||current.mtimeMs>Date.parse(meta.capturedAt));
   entries.push({agent:meta.agent as Agent,path:live?meta.originalPath:path,identityPath:meta.originalPath,nativeId:meta.nativeId,parentNativeId:meta.parentNativeId as string|null,hookSnapshot:true as const,hookStorage:live?'live' as const:'snapshot' as const});
  }catch{/* A failed/incomplete snapshot falls back to the original transcript. */}
 }
 return entries;
}
