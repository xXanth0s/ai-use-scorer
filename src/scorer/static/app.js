/* scorer dashboard — vanilla JS against the serve.py JSON API. No build step. */
'use strict';

/* ---------------- tiny DOM + format helpers ---------------- */
const $ = (s, r = document) => r.querySelector(s);
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
const icon = (id, cls = 'icon') => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const SVGNS = 'http://www.w3.org/2000/svg';

const nf1 = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });
const isNum = (v) => typeof v === 'number' && isFinite(v);
const pct = (v) => (isNum(v) ? `${Math.round(v * 100)}%` : '—');
const usd = (v) => (isNum(v) ? `$${v.toFixed(2)}` : '—');
const num = (v) => (isNum(v) ? nf1.format(v) : '—');
function dur(s) {
  if (!isNum(s)) return '—';
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
function tok(n) {
  if (!isNum(n)) return '—';
  if (n >= 1e6) return `${nf1.format(n / 1e6)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(Math.round(n));
}
const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '—');
const dayLabel = (d, opts = { weekday: 'long', day: 'numeric', month: 'long' }) =>
  new Date(`${d}T12:00:00`).toLocaleDateString('en-GB', opts);
const secs = (a, b) => (a && b ? (new Date(b) - new Date(a)) / 1000 : null);
function median(xs) {
  const v = xs.filter(isNum).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
const UNCAL = '<span class="chip-uncal" tabindex="0" title="Judge not yet calibrated (κ on 50 double-labelled turns pending)">uncalibrated</span>';
/* you / agent split: "you" = Pine (foreground), "agent" = muted grey, "none" = empty track (same as Leverage) */
function splitBar(segs, caption, label) {
  const tot = segs.reduce((a, g) => a + (isNum(g.n) ? g.n : 0), 0);
  const bar = tot ? segs.filter((g) => g.n > 0).map((g) => `<span class="${g.cls}" style="width:${((100 * g.n) / tot).toFixed(1)}%"></span>`).join('') : '';
  return `<div class="split"><div class="trk" role="img" aria-label="${esc(label || caption.replace(/<[^>]+>/g, ''))}">${bar}</div><div class="cap">${caption}</div></div>`;
}
const CAUSE_OWNER = { info_missing: 'you', requirement_changed: 'you', instruction_violated: 'agent', env_failure: 'environment' };
function ownerOf(cause, turnId, attr) {
  const c = attr && attr.corrections;
  if (c && turnId != null) {
    for (const [k, o] of [['user_preventable', 'you'], ['agent_error', 'agent'], ['environment', 'environment'], ['unclear', 'unclear']]) {
      if (c[k] && (c[k].turn_ids || []).includes(turnId)) return o;
    }
  }
  return CAUSE_OWNER[String(cause || '').replace(/^cause:/, '')] || 'unclear';
}
const ownerChip = (o) => `<span class="owner ${o}" title="${o === 'you' ? 'Preventable by you' : o === 'agent' ? 'The agent got it wrong' : o === 'environment' ? 'Environment or tooling failure' : 'Owner unclear'}">${o === 'you' ? 'you' : o === 'agent' ? 'agent' : o === 'environment' ? 'environment' : 'unclear'}</span>`;
const ltrunc = (txt, full) => `<span class="ltrunc" title="${esc(full || txt)}"><bdi>${esc(txt)}</bdi></span>`;
const pretty = (s) => String(s).replace(/^(cause|event|detection):/, '').replace(/[_:]/g, ' ');

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
async function pool(items, n, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k]); }
  }));
  return out;
}

/* ---------------- state + caches ---------------- */
const S = { days: [], today: null, day: null, tab: 'today', selTurn: null, trendRange: 14, includeTemp: false };
try { S.includeTemp = localStorage.getItem('scorer-include-temp') === '1'; } catch (e) { /* private mode */ }
const cache = { day: new Map(), turn: new Map(), trend: new Map() };
const getDay = (d) => { const k = `${d}|${S.includeTemp ? 1 : 0}`; if (!cache.day.has(k)) cache.day.set(k, api(`/api/day/${d}${S.includeTemp ? '?include_temp=1' : ''}`).catch((e) => { cache.day.delete(k); throw e; })); return cache.day.get(k); };
const getTurn = (id) => { if (!cache.turn.has(id)) cache.turn.set(id, api(`/api/turn/${id}`).catch((e) => { cache.turn.delete(id); throw e; })); return cache.turn.get(id); };
const getTrend = (end, n) => { const k = `${end}/${n}`; if (!cache.trend.has(k)) cache.trend.set(k, api(`/api/trend?end=${end}&days=${n}`)); return cache.trend.get(k); };

/* ---------------- metric vocabulary ---------------- */
const SCORED = [
  { k: 'V', letter: 'V', name: 'Verified edits', desc: 'Share of edit batches followed by a check (tests, build, run). Higher is better.', fmt: pct },
  { k: 'H', letter: 'H', name: 'Hand edits', desc: 'Edits you made by hand after the agent finished. Lower is better.', fmt: num },
  { k: 'B_min', letter: 'B', name: 'Blocked', desc: 'Minutes an agent sat waiting on you. Lower is better.', fmt: (v) => (isNum(v) ? `${nf1.format(v)} min` : '—') },
  { k: 'C', letter: 'C', name: 'Corrections', desc: 'Turns where you corrected or redirected the agent. Lower is better.', fmt: num },
];
const hUnmeasured = (d) => { const h = d.scored && d.scored.H; return !h || String(h.unit || '').endsWith('(partial)'); };
const TIER = {
  verified_pass: ['success', 'Verified'],
  verified_fail: ['destructive', 'Check failed'],
  inspected: ['', 'Inspected'],
  none: ['', 'Unverified'],
  no_edits: ['tag', 'No edits'],
};
const tierBadge = (t) => { const [c, l] = TIER[t] || ['tag', t || '—']; return `<span class="badge ${c}">${esc(l)}</span>`; };
const agentBadge = (a) => `<span class="badge"><span class="dot ${a === 'codex' ? 'codex' : 'claude'}"></span>${a === 'codex' ? 'Codex' : 'Claude'}</span>`;

/* ---------------- router ---------------- */
const TABS = ['today', 'sessions', 'trends', 'leverage'];
const TAB_NAMES = { today: 'Today', sessions: 'Sessions', trends: 'Trends', leverage: 'Leverage' };
function readHash() { const m = location.hash.match(/^#\/(\w+)/); return m && TABS.includes(m[1]) ? m[1] : 'today'; }
window.addEventListener('hashchange', () => { S.tab = readHash(); render(); });

let renderSeq = 0;
async function render() {
  const seq = ++renderSeq;
  document.querySelectorAll('.tabs a').forEach((a) => a.setAttribute('aria-current', a.dataset.tab === S.tab ? 'page' : 'false'));
  $('#crumb').textContent = TAB_NAMES[S.tab];
  const view = $('#view');
  if (!S.day) { view.innerHTML = '<div class="empty">No days ingested yet. Run <span class="mono">scorer ingest</span>.</div>'; return; }
  view.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const html = await VIEWS[S.tab]();
    if (seq !== renderSeq) return;
    view.innerHTML = html;
    AFTER[S.tab] && AFTER[S.tab]();
  } catch (e) {
    if (seq !== renderSeq) return;
    view.innerHTML = `<div class="empty">Could not load this view. ${esc(e.message)}</div>`;
  }
}

/* ================= Today ================= */
async function baselineFor(day) {
  const t = await getTrend(day, 8);
  const out = {};
  for (const k of ['V', 'H', 'B_min', 'C', 'active_h']) {
    const s = t.series[k] || [];
    out[k] = { base: median(s.slice(0, -1)), series: s, today: s[s.length - 1] };
  }
  return out;
}

function sparkline(series) {
  const W = 88, H = 24, P = 3;
  const vals = series.filter(isNum);
  if (vals.length < 2) return '<span class="muted">—</span>';
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = (i) => P + (i * (W - 2 * P)) / (series.length - 1);
  const y = (v) => H - P - ((v - lo) / span) * (H - 2 * P);
  let d = '', pen = false, last = null;
  series.forEach((v, i) => {
    if (!isNum(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true; last = [x(i), y(v)];
  });
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path d="${d}"/>${last ? `<circle cx="${last[0]}" cy="${last[1]}" r="2.25"/>` : ''}</svg>`;
}

function evidenceLinks(ev, max = 6) {
  const seen = new Map();
  for (const e of ev || []) if (e.turn_id != null && !seen.has(e.turn_id)) seen.set(e.turn_id, e.note || '');
  if (!seen.size) return '<span class="muted" style="font-size:13px">—</span>';
  const items = [...seen.entries()];
  const links = items.slice(0, max).map(([id, note]) => `<button type="button" data-turn="${id}" title="${esc(note)}">#${id}</button>`).join('');
  const more = items.length > max ? `<span class="muted">+${items.length - max}</span>` : '';
  return `<div class="evid">${links}${more}</div>`;
}

function scoredRows(d, base) {
  return SCORED.map((m) => {
    const row = m.k === 'C' ? null : d.scored[m.k];
    let today = m.k === 'C' ? base.C.today : row && row.value;
    let val = m.fmt(today);
    let extra = '';
    let ev = row ? row.evidence : [];
    if (m.k === 'H' && hUnmeasured(d)) val = '<span class="muted" style="font-weight:400">not measured</span>';
    if (m.k === 'V' && row) extra = `<div class="metric-break">${row.n} edit batches · ${pct(1 - row.value)} unverified</div>`;
    if (m.k === 'B_min' && row) extra = `<div class="metric-break">${row.n} waits</div>`;
    if (m.k === 'C') {
      const cs = d.scored.C || [];
      ev = cs.flatMap((c) => c.evidence || []);
      const causes = cs.filter((c) => /^C:cause:/.test(c.metric));
      extra = `<div class="metric-break">${causes.length ? causes.map((c) => `${esc(pretty(c.metric.slice(8)))} ${num(c.value)}`).join(' · ') : 'no causes labelled'}</div>`;
    }
    const b = base[m.k];
    return `<tr class="row">
      <td title="${esc(m.desc)}"><div class="metric-name"><span class="metric-letter">${m.letter}</span><span class="t">${m.name}</span></div>${extra}</td>
      <td class="num"><span class="metric-val">${val}</span></td>
      <td class="num">${m.k === 'H' && hUnmeasured(d) ? '—' : m.fmt(b.base)}</td>
      <td>${sparkline(b.series)}</td>
      <td>${evidenceLinks(ev, 4)}</td></tr>`;
  }).join('');
}

function diagnostics(d) {
  const g = {}; for (const x of d.diagnostics) g[x.metric] = x;
  const v = (k) => (g[k] ? g[k].value : null);
  const items = [
    ['Active agent time', dur(d.computed.active_s)],
    ['In parallel', dur(d.computed.overlap_s)],
    ['Turns', num(d.computed.turns)],
    ['Sessions', num(d.computed.sessions)],
    ['Interruptions', num(d.computed.interrupts)],
    ['Median gap between turns', dur(v('gap_median_s'))],
    ['Gaps <30s / 2m / 5m / 15m / break', ['lt30s', '30s-2m', '2-5m', '5-15m', 'break'].map((k) => num(v(`gap_bucket:${k}`) || 0)).join(' · ')],
    ['Gaps with a session switch', pct(v('switch_share'))],
    ['Orientation before first prompt', dur(v('orientation_s'))],
    ['Work thrown away on interrupt', dur(v('thrown_s'))],
    ['Tool calls', num(v('tool_calls'))],
    ['Tool errors', pct(v('tool_error_share'))],
    ['Context used, p50 / p90', `${num(v('context_p50'))}% / ${num(v('context_p90'))}%`],
    ['Tokens in / out', `${tok(v('tokens_in'))} / ${tok(v('tokens_out'))}`],
    ['Tokens from cache', tok(v('tokens_cached'))],
  ];
  const models = d.diagnostics.filter((x) => x.base === 'tokens' && x.key).sort((a, b) => b.value - a.value);
  for (const m of models) {
    const c = g[`cost_usd:${m.key}`];
    items.push([m.key, `${tok(m.value)} tok · ${usd(c && c.value)}`]);
  }
  items.push(['Total cost', usd(v('cost_usd'))]);
  if (v('unpriced_calls')) items.push(['Unpriced API calls', num(v('unpriced_calls'))]);
  return `<dl class="dl2">${items.map(([k, val]) => `<div><dt>${esc(k)}</dt><dd>${val}</dd></div>`).join('')}</dl>`;
}

/* ---- optional endpoints (404/501 -> fallback) ---- */
const missing = new Set();
async function optional(path) {
  const key = path.split('?')[0].replace(/\/\d{4}-\d{2}-\d{2}$/, '');
  if (missing.has(key)) return null;
  try {
    const r = await fetch(path, { cache: 'no-store' });
    if (r.status === 404 || r.status === 501) { if (r.status === 501 || /not_implemented|unknown/i.test(await r.text())) missing.add(key); return null; }
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}
cache.report = new Map(); cache.narr = new Map(); cache.summary = new Map();
cache.attr = new Map();
const getAttr = (d) => { if (!cache.attr.has(d)) cache.attr.set(d, optional(`/api/attribution?day=${d}`)); return cache.attr.get(d); };
const getReport = (d) => { const k = `${d}|${S.includeTemp ? 1 : 0}`; if (!cache.report.has(k)) cache.report.set(k, optional(`/api/report/${d}${S.includeTemp ? '?include_temp=1' : ''}`)); return cache.report.get(k); };
const getNarrative = (d) => { if (!cache.narr.has(d)) cache.narr.set(d, optional(`/api/narrative?day=${d}`)); return cache.narr.get(d); };

const turnSession = (d, tid) => d.sessions.find((s) => s.turns.some((t) => t.id === tid));
const noteQuote = (n) => { n = n || ''; const i = n.indexOf(': '); return i >= 0 ? n.slice(i + 2) : n; };

/* Facts in the shape of report.facts(); derived from /api/day when /api/report is missing. */
function factsFallback(d) {
  const sc = d.scored || {};
  const V = sc.V, B = sc.B_min;
  const vd = d.verdict || {};
  let text = vd.where || '';
  if (vd.basis === 'V' && V) text = `${Math.round(V.n * (1 - V.value))} of ${V.n} edit batches never verified.`;
  else if (vd.basis === 'B_min' && B) text = `${num(B.value)} min with an agent waiting on you.`;
  const basisRow = vd.basis && sc[vd.basis] && !Array.isArray(sc[vd.basis]) ? sc[vd.basis] : null;
  const unv = new Map();
  for (const e of (V && V.evidence) || []) {
    if (!/^unverified/.test(e.note || '')) continue;
    const s = turnSession(d, e.turn_id);
    const sid = s ? s.id : `?${e.turn_id}`;
    const prev = unv.get(sid);
    if (!prev || e.turn_id > prev.turn_id) unv.set(sid, { session_id: sid, turn_id: e.turn_id, tier: 'none', files: [noteQuote(e.note)], command: null });
  }
  const corrections = [];
  for (const c of (sc.C || []).filter((x) => /^C:cause:/.test(x.metric)).sort((a, b) => b.value - a.value)) {
    for (const e of c.evidence || []) corrections.push({ cause: c.metric.slice(8), turn_id: e.turn_id, quote: noteQuote(e.note), prevent: null });
  }
  const human = ((d.leverage && d.leverage.tooling && d.leverage.tooling.items) || []).filter((x) => x.initiator === 'human').sort((a, b) => b.count - a.count)[0];
  return {
    derived: true,
    verdict: { text, metric: vd.basis },
    tomorrow: { change: vd.change, evidence_turns: ((basisRow && basisRow.evidence) || []).map((e) => e.turn_id) },
    unverified_now: [...unv.values()],
    worst_window: null,
    blocked: (B && B.evidence) || [],
    corrections: corrections.slice(0, 4),
    pattern: human ? { kind: 'tooling', category: human.category, name: human.name, uses: human.count, turns: [] } : null,
  };
}

function turnLinks(ids, max = 5) {
  const u = [...new Set((ids || []).filter((x) => x != null))];
  if (!u.length) return '';
  return `<span class="evid" style="display:inline-flex">${u.slice(0, max).map((id) => `<button type="button" data-turn="${id}">#${id}</button>`).join('')}${u.length > max ? `<span class="muted">+${u.length - max}</span>` : ''}</span>`;
}

/* ---- Today ---- */
function line(n, title, summary, body, open = false) {
  return `<details class="rline"${open ? ' open' : ''}>
    <summary><span class="n">${n}</span><span class="rt">${title}</span><span class="rs">${summary}</span>${icon('chev', 'icon chev')}</summary>
    <div class="rb">${body}</div></details>`;
}

function deltaHTML(today, base, unit, lowerBetter) {
  if (!isNum(today) || !isNum(base)) return '<span>no 7-day median yet</span>';
  const diff = today - base;
  const fmt = (v) => (unit === '%' ? `${Math.round(v * 100)}%` : `${num(v)}${unit ? ` ${unit}` : ''}`);
  if (Math.abs(diff) < 1e-9) return `<span>same as the 7-day median</span>`;
  const worse = lowerBetter ? diff > 0 : diff < 0;
  const mag = unit === '%' ? `${Math.round(Math.abs(diff) * 100)} points` : `${num(Math.abs(diff))}${unit ? ` ${unit}` : ''}`;
  return `<span class="${worse ? 'worse' : 'better'}">${mag} ${diff > 0 ? 'more' : 'less'}</span> than the 7-day median (${fmt(base)})`;
}
/* V in plain words: "37% unverified · 7-day median 100%" */
function unverifiedDeltaHTML(unvShare, unvBase) {
  if (!isNum(unvShare)) return 'no edits';
  const worse = isNum(unvBase) && unvShare > unvBase + 1e-9, better = isNum(unvBase) && unvShare < unvBase - 1e-9;
  return `<span class="${worse ? 'worse' : better ? 'better' : ''}">${pct(unvShare)} unverified</span> · ${isNum(unvBase) ? `7-day median ${pct(unvBase)}` : 'no 7-day median yet'}`;
}

VIEWS_today = async function () {
  const [d, base, rep, attrRaw] = await Promise.all([getDay(S.day), baselineFor(S.day), getReport(S.day), getAttr(S.day)]);
  const attr = attrRaw && attrRaw.available ? attrRaw : null;
  const f = rep || factsFallback(d);
  const sid8 = (s) => String(s || '').replace(/^\?/, '').slice(0, 8);
  const V = d.scored.V, B = d.scored.B_min;

  // summary band
  const unvN = V ? Math.round(V.n * (1 - V.value)) : null;
  const unvBase = isNum(base.V.base) ? 1 - base.V.base : null;
  const cTotal = base.C.today;
  const causes = (d.scored.C || []).filter((x) => /^C:cause:/.test(x.metric));
  const sp = d.split || {};
  const va = attr && attr.verification;
  const vs = va ? { you: va.asked_by_you.count, agent: va.agent_initiative.count, none: va.none.count }
    : sp.verification ? { you: sp.verification.asked_by_you, agent: sp.verification.agent_initiative, none: sp.verification.none } : null;
  const vSplit = vs && vs.you + vs.agent + vs.none ? splitBar([{ n: vs.you, cls: 'you' }, { n: vs.agent, cls: 'agent' }, { n: vs.none, cls: 'none' }],
    `Checks: <b>${vs.you} you asked</b> · ${vs.agent} agent on its own · ${vs.none} nobody`) : '';
  const bs = sp.blocked;
  const bSplit = bs && bs.elsewhere_s + bs.quick_reply_s ? splitBar([{ n: bs.elsewhere_s, cls: 'you' }, { n: bs.quick_reply_s, cls: 'agent' }],
    `<b>${dur(bs.elsewhere_s)} while you were in another session</b> · ${dur(bs.quick_reply_s)} quick replies`) : '';
  let cs = null;
  if (attr && attr.corrections) {
    const c = attr.corrections;
    cs = { you: c.user_preventable.count, agent: c.agent_error.count, other: c.environment.count + c.unclear.count, src: 'judge' };
  } else if (causes.length) {
    cs = { you: 0, agent: 0, other: 0, src: 'cause' };
    for (const c of causes) { const o = ownerOf(c.metric.slice(8), null, null); cs[o === 'you' ? 'you' : o === 'agent' ? 'agent' : 'other'] += c.value || 0; }
  }
  const cSplit = cs && cs.you + cs.agent + cs.other ? splitBar([{ n: cs.you, cls: 'you' }, { n: cs.agent, cls: 'agent' }, { n: cs.other, cls: 'none' }],
    `<b>${num(cs.you)} preventable by you</b> · ${num(cs.agent)} agent error${cs.agent === 1 ? '' : 's'}${cs.other ? ` · ${num(cs.other)} environment or unclear` : ''}${cs.src === 'cause' ? ' <span class="muted">(by cause)</span>' : ''}`) : '';
  const ts = sp.time;
  const tSplit = ts && ts.agent_s + ts.waiting_s ? splitBar([{ n: ts.waiting_s, cls: 'you' }, { n: ts.agent_s, cls: 'agent' }],
    `<b>${dur(ts.waiting_s)} waiting on you</b> · ${dur(ts.agent_s)} agent working`) : '';
  const band = `<div class="band">
    <div class="stat"><span class="k">Unverified edits</span>
      <span class="v">${unvN != null ? unvN : '—'}${V ? `<small>of ${V.n} batches</small>` : ''}</span>
      <span class="d">${V ? unverifiedDeltaHTML(1 - V.value, unvBase) : 'no edits'}</span>
      ${V ? evidenceLinks(V.evidence.filter((e) => /^unverified/.test(e.note || '')), 2) : ''}${vSplit}</div>
    <div class="stat"><span class="k">Blocked</span>
      <span class="v">${B ? num(B.value) : '0'}<small>min</small></span>
      <span class="d">${deltaHTML(B ? B.value : 0, base.B_min.base, 'min', true)}</span>
      ${B ? evidenceLinks(B.evidence, 2) : ''}${bSplit}</div>
    <div class="stat"><span class="k">Corrections ${UNCAL}</span>
      <span class="v">${isNum(cTotal) ? `${num(cTotal)}<small>turns</small>` : '—<small>judge pending</small>'}</span>
      <span class="d">${isNum(cTotal) ? deltaHTML(cTotal, base.C.base, '', true) : 'run <span class="code">scorer judge</span>'}</span>
      ${causes.length ? evidenceLinks(causes.flatMap((c) => c.evidence || []), 2) : ''}${cSplit}</div>
    <div class="stat"><span class="k">Active agent time</span>
      <span class="v">${num(d.computed.active_s / 3600)}<small>h</small></span>
      <span class="d">${isNum(base.active_h.base) ? `median ${num(base.active_h.base)} h` : 'no 7-day median yet'}</span>
      <span class="d">${dur(d.computed.overlap_s)} with two or more agents at once</span>${tSplit}</div>
  </div>`;

  // right now
  const un = f.unverified_now || [];
  const rightNow = un.length ? `<ul class="plain">${un.map((u) => {
    const s = d.sessions.find((x) => x.id === u.session_id);
    const files = (u.files || []).map((p) => String(p).replace(/\/+$/, '').split('/').slice(-2).join('/'));
    const wl = s ? worktreeOf(s).label : sid8(u.session_id);
    return `<li><strong style="font-weight:600;max-width:60%" class="trunc" title="${esc((s && s.cwd) || wl)}">${esc(wl)}</strong>${s && branchTxt(s.branch) ? ` <span class="muted">${esc(s.branch)}</span>` : ''}
      ${turnLinks([u.turn_id])} ${tierBadge(u.tier)}
      <div class="sub-note" style="display:flex;gap:4px;min-width:0;align-items:baseline"><span class="code" style="min-width:0;max-width:100%">${ltrunc(files.join(', ') || 'files unknown', [u.manifest_dir, (u.files || []).join(', ')].filter(Boolean).join(' · '))}</span>${u.command ? ` · run <code class="cmd">${esc(u.command)}</code>` : ''}</div></li>`;
  }).join('')}</ul>` : '<p class="sub-note">Every session ended on a verified edit.</p>';

  // corrections by cause
  const cc = f.corrections || [];
  const corr = cc.length ? `<ul class="plain">${cc.map((c) => `<li><span class="badge tag">${esc(pretty(c.cause))}</span> ${ownerChip(ownerOf(c.cause, c.turn_id, attr))} ${turnLinks([c.turn_id])}
      <blockquote class="q">${esc(c.quote)}</blockquote>${c.prevent ? `<div class="sub-note">Prevent it: ${esc(c.prevent)}</div>` : ''}</li>`).join('')}</ul>`
    : '<p class="sub-note">No corrections labelled by the judge. Run <code class="cmd">scorer judge</code>.</p>';

  // worst window
  const w = f.worst_window;
  const blockedTurns = ((B && B.evidence) || []).map((e) => ({ id: e.turn_id, s: +((e.note || '').match(/(\d+)s/) || [0, 0])[1], att: /attended/.test(e.note || '') }));
  const worst = `${w ? `<p>${esc(w.start)}–${esc(w.end)}: ${num(w.minutes)} min lost across ${w.sessions} session${w.sessions === 1 ? '' : 's'}. ${turnLinks(w.turns)}</p>`
      : blockedTurns.length ? `<p>Longest waits: ${blockedTurns.slice(0, 3).map((b) => `${turnLinks([b.id])} ${dur(b.s)}`).join(', ')}.</p>` : '<p class="sub-note">No agent waited on you.</p>'}
    ${hourStrip(d)}
    <div class="legend"><span><i class="sw a"></i>Agent working</span><span><i class="sw w"></i>Waiting on you</span><span><i class="sw i"></i>Idle</span></div>`;

  // pattern
  const p = f.pattern;
  const pattern = !p ? '<p class="sub-note">Nothing stood out today.</p>' : p.kind === 'verified_session'
    ? `<p>Session ${esc(p.session)} verified ${p.verified} of ${p.batches} edit batches with ${p.command ? `<code class="cmd">${esc(p.command)}</code>` : 'a test command'}; you asked for ${p.human_initiated} of those checks. ${turnLinks(p.turns)}</p>`
    : `<p>You reached for <strong>${esc(p.name)}</strong> (${esc(CAT[p.category] || p.category)}) ${p.uses} time${p.uses === 1 ? '' : 's'}.</p>`;

  // diagnostics row
  const g = {}; for (const x of d.diagnostics) g[x.metric] = x.value;
  const models = d.diagnostics.filter((x) => x.base === 'tokens' && x.key).sort((a, b) => b.value - a.value);
  const diag = `<div class="diag-row">
      <div><div class="k">Interruptions</div><div class="v">${d.computed.interrupts}</div></div>
      <div><div class="k">Median gap</div><div class="v">${dur(g.gap_median_s)}</div></div>
      <div><div class="k">Tool errors</div><div class="v">${pct(g.tool_error_share)}</div></div>
      <div><div class="k">Context p50 / p90</div><div class="v">${isNum(g.context_p50) ? Math.round(g.context_p50) : '—'}% / ${isNum(g.context_p90) ? Math.round(g.context_p90) : '—'}%</div></div>
      <div><div class="k">Tokens</div><div class="v">${tok((g.tokens_in || 0) + (g.tokens_out || 0) + (g.tokens_cached || 0))}</div></div>
      <div><div class="k">Cost</div><div class="v">${usd(g.cost_usd)}</div></div>
    </div>
    <details class="rline plainsum"><summary><span class="rt" style="font-size:14px">Cost by model and all diagnostics</span>${icon('chev', 'icon chev')}</summary>
      <div class="rb" style="padding-left:0">
        <table class="models">${models.map((m) => { const c = d.diagnostics.find((x) => x.metric === `cost_usd:${m.key}`); return `<tr><td>${esc(m.key)}</td><td class="r">${tok(m.value)} tokens</td><td class="r">${usd(c && c.value)}</td></tr>`; }).join('')}</table>
        ${diagnostics(d)}</div></details>`;

  return `
  <section class="today-top">
    <div class="narrative-block">
      <div class="date">${esc(dayLabel(S.day))}</div>
      <div id="narrative">${narrativeFallbackHTML(f, true)}</div>
      ${f.tomorrow && f.tomorrow.change ? `<div class="tomorrow"><b>Tomorrow:</b> ${esc(f.tomorrow.change)}</div>` : ''}
    </div>
    ${band}
  </section>

  <section class="panel timeline-panel">
    <div class="panel-head"><h2>Day by worktree</h2>
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div class="legend"><span><i class="dot claude"></i>Claude</span><span><i class="dot codex"></i>Codex</span><span><i class="ov"></i>Two or more at once</span><span><i class="tk"></i>Interruption</span></div>
        ${d.temp_hidden || S.includeTemp ? `<button type="button" class="btn outline sm" data-toggle-temp aria-pressed="${S.includeTemp ? 'true' : 'false'}">${S.includeTemp ? 'Hide test/temp sessions' : `Show test/temp sessions (${d.temp_hidden})`}</button>` : ''}
        <button type="button" class="btn outline sm" id="tl-expand" aria-pressed="${S.tlExpanded ? 'true' : 'false'}">${S.tlExpanded ? 'Group by worktree' : 'Show sessions'}</button></div></div>
    <div class="timeline-wrap"><div class="timeline" id="timeline"></div></div>
    <div id="turn-detail"></div>
  </section>

  <section class="grid2">
    <div class="colstack">
      <div class="panel"><div class="panel-head"><h2>Right now: unverified</h2><span class="note">${un.some((u) => u.command) ? 'run before you push' : 'run the tests before you push'}</span></div>${rightNow}</div>
      <div class="panel"><div class="panel-head"><h2>Corrections by cause ${UNCAL}</h2><span class="note">${causes.map((c) => `${esc(pretty(c.metric.slice(8)))} ${num(c.value)} (${ownerOf(c.metric.slice(8), null, null)})`).join(' · ')}</span></div>${corr}</div>
    </div>
    <div class="colstack">
      <div class="panel"><div class="panel-head"><h2>Worst attention window</h2><span class="note">${B ? `${num(B.value)} min blocked today` : ''}</span></div>${worst}</div>
      <div class="panel"><div class="panel-head"><h2>Pattern to repeat</h2></div>${pattern}
        <div class="panel-head" style="margin-top:4px"><span class="note">Most-used tools · by you / by the agent</span><a class="note" href="#/leverage">All in Leverage</a></div>
        ${skillBars(d, 'all', 4)}</div>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Diagnostics</h2><span class="note">context for the numbers, not scored</span></div>
    ${diag}
  </section>

  <section class="panel" id="strengths" aria-labelledby="strengths-h">
    <div class="panel-head"><h2 id="strengths-h">What you did well</h2><span class="note">your choices worth repeating</span></div>
    <div id="strengths-body"><span class="shimmer" style="font-size:13px">Loading</span></div>
  </section>

  <section class="panel" id="actions" aria-labelledby="actions-h">
    <div class="panel-head"><h2 id="actions-h">Action items</h2><span class="note">what to change, with the turns behind it</span></div>
    <div id="actions-body"><span class="shimmer" style="font-size:13px">Loading action items</span></div>
  </section>`;
};

/* ---- action items (judge.action_items via /api/actions) ---- */
cache.actions = new Map();
const getActions = (d) => { if (!cache.actions.has(d)) cache.actions.set(d, optional(`/api/actions?day=${d}`)); return cache.actions.get(d); };
let actionItems = [], strengthItems = [];
cache.strengths = new Map();
const getStrengths = (d) => { if (!cache.strengths.has(d)) cache.strengths.set(d, optional(`/api/strengths?day=${d}`)); return cache.strengths.get(d); };
function actionsHTML(r, kind = 'a') {
  const items = (r && r.items) || [];
  const good = kind === 's';
  if (!items.length) {
    const what = good ? 'Strengths' : 'Action items';
    return `<p class="sub-note">${r && r.available === false ? `${what} appear here once the judge provides them (<code class="cmd">scorer judge</code>).` : r && r.error ? `Could not compute ${what.toLowerCase()} for this day.` : good ? 'Nothing flagged as a strength for this day yet.' : 'No action items for this day.'}</p>`;
  }
  return `<ol class="actions${good ? ' good' : ''}">${items.map((a, i) => `<li>
      <span class="an" aria-hidden="true">${i + 1}</span>
      <div class="ab">
        <div class="at">${esc(a.text)}</div>
        <div class="am">${a.category ? `<span class="badge ${good ? 'grass' : 'tag'}">${esc(pretty(a.category))}</span>` : ''}${a.count ? `<span class="muted">${num(a.count)}×</span>` : ''}${a.uncalibrated || a.source === 'judge' ? UNCAL : ''}${turnLinks(a.turn_ids)}
          <button type="button" class="linkbtn" data-askwhy="${kind}:${i}">${icon('spark')}${good ? 'Ask how to repeat it' : 'Ask why'}</button></div>
        ${a.example_quote ? `<blockquote class="q">${esc(a.example_quote)}</blockquote>` : ''}
        ${a.claude_md_line ? `<div class="mdline"><span class="lbl">CLAUDE.md</span><code class="cmd">${esc(a.claude_md_line)}</code><button type="button" class="btn outline sm" data-copyline="${kind}:${i}" aria-label="Copy the CLAUDE.md line">Copy</button></div>` : ''}
      </div></li>`).join('')}</ol>`;
}
async function fillActions() {
  const day = S.day;
  const [r, g] = await Promise.all([getActions(day), getStrengths(day)]);
  if (S.day !== day || S.tab !== 'today') return;
  const box = $('#actions-body'), gb = $('#strengths-body');
  actionItems = (r && r.items) || [];
  strengthItems = (g && g.items) || [];
  if (gb) gb.innerHTML = actionsHTML(g, 's');
  if (box) box.innerHTML = actionsHTML(r, 'a');
}
const listItem = (key) => { const [k, i] = String(key).split(':'); return (k === 's' ? strengthItems : actionItems)[+i]; };

function narrativeFallbackHTML(f, loading) {
  return `<h1>${esc((f.verdict && f.verdict.text) || 'No agent activity recorded.')}</h1>
    <div class="byline">${loading && !missing.has('/api/narrative') && chat.available ? '<span class="shimmer" style="font-size:13px">Writing the daily narrative</span>' : (chat.available ? 'Deterministic verdict. This server does not provide the analyst narrative yet.' : 'Deterministic verdict. Set ANTHROPIC_API_KEY for the analyst narrative.')}</div>`;
}

let narrCites = {};
async function fillNarrative() {
  const day = S.day;
  const box = $('#narrative');
  if (!box) return;
  const n = chat.available ? await getNarrative(day) : null;
  const b2 = $('#narrative');
  if (!b2 || S.day !== day || S.tab !== 'today') return;
  if (n && n.markdown) {
    narrCites = {}; for (const c of n.citations || []) narrCites[c.n] = c;
    b2.innerHTML = `<div class="md narr">${md(n.markdown)}</div><div class="byline">Written by analyst agent${n.model ? ` · ${esc(n.model)}` : chat.model ? ` · ${esc(chat.model)}` : ''}${n.generated_at ? ` · ${hhmm(n.generated_at)}` : ''}${n.cached ? ' · cached' : ''}</div>`;
  } else {
    const f = (await getReport(day)) || factsFallback(await getDay(day));
    b2.innerHTML = narrativeFallbackHTML(f, false);
  }
}

/* ---- skills two-tone bars (Today teaser + Leverage) ---- */
function skillAgg(d) {
  const m = new Map();
  for (const it of (d.leverage && d.leverage.tooling && d.leverage.tooling.items) || []) {
    const k = `${it.category}\u0000${it.name}`;
    if (!m.has(k)) m.set(k, { category: it.category, name: it.name, you: 0, agent: 0, other: 0 });
    const r = m.get(k);
    if (it.initiator === 'human') r.you += it.count; else if (it.initiator === 'agent') r.agent += it.count; else r.other += it.count;
  }
  return [...m.values()].map((r) => ({ ...r, total: r.you + r.agent + r.other })).sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
}
const CAT = { skill: 'Skill', command: 'Command', mcp: 'MCP server', plugin: 'Plugin' };
function skillBars(d, cat = 'all', limit = 0) {
  let rows = skillAgg(d).filter((r) => cat === 'all' || r.category === cat);
  if (limit) rows = rows.slice(0, limit);
  if (!rows.length) return '<p class="sub-note">No skills, commands, MCP servers or plugins used.</p>';
  const hi = Math.max(...rows.map((r) => r.total));
  return `<div class="skills">${rows.map((r) => `<div class="skill-row">
      <div class="nm" title="${esc(r.name)}">${esc(r.name)}<small>${esc(CAT[r.category] || r.category)}</small></div>
      <div class="track" role="img" aria-label="${r.you} by you, ${r.agent + r.other} by the agent">
        ${r.you ? `<span class="you" style="width:${(100 * r.you) / hi}%"></span>` : ''}${r.agent + r.other ? `<span class="agent" style="width:${(100 * (r.agent + r.other)) / hi}%"></span>` : ''}</div>
      <div class="ct">${r.you} / ${r.agent + r.other}</div></div>`).join('')}</div>`;
}

/* ---- interval helpers ---- */
function union(iv) {
  const s = iv.filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0]);
  const out = [];
  for (const [a, b] of s) { const l = out[out.length - 1]; if (l && a <= l[1]) l[1] = Math.max(l[1], b); else out.push([a, b]); }
  return out;
}
function subtract(iv, cut) {
  const out = [];
  for (let [a, b] of iv) {
    for (const [c, e] of cut) { if (e <= a || c >= b) continue; if (c > a) out.push([a, c]); a = Math.max(a, e); if (a >= b) break; }
    if (a < b) out.push([a, b]);
  }
  return out;
}
const within = (iv, a, b) => iv.reduce((t, [x, y]) => t + Math.max(0, Math.min(y, b) - Math.max(x, a)), 0);


/* ---- hourly strip: agent working / waiting on you / idle (client-side until /api/day has hours) ---- */
function hourStrip(d) {
  const sessions = d.sessions.filter((s) => s.turns.length);
  if (!sessions.length) return '';
  const endOf = (t) => +new Date(t.end || t.start);
  const hour = 3600e3;
  let hours;
  if (Array.isArray(d.hours) && d.hours.length) {
    hours = d.hours.map((h) => ({ t: +new Date(h.start), a: (h.active_s || 0) / 3600, w: (h.waiting_s || 0) / 3600 }));
  } else {
    const active = union(sessions.flatMap((s) => s.turns.map((t) => [+new Date(t.start), endOf(t)])));
    if (!active.length) return '<p class="sub-note">Too little agent activity for an hourly view.</p>';
    const waiting = subtract(union((d.gaps || []).filter((g) => g.bucket !== 'break').map((g) => [+new Date(g.start), +new Date(g.end)])), active);
    const t0 = Math.floor(active[0][0] / hour) * hour, t1 = Math.ceil(active[active.length - 1][1] / hour) * hour;
    hours = [];
    for (let h = t0; h < t1; h += hour) hours.push({ t: h, a: within(active, h, h + hour) / hour, w: within(waiting, h, h + hour) / hour });
  }
  return `<div class="hours" role="img" aria-label="Agent working, waiting and idle per hour">${hours.map((h) => {
    const a = Math.min(h.a, 1), w = Math.min(h.w, 1 - a);
    return `<div class="hr" title="${hhmm(new Date(h.t).toISOString())}: ${Math.round(h.a * 60)} min working, ${Math.round(h.w * 60)} min waiting on you">
      <div class="cell"><span class="w" style="height:${(w * 100).toFixed(1)}%"></span><span class="a" style="height:${(a * 100).toFixed(1)}%"></span></div>
      <span class="lb">${hhmm(new Date(h.t).toISOString())}</span></div>`;
  }).join('')}</div>`;
}

/* ---- timeline svg: grouped by worktree/repo ---- */
function worktreeOf(s) {
  if (s.cwd) {
    const parts = String(s.cwd).replace(/\/+$/, '').split('/');
    return { key: s.cwd, label: parts.slice(-2).join('/'), branch: branchTxt(s.branch) };
  }
  return { key: `${s.repo || '?'}\u0000${branchTxt(s.branch)}`, label: s.repo || 'unknown repo', branch: branchTxt(s.branch) };
}
function drawTimeline() {
  const host = $('#timeline');
  if (!host) return;
  getDay(S.day).then((d) => {
    const endOf = (t) => +new Date(t.end || t.start);
    const sessions = d.sessions.filter((s) => s.turns.length)
      .map((s) => ({ ...s, first: Math.min(...s.turns.map((t) => +new Date(t.start))) }))
      .sort((a, b) => a.first - b.first);
    if (!sessions.length) { host.innerHTML = '<div class="empty">No turns on this day.</div>'; return; }
    const groups = new Map();
    for (const s of sessions) {
      const w = worktreeOf(s);
      if (!groups.has(w.key)) groups.set(w.key, { ...w, sessions: [] });
      groups.get(w.key).sessions.push(s);
    }
    let t0 = sessions[0].first;
    let t1 = Math.max(...sessions.flatMap((s) => s.turns.map(endOf)));
    const span0 = Math.max(t1 - t0, 30 * 60e3);
    const STEPS = [5, 10, 15, 30, 60, 120, 180, 240].map((m) => m * 60e3);
    const maxTicks = Math.max(3, Math.floor((Math.max(host.clientWidth, 480) - 208) / 64));
    const hour = 3600e3;
    const pad = (st) => { const u = Math.max(st, hour); return [Math.floor((t0 - st * 0.25) / u) * u, Math.ceil((t1 + st * 0.25) / u) * u]; };
    const step = STEPS.find((st) => { const [a, b] = pad(st); return (b - a) / st <= maxTicks; }) || STEPS[STEPS.length - 1];
    [t0, t1] = pad(step);

    const W = Math.max(host.clientWidth, 480), LBL = 200, R = 8, TOP = 22, LANE = 28, GH = 24, BAR = 12, STRIP = 32;
    const x = (ms) => LBL + ((ms - t0) / (t1 - t0)) * (W - LBL - R);
    const rows = [];
    for (const g of groups.values()) {
      if (S.tlExpanded) { rows.push({ type: 'ghead', g, h: GH }); for (const s of g.sessions) rows.push({ type: 'session', g, s, turns: s.turns, h: LANE }); }
      else rows.push({ type: 'group', g, turns: g.sessions.flatMap((s) => s.turns.map((t) => ({ ...t, _s: s }))), h: LANE });
    }
    const lanesH = rows.reduce((a, r) => a + r.h, 0);
    const H = TOP + lanesH + 4;
    const parts = [];
    for (let t = t0; t <= t1; t += step) {
      const xx = x(t).toFixed(1);
      parts.push(`<line class="grid" x1="${xx}" x2="${xx}" y1="${TOP - 4}" y2="${TOP + lanesH}"/>`);
      parts.push(`<text x="${xx}" y="12" text-anchor="${t + step > t1 ? 'end' : 'middle'}">${hhmm(new Date(t).toISOString())}</text>`);
    }
    for (const o of d.overlap) {
      const a = x(+new Date(o.start)), b = x(+new Date(o.end));
      parts.push(`<rect class="overlap" x="${a.toFixed(1)}" y="${TOP - 4}" width="${Math.max(b - a, 1).toFixed(1)}" height="${lanesH + 4}"/>`);
    }
    let y = TOP;
    const trunc = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
    rows.forEach((r, i) => {
      const cy = y + r.h / 2;
      if (i && (r.type !== 'session')) parts.push(`<line class="grid" x1="0" x2="${W}" y1="${y}" y2="${y}"/>`);
      if (r.type === 'ghead' || r.type === 'group') {
        parts.push(`<text class="group-label" x="0" y="${cy + (r.type === 'group' && r.g.branch ? -1 : 4)}">${esc(trunc(r.g.label, 24))}<title>${esc(r.g.label)}</title></text>`);
        if (r.type === 'group' && r.g.branch) parts.push(`<text class="group-sub" x="0" y="${cy + 11}">${esc(trunc(r.g.branch + (r.g.sessions.length > 1 ? ` · ${r.g.sessions.length} sessions` : ''), 30))}</text>`);
        if (r.type === 'ghead' && r.g.branch) parts.push(`<text class="group-sub" x="${Math.min(8 + r.g.label.length * 7.4, 190)}" y="${cy + 4}">${esc(trunc(r.g.branch, 14))}</text>`);
      }
      if (r.type !== 'ghead') {
        const hitAttr = r.type === 'session' ? `data-lane-session="${esc(r.s.id)}"` : `data-lane-group="${esc(r.g.key)}"`;
        parts.push(`<rect class="lane-hit" ${hitAttr} x="0" y="${y}" width="${W}" height="${r.h}"/>`);
        if (r.type === 'session') {
          parts.push(`<rect x="12" y="${cy - 4}" width="8" height="8" rx="2" fill="var(--lane-${r.s.agent === 'codex' ? 'codex' : 'claude'})" pointer-events="none"/>`);
          parts.push(`<text x="28" y="${cy + 4}" pointer-events="none">${esc(r.s.short)} · ${r.s.turns.length} turn${r.s.turns.length === 1 ? '' : 's'}</text>`);
        }
        for (const t of r.turns) {
          const s = t._s || r.s;
          const a = x(+new Date(t.start)), b = x(endOf(t));
          const cls = `bar ${s.agent === 'codex' ? 'codex' : 'claude'}${t.interrupted ? ' interrupted' : ''}${S.selTurn === t.id ? ' sel' : ''}`;
          parts.push(`<rect class="${cls}" data-turn="${t.id}" data-sess="${esc(s.id)}" x="${a.toFixed(1)}" y="${cy - BAR / 2}" width="${Math.max(b - a, 3).toFixed(1)}" height="${BAR}" rx="2"/>`);
        }
        const ints = r.type === 'session' ? (r.s.interrupts || []) : r.g.sessions.flatMap((s) => s.interrupts || []);
        for (const it of ints) {
          const xx = x(+new Date(it.at)).toFixed(1);
          parts.push(`<line class="tick" x1="${xx}" x2="${xx}" y1="${cy - BAR / 2 - 4}" y2="${cy + BAR / 2 + 4}" pointer-events="none"/>`);
        }
      }
      y += r.h;
    });
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Agent activity by worktree">${parts.join('')}</svg>`;
  });
}

/* ---- hover popover on bars and lanes ---- */
const pop = document.createElement('div');
pop.className = 'pop'; pop.setAttribute('role', 'dialog'); pop.id = 'pop';
document.body.appendChild(pop);
let popTimer = 0, popHide = 0, popKey = null, popPrefill = '';
function placePop(el) {
  const r = el.getBoundingClientRect();
  const pw = 320, ph = pop.offsetHeight || 160;
  let left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 8);
  let top = r.bottom + 8;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
  pop.style.left = `${left}px`; pop.style.top = `${top}px`;
}
async function summaryFor(sessionId, turnId) {
  const k = `${sessionId}|${turnId || ''}`;
  if (!cache.summary.has(k)) cache.summary.set(k, optional(`/api/summary?session_id=${encodeURIComponent(sessionId)}${turnId ? `&turn_id=${turnId}` : ''}`));
  return cache.summary.get(k);
}
async function showPop(el) {
  const d = await getDay(S.day);
  let key, head, meta, fallback, prefill, sessionId = null, turnId = null;
  if (el.dataset.turn) {
    turnId = +el.dataset.turn; sessionId = el.dataset.sess;
    const s = d.sessions.find((x) => x.id === sessionId);
    const t = s && s.turns.find((x) => x.id === turnId);
    key = `t${turnId}`;
    head = `Turn #${turnId} <span class="mono">${esc(s ? s.short : '')}</span>`;
    meta = `${t ? `${hhmm(t.start)}–${hhmm(t.end)} · ${dur(secs(t.start, t.end))}` : ''}${t && t.interrupted ? ' · interrupted' : ''}`;
    fallback = async () => { try { const full = await getTurn(turnId); meta += ` · ${full.tools_total != null ? full.tools_total : (full.tools || []).length} tools · ${usd(full.api && full.api.cost_usd)}`; return { text: full.prompt || '—', tier: full.tier }; } catch (e) { return { text: '—' }; } };
    prefill = `What happened in turn #${turnId} (session ${s ? s.short : ''}), and what should I have done differently?`;
  } else if (el.dataset.laneSession) {
    const s = d.sessions.find((x) => x.id === el.dataset.laneSession); if (!s) return;
    sessionId = s.id; key = `s${s.id}`;
    head = `${esc(s.repo || 'session')} <span class="mono">${esc(s.short)}</span>`;
    meta = `${s.turns.length} turns · ${dur(union(s.turns.map((t) => [+new Date(t.start), +new Date(t.end || t.start)])).reduce((a, [p, q]) => a + q - p, 0) / 1000)} active${s.branch ? ` · ${esc(s.branch)}` : ''}`;
    fallback = async () => ({ text: `${s.agent === 'codex' ? 'Codex' : 'Claude'} session in ${s.repo || 'an unknown repo'}, ${hhmm(s.turns[0].start)}–${hhmm(s.turns[s.turns.length - 1].end)}.` });
    prefill = `Summarize session ${s.short} in ${s.repo || 'this repo'}: what got done, where did I lose time?`;
  } else if (el.dataset.laneGroup) {
    const ss = d.sessions.filter((s) => s.turns.length && worktreeOf(s).key === el.dataset.laneGroup); if (!ss.length) return;
    const w = worktreeOf(ss[0]); key = `g${w.key}`;
    if (ss.length === 1) sessionId = ss[0].id;
    head = `${esc(w.label)}${w.branch ? ` <span class="mono">${esc(w.branch)}</span>` : ''}`;
    const turns = ss.flatMap((s) => s.turns);
    meta = `${ss.length} session${ss.length === 1 ? '' : 's'} · ${turns.length} turns · ${dur(union(turns.map((t) => [+new Date(t.start), +new Date(t.end || t.start)])).reduce((a, [p, q]) => a + q - p, 0) / 1000)} active`;
    fallback = async () => ({ text: `${ss.length > 1 ? `${ss.length} sessions` : 'One session'} in this worktree. Select "Show sessions" to split them.` });
    prefill = `What happened in ${w.label}${w.branch ? ` on ${w.branch}` : ''} today?`;
  } else return;
  if (popKey === key && pop.classList.contains('show')) return;
  popKey = key; popPrefill = prefill;
  pop.innerHTML = `<div class="ph">${head}</div><div class="pb"><span class="shimmer" style="font-size:13px">Summarizing</span></div><div class="pm">${meta}</div>
    <button type="button" class="btn outline sm" data-askabout>${icon('spark')}Ask about this in chat</button>`;
  pop.classList.add('show'); placePop(el);
  const sm = sessionId && chat.available ? await summaryFor(sessionId, turnId) : null;
  if (popKey !== key) return;
  if (sm && sm.summary) { pop.querySelector('.pb').textContent = sm.summary; }
  else {
    const fb = await fallback();
    if (popKey !== key) return;
    const pb = pop.querySelector('.pb');
    pb.className = `pb${el.dataset.turn ? ' fallback' : ''}`; pb.textContent = fb.text;
    pop.querySelector('.pm').innerHTML = `${meta}${fb.tier ? ` ${tierBadge(fb.tier)}` : ''}`;
  }
  placePop(el);
}
function hidePopSoon() { clearTimeout(popTimer); clearTimeout(popHide); popHide = setTimeout(() => { pop.classList.remove('show'); popKey = null; }, 220); }
document.addEventListener('mouseover', (e) => {
  if (e.target.closest('#pop')) { clearTimeout(popHide); return; }
  const el = e.target.closest('.timeline [data-turn], .timeline [data-lane-session], .timeline [data-lane-group]');
  if (!el) return;
  clearTimeout(popHide); clearTimeout(popTimer);
  popTimer = setTimeout(() => showPop(el), 250);
});
document.addEventListener('mouseout', (e) => {
  const from = e.target.closest('.timeline [data-turn], .timeline [data-lane-session], .timeline [data-lane-group], #pop');
  if (from && !(e.relatedTarget && from.contains(e.relatedTarget))) hidePopSoon();
});
pop.addEventListener('click', (e) => {
  if (!e.target.closest('[data-askabout]')) return;
  pop.classList.remove('show'); popKey = null;
  setAsk(true);
  input.value = popPrefill; autoGrow(); syncComposer();
  setTimeout(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 330);
});

/* ---- turn detail (shared) ---- */
const TOOL_INTENT = (t) => t.bash_intent || t.kind || '';
async function turnDetailHTML(id, closable = true) {
  const t = await getTurn(id);
  const tools = (t.tools || []).map((c) => `<li>
      <span class="nm" title="${esc(c.name)}">${esc(c.name)}</span>
      <span class="muted">${esc(TOOL_INTENT(c))}</span>
      <span class="args${c.is_error ? ' err' : ''}" title="${esc(c.args || '')}">${esc(c.args || '')}</span>
      <span class="dur">${isNum(c.duration_ms) ? (c.duration_ms >= 1000 ? `${nf1.format(c.duration_ms / 1000)} s` : `${c.duration_ms} ms`) : '—'}</span></li>`).join('');
  const a = t.api || {};
  const kv = [
    ['Duration', dur(secs(t.start, t.end))],
    ['Agent started after', dur(secs(t.start, t.agent_started_at))],
    ['Initiator', esc(t.initiator || '—')],
    ['Kind', esc(t.prompt_kind || '—')],
    ['API calls', num(a.calls)],
    ['Tokens in / out', `${tok(a.input)} / ${tok(a.output)}`],
    ['Cached', tok(a.cached)],
    ['Context peak', tok(a.context_peak)],
    ['Cost', usd(a.cost_usd)],
  ];
  return `<div class="turn-detail" data-detail="${t.id}">
    <div class="td-head">
      <span class="title">Turn #${t.id}</span>${agentBadge(t.agent)}${tierBadge(t.tier)}
      ${t.interrupted ? '<span class="badge destructive">Interrupted</span>' : ''}
      <span class="muted mono" style="font-size:13px">${esc(t.repo || '')} · ${hhmm(t.start)}–${hhmm(t.end)}</span>
      <span class="spacer"></span>
      ${closable ? `<button class="btn ghost sm" type="button" data-close-detail>${icon('x')}<span class="sr-only">Close</span></button>` : ''}
    </div>
    <div class="td-body">
      <div>
        <div class="lbl">Prompt (clipped)</div>
        <blockquote>${esc(t.prompt || '—')}</blockquote>
        <div class="lbl">Tool calls · ${(t.tools || []).length}${t.tools_total && t.tools_total > (t.tools || []).length ? ` of ${t.tools_total}` : ''}</div>
        ${tools ? `<ul class="tools">${tools}</ul>` : '<div class="muted" style="font-size:13px">None</div>'}
      </div>
      <dl class="kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
    </div></div>`;
}

async function openTurn(id, { scroll = true, flash = false } = {}) {
  if (S.tab !== 'today') { S.selTurn = id; location.hash = '#/today'; pendingFlash = flash ? id : null; return; }
  S.selTurn = id;
  document.querySelectorAll('.timeline .bar.sel').forEach((b) => b.classList.remove('sel'));
  const bar = document.querySelector(`.timeline .bar[data-turn="${id}"]`);
  if (bar) {
    bar.classList.add('sel');
    if (flash) { bar.classList.remove('flash'); void bar.getBoundingClientRect(); bar.classList.add('flash'); }
  }
  const box = $('#turn-detail');
  if (!box) return;
  box.innerHTML = '<div class="empty" style="padding:16px">Loading turn…</div>';
  try { box.innerHTML = await turnDetailHTML(id); } catch (e) { box.innerHTML = `<div class="empty">Turn #${id} not found.</div>`; }
  if (scroll) (flash && bar ? bar.closest('.timeline-wrap') : box).scrollIntoView({ behavior: 'smooth', block: 'center' });
}
let pendingFlash = null;

/* ================= Sessions ================= */
function unionSeconds(turns) {
  return union(turns.map((t) => [+new Date(t.start), +new Date(t.end || t.start)])).reduce((a, [p, q]) => a + q - p, 0) / 1000;
}
const branchTxt = (b) => (b && b !== 'HEAD' ? b : '');
function blockedBySession(d) {
  const out = {};
  for (const e of (d.scored.B_min && d.scored.B_min.evidence) || []) {
    const s = turnSession(d, e.turn_id); if (!s) continue;
    const sec = +(((e.note || '').match(/(\d+)s/) || [0, 0])[1]);
    out[s.id] = (out[s.id] || 0) + sec;
  }
  return out;
}

VIEWS_sessions = async function () {
  const d = await getDay(S.day);
  const ss = d.sessions.filter((s) => s.turns.length)
    .sort((a, b) => new Date(a.turns[0].start) - new Date(b.turns[0].start));
  const tempBtn = d.temp_hidden || S.includeTemp
    ? `<button type="button" class="btn outline sm" data-toggle-temp aria-pressed="${S.includeTemp ? 'true' : 'false'}">${S.includeTemp ? 'Hide test/temp sessions' : `Show test/temp sessions (${d.temp_hidden})`}</button>` : '';
  if (!ss.length) return `<section class="panel"><div class="panel-head"><h2>Sessions</h2>${tempBtn}</div><p class="sub-note">No turns fall inside ${esc(dayLabel(S.day))} in local time.</p></section>`;
  const blocked = blockedBySession(d);
  const endOf = (t) => +new Date(t.end || t.start);
  const H = 3600e3;
  const t0 = Math.floor(Math.min(...ss.map((s) => +new Date(s.turns[0].start))) / H) * H;
  const t1 = Math.ceil(Math.max(...ss.flatMap((s) => s.turns.map(endOf))) / H) * H;
  const mini = (s) => {
    const W = 300, x = (ms) => ((ms - t0) / ((t1 - t0) || H)) * W;
    const bars = s.turns.map((t) => `<rect class="on ${s.agent === 'codex' ? 'codex' : 'claude'}" x="${x(+new Date(t.start)).toFixed(1)}" y="3" width="${Math.max(x(endOf(t)) - x(+new Date(t.start)), 1.5).toFixed(1)}" height="12" rx="1"/>`).join('');
    return `<svg class="mini-tl" viewBox="0 0 ${W} 18" preserveAspectRatio="none" aria-hidden="true"><rect class="base" x="0" y="8" width="${W}" height="2"/>${bars}</svg>`;
  };
  const tokTotal = (s) => (s.tokens ? s.tokens.in + s.tokens.cached + s.tokens.out : null);
  let totCost = 0, totTok = 0, nTurns = 0;
  const rows = ss.map((s) => {
    const v = s.verification || { batches: 0, verified_pass: 0, verified_fail: 0, inspected: 0, none: 0 };
    const ok = v.verified_pass, fail = v.verified_fail, unv = v.inspected + v.none, ed = v.batches;
    totCost += s.cost_usd || 0; totTok += tokTotal(s) || 0; nTurns += s.turns.length;
    const wt = worktreeOf(s);
    const p = (n) => `${ed ? (100 * n) / ed : 0}%`;
    const tt = s.tokens ? `${tok(s.tokens.in)} in · ${tok(s.tokens.cached)} cached · ${tok(s.tokens.out)} out` : '';
    return `<tr class="row clickable${S.selSession === s.id ? ' expanded' : ''}" data-session="${esc(s.id)}" tabindex="0" aria-expanded="${S.selSession === s.id ? 'true' : 'false'}">
      <td class="clip" title="${esc(`${s.cwd || wt.label} ${s.branch || ''}`)}"><span style="display:inline-flex;align-items:center;gap:8px;max-width:100%;min-width:0"><span class="dot ${s.agent === 'codex' ? 'codex' : 'claude'}"></span><span class="trunc" title="${esc(s.cwd || wt.label)}">${esc(wt.label)}</span></span>${branchTxt(s.branch) ? ` <span class="muted" style="font-size:13px">${esc(s.branch)}</span>` : ''}
        <div class="muted" style="font-size:12px;padding-left:16px">${s.agent === 'codex' ? 'Codex' : 'Claude'} · ${esc(s.short)}${s.parent_id ? ' · subagent thread' : ''}${s.temp ? ' · <span class="badge tag">temp</span>' : ''}</div></td>
      <td style="padding-top:16px">${mini(s)}</td>
      <td class="num">${hhmm(s.turns[0].start)}–${hhmm(s.turns[s.turns.length - 1].end)}</td>
      <td class="num">${s.turns.length}</td>
      <td class="num">${dur(isNum(s.active_s) ? s.active_s : unionSeconds(s.turns))}</td>
      <td>${ed ? `<div class="vcell" title="${ok} verified, ${fail} check failed, ${unv} unverified of ${ed} edit batches"><div class="vbar" style="flex:1"><span class="ok" style="width:${p(ok)}"></span><span class="fail" style="width:${p(fail)}"></span><span class="unv" style="width:${p(unv)}"></span></div><span class="t">${ok + fail}/${ed}</span></div>` : '<span class="muted" style="font-size:13px">no edits</span>'}</td>
      <td class="num">${blocked[s.id] ? dur(blocked[s.id]) : '—'}</td>
      <td class="num" title="${esc(tt)}">${tok(tokTotal(s))}</td>
      <td class="num">${usd(s.cost_usd)}</td></tr>`;
  }).join('');
  return `
  <section class="panel" style="padding:0;overflow:hidden">
    <div class="panel-head" style="padding:16px 20px 0"><h2>Sessions</h2>
      <div class="legend"><span>${esc(dayLabel(S.day))} · ${ss.length} sessions · ${nTurns} turns · ${tok(totTok)} tokens · ${usd(totCost)}</span>
        <span><i class="sw ok"></i>Verified</span><span><i class="sw fail"></i>Check failed</span><span><i class="sw unv"></i>Unverified</span>${tempBtn}</div></div>
    <table class="sess" style="margin-top:12px">
      <colgroup><col style="width:20%"><col style="width:18%"><col style="width:10%"><col style="width:5%"><col style="width:8%"><col style="width:13%"><col style="width:8%"><col style="width:9%"><col style="width:9%"></colgroup>
      <thead><tr><th>Worktree</th><th><div style="display:flex;justify-content:space-between;gap:8px"><span>Activity</span><span style="font-weight:400">${hhmm(new Date(t0).toISOString())}–${hhmm(new Date(t1).toISOString())}</span></div></th><th class="num">Time</th><th class="num">Turns</th><th class="num">Active</th><th>Verified batches</th><th class="num">Blocked</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="9" class="muted">No sessions.</td></tr>'}</tbody></table>
  </section>
  <div id="sel-session"></div>`;
};

function sessionSplitHTML(s) {
  const v = (s.split && s.split.verification) || null;
  const t = (s.split && s.split.tooling) || null;
  const parts = [];
  if (v && v.asked_by_you + v.agent_initiative + v.none) parts.push(splitBar([{ n: v.asked_by_you, cls: 'you' }, { n: v.agent_initiative, cls: 'agent' }, { n: v.none, cls: 'none' }], `Checks: <b>${v.asked_by_you} you asked</b> · ${v.agent_initiative} agent on its own · ${v.none} nobody`));
  if (t && t.you + t.agent + t.default) parts.push(splitBar([{ n: t.you, cls: 'you' }, { n: t.agent + t.default, cls: 'agent' }], `Skills, commands, MCP: <b>${t.you} by you</b> · ${t.agent + t.default} by the agent${t.default ? ` (${t.default} by hooks)` : ''}`));
  const a = S.attr && S.attr.corrections;
  if (a) {
    const ids = new Set(s.turns.map((x) => x.id));
    const n = (k) => (a[k] ? (a[k].turn_ids || []).filter((x) => ids.has(x)).length : 0);
    const y = n('user_preventable'), g = n('agent_error'), o = n('environment') + n('unclear');
    if (y + g + o) parts.push(splitBar([{ n: y, cls: 'you' }, { n: g, cls: 'agent' }, { n: o, cls: 'none' }], `Corrections: <b>${y} preventable by you</b> · ${g} agent error${g === 1 ? '' : 's'}${o ? ` · ${o} other` : ''}`));
  }
  return parts.length ? `<div class="split-grid">${parts.join('')}</div>` : '<div class="s">No checks, tooling or judged corrections in this session.</div>';
}

function sessionFactsHTML(s, d) {
  const v = s.verification || {};
  const t = s.tokens || {};
  const sub = s.subagents || {};
  const bl = blockedBySession(d)[s.id];
  const models = (s.models || []).map((m) => `<li><span class="mono">${esc(m.model)}</span><span>${tok(m.tokens)} · ${usd(m.cost_usd)}</span></li>`).join('');
  return `<div class="sess-facts">
    <div><div class="k">Tokens</div><div class="v">${tok((t.in || 0) + (t.cached || 0) + (t.out || 0))}</div><div class="s">${tok(t.in)} in · ${tok(t.cached)} cached · ${tok(t.out)} out</div></div>
    <div><div class="k">Subagents</div><div class="v">${num(sub.count || 0)}</div><div class="s">${sub.count ? `${tok(sub.tokens)} tokens · ${usd(sub.cost_usd)}` : 'none launched'}</div></div>
    <div><div class="k">Verification</div><div class="v">${v.batches ? `${(v.verified_pass || 0) + (v.verified_fail || 0)}/${v.batches}` : '—'}</div><div class="s">${v.batches ? `${v.verified_pass} passed · ${v.verified_fail} failed · ${v.inspected} inspected · ${v.none} none` : 'no edit batches'}</div></div>
    <div><div class="k">Interruptions</div><div class="v">${(s.interrupts || []).length}</div><div class="s">${bl ? `${dur(bl)} blocked on you` : 'never blocked on you'}</div></div>
    <div><div class="k">Corrections</div><div class="v">${num(s.corrections || 0)}</div><div class="s">turns the judge labelled</div></div>
    <div class="wide"><div class="k">You / agent</div>${sessionSplitHTML(s)}</div>
    <div class="wide"><div class="k">Models</div>${models ? `<ul class="mix">${models}</ul>` : '<div class="s">no API calls recorded</div>'}</div>
  </div>`;
}

async function selectSession(id) {
  S.selSession = S.selSession === id ? null : id;
  document.querySelectorAll('tr[data-session]').forEach((r) => { const on = r.dataset.session === S.selSession; r.classList.toggle('expanded', on); r.setAttribute('aria-expanded', String(on)); });
  const box = $('#sel-session'); if (!box) return;
  if (!S.selSession) { box.innerHTML = ''; return; }
  const d = await getDay(S.day);
  const s = d.sessions.find((x) => x.id === id);
  if (!s) { box.innerHTML = ''; return; }
  const ts = await pool(s.turns.map((t) => t.id), 6, (tid) => getTurn(tid).catch(() => null));
  const at = await getAttr(S.day); S.attr = at && at.available ? at : null;
  const gapAfter = {};
  for (const g of d.gaps || []) if (g.session_id === id) {
    const prev = s.turns.filter((t) => (t.end || t.start) <= g.start).pop();
    if (prev) gapAfter[prev.id] = g.seconds;
  }
  const wt = worktreeOf(s);
  const rows = s.turns.map((t, i) => {
    const f = ts[i] || {};
    return `<tr class="row clickable" data-turnrow="${t.id}" tabindex="0">
      <td class="num" style="text-align:left">#${t.id}</td><td class="num">${hhmm(t.start)}</td>
      <td class="clip" style="width:38%;font-family:var(--font-mono);font-size:12.5px" title="${esc(f.prompt || '')}">${esc(f.prompt || '—')}</td>
      <td class="num">${f.tools_total != null ? f.tools_total : (f.tools || []).length}</td>
      <td>${tierBadge(f.tier)}${t.interrupted ? ' <span class="badge destructive">Interrupted</span>' : ''}</td>
      <td class="num">${gapAfter[t.id] != null ? dur(gapAfter[t.id]) : '—'}</td></tr>
      <tr class="sub" hidden data-turnsub="${t.id}"><td colspan="6" style="padding:12px 16px"></td></tr>`;
  }).join('');
  box.innerHTML = `<div class="sel-session" style="margin-top:16px">
    <div class="sh2"><span class="t">${s.agent === 'codex' ? 'Codex' : 'Claude'} · ${esc(wt.label)}</span>
      <span class="muted mono" style="font-size:12px">${esc(branchTxt(s.branch) || s.short)} · ${hhmm(s.turns[0].start)}–${hhmm(s.turns[s.turns.length - 1].end)} · ${usd(s.cost_usd)}</span>
      <span style="flex:1"></span><button class="btn outline sm" type="button" data-ask-session="${esc(s.id)}">${icon('spark')}Ask about this session</button></div>
    <div class="sum" id="sel-sum">${dur(isNum(s.active_s) ? s.active_s : unionSeconds(s.turns))} of agent time over ${s.turns.length} turn${s.turns.length === 1 ? '' : 's'}.</div>
    ${sessionFactsHTML(s, d)}
    <table><thead><tr><th>Turn</th><th class="num">Start</th><th>Prompt</th><th class="num">Tools</th><th>Verification</th><th class="num">Gap after</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  if (chat.available) summaryFor(s.id, null).then((sm) => { const el = $('#sel-sum'); if (sm && sm.summary && el && S.selSession === id) el.textContent = sm.summary; });
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function toggleTurnRow(row) {
  const id = row.dataset.turnrow;
  const sub = document.querySelector(`tr[data-turnsub="${id}"]`);
  sub.hidden = !sub.hidden;
  if (!sub.hidden && !sub.firstElementChild.innerHTML) sub.firstElementChild.innerHTML = await turnDetailHTML(+id, false);
}

/* ================= Trends ================= */
/* null = no data that day: drawn as a gap (no bar); 0 = a real zero: a 2px stub. */
function barRow(series, days, fmt, curIdx, H = 44) {
  const W = 300, gap = series.length > 10 ? 2 : 6;
  const vals = series.filter(isNum);
  const hi = Math.max(...vals, 0) || 1;
  const bw = (W - gap * (series.length - 1)) / series.length;
  const bars = series.map((v, i) => {
    const xx = (i * (bw + gap)).toFixed(1);
    if (!isNum(v)) return `<rect class="na" x="${xx}" y="0" width="${bw.toFixed(1)}" height="${H}"><title>${days[i]}: no data</title></rect>`;
    const h = v > 0 ? Math.max((v / hi) * (H - 4), 1.5) : 2;
    return `<rect class="${i === curIdx ? 'cur' : ''}" x="${xx}" y="${(H - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"><title>${days[i]}: ${fmt(v)}</title></rect>`;
  }).join('');
  const med = median(series);
  const my = isNum(med) && med > 0 ? (H - Math.max((med / hi) * (H - 4), 1)).toFixed(1) : null;
  const ml = my ? `<line class="med" x1="0" x2="${W}" y1="${my}" y2="${my}" vector-effect="non-scaling-stroke"/>` : '';
  return `<svg class="bars" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="${H}" aria-hidden="true">${bars}${ml}</svg>`;
}

/* stacked bars: stacks = [{name, series, color}] */
function stackRow(stacks, days, fmt, curIdx, H = 44) {
  const n = days.length, W = 300, gap = n > 10 ? 2 : 6;
  const tot = days.map((_, i) => (stacks.some((s) => isNum(s.series[i])) ? stacks.reduce((a, s) => a + (isNum(s.series[i]) ? s.series[i] : 0), 0) : null));
  const hi = Math.max(...tot.filter(isNum), 0) || 1;
  const bw = (W - gap * (n - 1)) / n;
  const out = days.map((day, i) => {
    const xx = (i * (bw + gap)).toFixed(1);
    if (!isNum(tot[i])) return `<rect class="na" x="${xx}" y="0" width="${bw.toFixed(1)}" height="${H}"><title>${day}: no data</title></rect>`;
    let y = H;
    const tip = `${day}: ${stacks.filter((s) => s.series[i]).map((s) => `${s.name} ${fmt(s.series[i])}`).join(', ') || fmt(0)}`;
    const segs = stacks.map((s) => {
      const v = s.series[i];
      if (!isNum(v) || v <= 0) return '';
      const h = (v / hi) * (H - 4);
      y -= h;
      return `<rect class="seg${i === curIdx ? ' cur' : ''}" style="fill:${s.color}" x="${xx}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h, 0.5).toFixed(1)}"><title>${esc(tip)}</title></rect>`;
    }).join('');
    return segs || `<rect class="${i === curIdx ? 'cur' : ''}" x="${xx}" y="${H - 2}" width="${bw.toFixed(1)}" height="2"><title>${esc(tip)}</title></rect>`;
  }).join('');
  return `<svg class="bars" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="${H}" aria-hidden="true">${out}</svg>`;
}
const shade = (base, k) => `color-mix(in srgb, ${base} ${[100, 62, 38, 22, 12][k % 5]}%, transparent)`;

VIEWS_trends = async function () {
  const n = S.trendRange;
  const t = await getTrend(S.day, n);
  const sr = t.series;
  const z = (k) => sr[k] || t.days.map(() => null);
  const cur = t.days.length - 1;
  const short = (d) => dayLabel(d, { day: 'numeric', month: 'short' });
  const head = (nm, v) => `<div class="mh"><span class="nm">${nm}</span><span class="v">${v}</span></div>`;
  const axis = `<div class="axis"><span>${esc(short(t.days[0]))}</span><span>${esc(short(t.days[cur]))}</span></div>`;
  const mult = (nm, sub, s, f) => `<div class="mult">${head(nm, f(s[cur]))}${barRow(s, t.days, f, cur)}${axis}<div class="ms">${sub}</div></div>`;
  const legend = (stacks) => `<div class="legend mini">${stacks.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>`;
  const multS = (nm, sub, stacks, f) => {
    const tot = t.days.map((_, i) => (stacks.some((s) => isNum(s.series[i])) ? stacks.reduce((a, s) => a + (s.series[i] || 0), 0) : null));
    return `<div class="mult">${head(nm, f(tot[cur]))}${stackRow(stacks, t.days, f, cur)}${axis}${legend(stacks)}<div class="ms">${sub}</div></div>`;
  };
  const minF = (v) => (isNum(v) ? `${num(v)} min` : '—');
  const hF = (v) => (isNum(v) ? `${num(v)} h` : '—');
  const pctPt = (v) => (isNum(v) ? `${Math.round(v)}%` : '—');
  const fg = 'var(--foreground)';

  // V: pooled over the range plus the median of days that had edit batches
  const V = z('V'), Vn = z('V_n');
  let vb = 0, vv = 0;
  V.forEach((v, i) => { if (isNum(v) && isNum(Vn[i])) { vb += Vn[i]; vv += v * Vn[i]; } });
  const vDays = V.filter(isNum).length;
  const vSub = vb ? `${pct(vv / vb)} of ${num(vb)} batches verified over ${vDays} day${vDays === 1 ? '' : 's'} with edits. Daily median ${pct(median(V))}. Days without edits are gaps.`
    : vDays ? `Verified share on ${vDays} day${vDays === 1 ? '' : 's'} with edits. Median ${pct(median(V))}. Days without edits are gaps.` : 'No edit batches in this range.';
  const Hs = z('H');
  const hSub = Hs.some(isNum) ? `Edits you made by hand. Median ${num(median(Hs))}.` : 'Not measured yet: needs the hook git snapshots.';
  const C = z('C');
  const causes = Object.keys(sr).filter((k) => k.startsWith('C:cause:'))
    .map((k) => ({ k: k.slice(8), s: sr[k], total: sr[k].reduce((a, v) => a + (v || 0), 0) })).sort((a, b) => b.total - a.total);
  const causeStacks = causes.map((c, i) => ({ name: pretty(c.k), series: c.s, color: shade(i < 2 ? 'var(--brand-clay)' : fg, i < 2 ? i : i - 2) }));
  const scored = [
    mult('V verified edits', vSub, V, pct),
    mult('H hand edits', hSub, Hs, num),
    mult('B blocked', `Minutes an agent waited on you. Median ${minF(median(z('B_min')))}.`, z('B_min'), minF),
    causeStacks.length
      ? multS(`C corrections ${UNCAL}`, `Turns you corrected, by judge cause. Median ${num(median(C))}. Days the judge has not run are gaps.`, causeStacks, num)
      : mult(`C corrections ${UNCAL}`, `Turns you corrected. ${C.some(isNum) ? `Median ${num(median(C))}.` : 'Run <code class="cmd">scorer judge</code>.'}`, C, num),
  ].join('');

  const gapKeys = [['gap_bucket:lt30s', '< 30 s'], ['gap_bucket:<30s', '< 30 s'], ['gap_bucket:30s-2m', '30 s–2 min'], ['gap_bucket:2-5m', '2–5 min'], ['gap_bucket:5-15m', '5–15 min'], ['gap_bucket:break', 'break > 15 min']]
    .filter(([k]) => sr[k]);
  const gapStacks = gapKeys.map(([k, nm], i) => ({ name: nm, series: sr[k], color: shade(k.endsWith('break') ? 'var(--brand-clay)' : fg, k.endsWith('break') ? 1 : i) }));
  const time = [
    mult('Active agent time', 'Hours with at least one agent working.', z('active_h'), hF),
    mult('Parallel time', 'Hours with two or more agents working at once.', z('overlap_h'), hF),
    mult('Interruptions', `Turns you stopped mid-run. Median ${num(median(z('interrupts')))}.`, z('interrupts'), num),
    mult('Thrown-away agent time', 'Agent minutes discarded by those interruptions.', z('thrown_min'), minF),
    gapStacks.length ? multS('Gaps between turns', `Time from a turn's end to your next prompt. Median gap ${dur(z('gap_median_s')[cur])} on the selected day.`, gapStacks, num) : '',
  ].join('');

  const models = t.models || [];
  const famColor = (m) => (/^claude/.test(m) ? 'var(--lane-claude)' : 'var(--lane-codex)');
  const famIdx = {};
  const costStacks = models.map((m) => { const f = famColor(m); famIdx[f] = (famIdx[f] || 0); return { name: m, series: sr[`cost_usd:${m}`] || [], color: shade(f, famIdx[f]++) }; });
  const tokStacks = [
    { name: 'input', series: z('tokens_in'), color: shade(fg, 0) },
    { name: 'output', series: z('tokens_out'), color: shade('var(--brand-clay)', 0) },
    { name: 'cached', series: z('tokens_cached'), color: shade(fg, 3) },
  ];
  const ctxStacks = [
    { name: 'p50', series: z('context_p50'), color: shade(fg, 0) },
    { name: 'p50–p90', series: z('context_p90').map((v, i) => (isNum(v) && isNum(z('context_p50')[i]) ? Math.max(v - z('context_p50')[i], 0) : v)), color: shade(fg, 3) },
  ];
  const subTok = z('subagent_tokens'), subCost = z('subagent_cost_usd');
  const agents = [
    mult('Cost per day', 'All models, list prices.', z('cost_usd'), usd),
    costStacks.length ? multS('Cost by model', 'Claude in clay, Codex/GPT in blue.', costStacks, usd) : '',
    multS('Tokens', `Input, output and cache reads per day. ${tok(sr.tokens_out ? sr.tokens_out[cur] : null)} output on the selected day.`, tokStacks, tok),
    mult('Subagents launched', `Selected day: ${tok(subTok[cur])} subagent tokens, ${usd(subCost[cur])}.`, z('subagents'), num),
    mult('Subagent tokens', `Tokens spent inside subagents. ${usd(subCost.reduce((a, v) => a + (v || 0), 0))} in range.`, subTok, tok),
    multS('Context window used', `Median and p90 of how full the context was per call. p90 ${pctPt(z('context_p90')[cur])} on the selected day.`, ctxStacks, pctPt),
    mult('Tooling started by you', 'Share of skills, commands, MCP and plugin uses you started.', z('tooling_human_share'), pct),
  ].join('');

  const dayRows = t.days.map((dd, i) => i).reverse().filter((i) => isNum(sr.active_h[i]) || isNum(z('cost_usd')[i])).map((i) => `<tr class="row">
      <td class="mono" style="font-size:13px">${esc(short(t.days[i]))}</td>
      <td class="num">${pct(V[i])}</td><td class="num">${num(Hs[i])}</td><td class="num">${num(z('B_min')[i])}</td>
      <td class="num">${num(C[i])}</td><td class="num">${num(sr.active_h[i])}</td><td class="num">${num(z('interrupts')[i])}</td><td class="num">${tok(isNum(z('tokens_in')[i]) ? z('tokens_in')[i] + (z('tokens_cached')[i] || 0) + (z('tokens_out')[i] || 0) : null)}</td><td class="num">${usd(z('cost_usd')[i])}</td></tr>`).join('');
  return `
  <section class="section-head">
    <div><h1 class="page-title">Trends</h1><p class="page-sub">Last ${n} days to ${esc(short(t.days[cur]))}. Dark bar: selected day. Dashed line: median of the days with data.</p></div>
    <div class="seg" role="group" aria-label="Range">${[7, 14, 30].map((k) => `<button type="button" data-range="${k}" aria-pressed="${n === k}">${k} days</button>`).join('')}</div>
  </section>
  <section class="section"><div class="section-head"><h2>Scored</h2><span class="note">V, H, B, C</span></div><div class="multiples auto">${scored}</div></section>
  <section class="section"><div class="section-head"><h2>Time</h2><span class="note">active, parallel, interruptions, gaps</span></div><div class="multiples auto">${time}</div></section>
  <section class="section"><div class="section-head"><h2>Agents &amp; cost</h2><span class="note">tokens, cost by model, subagents, context</span></div><div class="multiples auto">${agents}</div></section>
  <details class="rline trends-by-day">
    <summary><span class="rt">By day</span><span class="rs">table of the days with activity</span>${icon('chev', 'icon chev')}</summary>
    <div class="rb"><div class="table-shell"><table><thead><tr><th>Day</th><th class="num">V</th><th class="num">H</th><th class="num">B min</th><th class="num">C</th><th class="num">Active h</th><th class="num">Interrupts</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
    <tbody>${dayRows || '<tr><td colspan="9" class="muted">No activity.</td></tr>'}</tbody></table></div></div>
  </details>`;
};

/* ================= Leverage ================= */
VIEWS_leverage = async function () {
  const d = await getDay(S.day);
  const L = d.leverage || {};
  const T = L.tooling || {};
  const cats = Object.entries(T.categories || {});
  const totalUses = cats.reduce((a, [, c]) => a + (c.total || 0), 0);
  const f = S.skillCat || 'all';
  const segs = [['all', 'All'], ['skill', 'Skills'], ['command', 'Commands'], ['mcp', 'MCP servers'], ['plugin', 'Plugins']];
  return `
  <section>
    <h1 class="page-title">Leverage</h1>
    <p class="page-sub">Not scored. How work was delegated on ${esc(dayLabel(S.day))}, and who started it.</p>
  </section>
  <dl class="dl2">
    <div><dt>Subagents launched</dt><dd>${num(L.subagents)}</dd></div>
    <div><dt>Skill, command, MCP and plugin uses</dt><dd>${num(totalUses)}</dd></div>
    <div><dt>Context compactions</dt><dd>${num(L.compactions)}</dd></div>
    <div><dt>Started by you</dt><dd>${pct(T.human_share)}</dd></div>
  </dl>
  <section class="section">
    <div class="section-head"><h2>Skills and tools</h2>
      <div class="seg" role="group" aria-label="Category">${segs.map(([k, l]) => `<button type="button" data-skillcat="${k}" aria-pressed="${f === k}">${l}</button>`).join('')}</div></div>
    <div class="legend"><span><i class="sw you"></i>Started by you</span><span><i class="sw agt"></i>Started by the agent</span><span class="muted">Counts: you / agent</span></div>
    <div id="skillbars">${skillBars(d, f)}</div>
  </section>
  ${cats.some(([, c]) => c.default) ? `<p class="sub-note">Hooks and CLAUDE.md / AGENTS.md started ${num(cats.reduce((a, [, c]) => a + (c.default || 0), 0))} calls on their own. Those count as default, not you or the agent.</p>` : ''}
  <section class="section">
    <div class="section-head"><h2>Subagents</h2><span class="note">not scored</span></div>
    ${Array.isArray(L.subagent_types) && L.subagent_types.length ? `<div class="table-shell"><table><thead><tr><th>Subagent</th><th class="num">Runs</th><th>Started by</th><th class="num">Median time</th><th>Model</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead><tbody>
      ${L.subagent_types.map((a) => `<tr class="row"><td class="mono" style="font-size:13px">${esc(a.name)}</td><td class="num">${num(a.runs)}</td><td>${esc(Object.entries(a.initiators || {}).map(([k, v]) => `${k === 'human' ? 'you' : k} ${v}`).join(' · '))}</td><td class="num">${dur(a.median_s)}</td><td class="mono" style="font-size:12.5px">${esc(a.model || '—')}</td><td class="num">${tok(a.tokens)}</td><td class="num">${usd(a.cost_usd)}</td></tr>`).join('')}</tbody></table></div>`
      : `<p class="sub-note">${num(L.subagents)} subagent runs today. A breakdown by subagent type is not available from this server yet.</p>`}
  </section>
  <section class="section">
    <div class="section-head"><h2>By category</h2></div>
    <div class="table-shell"><table><thead><tr><th>Category</th><th class="num">You</th><th class="num">Agent</th><th class="num">Default</th><th class="num">Total</th></tr></thead>
    <tbody>${cats.length ? cats.sort((a, b) => (b[1].total || 0) - (a[1].total || 0)).map(([k, c]) => `<tr class="row"><td>${esc(CAT[k] || k)}</td><td class="num">${num(c.human)}</td><td class="num">${num(c.agent)}</td><td class="num">${num(c.default)}</td><td class="num">${num(c.total)}</td></tr>`).join('') : '<tr><td colspan="5" class="muted">No tooling use.</td></tr>'}</tbody></table></div>
  </section>`;
};

const VIEWS = { today: () => VIEWS_today(), sessions: () => VIEWS_sessions(), trends: () => VIEWS_trends(), leverage: () => VIEWS_leverage() };
var VIEWS_today, VIEWS_sessions, VIEWS_trends, VIEWS_leverage;
const AFTER = {
  today() {
    drawTimeline();
    fillNarrative();
    fillActions();
    if (S.selTurn != null) {
      const id = S.selTurn, fl = pendingFlash === id; pendingFlash = null;
      getDay(S.day).then((d) => {
        if (d.sessions.some((s) => s.turns.some((t) => t.id === id))) setTimeout(() => openTurn(id, { scroll: fl, flash: fl }), 0);
        else S.selTurn = null;
      });
    }
  },
  sessions() { if (S.selSession) { const id = S.selSession; S.selSession = null; selectSession(id); } },
};

/* ---------------- global clicks ---------------- */
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-turn]');
  if (t && !t.closest('.ask')) { openTurn(+t.dataset.turn, { scroll: !t.closest('.timeline') }); return; }
  if (e.target.closest('[data-close-detail]')) { S.selTurn = null; $('#turn-detail').innerHTML = ''; document.querySelectorAll('.timeline .bar.sel').forEach((b) => b.classList.remove('sel')); return; }
  const tr = e.target.closest('tr[data-turnrow]'); if (tr) { toggleTurnRow(tr); return; }
  const sr = e.target.closest('tr[data-session]'); if (sr) { selectSession(sr.dataset.session); return; }
  const as = e.target.closest('[data-ask-session]'); if (as) { const id = as.dataset.askSession; getDay(S.day).then((d) => { const s = d.sessions.find((x) => x.id === id); setAsk(true); input.value = `Summarize session ${s ? s.short : id} in ${s ? s.repo : 'this repo'}: what got done, and where did I lose time?`; autoGrow(); syncComposer(); setTimeout(() => input.focus(), 330); }); return; }
  const aw = e.target.closest('[data-askwhy]');
  if (aw) { const good = aw.dataset.askwhy.startsWith('s:'); const a = listItem(aw.dataset.askwhy); if (a) { setAsk(true); const ids = (a.turn_ids || []).slice(0, 5).map((t) => `#${t}`).join(', '); input.value = good ? `What exactly did I do well here: "${a.text}"? Show me the turns${ids ? ` (${ids})` : ''} and how to make it a habit.` : `Why this action item: "${a.text}"? Walk me through the evidence${ids ? ` (turns ${ids})` : ''} and what to change.`; autoGrow(); syncComposer(); setTimeout(() => input.focus(), 330); } return; }
  const cl = e.target.closest('[data-copyline]');
  if (cl) { const a = listItem(cl.dataset.copyline); if (a && a.claude_md_line) { const done = (ok) => { cl.textContent = ok ? 'Copied' : 'Copy failed'; setTimeout(() => { cl.textContent = 'Copy'; }, 1500); }; try { navigator.clipboard.writeText(a.claude_md_line).then(() => done(true), () => done(false)); } catch (err) { done(false); } } return; }
  const tg = e.target.closest('[data-toggle-temp]');
  if (tg) { S.includeTemp = !S.includeTemp; try { localStorage.setItem('scorer-include-temp', S.includeTemp ? '1' : '0'); } catch (err) { /* ignore */ } S.selSession = null; render(); return; }
  const rg = e.target.closest('[data-range]'); if (rg) { S.trendRange = +rg.dataset.range; render(); return; }
  const sc = e.target.closest('[data-skillcat]');
  if (sc) { S.skillCat = sc.dataset.skillcat; document.querySelectorAll('[data-skillcat]').forEach((b) => b.setAttribute('aria-pressed', String(b === sc))); getDay(S.day).then((d) => { $('#skillbars').innerHTML = skillBars(d, S.skillCat); }); return; }
  if (e.target.closest('#tl-expand')) { S.tlExpanded = !S.tlExpanded; const b = $('#tl-expand'); b.setAttribute('aria-pressed', String(S.tlExpanded)); b.textContent = S.tlExpanded ? 'Group by worktree' : 'Show sessions'; drawTimeline(); return; }
  const nc = e.target.closest('.narr .cite');
  if (nc) { const c = narrCites[nc.dataset.cite]; if (c) { setAsk(true); openPeek(c); } return; }
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const tr = e.target.closest && (e.target.closest('tr[data-turnrow]') || e.target.closest('tr[data-session]'));
  if (tr && tr === e.target) { e.preventDefault(); tr.click(); }
});
let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(drawTimeline, 120); });

/* ---------------- header: day picker, theme, ask toggle ---------------- */
function setDay(d) {
  if (!d || d === S.day) return;
  S.day = d; S.selTurn = null; $('#day').value = d;
  const u = new URL(location.href); u.searchParams.set('day', d); history.replaceState(null, '', u);
  render();
}
$('#day').addEventListener('change', (e) => setDay(e.target.value));
$('#prev').addEventListener('click', () => { const i = S.days.findIndex((x) => x.day === S.day); if (i < S.days.length - 1) setDay(S.days[i + 1].day); });
$('#next').addEventListener('click', () => { const i = S.days.findIndex((x) => x.day === S.day); if (i > 0) setDay(S.days[i - 1].day); });

function currentTheme() {
  const a = document.documentElement.getAttribute('data-theme');
  return a || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}
function syncThemeIcon() { $('#theme use').setAttribute('href', currentTheme() === 'dark' ? '#i-sun' : '#i-moon'); }
$('#theme').addEventListener('click', () => {
  const t = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('scorer-theme', t); } catch (e) {}
  syncThemeIcon();
});

function setAsk(open) {
  const a = $('#ask');
  a.classList.toggle('open', open);
  a.setAttribute('aria-hidden', String(!open));
  $('#askbtn').setAttribute('aria-pressed', String(open));
  $('#askbtn').classList.toggle('outline', !open);
  if (open) setTimeout(() => $('#input').focus({ preventScroll: true }), 320);
}
$('#askbtn').addEventListener('click', () => setAsk(!$('#ask').classList.contains('open')));
$('#ask').addEventListener('transitionend', (e) => { if (e.propertyName === 'width') drawTimeline(); });

/* ================= Ask scorer (chat) ================= */
const chat = { messages: [], streaming: false, ctrl: null, available: true, reason: null };
const SUGGESTIONS = [
  'Why was so much of today unverified?',
  'Which turn blocked me the longest, and why?',
  'What were my corrections about?',
  'Compare today with the last 7 days.',
];
const TOOL_META = {
  query_db: ['Querying the database', 'Queried the database'],
  get_turn: ['Reading a turn', 'Read a turn'],
  get_metric: ['Querying metrics', 'Queried metrics'],
  list_sessions: ['Listing sessions', 'Listed sessions'],
};
const humanize = (n) => String(n || 'step').replace(/[-_]/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^./, (c) => c.toUpperCase());
function stepLabels(name, input) {
  const m = TOOL_META[name];
  let a = m ? m[0] : humanize(name), d = m ? m[1] : humanize(name);
  if (name === 'get_turn' && input && input.turn_id != null) { a = `Reading turn #${input.turn_id}`; d = `Read turn #${input.turn_id}`; }
  if (name === 'get_metric' && input && input.metric) { a = `Querying ${input.metric}`; d = `Queried ${input.metric}`; }
  return [a, d];
}

/* tiny safe markdown: escape first, then paragraphs, lists, headings, bold, code, [n] pills */
function inlineMd(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\[(\d{1,3})\](?!\()/g, '<button type="button" class="cite" data-cite="$1">$1</button>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}
function md(src) {
  const lines = esc(src).split('\n');
  const out = []; let para = [], list = null, fence = null;
  const flushP = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; } };
  const flushL = () => { if (list) { out.push(`<${list.t}>${list.items.map((x) => `<li>${inlineMd(x)}</li>`).join('')}</${list.t}>`); list = null; } };
  for (const raw of lines) {
    if (fence) { if (/^\s*```/.test(raw)) { out.push(`<pre><code>${fence.join('\n')}</code></pre>`); fence = null; } else fence.push(raw); continue; }
    if (/^\s*```/.test(raw)) { flushP(); flushL(); fence = []; continue; }
    const li = raw.match(/^\s*(?:[-*]|(\d+)\.)\s+(.*)$/);
    if (li) { flushP(); const t = li[1] ? 'ol' : 'ul'; if (!list || list.t !== t) { flushL(); list = { t, items: [] }; } list.items.push(li[2]); continue; }
    const h = raw.match(/^\s*#{1,6}\s+(.*)$/);
    if (h) { flushP(); flushL(); out.push(`<h4>${inlineMd(h[1])}</h4>`); continue; }
    if (!raw.trim()) { flushP(); flushL(); continue; }
    if (list && /^\s{2,}/.test(raw)) { list.items[list.items.length - 1] += ' ' + raw.trim(); continue; }
    flushL(); para.push(raw.trim());
  }
  if (fence) out.push(`<pre><code>${fence.join('\n')}</code></pre>`);
  flushP(); flushL();
  return out.join('');
}

function stepsHTML(m, live) {
  const steps = m.steps;
  const active = live ? steps.find((s) => !s.done) : null;
  if (live && (active || !m.text)) {
    let label = active ? active.active : 'Thinking';
    const parent = active && active.parent ? steps.find((s) => s.id === active.parent) : null;
    if (parent) label = `${parent.agent}: ${label}`;
    return `<div><span class="shimmer">${esc(label.replace(/(\.\.\.|…)\s*$/, ''))}</span></div>`;
  }
  const done = steps.filter((s) => s.done && !s.parent);
  if (!done.length) return '';
  const n = steps.filter((s) => s.done).length;
  const li = (s, nested) => `<li class="${nested ? 'nested' : ''}"><span class="ico${s.ok === false ? ' fail' : ''}">${icon(s.ok === false ? 'alert' : 'check')}</span>
      <span class="lab">${esc(s.doneLabel)}<small><span class="tn">${s.agent ? 'subagent · ' : ''}${esc(s.agent ? (steps.filter((c) => c.parent === s.id).map((c) => c.name).join(' · ') || s.name) : s.name)}</span>${s.summary ? ` · ${esc(s.summary)}` : ''}</small>${s.sql ? `<details class="sqlq"><summary>show query</summary><pre><code>${esc(s.sql)}</code></pre></details>` : ''}</span></li>`;
  const items = done.map((s) => li(s, false) + steps.filter((c) => c.parent === s.id && c.done).map((c) => li(c, true)).join('')).join('');
  return `<div><button type="button" class="steps-toggle" aria-expanded="${m.open ? 'true' : 'false'}" data-steps="${m.id}">
      <span>${n} step${n === 1 ? '' : 's'} completed</span>${icon('chev')}</button>
      ${m.open ? `<ol class="steps">${items}</ol>` : ''}</div>`;
}

function messageHTML(m, i) {
  if (m.role === 'user') return `<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>`;
  const live = chat.streaming && i === chat.messages.length - 1;
  const card = (m.cards || []).filter((k) => k.cites.length).map((k) => `<div class="sources"><div class="sh"><span class="mono">${esc(k.name)}${k.summary ? ` · ${esc(k.summary)}` : ''}</span><span class="ct">${k.cites.length > 3 ? `3 of ${k.cites.length}` : k.cites.length} row${k.cites.length === 1 ? '' : 's'}</span></div>
      ${k.cites.slice(0, 3).map((c) => `<button type="button" data-peek="${c.n}"><span class="sn">[${c.n}]</span><span class="sl">${c.turn_id != null ? `<span class="mono">#${c.turn_id}</span> ` : ''}${esc(c.label || '')}</span></button>`).join('')}</div>`).join('');
  return `<div class="msg assistant" data-mi="${i}">${stepsHTML(m, live)}${card}
    ${m.text ? `<div class="md">${md(m.text)}</div>` : ''}
    ${m.error ? `<div class="errline">${esc(m.error)}</div>` : ''}
    ${!live && i === chat.messages.length - 1 && (m.suggestions || []).length ? `<div class="quick" role="group" aria-label="Suggested follow-ups">${m.suggestions.map((q, k) => `<button type="button" data-quick="${k}" title="${esc(q.prompt)}">${esc(q.label || q.prompt)}</button>`).join('')}</div>` : ''}</div>`;
}

function emptyHTML() {
  return `<div class="empty-chat">
      <div class="icon-tile">${icon('chart')}</div>
      <div><span class="t">Ask scorer</span><span class="s">Questions about your day, answered from the scorer database with links to the turns behind each claim.</span></div>
      <div class="suggest">${SUGGESTIONS.map((s) => `<button type="button" data-suggest="${esc(s)}" ${chat.available ? '' : 'disabled'}>${esc(s)}</button>`).join('')}</div>
    </div>${chat.available ? '' : `<div class="nokey">The analyst is unavailable: ${esc(chat.reason || 'no API key set')}. Set <span class="mono">ANTHROPIC_API_KEY</span> and restart <span class="mono">scorer serve</span>.</div>`}`;
}

const conv = $('#conv');
const nearBottom = () => conv.scrollHeight - conv.scrollTop - conv.clientHeight < 80;
let raf = 0;
function renderChat(force) {
  if (raf && !force) return;
  const run = () => {
    raf = 0;
    const stick = nearBottom();
    $('#log').innerHTML = chat.messages.length ? chat.messages.map(messageHTML).join('') : emptyHTML();
    if (stick || force) conv.scrollTop = conv.scrollHeight;
    syncComposer();
  };
  if (force) { cancelAnimationFrame(raf); run(); } else raf = requestAnimationFrame(run);
}

const input = $('#input'), sendBtn = $('#send');
function syncComposer() {
  const can = chat.available && !chat.streaming && input.value.trim().length > 0;
  input.disabled = chat.streaming || !chat.available;
  sendBtn.disabled = !chat.streaming && !can;
  sendBtn.className = `send${chat.streaming ? ' stop' : can ? ' ready' : ''}`;
  sendBtn.setAttribute('aria-label', chat.streaming ? 'Stop response' : 'Send message');
  sendBtn.innerHTML = chat.streaming ? '<span class="sq"></span>' : icon('up');
}
function autoGrow() { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`; }
input.addEventListener('input', () => { autoGrow(); syncComposer(); });
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } });
sendBtn.addEventListener('click', () => (chat.streaming ? stop() : send(input.value)));

function stop() { if (chat.ctrl) chat.ctrl.abort(); }

let msgSeq = 0;
async function send(text) {
  text = (text || '').trim();
  if (!text || chat.streaming || !chat.available) return;
  input.value = ''; autoGrow();
  chat.messages.push({ role: 'user', content: text });
  const m = { role: 'assistant', id: ++msgSeq, text: '', steps: [], cites: {}, error: null, open: false };
  const history = chat.messages.filter((x) => x.role === 'user' || x.text).map((x) => ({ role: x.role, content: x.role === 'user' ? x.content : x.text }));
  chat.messages.push(m);
  chat.streaming = true; chat.ctrl = new AbortController();
  renderChat(true);
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: history, day: S.day }), signal: chat.ctrl.signal });
    if (!r.ok || !r.body) throw new Error(r.status === 501 ? 'The analyst is not installed on this server.' : `Request failed (${r.status}).`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, k); buf = buf.slice(k + 2);
        const data = block.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).trimStart()).join('\n');
        if (data) { try { onEvent(m, JSON.parse(data)); } catch (e) { /* skip malformed */ } }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') m.error = e.message || 'Something went wrong.';
    else if (!m.text) m.error = 'Stopped.';
  } finally {
    m.steps.forEach((s) => { if (!s.done) { s.done = true; s.ok = s.ok !== false && !chat.ctrl.signal.aborted ? s.ok : s.ok; } });
    chat.streaming = false; chat.ctrl = null;
    renderChat(true);
  }
}

function onEvent(m, ev) {
  const openSub = () => [...m.steps].reverse().find((s) => s.agent && !s.done);
  switch (ev.type) {
    case 'text-delta': m.text += ev.delta || ''; break;
    case 'tool-start': {
      const [a, d] = stepLabels(ev.name, ev.input);
      const sub = ev.parent_id || ev.subagent_id ? m.steps.find((s) => s.id === (ev.parent_id || ev.subagent_id)) : openSub();
      m.steps.push({ id: ev.id || `t${m.steps.length}`, name: ev.name, active: a, doneLabel: d, done: false, parent: sub ? sub.id : null, sql: ev.sql_full || null });
      break;
    }
    case 'tool-result': {
      const s = m.steps.find((x) => x.id === ev.id) || [...m.steps].reverse().find((x) => !x.done && x.name === ev.name);
      if (s) { s.done = true; s.ok = ev.ok !== false; s.summary = ev.summary || ''; }
      for (const c of ev.citations || []) m.cites[c.n] = c;
      if ((ev.citations || []).length) (m.cards = m.cards || []).push({ name: ev.name, summary: ev.summary || '', cites: ev.citations });
      break;
    }
    case 'subagent-start': {
      const agent = ev.name || ev.agent || 'subagent';
      const task = ev.task || ev.summary || (ev.input && (ev.input.task || ev.input.question)) || '';
      m.steps.push({ id: ev.id || `s${m.steps.length}`, agent, name: agent, active: `${agent}: ${task || 'working'}`, doneLabel: `${agent}: ${task || 'done'}`, done: false, parent: null });
      break;
    }
    case 'subagent-result': {
      const s = m.steps.find((x) => x.id === ev.id) || openSub();
      if (s) { s.done = true; s.ok = ev.ok !== false; s.summary = ev.summary || ''; m.steps.filter((c) => c.parent === s.id && !c.done).forEach((c) => { c.done = true; }); }
      for (const c of ev.citations || []) m.cites[c.n] = c;
      if ((ev.citations || []).length) (m.cards = m.cards || []).push({ name: ev.name || 'subagent', summary: ev.summary || '', cites: ev.citations });
      break;
    }
    case 'suggestions': m.suggestions = (ev.items || []).filter((x) => x && x.prompt).slice(0, 4); break;
    case 'error': m.error = ev.message || 'The analyst hit an error.'; break;
    case 'done': break;
    default: return;
  }
  renderChat();
}

$('#log').addEventListener('click', (e) => {
  const sg = e.target.closest('[data-suggest]'); if (sg) { send(sg.dataset.suggest); return; }
  const qk = e.target.closest('[data-quick]');
  if (qk) { const mi = +qk.closest('[data-mi]').dataset.mi; const q = (chat.messages[mi].suggestions || [])[+qk.dataset.quick]; if (q) send(q.prompt); return; }
  const st = e.target.closest('[data-steps]');
  if (st) { const m = chat.messages.find((x) => x.id === +st.dataset.steps); if (m) { m.open = !m.open; renderChat(true); } return; }
  const pk = e.target.closest('[data-peek]');
  if (pk) { const mi = +pk.closest('[data-mi]').dataset.mi; const cite = chat.messages[mi].cites[pk.dataset.peek]; if (cite) openPeek(cite); return; }
  const c = e.target.closest('.cite');
  if (c) {
    const mi = +c.closest('[data-mi]').dataset.mi;
    const cite = chat.messages[mi].cites[c.dataset.cite];
    if (cite) openPeek(cite);
  }
});

/* citation peek */
async function openPeek(c) {
  const p = $('#peek');
  $('#peek-title').textContent = c.turn_id != null ? `Turn #${c.turn_id}` : `Source ${c.n}`;
  $('#peek-sub').textContent = `[${c.n}]${c.session_id ? ` · session ${String(c.session_id).slice(0, 8)}` : ''}`;
  const body = $('#peek-body');
  body.innerHTML = `${c.label ? `<div class="lbl">Evidence</div><blockquote>${esc(c.label)}</blockquote>` : ''}
    ${c.turn_id != null ? `<div><button class="btn outline sm" type="button" data-show="${c.turn_id}">Show in timeline</button></div><div id="peek-turn" class="muted" style="font-size:13px">Loading turn…</div>` : ''}`;
  p.classList.add('open'); p.setAttribute('aria-hidden', 'false');
  if (c.turn_id == null) return;
  try {
    const t = await getTurn(c.turn_id);
    const box = $('#peek-turn'); if (!box) return;
    box.className = '';
    box.innerHTML = `<div class="lbl" style="margin-bottom:6px">Prompt (clipped)</div><blockquote>${esc(t.prompt || '—')}</blockquote>
      <dl class="kv" style="margin-top:12px">
        <dt>Verification</dt><dd>${tierBadge(t.tier)}</dd>
        <dt>Started</dt><dd>${esc(dayLabel(String(t.start).slice(0, 10), { day: 'numeric', month: 'short' }))} ${hhmm(t.start)}</dd>
        <dt>Duration</dt><dd>${dur(secs(t.start, t.end))}</dd>
        <dt>Tool calls</dt><dd>${t.tools_total != null ? t.tools_total : (t.tools || []).length}</dd>
        <dt>Cost</dt><dd>${usd(t.api && t.api.cost_usd)}</dd></dl>`;
  } catch (e) { const box = $('#peek-turn'); if (box) box.textContent = 'Turn not found in the latest ingest.'; }
}
function closePeek() { const p = $('#peek'); p.classList.remove('open'); p.setAttribute('aria-hidden', 'true'); }
$('#peek-close').addEventListener('click', closePeek);
$('#peek').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-show]'); if (!b) return;
  const id = +b.dataset.show;
  closePeek();
  // The turn may belong to another day: switch the picker to the turn's local day first.
  try {
    const t = await getTurn(id);
    const local = new Date(t.start); const ld = `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, '0')}-${String(local.getDate()).padStart(2, '0')}`;
    if (ld !== S.day && S.days.some((x) => x.day === ld)) { S.day = ld; $('#day').value = ld; }
  } catch (err) { /* fall through to current day */ }
  S.selTurn = id; pendingFlash = id;
  if (S.tab !== 'today') location.hash = '#/today'; else render();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#peek').classList.contains('open')) closePeek(); });

/* ---------------- boot ---------------- */
(async function boot() {
  syncThemeIcon();
  S.tab = readHash();
  if (!location.hash) history.replaceState(null, '', `${location.pathname}${location.search}#/today`);
  setAsk(window.innerWidth >= 1440);
  try {
    const st = await api('/api/chat/status');
    chat.available = !!st.available; chat.reason = st.reason; chat.model = st.model;
  } catch (e) { chat.available = false; chat.reason = 'status endpoint unreachable'; }
  renderChat(true);
  try {
    const r = await api('/api/days');
    S.days = r.days; S.today = r.today;
    const want = new URLSearchParams(location.search).get('day');
    S.day = (want && r.days.some((x) => x.day === want)) ? want : (r.days[0] && r.days[0].day);
    $('#day').innerHTML = r.days.map((x) => `<option value="${x.day}">${esc(dayLabel(x.day, { weekday: 'short', day: 'numeric', month: 'short' }))} · ${x.turns} turns</option>`).join('');
    if (S.day) $('#day').value = S.day;
  } catch (e) { $('#view').innerHTML = `<div class="empty">Could not reach the scorer API. ${esc(e.message)}</div>`; return; }
  render();
})();
