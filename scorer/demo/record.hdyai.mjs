// Scripted, recordable click-through of Ari's app ("how do you ai") with the scorer bridge.
// Usage: node record.hdyai.mjs        (Ari's app must be running at BASE, default http://127.0.0.1:3300)
// Output: out/left.webm (1344x1080 = left 70% of a 1920x1080 frame), out/left.marks.json, out/stills/hd-<beat>.png
import { chromium } from 'playwright';
import { spawnSync } from 'node:child_process';
import { mkdirSync, renameSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const STILLS = join(OUT, 'stills');
const VIDEO_TMP = join(OUT, '_video_left');

const BASE = process.env.HDYAI_URL || 'http://127.0.0.1:3300';
const REPORT = process.env.HDYAI_REPORT || 'scorer-demo-2026-10-14';
const QUESTION = 'Which corrections happened right after I switched sessions?';
const W = 1344, H = 1080;

// ---- timings (ms). No voice-over: every sub-beat holds max(action time, anchor + GAP), anchors (s) from
// out/beats.json → anchors (regenerate with `node beats.mjs`), re-read at start.
let VO = {};
try { VO = JSON.parse(readFileSync(join(OUT, 'beats.json'), 'utf8')).anchors || {}; } catch { console.log('no out/beats.json anchors, using defaults'); }
const VO_DEFAULT = 3;          // seconds, when a beat has no anchor
const GAP = 300;               // breathing room after each anchor hold
const T = { typeDelay: 32, settle: 500, connectRetry: 60_000, chatTimeout: 90_000 };
const STOP_AFTER = process.env.STOP_AFTER || '';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

async function waitForServer() {
  const t0 = Date.now(); let last = null;
  while (Date.now() - t0 < T.connectRetry) {
    try { const r = await fetch(`${BASE}/api/reports/${REPORT}`); if (r.ok) return; last = `HTTP ${r.status}`; }
    catch (e) { last = e.message; }
    await sleep(1000);
  }
  throw new Error(`app at ${BASE} not reachable after ${T.connectRetry / 1000} s (${last})`);
}

// ---- fake cursor + click ring, injected before every page script ----
const CURSOR_SCRIPT = `
(() => {
  if (window.__demoCursorInstalled) return;
  window.__demoCursorInstalled = true;
  const css = \`
    #__cursor { position: fixed; left: 0; top: 0; width: 22px; height: 30px; z-index: 2147483647; pointer-events: none;
      transition: transform .08s linear; transform: translate(-9999px, -9999px); filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)); }
    #__cursor.down svg { transform: scale(.88); transform-origin: 2px 2px; }
    .__ring { position: fixed; z-index: 2147483646; pointer-events: none; border: 3px solid #b36813; border-radius: 8px;
      box-shadow: 0 0 0 4px rgba(179,104,19,.22); animation: __ring .7s ease-out forwards; }
    @keyframes __ring { 0% { opacity: 1; transform: scale(1); } 100% { opacity: 0; transform: scale(1.08); } }
    nextjs-portal { display: none !important; }
  \`;
  // Elements in the top layer (open <dialog>) cover anything with z-index; re-parent cursor into the open dialog.
  const host = () => document.querySelector('dialog[open]') || document.body;
  const install = () => {
    if (document.getElementById('__cursor')) return;
    const style = document.createElement('style'); style.textContent = css; document.head.appendChild(style);
    const c = document.createElement('div'); c.id = '__cursor';
    c.innerHTML = '<svg viewBox="0 0 22 30" width="22" height="30"><path d="M2 2 L2 23 L7.5 18 L11 27 L15 25.5 L11.5 17 L19 17 Z" fill="#1c2d22" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    const rehost = () => { const h = host(); if (c.parentNode !== h) h.appendChild(c); };
    new MutationObserver(rehost).observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ['open'] });
    document.addEventListener('mousemove', (e) => { rehost(); c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)'; }, true);
    document.addEventListener('mousedown', (e) => {
      c.classList.add('down');
      const t = e.target instanceof Element ? e.target : null;
      if (!t) return;
      const r = t.getBoundingClientRect();
      const ring = document.createElement('div'); ring.className = '__ring';
      ring.style.left = (r.left - 5) + 'px'; ring.style.top = (r.top - 5) + 'px';
      ring.style.width = (r.width + 10) + 'px'; ring.style.height = (r.height + 10) + 'px';
      host().appendChild(ring); setTimeout(() => ring.remove(), 750);
    }, true);
    document.addEventListener('mouseup', () => c.classList.remove('down'), true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();`;

// ---- frame-accurate marks: a 4x4 px gray dot in the bottom-left corner steps through 3 gray levels, one step per
// mark. Under load the recorder lags the wall clock by up to ~2 s, so after the take each mark is located in the webm
// by the frame where the dot reaches that mark's level. (State survives navigations via sessionStorage.)
const LEVELS = [40, 125, 205];
const MARKER_SCRIPT = (LV) => {
  const paint = () => {
    if (!document.body) return;
    let d = document.getElementById('__mk');
    if (!d) { d = document.createElement('div'); d.id = '__mk'; d.style.cssText = 'position:fixed;left:0;bottom:0;width:4px;height:4px;z-index:2147483647;pointer-events:none'; }
    const h = document.querySelector('dialog[open]') || document.body; if (d.parentNode !== h) h.appendChild(d);
    let k = -1; try { k = Number(sessionStorage.getItem('__mk') ?? -1); } catch {}
    const v = k < 0 ? 250 : LV[k % LV.length]; d.style.background = `rgb(${v},${v},${v})`;
  };
  window.__mkPaint = paint;
  setInterval(paint, 50); document.addEventListener('DOMContentLoaded', paint);
};
const markOrder = [];
async function mark(id) {
  const t = now(); marks[id] = t; markOrder.push(id);
  const k = markOrder.length - 1;
  await page.evaluate((k) => { try { sessionStorage.setItem('__mk', String(k)); } catch {} window.__mkPaint?.(); }, k).catch(() => {});
  return t;
}

// ---- privacy: only demo sessions (/Users/demo/*, /sandbox/*) may be readable on camera ----
// Private sessions in the same DB/report are redacted live with a CSS Custom Highlight (no DOM edits, React-safe):
// any text matching a private prompt/title snippet, and every quoted span in analyst answers. Session-list rows of
// private sessions get a blur filter. Evidence links are chosen so the inspector only opens demo turns.
const isDemoCwd = (cwd) => /^\/Users\/demo\/|^\/sandbox\//.test(cwd || '');
async function loadPrivacy() {
  const get = async (u) => (await fetch(`${BASE}${u}`)).json();
  const sessions = await get('/api/sessions');
  const demoIds = sessions.filter((x) => isDemoCwd(x.cwd)).map((x) => x.id);
  const priv = sessions.filter((x) => !isDemoCwd(x.cwd));
  const norm = (t) => String(t || '').replace(/\s+/g, ' ').trim();
  const snippets = new Set(); const titles = [];
  for (const x of priv) {
    const title = norm(x.title); titles.push(title);
    const tail = title.split(' · ').slice(1).join(' · ');
    if (tail.length >= 10) snippets.add(tail.slice(0, 28));
    try {
      const ev = await get(`/api/evidence?id=${encodeURIComponent(x.id)}&session=1&report=${REPORT}`);
      for (const e of ev.events || []) {
        if (e.kind !== 'user') continue;
        const t = norm(e.text); if (t.length < 12) continue;
        snippets.add(t.slice(0, 28));
      }
    } catch (e) { log('  privacy: no events for', x.id.slice(0, 8), e.message); }
  }
  return { demoIds, titles, snippets: [...snippets] };
}
const PRIVACY_SCRIPT = (P) => {
  if (window.__privInstalled) return; window.__privInstalled = true;
  const css = `::highlight(priv){color:transparent;text-shadow:0 0 7px rgba(40,50,40,.8)}
    [data-priv]{filter:blur(5px)}`;
  const norm = (t) => t.replace(/\s+/g, ' ');
  const esc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s+');
  const RES = P.snippets.map((x) => new RegExp(esc(x), 'g'));
  const run = () => {
    if (!document.body) return;
    if (!document.getElementById('__privcss')) { const st = document.createElement('style'); st.id = '__privcss'; st.textContent = css; document.head.appendChild(st); }
    for (const row of document.querySelectorAll('.session-row')) {
      const t = norm(row.querySelector('.session-info strong')?.textContent || '').trim();
      const priv = P.titles.some((x) => x === t || (t.endsWith('…') ? x.startsWith(t.slice(0, -1)) : false) || x.startsWith(t));
      if (priv && !row.hasAttribute('data-priv')) row.setAttribute('data-priv', ''); else if (!priv && row.hasAttribute('data-priv')) row.removeAttribute('data-priv');
    }
    if (!window.CSS?.highlights) return;
    const ranges = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const raw = n.nodeValue; if (!raw || raw.length < 4) continue;
      const el = n.parentElement; if (!el || el.closest('#__cursor, #__mk, script, style')) continue;
      const add = (a, b) => { const r = new Range(); r.setStart(n, a); r.setEnd(n, b); ranges.push(r); };
      // private prompt/title text: redact from the match to the end of the text node (the rest of that prompt)
      for (const re of RES) { re.lastIndex = 0; const m = re.exec(raw); if (m) { add(m.index, raw.length); break; } }
      // analyst answer prose: any quoted span may be a private prompt
      if (el.closest('.analyst-a p')) for (const m of raw.matchAll(/["“][^"”\n]{3,}["”]/g)) add(m.index, m.index + m[0].length);
    }
    CSS.highlights.set('priv', new Highlight(...ranges));
  };
  setInterval(run, 120);
  document.addEventListener('DOMContentLoaded', run);
};

// ---- main ----
mkdirSync(STILLS, { recursive: true });
rmSync(VIDEO_TMP, { recursive: true, force: true });
mkdirSync(VIDEO_TMP, { recursive: true });

log('waiting for app', BASE);
await waitForServer();
// Warm Next dev compiles (pages + API routes) before recording so nothing stalls on camera.
for (const p of ['/sessions', `/reports/${REPORT}`, '/api/sessions', `/api/reports/${REPORT}`]) {
  await fetch(`${BASE}${p}`).catch(() => {});
}

const PRIV = await loadPrivacy();
log(`privacy: ${PRIV.demoIds.length} demo sessions, ${PRIV.titles.length} private sessions, ${PRIV.snippets.length} snippets`);
const isDemoEvidence = (id) => PRIV.demoIds.some((d) => id.startsWith(`${d}:`));
// p3e target: first tile (C, episodes, H, V) whose visible evidence buttons include a demo-session turn.
const REPORT_JSON = await (await fetch(`${BASE}/api/reports/${REPORT}`)).json();
let P3E = null;
for (const key of ['C', 'episodes', 'H', 'V']) {
  const t = REPORT_JSON.scorerMetrics?.tiles?.find((x) => x.key === key); if (!t) continue;
  const i = t.evidence.slice(0, 4).findIndex((e) => !e.id.startsWith('metric:') && isDemoEvidence(e.id));
  if (i >= 0) { P3E = { key, index: i, id: t.evidence[i].id }; break; }
}
log('p3e target:', JSON.stringify(P3E));

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: W, height: H },
  deviceScaleFactor: 2,
  colorScheme: 'light',
  recordVideo: { dir: VIDEO_TMP, size: { width: W, height: H } },
});
await context.addInitScript(CURSOR_SCRIPT);
await context.addInitScript(PRIVACY_SCRIPT, PRIV);
await context.addInitScript(MARKER_SCRIPT, LEVELS);
const page = await context.newPage();
const T0 = Date.now();
const now = () => (Date.now() - T0) / 1000;

const failures = [];
const marks = {};
async function vo(id, fn) {
  const t = await mark(id); log(`${id} @ ${t.toFixed(2)}s`);
  try { await fn(); } catch (e) { failures.push({ beat: id, error: e.message.split('\n')[0] }); log(`  FAILED: ${e.message.split('\n')[0]}`); }
  const need = ((VO[id] ?? VO_DEFAULT) * 1000) + GAP - (now() - t) * 1000;
  if (need > 0) await sleep(need);
  if (STOP_AFTER === id) { log('STOP_AFTER', id); await context.close(); await browser.close(); process.exit(0); }   // stills are cut from the webm afterwards: a live 2x screenshot stalls the take on a loaded machine
}

async function glideTo(locator, steps = 14) {
  const box = await locator.filter({ visible: true }).first().boundingBox({ timeout: 5000 });
  if (!box) throw new Error('element has no bounding box');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps });
}
async function glideClick(locator) { const l = locator.filter({ visible: true }).first(); await l.scrollIntoViewIfNeeded(); await glideTo(l); await sleep(350); await l.click(); }
// Smooth-scroll so `sel` sits `off` px below the top of the viewport (works for window or any scroll parent).
const scrollToSel = (sel, off = 80) => page.evaluate(([s, o]) => {
  const el = document.querySelector(s); if (!el) return false;
  const y = el.getBoundingClientRect().top + window.scrollY - o;
  window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' }); return true;
}, [sel, off]);
const closeInspector = async () => {
  if (await page.locator('[data-testid="evidence-inspector"][open]').count()) { await page.keyboard.press('Escape'); await sleep(400); }
};
async function gotoReady(url, readySel) {
  const t0 = Date.now();
  for (;;) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await page.waitForSelector(readySel, { timeout: 30_000 });
      return;
    } catch (e) {
      if (Date.now() - t0 > T.connectRetry) throw e;
      log('  retrying goto:', e.message.split('\n')[0]); await sleep(1500);
    }
  }
}

// ---- preroll (trimmed away in assembly) ----
await gotoReady(`${BASE}/reports/${REPORT}`, '[data-testid="scorer-kpis"]');     // warm the report route in-browser
await gotoReady(`${BASE}/sessions`, '.session-row');
await page.mouse.move(W / 2, 300);
await sleep(800);
await sleep(1200);
await page.evaluate(() => { try { sessionStorage.removeItem('__mk'); } catch {} });
marks.start = now();   // == p3a (no marker step of its own)

// ¶3
await vo('p3a', async () => {           // Sessions page: every session the hooks captured.
  const rows = page.locator('.session-row');
  const n = Math.min(await rows.count(), 6);
  await glideTo(rows.nth(0), 14);
  for (let i = 1; i < n; i++) { await glideTo(rows.nth(i), 6); await sleep(120); }
});
await vo('p3b', async () => {           // The daily report: their KPIs + ours.
  await page.goto(`${BASE}/reports/${REPORT}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="scorer-kpis"]', { timeout: 20_000 });
  await page.mouse.move(W / 2, 260, { steps: 4 });
  await sleep(500);
  await glideTo(page.locator('.metric-ribbon .visual-metric').first(), 10);
  await sleep(400);
  // Scroll so the Scores block's bottom sits at the viewport bottom: their ribbon above, our tiles below.
  await page.evaluate((h) => {
    const r = document.querySelector('[data-testid="scorer-kpis"]').getBoundingClientRect();
    window.scrollTo({ top: Math.max(0, r.bottom + window.scrollY - h + 24), behavior: 'smooth' });
  }, H);
  await sleep(700);
  await glideTo(page.locator('[data-testid="scorer-kpis"] .panel-heading h2'), 10);
});
await vo('p3c', async () => {           // Verification, corrections (uncalibrated), blocked minutes.
  // Split the hold ≈ 40% verification · 40% corrections · 20% blocked minutes.
  const len = (VO.p3c ?? VO_DEFAULT) * 1000, t0 = Date.now();
  const until = async (ms) => { const d = ms - (Date.now() - t0); if (d > 0) await sleep(d); };
  await glideTo(page.locator('[data-testid="scorer-kpis"] [data-testid="scorer-tile-V"] strong'), 10);
  await until(len * 0.4);
  const c = page.locator('[data-testid="scorer-kpis"] [data-testid="scorer-tile-C"]');
  await glideTo(c.locator('strong'), 10);
  const chip = c.getByText(/uncalibrated/i).first();
  if (await chip.count()) { await sleep(500); await glideTo(chip, 8); } else log('  no uncalibrated chip found');
  await until(len * 0.8);
  await glideTo(page.locator('[data-testid="scorer-kpis"] [data-testid="scorer-tile-B_min"] strong'), 10);
});
await vo('p3d', async () => {           // Leverage: skills/tools you vs agent, subagents, cost per model.
  const LEV = '[data-testid="scorer-leverage"]';
  await page.waitForSelector(LEV, { timeout: 5000 });
  await scrollToSel(LEV, 120);
  await sleep(600);
  const tiles = page.locator(`${LEV} .scores-tile`);
  const want = ['tooling_skill', 'tooling_mcp', 'subagents', 'cost'];
  const len = (VO.p3d ?? VO_DEFAULT) * 1000 - 600, t0 = Date.now();
  for (let i = 0; i < want.length; i++) {
    let tl = page.locator(`${LEV} [data-testid="scorer-tile-${want[i]}"]`).filter({ visible: true });
    if (!(await tl.count())) tl = tiles.nth(i);
    await glideTo(tl.locator('strong'), 10).catch(() => glideTo(tl, 10));
    const d = (len * (i + 1)) / want.length - (Date.now() - t0); if (d > 0) await sleep(d);
  }
});
await vo('p3e', async () => {           // Click a metric → evidence inspector lands on the exact turn.
  await scrollToSel('[data-testid="scorer-kpis"]', 120); await sleep(450);
  if (!P3E) throw new Error('no demo-session turn link on any Scores tile');
  const tile = page.locator(`[data-testid="scorer-kpis"] [data-testid="scorer-tile-${P3E.key}"]`);
  const link = tile.locator('.evidence-links button').nth(P3E.index);
  await glideClick(link);
  await page.waitForSelector('[data-testid="evidence-inspector"][open] .evidence-event', { timeout: 8000 });
  const h2 = await page.locator('[data-testid="evidence-inspector"] h2').textContent();
  if (PRIV.titles.includes(h2.trim())) throw new Error(`inspector opened a private session: ${h2.slice(0, 20)}`);
  await page.mouse.move(W / 2, H / 2, { steps: 8 });
  await sleep(1500);   // let the inspector sit on screen before the next beat closes it
});

// ¶4
await vo('p4a', async () => {           // Ask the analyst.
  await closeInspector();
  // >=1280px wide the analyst is a sticky right sidebar; below that it is a drawer behind [data-testid=analyst-toggle].
  const toggle = page.locator('[data-testid="analyst-toggle"]');
  if (await toggle.isVisible()) await glideClick(toggle);
  const input = page.locator('[data-testid="analyst-input"]');
  await input.waitFor({ state: 'visible', timeout: 8000 });
  await page.waitForFunction(() => !document.querySelector('[data-testid="analyst-input"]')?.disabled, null, { timeout: 15_000 });
  await glideTo(page.locator('[data-testid="analyst-panel-root"] .analyst-head'), 10);
  await sleep(500);
  await glideClick(page.locator('[data-testid="analyst-input"]'));
  await page.keyboard.type(QUESTION, { delay: T.typeDelay });
  await sleep(300);
  await glideClick(page.locator('[data-testid="analyst-panel-root"] button[type="submit"]'));
});
let streaming = false;
const followAnswer = setInterval(() => page.evaluate(() => {
  const l = document.querySelector('.analyst-log'); if (l) l.scrollTop = l.scrollHeight;
}).catch(() => {}), 400);
await vo('p4b', async () => {           // Streaming: tool steps, then the answer.
  streaming = await page.waitForSelector('[data-testid="analyst-answer"] :is(.analyst-step, p:not(.analyst-wait):not(.analyst-error))', { timeout: 15_000 })
    .then(() => true, () => false);
  if (!streaming) throw new Error('analyst streaming never started (panel unwired or API error)');
});
await mark('streamWait');
if (streaming) try {
  await page.waitForFunction(() => {
    const input = document.querySelector('[data-testid="analyst-input"]');
    const a = [...document.querySelectorAll('[data-testid="analyst-answer"]')].pop();
    return input && !input.disabled && a && a.querySelector('p:not(.analyst-wait):not(.analyst-error)');
  }, null, { timeout: T.chatTimeout });
  log('  answer received');
  if (await page.locator('[data-testid="analyst-answer"] .analyst-error').count()) failures.push({ beat: 'p4b', error: 'analyst returned an error' });
} catch { failures.push({ beat: 'p4b', error: 'answer did not finish in time' }); }
await mark('answered');
clearInterval(followAnswer);
await page.evaluate(() => { const l = document.querySelector('.analyst-log'); if (l) l.scrollTo({ top: l.scrollHeight, behavior: 'smooth' }); });
await sleep(800);
await vo('p4c', async () => {           // Click a citation → evidence inspector.
  const cite = page.locator('[data-testid="analyst-answer"] .analyst-cite');
  if (!(await cite.count())) throw new Error('no citation in the answer');
  // Only cite a demo-session turn (or a metric definition); never open a private session's evidence.
  const ids = await cite.evaluateAll((els) => els.map((e) => e.getAttribute('title') || ''));
  let k = ids.findIndex(isDemoEvidence);
  if (k < 0) { k = ids.findIndex((id) => id.startsWith('metric:')); log('  p4c: no demo-turn citation, using a metric citation'); }
  if (k < 0) throw new Error('no demo-session or metric citation in the answer');
  log('  p4c cite:', ids[k]);
  await glideClick(cite.nth(k));
  await page.waitForSelector('[data-testid="evidence-inspector"][open] .evidence-event', { timeout: 8000 });
  await page.mouse.move(W / 2, H / 2, { steps: 15 });
  await sleep(1200);
});
await mark('end');
await sleep(3500);   // tail: the recorder lags the clock; let the end marker reach the video

// ---- finalize video ----
const videoPath = await page.video().path();
await context.close();
await browser.close();
const webm = join(OUT, 'left.webm');
rmSync(webm, { force: true });
renameSync(videoPath, webm);
rmSync(VIDEO_TMP, { recursive: true, force: true });
// Locate every mark in the video: frame where the corner dot first shows that mark's gray level (in order).
{
  const FPS = 50;
  const rr = spawnSync('ffmpeg', ['-loglevel', 'error', '-i', webm, '-vf', `fps=${FPS},crop=2:2:1:${H - 3},format=gray`, '-f', 'rawvideo', '-'], { maxBuffer: 1 << 26 });
  const b = rr.stdout || Buffer.alloc(0); const n = Math.floor(b.length / 4);
  const lum = (f) => (b[f * 4] + b[f * 4 + 1] + b[f * 4 + 2] + b[f * 4 + 3]) / 4;
  let f = 0, lastOff = 0, found = 0;
  for (let k = 0; k < markOrder.length; k++) {
    const id = markOrder[k], want = LEVELS[k % LEVELS.length];
    // earliest frame at/after the previous hit where the dot is within 25 levels of the wanted gray and stays for 3 frames
    let hit = -1;
    for (let g = f; g + 2 < n; g++) {
      if ([0, 1, 2].every((d) => Math.abs(lum(g + d) - want) < 25)) { hit = g; break; }
    }
    if (hit >= 0) { lastOff = hit / FPS - marks[id]; marks[id] = hit / FPS; f = hit + 1; found++; }
    else { marks[id] += lastOff; log(`  marker for ${id} not found; clock + ${lastOff.toFixed(2)} s`); failures.push({ beat: id, error: 'marker not found in video' }); }
  }
  log(`marks aligned to video frames: ${found}/${markOrder.length} (final lag ${lastOff.toFixed(2)} s)`);
  marks.start = marks.p3a;
}
const ends = {};
for (let k = 0; k < markOrder.length - 1; k++) if (/^p\d/.test(markOrder[k])) ends[markOrder[k]] = marks[markOrder[k + 1]];
for (const k of Object.keys(marks)) marks[k] = Math.round(marks[k] * 1000) / 1000;
writeFileSync(join(OUT, 'left.marks.json'), JSON.stringify(marks, null, 2));
for (const [id, t] of Object.entries(ends)) {
  const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', Math.max(0, t - 0.15).toFixed(2), '-i', webm, '-frames:v', '1', join(STILLS, `hd-${id}.png`)]);
  if (r.status !== 0) log(`  still ${id} failed: ${String(r.stderr).split('\n')[0]}`);
}
log('video:', webm, 'marks:', JSON.stringify(marks));
if (failures.length) { log('beats with problems:'); for (const f of failures) log(`  ${f.beat}: ${f.error}`); }
else log('all beats ok');
process.exit(failures.length ? 1 : 0);
