import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import type { Agent, Evidence, Session, TokenTotals, TokenUsageRecord } from './types';

const tokenNumber = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
function nativeTokens(value: unknown, agent: Agent): TokenTotals | null {
  if (!value || typeof value !== 'object') return null;
  const u = value as Record<string, unknown>;
  if (!tokenNumber(u.input_tokens) || !tokenNumber(u.output_tokens)) return null;
  const cached = agent === 'claude' ? u.cache_read_input_tokens : u.cached_input_tokens;
  const written = agent === 'claude' ? u.cache_creation_input_tokens : u.cache_write_input_tokens;
  if ((cached !== undefined && !tokenNumber(cached)) || (written !== undefined && !tokenNumber(written))) return null;
  const cachedInputTokens = (cached as number | undefined) ?? 0;
  const cacheWriteInputTokens = (written as number | undefined) ?? 0;
  // Claude reports three disjoint input categories. Codex input includes cached input.
  const inputTokens = u.input_tokens + (agent === 'claude' ? cachedInputTokens + cacheWriteInputTokens : 0);
  const totalTokens = inputTokens + u.output_tokens;
  if (!Number.isSafeInteger(totalTokens)) return null;
  return {inputTokens,outputTokens:u.output_tokens,cachedInputTokens,cacheWriteInputTokens,totalTokens};
}

function eventTime(value: unknown, fallback: string): string { if(typeof value==='number')return new Date(value<1e12?value*1000:value).toISOString();return typeof value==='string'?value:fallback; }
export const sessionKey = (agent: Agent, path: string) => createHash('sha256').update(`${agent}:${path}`).digest('hex').slice(0, 24);
export function redact(text: string): string {
  return text.replace(/\b(?:invite_[A-Za-z0-9_-]{43}|hdyai_[A-Za-z0-9_-]{43}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g, '[REDACTED TOKEN]')
    .replace(/((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*["']?)[^\s"',;}]{8,}/gi, '$1[REDACTED]')
    .replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]{12,}/gi, '$1[REDACTED]');
}
function contentText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map(b => typeof b === 'string' ? b : b?.text ?? (b?.type === 'image' || b?.type === 'input_image' ? '[Image attachment; pixels not ingested]' : '')).filter(Boolean).join('\n');
  return content == null ? '' : JSON.stringify(content);
}
export function humanText(text: string): string {
  if (/^\s*(# AGENTS\.md|<environment_context>|<external_|<turn_aborted>|<permissions instructions>|## Code review guidelines:|The following is the Codex agent history|\[Automated)/.test(text)) return '';
  if (text.includes('## My request:')) return text.split('## My request:').slice(1).join('## My request:').trim();
  if (text.includes('<send_user_message_question_reply>')) {
    try { return JSON.parse(text.replace(/<\/?send_user_message_question_reply>/g, '').trim()).map((a: {question: string; answer: string}) => `Question: ${a.question}\nCandidate answer: ${a.answer}`).join('\n'); } catch { return ''; }
  }
  if (text.includes('# Browser comments:')) {
    // Preserve candidate comments but discard the supplied page instructions and markup.
    const comments = [...text.matchAll(/(?:User comment|Comment text|Comment):\s*([\s\S]*?)(?=\n(?:## |The next image|Untrusted page evidence)|$)/gi)].map(m => m[1].trim());
    return comments.length ? comments.join('\n') : '[Candidate supplied an annotated browser screenshot; comment text unavailable]';
  }
  return text.trim();
}
export async function parseSession(agent: Agent, path: string, fingerprint: string, identity= sessionKey(agent,path)): Promise<Session> {
  const id = identity;
  const s: Session = { id, nativeId: id, agent, title: '', cwd: '', path, startedAt: '', updatedAt: '', parentId: null, source: '', hidden: false, userCount: 0, assistantCount: 0, toolCount: 0, tools: [], malformedLines: 0, truncated: false, events: [], fingerprint, tasks: [] };
  const seen = new Set<string>(); const toolNames = new Set<string>(); let line = 0;
  const usage: TokenUsageRecord[] = [], finals = new Map<string, {id:string;timestamp:string}>();
  const claudeUsage = new Map<string, TokenUsageRecord>();
  const finalTurns = new Set<string>();
  let cumulative: TokenTotals | null = null, finalOutputCountKnown = false, currentTurn = '', turnHasFinal = false;
  const final = (key:string,timestamp:string) => { finals.set(key,{id:key,timestamp}); finalOutputCountKnown = true; };
  function add(kind: Evidence['kind'], text: string, timestamp: string, suffix = '', opts: Partial<Evidence> = {}, dedup = '') {
    if (!text.trim()) return;
    if (dedup && seen.has(dedup)) return;
    if (dedup) seen.add(dedup);
    const truncated = text.length > 12000;
    const e: Evidence = { id: `${id}:${line}${suffix}`, sessionId: id, line, timestamp, kind, text: redact(text.slice(0, 12000)), ...opts, truncated };
    s.events.push(e); s.truncated ||= truncated;
    if (timestamp) { s.startedAt ||= timestamp; s.updatedAt = timestamp; }
    if (kind === 'user') { s.userCount++; if (!s.title) s.title = text.replace(/\s+/g, ' ').slice(0, 150); }
    if (kind === 'assistant') s.assistantCount++;
    if (kind === 'tool_call') { s.toolCount++; if (opts.tool) toolNames.add(opts.tool); }
  }
  const stream = createReadStream(path, { encoding: 'utf8' });
  const reader = createInterface({ input: stream, crlfDelay: Infinity });
  for await (const raw of reader) {
    line++;
    let r; try { r = JSON.parse(raw); } catch { if (raw.trim()) s.malformedLines++; continue; }
    const timestamp = typeof r.timestamp === 'string' ? r.timestamp : '';
    if (agent === 'codex') {
      const p = r.payload ?? {};
      if (r.type === 'event_msg' && p.type === 'token_count') {
        const total = nativeTokens(p.info?.total_token_usage, agent);
        const last = nativeTokens(p.info?.last_token_usage, agent);
        if (total) {
          let delta: TokenTotals | null;
          // An initial cumulative counter can include history outside this log/day.
          // Without a last-request count, establish a baseline rather than invent attribution.
          if (!cumulative) delta = last;
          else if (total.totalTokens === cumulative.totalTokens) delta = null;
          else if (total.inputTokens < cumulative.inputTokens || total.outputTokens < cumulative.outputTokens) delta = last;
          else delta = {inputTokens:total.inputTokens-cumulative.inputTokens,outputTokens:total.outputTokens-cumulative.outputTokens,cachedInputTokens:Math.max(0,total.cachedInputTokens-cumulative.cachedInputTokens),cacheWriteInputTokens:Math.max(0,total.cacheWriteInputTokens-cumulative.cacheWriteInputTokens),totalTokens:total.totalTokens-cumulative.totalTokens};
          cumulative = total;
          if (delta) usage.push({...delta,id:`${id}:usage:${line}`,timestamp});
        }
      }
      if (r.type === 'session_meta') {
        s.nativeId = p.session_id ?? p.id ?? id; s.cwd = p.cwd ?? ''; s.parentId = p.parent_thread_id ?? null;
        s.source = typeof p.source === 'string' ? p.source : JSON.stringify(p.source ?? {});
        s.hidden = p.thread_source === 'guardian_review' || s.source.includes('guardian');
      }
      if (r.type === 'event_msg' && ['task_started','task_complete','turn_aborted'].includes(p.type)) {
        const tid = p.turn_id ?? p.root_turn_id ?? String(line);
        if (p.type === 'task_started') {currentTurn=tid;turnHasFinal=false;finalOutputCountKnown=true;}
        if (p.type === 'task_complete') {
          finalOutputCountKnown=true;
          // Older logs expose only the completed turn's last answer. Never count both.
          if (!turnHasFinal && !finalTurns.has(tid) && typeof p.last_agent_message === 'string' && p.last_agent_message.trim()) final(`turn:${tid}`,eventTime(p.completed_at,timestamp));
          finalTurns.add(tid);
          currentTurn='';turnHasFinal=false;
        }
        if (p.type === 'turn_aborted') {currentTurn='';turnHasFinal=false;finalOutputCountKnown=true;}
        let task = s.tasks.find(t => t.id === tid);
        if (!task) { task = {id:tid,start:eventTime(p.started_at,timestamp),end:null,aborted:false,reason:null}; s.tasks.push(task); }
        if (p.type !== 'task_started') { task.end = eventTime(p.completed_at,timestamp); task.aborted = p.type === 'turn_aborted'; task.reason = p.reason ?? null; }
      }
      if (r.type === 'compacted') { s.truncated = true; continue; }
      if (r.type !== 'response_item') continue;
      if (p.type === 'message' && (p.role === 'user' || p.role === 'assistant')) {
        const text = contentText(p.content);
        if (p.role === 'assistant') {
          if (p.channel || p.phase) finalOutputCountKnown=true;
          if ((p.channel === 'final' || p.phase === 'final_answer') && text.trim()) {final(p.id ? `message:${p.id}` : currentTurn ? `turn:${currentTurn}` : `message:${line}`,timestamp);turnHasFinal=true;if(currentTurn)finalTurns.add(currentTurn);}
        }
        add(p.role, p.role === 'user' ? humanText(text) : text, timestamp, '', {}, p.id ? `message:${p.id}` : '');
      } else if (p.type === 'function_call' || p.type === 'custom_tool_call') {
        add('tool_call', contentText(p.arguments ?? p.input), timestamp, '', { tool: p.namespace ? `${p.namespace}.${p.name}` : p.name, callId: p.call_id }, `call:${p.call_id ?? line}`);
      } else if (p.type === 'function_call_output' || p.type === 'custom_tool_call_output') {
        add('tool_result', contentText(p.output), timestamp, '', { callId: p.call_id }, `result:${p.call_id ?? line}`);
      }
    } else {
      s.nativeId = r.sessionId ?? s.nativeId; s.cwd = r.cwd ?? s.cwd;
      if (r.agentId) s.source = 'subagent';
      const m = r.message;
      if (!m || r.isMeta || r.isCompactSummary) { if (r.isCompactSummary) s.truncated = true; continue; }
      const blocks = Array.isArray(m.content) ? m.content : [{ type: 'text', text: m.content }];
      const text = blocks.filter((b: {type: string}) => b.type === 'text').map((b: {text: string}) => b.text).join('\n');
      if (r.type === 'assistant') {
        const key = m.id ?? r.requestId ?? r.uuid;
        const u = nativeTokens(m.usage, agent);
        if (key && u) {
          const prior = claudeUsage.get(key);
          // Streaming snapshots may repeat a message ID with a larger output count.
          if (!prior) claudeUsage.set(key,{...u,id:`message:${key}`,timestamp});
          else {
            const inputTokens=Math.max(prior.inputTokens,u.inputTokens),outputTokens=Math.max(prior.outputTokens,u.outputTokens);
            claudeUsage.set(key,{id:prior.id,timestamp:u.outputTokens>=prior.outputTokens?timestamp:prior.timestamp,inputTokens,outputTokens,cachedInputTokens:Math.max(prior.cachedInputTokens,u.cachedInputTokens),cacheWriteInputTokens:Math.max(prior.cacheWriteInputTokens,u.cacheWriteInputTokens),totalTokens:inputTokens+outputTokens});
          }
        }
        if (m.stop_reason != null) finalOutputCountKnown=true;
        if (key && m.stop_reason === 'end_turn' && text.trim() && !blocks.some((b:{type:string})=>b.type==='tool_use')) final(`message:${key}`,timestamp);
      }
      if (r.type === 'user') add('user', humanText(text), timestamp, '', {}, r.uuid ? `message:${r.uuid}` : '');
      if (r.type === 'assistant') add('assistant', text, timestamp, '', {}, r.uuid ? `message:${r.uuid}` : '');
      blocks.forEach((b: {type: string; input: unknown; name: string; id: string; content: unknown; tool_use_id: string; is_error: boolean}, i: number) => {
        if (b.type === 'tool_use') add('tool_call', contentText(b.input), timestamp, `:${i}`, {tool: b.name, callId: b.id}, `call:${b.id}`);
        if (b.type === 'tool_result') add('tool_result', contentText(b.content), timestamp, `:${i}`, {callId: b.tool_use_id, error: b.is_error}, `result:${b.tool_use_id}`);
      });
    }
  }
  s.tools = [...toolNames]; s.title ||= s.source === 'subagent' ? 'Subagent session' : 'Session without candidate messages';
  s.tokenUsage = {records:agent === 'claude' ? [...claudeUsage.values()] : usage,finalOutputs:[...finals.values()],finalOutputCountKnown};
  return s;
}
