// Scripted, recordable click-through of the scorer dashboard.
// Usage: node record.mjs            (server must be running at BASE)
// Output: out/walkthrough.webm (+ .mp4 if ffmpeg exists), out/stills/NN-<beat>.png
import { chromium } from 'playwright';
import { spawnSync } from 'node:child_process';
import { mkdirSync, renameSync, rmSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const STILLS = join(OUT, 'stills');
const VIDEO_TMP = join(OUT, '_video');

const BASE = process.env.SCORER_URL || 'http://127.0.0.1:8811';
const DAY = process.env.SCORER_DAY || '2026-10-14';
const URL = `${BASE}/?day=${DAY}&theme=light#/today`;
const QUESTION = 'Where did I lose the most time today?';

// ---- timings (ms). Narration-driven: every sub-beat holds at least its VO clip (out/vo/durations.json) + GAP. ----
const VO = JSON.parse(readFileSync(join(OUT, 'vo', 'durations.json'), 'utf8'));
const GAP = 450;               // silence after each VO line
const T = {
  slowMo: 0,
  typeDelay: 55,
  settle: 500,
  connectRetry: 60_000,
  chatTimeout: 90_000,
  trendsHold: 3000,            // silent Trends hold (supports "better week over week")
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---- wait for the server (it may restart while another agent edits src/) ----
async function waitForServer() {
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < T.connectRetry) {
    try {
      const r = await fetch(`${BASE}/api/days`);
      if (r.ok) return;
      last = `HTTP ${r.status}`;
    } catch (e) { last = e.message; }
    await sleep(1000);
  }
  throw new Error(`server at ${BASE} not reachable after ${T.connectRetry / 1000} s (${last})`);
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
  \`;
  const install = () => {
    if (document.getElementById('__cursor')) return;
    const style = document.createElement('style'); style.textContent = css; document.head.appendChild(style);
    const c = document.createElement('div'); c.id = '__cursor';
    c.innerHTML = '<svg viewBox="0 0 22 30" width="22" height="30"><path d="M2 2 L2 23 L7.5 18 L11 27 L15 25.5 L11.5 17 L19 17 Z" fill="#1c2d22" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    document.addEventListener('mousemove', (e) => { c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)'; }, true);
    document.addEventListener('mousedown', (e) => {
      c.classList.add('down');
      const t = e.target instanceof Element ? e.target : null;
      if (!t) return;
      const r = t.getBoundingClientRect();
      const ring = document.createElement('div'); ring.className = '__ring';
      ring.style.left = (r.left - 5) + 'px'; ring.style.top = (r.top - 5) + 'px';
      ring.style.width = (r.width + 10) + 'px'; ring.style.height = (r.height + 10) + 'px';
      document.body.appendChild(ring); setTimeout(() => ring.remove(), 750);
    }, true);
    document.addEventListener('mouseup', () => c.classList.remove('down'), true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();`;

// ---- main ----
mkdirSync(STILLS, { recursive: true });
rmSync(VIDEO_TMP, { recursive: true, force: true });
mkdirSync(VIDEO_TMP, { recursive: true });

log('waiting for server', BASE);
await waitForServer();

const browser = await chromium.launch({ headless: true, slowMo: T.slowMo });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 2,
  colorScheme: 'light',
  recordVideo: { dir: VIDEO_TMP, size: { width: 1920, height: 1080 } },
});
await context.addInitScript(CURSOR_SCRIPT);
const page = await context.newPage();
const T0 = Date.now();                       // video t=0 (recording starts with the page)
const now = () => (Date.now() - T0) / 1000;

const failures = [];
const marks = {};                            // id -> seconds into walkthrough.webm
// One narrated sub-beat: mark the start, run the action, hold until the VO line has finished.
async function vo(id, fn) {
  const t = now(); marks[id] = t; log(`${id} @ ${t.toFixed(2)}s`);
  try { await fn(); } catch (e) { failures.push({ beat: id, error: e.message.split('\n')[0] }); log(`  FAILED: ${e.message.split('\n')[0]}`); }
  const need = ((VO[id] ?? (id === 'trends' ? T.trendsHold / 1000 : 2)) * 1000) + GAP - (now() - t) * 1000;
  if (need > 0) await sleep(need);
  await page.screenshot({ path: join(STILLS, `${id}.png`) }).catch(() => {});
}

async function glideTo(locator, steps = 30) {
  const box = await locator.filter({ visible: true }).first().boundingBox({ timeout: 5000 });
  if (!box) throw new Error('element has no bounding box');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps });
}
async function glideClick(locator) { const l = locator.filter({ visible: true }).first(); await glideTo(l); await sleep(350); await l.click(); }
const scrollMain = (top) => page.evaluate((y) => document.getElementById('maincol').scrollTo({ top: y, behavior: 'smooth' }), top);
const scrollToSel = (sel, off = 64) => page.evaluate(([s, o]) => {
  const el = document.querySelector(s); const col = document.getElementById('maincol');
  if (el) col.scrollTo({ top: el.getBoundingClientRect().top - col.getBoundingClientRect().top + col.scrollTop - o, behavior: 'smooth' });
}, [sel, off]);

async function goto() {
  const t0 = Date.now();
  for (;;) {
    try {
      await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 15_000 });
      await page.waitForSelector('.band .stat', { timeout: 30_000 });
      return;
    } catch (e) {
      if (Date.now() - t0 > T.connectRetry) throw e;
      log('  retrying goto:', e.message.split('\n')[0]);
      await sleep(1500);
    }
  }
}
async function closeAskIfOpen() {
  if ((await page.getAttribute('#askbtn', 'aria-pressed')) === 'true') { await page.click('#askbtn'); await sleep(450); }
}
async function switchTab(tab) {
  await glideClick(page.locator(`.tabs a[data-tab="${tab}"]`));
  await page.waitForSelector(`.tabs a[data-tab="${tab}"][aria-current="page"]`, { timeout: 10_000 });
  await sleep(T.settle);
}

// ---- preroll (trimmed away in assembly) ----
await goto();
await page.mouse.move(960, 300);
await closeAskIfOpen();
await page.waitForSelector('#narrative h1, #narrative .narr', { timeout: 20_000 });
await page.waitForFunction(() => document.querySelectorAll('.band .stat').length === 4, null, { timeout: 15_000 });
await page.waitForSelector('#timeline svg rect.bar[data-turn]', { timeout: 20_000 });
await sleep(1500);
marks.start = now();

// ¶3
await vo('p3a', async () => {           // This is today. Each row is a worktree, each block a turn.
  await scrollToSel('.timeline-panel, #timeline', 140);
  await sleep(900);
  const tl = await page.locator('#timeline').boundingBox();
  await page.mouse.move(tl.x + 40, tl.y + tl.height * 0.55, { steps: 20 });
  await page.mouse.move(tl.x + tl.width - 40, tl.y + tl.height * 0.55, { steps: 70 });
});
await vo('p3b', async () => {           // Hover and the analyst summarizes what happened.
  const bars = page.locator('#timeline svg rect.bar[data-turn]');
  const n = await bars.count();
  let best = bars.first(), bestW = 0;
  for (let i = 0; i < Math.min(n, 80); i++) {
    const b = await bars.nth(i).boundingBox();
    if (b && b.width > bestW) { bestW = b.width; best = bars.nth(i); }
  }
  await glideTo(best);
  await page.waitForSelector('#pop.show', { timeout: 5000 }).catch(() => log('  popover did not show'));
});
await vo('p3c', async () => {           // The verdict is one line ...
  await page.mouse.move(1700, 700, { steps: 10 });
  await scrollMain(0); await sleep(700);
  await glideTo(page.locator('#narrative li, #narrative h1, #narrative .narr').first());
});
await vo('p3d', async () => {           // Verification: ...
  await glideTo(page.locator('.band .stat').nth(0));
});
await vo('p3e', async () => {           // Corrections: ... uncalibrated chip
  await glideTo(page.locator('.band .stat').nth(2));
  await sleep(1800);
  const chip = page.locator('.band .stat .chip-uncal').first();
  if (await chip.count()) await glideTo(chip);
});
await vo('p3f', async () => {           // Click a card and you land on the turn.
  let link = page.locator('.band .stat').nth(2).locator('[data-turn]').filter({ visible: true });
  if (!(await link.count())) {           // no turn links in the card: use the 'Corrections by cause' list
    await scrollToSel('#h-cor', 200); await sleep(800);
    link = page.locator('section:has(#h-cor) [data-turn]').filter({ visible: true });
  }
  await glideClick(link);
  await sleep(600);
  await scrollToSel('#turn-detail', 220);
});
await vo('trends', async () => {        // silent: 30-day trends
  await switchTab('trends');
  await page.waitForSelector('.multiples', { timeout: 15_000 });
  const b30 = page.locator('.seg button[data-range="30"]');
  if (await b30.count()) await glideClick(b30);
});
await vo('p3g', async () => {           // Leverage ... cost per model
  await switchTab('leverage');
  await page.waitForSelector('#skillbars', { timeout: 15_000 });
  await sleep(2500);
  const cost = page.locator('#maincol :is(h2,h3,th)', { hasText: /model|^cost$/i }).filter({ visible: true }).last();
  if (await cost.count()) { await cost.scrollIntoViewIfNeeded(); await sleep(400); await glideTo(cost); }
});

// ¶4
await vo('p4a', async () => {           // Ask AI answers from the database. I ask ...
  await switchTab('today');
  await page.evaluate(() => document.getElementById('maincol').scrollTo({ top: 0 }));
  if ((await page.getAttribute('#askbtn', 'aria-pressed')) !== 'true') await glideClick(page.locator('#askbtn'));
  await page.waitForSelector('#ask.open', { timeout: 5000 });
  await sleep(400);
  await glideClick(page.locator('#input'));
  await page.keyboard.type(QUESTION, { delay: T.typeDelay });
  await sleep(300);
  await glideClick(page.locator('#send.ready'));
});
await vo('p4b', async () => {           // It plans, runs SQL, comes back ... (streams while VO plays)
  await page.waitForSelector('#send.stop', { timeout: 8000 }).catch(() => log('  streaming never started'));
});
marks.streamWait = now();
try {
  await page.waitForFunction(() => !document.querySelector('#send.stop') && document.querySelector('.msg.assistant .md'), null, { timeout: T.chatTimeout });
  log('  answer received');
} catch { failures.push({ beat: 'p4b', error: 'answer did not finish in time' }); }
marks.answered = now();
await sleep(800);
await vo('p4c', async () => {           // Peek shows what it looked at.
  const src = page.locator('.msg.assistant [data-peek], .msg.assistant .cite:not(.bad)').first();
  await src.scrollIntoViewIfNeeded().catch(() => {});
  await glideClick(src);
  await page.waitForSelector('#peek.open', { timeout: 5000 }).catch(() => log('  peek did not open'));
  await sleep(1500);
});
marks.end = now();

// ---- finalize video ----
const videoPath = await page.video().path();
await context.close();
await browser.close();
const webm = join(OUT, 'walkthrough.webm');
rmSync(webm, { force: true });
renameSync(videoPath, webm);
rmSync(VIDEO_TMP, { recursive: true, force: true });
writeFileSync(join(OUT, 'marks.json'), JSON.stringify(marks, null, 2));
log('video:', webm, 'marks:', JSON.stringify(marks));
if (failures.length) { log('beats with problems:'); for (const f of failures) log(`  ${f.beat}: ${f.error}`); }
else log('all beats ok');
process.exit(0);
