// Lifecycle adapter inspired by Entire's MIT-licensed Codex/Claude adapters.
// See THIRD_PARTY_NOTICES.md. Runs independently of Next.js, tsx, Git and Entire.
import { readFile, writeFile, appendFile, mkdir, realpath, stat, rename, rm } from 'node:fs/promises';
import { dirname, join, relative, isAbsolute } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const agent = process.argv[2];
const base = process.env.HDYAI_CAPTURE_DIR || dirname(fileURLToPath(import.meta.url));
const events = new Set(['SessionStart','SessionEnd','UserPromptSubmit','Stop','PostToolUse','SubagentStart','SubagentStop']);
const clean = text => text.replace(/\b(?:invite_[A-Za-z0-9_-]{43}|hdyai_[A-Za-z0-9_-]{43}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g,'[REDACTED TOKEN]').replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]{12,}/gi,'$1[REDACTED]').replace(/((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*["']?)[^\s"',;}]{8,}/gi,'$1[REDACTED]');
function sanitize(value) {
 if (typeof value === 'string') return clean(value);
 if (Array.isArray(value)) return value.map(sanitize);
 if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v])=>[k, /^(?:api[_-]?key|access[_-]?token|password|secret|authorization)$/i.test(k)?'[REDACTED]':sanitize(v)]));
 return value;
}
async function snapshot(config, input, source, id, parentId) {
 if(typeof source!=='string'||!isAbsolute(source)||!source.endsWith('.jsonl')||typeof id!=='string'||!id||id.length>180) return;
 const originalPath=await realpath(source);
 const allowed=await Promise.all(config.roots[agent].map(p=>realpath(p).catch(()=>null)));
 if(!allowed.some(p=>{if(!p)return false;const r=relative(p,originalPath);return r&&!r.startsWith('..')&&!isAbsolute(r);})) return;
 const info=await stat(originalPath);if(!info.isFile()||info.size>50*1024*1024)return;
 const key=createHash('sha256').update(`${agent}:${id}`).digest('hex');
 const folder=join(base,'sessions',key);await mkdir(folder,{recursive:true,mode:0o700});
 // A per-session lock prevents overlapping hooks from rolling a snapshot back.
 const lock=join(folder,'lock');
 // Recover a lock abandoned by a killed hook; the normal budget is <= 5s.
 try{const old=await stat(lock);if(Date.now()-old.mtimeMs>60000)await rm(lock,{recursive:true,force:true});}catch{}
 try{await mkdir(lock,{mode:0o700});}catch{return;}
 try {
  const prior=await readFile(join(folder,'metadata.json'),'utf8').then(JSON.parse).catch(()=>null);
  if(input.hook_event_name==='PostToolUse'&&prior&&Date.now()-Date.parse(prior.capturedAt)<2000)return;
  if(agent==='claude'&&['Stop','SubagentStop','SessionEnd'].includes(input.hook_event_name))await new Promise(r=>setTimeout(r,100));
  const raw=await readFile(originalPath,'utf8');
  // Keep line positions stable; ignore a partially flushed final JSONL record.
  const lastNewline=raw.lastIndexOf('\n');
  const tail=raw.slice(lastNewline+1);
  let complete=raw.slice(0,lastNewline+1);
  try{if(tail.trim()){JSON.parse(tail);complete+=tail+'\n';}}catch{/* partially flushed record */}
  const sanitized=complete.split('\n').map(line=>{try{return JSON.stringify(sanitize(JSON.parse(line)));}catch{return '';}}).join('\n');
  const nonce=randomUUID();const temp=join(folder,`snapshot-${nonce}.tmp`);
  await writeFile(temp,sanitized,{mode:0o600,flag:'wx'});await rename(temp,join(folder,'full.jsonl'));
  const metadata={agent,nativeId:id,parentNativeId:parentId||null,originalPath,originalSize:Buffer.byteLength(raw),capturedAt:new Date().toISOString(),lastEvent:input.hook_event_name,cwd:typeof input.cwd==='string'?input.cwd.slice(0,2000):''};
  const metaTemp=join(folder,`metadata-${nonce}.tmp`);await writeFile(metaTemp,JSON.stringify(metadata),{mode:0o600,flag:'wx'});await rename(metaTemp,join(folder,'metadata.json'));
  // Record only lifecycle facts, never tool arguments, prompts or credentials.
  await appendFile(join(folder,'events.jsonl'),JSON.stringify({at:metadata.capturedAt,event:input.hook_event_name,turnId:typeof input.turn_id==='string'?input.turn_id.slice(0,180):null})+'\n',{mode:0o600});
 }finally{await rm(lock,{recursive:true,force:true});}
}
try {
 if(!['codex','claude'].includes(agent))throw new Error('Unsupported agent');
 let raw='';for await (const chunk of process.stdin){raw+=chunk;if(raw.length>1024*1024)throw new Error('Hook payload too large');}
 const input=JSON.parse(raw);if(!events.has(input.hook_event_name))throw new Error('Unsupported event');
 const config=JSON.parse(await readFile(join(base,'config.json'),'utf8'));
 if(config.active===false)throw new Error('Capture paused');
 await snapshot(config,input,input.transcript_path,input.session_id,null);
 if(input.hook_event_name==='SubagentStop')await snapshot(config,input,input.agent_transcript_path,input.agent_id,input.session_id);
}catch {
 // Observational hooks must never interrupt a user's task or expose payloads.
}
process.stdout.write('{}\n');
