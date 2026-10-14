// Login beat, local fallback (Ari's app on :3300, local mode).
//   node record.login.mjs            -> out/login-web.webm + out/login-web.marks.json (setup page, then Sessions)
//   node record.login.mjs --badge    -> out/login-badge.png (the "sped up" badge for the terminal segment)
//   node record.login.mjs --sheet IN.mp4 OUT.png  (contact-sheet helper is ffmpeg-only; see login_build.sh)
// Safety: nothing is clicked that changes state. "Connect & start" is never clicked (it is disabled without an
// invitation and would POST to the hosted service). The Sessions page is filtered to the demo sessions via the app's
// own search box; any row without "acme-api" is blurred by an injected style before it can render readable.
import { chromium } from 'playwright';
import { mkdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const BASE = process.env.HDYAI_URL || 'http://127.0.0.1:3300';
const DEMO_MARK = 'acme-api';   // all demo sessions (report scorer-demo-2026-10-14) live in /Users/demo/acme-api
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---- badge PNG (no drawtext in this ffmpeg) ----
if (process.argv.includes('--badge')) {
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })).newPage();
  await p.setContent(`<html><body style="margin:0;background:transparent">
    <div id="b" style="display:inline-flex;align-items:center;gap:12px;padding:12px 22px;border-radius:999px;
      background:#1c2d22;color:#fbf6ee;font:600 26px/1 'JetBrains Mono',ui-monospace,monospace;letter-spacing:.02em">
      <span style="width:12px;height:12px;border-radius:50%;background:#b36813"></span>real run · sped up ${process.env.BADGE_SPEED || '6×'}</div></body></html>`);
  await p.locator('#b').screenshot({ path: join(OUT, 'login-badge.png'), omitBackground: true });
  await b.close(); log('badge:', join(OUT, 'login-badge.png')); process.exit(0);
}

// ---- fake cursor + click ring (copied from record.mjs) ----
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
    nextjs-portal, [data-nextjs-toast], [data-next-badge-root] { display: none !important; }
    /* privacy: only demo sessions may be readable */
    .session-row:not([data-demo="1"]) { filter: blur(10px) !important; }
    .session-row:not([data-demo="1"]) * { color: transparent !important; text-shadow: 0 0 12px rgba(0,0,0,.35) !important; }
    .__hl { outline: 3px solid #b36813 !important; outline-offset: 6px; border-radius: 10px; transition: outline-color .2s; }
  \`;
  const markRows = () => document.querySelectorAll('.session-row').forEach((r) => {
    const ok = (r.textContent || '').includes('${DEMO_MARK}') ? '1' : '0';
    if (r.dataset.demo !== ok) r.dataset.demo = ok;
  });
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
    markRows(); new MutationObserver(markRows).observe(document.body, { childList: true, subtree: true, characterData: true });
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();`;

const VIDEO_TMP = join(OUT, '_video_login');
rmSync(VIDEO_TMP, { recursive: true, force: true });
mkdirSync(VIDEO_TMP, { recursive: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2, colorScheme: 'light',
  recordVideo: { dir: VIDEO_TMP, size: { width: 1920, height: 1080 } },
});
await context.addInitScript(CURSOR_SCRIPT);
const page = await context.newPage();
const T0 = Date.now();
const now = () => +((Date.now() - T0) / 1000).toFixed(2);
const marks = {};

async function glideTo(locator, steps = 22) {
  const box = await locator.filter({ visible: true }).first().boundingBox({ timeout: 8000 });
  if (!box) throw new Error('element has no bounding box');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps });
}

// ---- 1. setup page (local mode: detected agents) ----
await page.goto(`${BASE}/setup`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
await page.waitForSelector('.setup-agents', { timeout: 60_000 });
await page.mouse.move(1500, 160);
await sleep(1200);
marks.setup_start = now();
await sleep(500);
const rows = page.locator('.setup-agents > div');
await glideTo(rows.nth(1));                       // Claude Code · Found on this computer
await page.evaluate(() => document.querySelectorAll('.setup-agents > div')[1]?.classList.add('__hl'));
await sleep(900);
await glideTo(rows.nth(0));                       // Codex · Found on this computer
await page.evaluate(() => { const r = document.querySelectorAll('.setup-agents > div'); r[1]?.classList.remove('__hl'); r[0]?.classList.add('__hl'); });
await sleep(900);
await page.evaluate(() => document.querySelectorAll('.__hl').forEach((e) => e.classList.remove('__hl')));
await glideTo(page.locator('.setup-consent'));    // "...adds capture hooks for Codex and Claude Code"
await sleep(1200);
marks.setup_end = now();
await page.screenshot({ path: join(OUT, 'stills', 'login-setup.png') }).catch(() => {});

// ---- 2. sessions page, filtered to the demo sessions ----
await page.goto(`${BASE}/sessions`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
await page.waitForSelector('.session-row', { timeout: 60_000 });
await page.mouse.move(1500, 160);
await sleep(800);
const search = page.locator('input[placeholder="Search sessions or projects"]');
await search.fill(DEMO_MARK);                     // filter applied before the segment starts
await page.waitForFunction((m) => [...document.querySelectorAll('.session-row')].every((r) => r.textContent.includes(m)), DEMO_MARK, { timeout: 10_000 });
const leak = await page.evaluate((m) => [...document.querySelectorAll('.session-row')].filter((r) => !r.textContent.includes(m)).length, DEMO_MARK);
if (leak) throw new Error(`${leak} non-demo rows visible after filter`);
await sleep(600);
marks.sessions_start = now();
await glideTo(page.locator('.session-row').first(), 26);
await sleep(700);
await glideTo(page.locator('.session-row').nth(3), 20);
await sleep(1600);
marks.sessions_end = now();
await page.screenshot({ path: join(OUT, 'stills', 'login-sessions.png') }).catch(() => {});

await sleep(300);
const videoPath = await page.video().path();
await context.close(); await browser.close();
const webm = join(OUT, 'login-web.webm');
rmSync(webm, { force: true }); renameSync(videoPath, webm);
rmSync(VIDEO_TMP, { recursive: true, force: true });
writeFileSync(join(OUT, 'login-web.marks.json'), JSON.stringify(marks, null, 2));
log('video:', webm, JSON.stringify(marks));
