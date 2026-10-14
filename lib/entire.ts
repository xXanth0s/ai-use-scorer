import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readdir, readFile, realpath, stat, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve, isAbsolute } from 'node:path';
import type { Agent } from './types';
import { createHash } from 'node:crypto';
import { dataDir } from './db';
const exec=promisify(execFile),maxBytes=50*1024*1024;
export type EntireEntry={agent:Agent;path:string;identityPath?:string;nativeId?:string;repository:string;storage:'staged'|'checkpoint'|'live';review?:boolean};
const digest=(s:string)=>createHash('sha256').update(s).digest('hex');
const agentOf=(a:unknown):Agent|null=>typeof a==='string'?/codex/i.test(a)?'codex':/claude/i.test(a)?'claude':null:null;
async function git(repo:string,args:string[],limit=1024*1024){return (await exec('git',['-C',repo,...args],{timeout:15000,maxBuffer:limit,env:{...process.env,GIT_NO_LAZY_FETCH:'1'}})).stdout;}
async function json(path:string){if((await stat(path)).size>1024*1024)throw new Error('Oversized Entire metadata.');return JSON.parse(await readFile(path,'utf8'));}
async function available(path:string){try{return await realpath(path);}catch{return null;}}
function objectPath(path:unknown){if(typeof path!=='string')return null;const p=path.replace(/^\/+/, '');return p&&!p.split('/').some(v=>v==='..'||v==='.'||!v)?p:null;}
async function blob(repo:string,ref:string,path:string){const size=Number((await git(repo,['cat-file','-s',`${ref}:${path}`])).trim());if(!Number.isFinite(size)||size>maxBytes)throw new Error('Entire transcript exceeds the 50 MB limit.');return git(repo,['show',`${ref}:${path}`],maxBytes+1024);}

// Read only local Git objects; never fetch checkpoint refs or push transcripts.
export async function entireTranscripts(repos:string[],cacheRoot=dataDir){
 const entries:EntireEntry[]=[],errors:string[]=[];const seen=new Set<string>();
 for(const configured of repos){try{
  const repo=await realpath(configured);const common=resolve(repo,(await git(repo,['rev-parse','--git-common-dir'])).trim());
  const stateDir=join(common,'entire-sessions');const states=await readdir(stateDir).catch(()=>[] as string[]);
  const missing=new Set<string>();
  for(const name of states.filter(n=>n.endsWith('.json'))){try{
   const s=await json(join(stateDir,name));const agent=agentOf(s.agent_type);if(!agent||typeof s.session_id!=='string')continue;
   const worktree=typeof s.worktree_path==='string'&&isAbsolute(s.worktree_path)?s.worktree_path:repo;
   if(resolve(worktree,(await git(worktree,['rev-parse','--git-common-dir'])).trim())!==common)continue;
   // Session IDs come from agent input: never permit them to escape metadata.
   if(!/^[A-Za-z0-9_-]{1,180}$/.test(s.session_id)){errors.push('An Entire session identifier was invalid.');continue;}
   const staged=await available(join(worktree,'.entire','metadata',s.session_id,'full.jsonl'));
   const original=typeof s.transcript_path==='string'&&isAbsolute(s.transcript_path)&&s.transcript_path.endsWith('.jsonl')?s.transcript_path:undefined;
   const live=original?await available(original):null;
   let path=staged||live,storage:EntireEntry['storage']=staged?'staged':'live';
   // A Stop copy excludes the next unfinished turn. Fill that live gap from
   // native evidence until the next Entire Stop replaces the staging copy.
   if(staged&&live&&s.phase==='active'&&(await stat(live)).mtimeMs>(await stat(staged)).mtimeMs){path=live;storage='live';}
   if(path){entries.push({agent,path,identityPath:original,nativeId:s.session_id,repository:worktree,storage,review:s.kind==='agent_review'});seen.add(`${agent}:${s.session_id}`);}
   else missing.add(`${agent}:${s.session_id}`);
  }catch{errors.push('An Entire session record could not be read.');}}
  const refs=(await git(repo,['for-each-ref','--sort=-committerdate','--format=%(refname) %(objectname)','refs/entire/checkpoints/','refs/heads/entire/checkpoints/v1'])).trim().split('\n').filter(Boolean);
  if(refs.length>200)errors.push('Entire checkpoint discovery is limited to the 200 most recent local refs.');
  const cache=join(cacheRoot,'entire',digest(common).slice(0,24));if(refs.length)await mkdir(cache,{recursive:true,mode:0o700});
  for(const row of refs.slice(0,200)){const [ref,oid]=row.split(' ');try{
   const indexPath=join(cache,`${oid}.index.json`);let cached:EntireEntry[]|null=null;
   try{cached=await json(indexPath);}catch{}
   if(Array.isArray(cached)&&(await Promise.all(cached.map(e=>typeof e.path==='string'&&e.path.startsWith(`${cache}/`)?available(e.path):null))).some(p=>!p))cached=null;
   if(!Array.isArray(cached)){
    cached=[];
    const paths=(await git(repo,['ls-tree','-r','--name-only',ref],4*1024*1024)).split('\n').filter(p=>/(^|\/)metadata\.json$/.test(p));
    for(const metaPath of paths){try{
     const summary=JSON.parse(await blob(repo,ref,metaPath));if(!Array.isArray(summary.sessions))continue;
     for(const files of summary.sessions){const metadataPath=objectPath(files.metadata),transcriptPath=objectPath(files.transcript);if(!metadataPath||!transcriptPath)continue;
      const m=JSON.parse(await blob(repo,ref,metadataPath));const agent=agentOf(m.agent);if(!agent||typeof m.session_id!=='string')continue;
      const path=join(cache,`${digest(`${oid}:${transcriptPath}`)}.jsonl`);
      if(!await available(path))await writeFile(path,await blob(repo,ref,transcriptPath),{mode:0o600,flag:'wx'});
      cached.push({agent,path,nativeId:m.session_id,repository:repo,storage:'checkpoint',review:m.kind==='agent_review'});
     }
    }catch{errors.push('An Entire checkpoint could not be imported.');}}
    await writeFile(indexPath,JSON.stringify(cached),{mode:0o600});
   }
   for(const e of cached){const key=`${e.agent}:${e.nativeId}`;if(seen.has(key))continue;if(!await available(e.path)){errors.push('A cached Entire checkpoint transcript is missing. Resync after removing its cache index.');continue;}seen.add(key);entries.push(e);}
  }catch{errors.push('An Entire checkpoint ref could not be read.');}}
  if([...missing].some(key=>!seen.has(key)))errors.push('An Entire session has no native, staged, or checkpoint transcript available.');
  if(!states.length&&!refs.length)errors.push('No Entire sessions recorded in this workspace yet. Hooks begin recording with the next supported agent session.');
 }catch{errors.push('Could not read an Entire workspace. Choose a Git repository and enable its hooks.');}}
 return {entries,errors};
}
