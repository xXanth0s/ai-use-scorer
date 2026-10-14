import { readdir, stat, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, basename, dirname } from 'node:path';
import { fingerprint, saveSession, setState, getState, listSessions } from './db';
import { parseSession, sessionKey } from './parser';
import { entireTranscripts } from './entire';
import type { EntireEntry } from './entire';
import type { Agent } from './types';
import { capturedTranscripts } from './capture-hooks';
async function files(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true }).catch((e: NodeJS.ErrnoException) => { if (e.code === 'ENOENT') return []; throw e; });
  const nested = await Promise.all(entries.map(e => e.isDirectory() ? files(join(root, e.name)) : Promise.resolve(e.isFile() && e.name.endsWith('.jsonl') ? [join(root,e.name)] : [])));
  return nested.flat();
}
export async function collect() {
  const roots: [Agent, string][] = [ ['codex', join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'sessions')], ['codex', join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'archived_sessions')], ['claude', join(process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude'), 'projects')] ];
  let scanned = 0, changed = 0, excluded = 0; const errors: string[] = [];
  const settings=getState('settings');
  const fromEntire=await entireTranscripts(settings?.entireRepos ?? []);
  errors.push(...fromEntire.errors);
  const candidates: ({agent:Agent;path:string}&Partial<EntireEntry>&{hookSnapshot?:boolean;hookStorage?:'live'|'snapshot';parentNativeId?:string|null})[]=[...await capturedTranscripts(),...fromEntire.entries];
  const existing=new Map(listSessions({allSources:true}).map(s=>[`${s.agent}:${s.nativeId}`,s.id]));
  for (const [agent, root] of settings?.nativeFallback===false?[]:roots) {
    let paths: string[]; try { paths = await files(root); } catch { errors.push(`Could not read ${agent} session directory.`); continue; }
    candidates.push(...paths.map(path=>({agent,path})));
  }
  const seenPaths=new Set<string>();
  const seenIds=new Set<string>();let entireCount=0;
  for(const candidate of candidates) {
      const {agent,path}=candidate;
      if(seenPaths.has(path))continue;seenPaths.add(path);
      scanned++;
      try {
        const st = await stat(path); const fp = candidate.hookSnapshot?`hooks-v2:${st.size}:${st.mtimeMs}`:candidate.repository?`v6:${candidate.storage}:${st.size}:${st.mtimeMs}`:`v5:${st.size}:${st.mtimeMs}`; const id = (candidate.nativeId&&existing.get(`${agent}:${candidate.nativeId}`))||sessionKey(agent,candidate.identityPath||path);
        if(seenIds.has(id))continue;seenIds.add(id);if(candidate.repository)entireCount++;
        if(candidate.identityPath)seenPaths.add(await realpath(candidate.identityPath).catch(()=>candidate.identityPath!));
        if (fingerprint(id) === fp) continue;
        if (st.size > 50 * 1024 * 1024) { errors.push(`${agent}: skipped a transcript larger than 50 MB.`); continue; }
        const session = await parseSession(agent,path,fp,id);
        const duplicate=existing.get(`${agent}:${session.nativeId}`);
        if(duplicate&&duplicate!==id){if(seenIds.has(duplicate))continue;session.id=duplicate;for(const e of session.events){e.id=e.id.replace(`${id}:`,`${duplicate}:`);e.sessionId=duplicate;}seenIds.add(duplicate);}
        existing.set(`${agent}:${session.nativeId}`,session.id);
        if(candidate.repository&&candidate.storage)session.capture={provider:'entire',repository:candidate.repository,storage:candidate.storage};
        if(candidate.hookSnapshot){session.capture={provider:'hooks',storage:candidate.hookStorage||'snapshot'};if(candidate.parentNativeId){session.parentId=candidate.parentNativeId;session.source='subagent';}}
        if(candidate.review)session.hidden=true;
        if (agent === 'claude' && basename(dirname(path)) === 'subagents') {
          session.source = 'subagent'; session.parentId = basename(dirname(dirname(path)));
        }
        saveSession(session); changed++; if (session.hidden) excluded++;
      } catch { errors.push(`${agent}: a transcript could not be read.`); }
  }
  const result = { syncedAt: new Date().toISOString(), scanned, changed, excluded, entire:entireCount, errors }; setState('collector', result); return result;
}
