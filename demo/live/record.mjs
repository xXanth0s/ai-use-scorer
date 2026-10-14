// Silent, text-only walkthrough of the public demo (https://hdyai-scorer-demo.vercel.app), analyst sidebar as the star.
// Usage: node live/record.mjs   → live/out/raw.webm + live/out/beats.json ({id, caption, start, end, speed} in seconds)
// The analyst endpoint is capped (6 questions/conversation, 20 requests/hour/IP): this script sends 3 questions and
// retries a failed one at most once.
import { chromium } from 'playwright';
import { mkdirSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const TMP = join(OUT, '_video');
const BASE = process.env.DEMO_URL || 'https://hdyai-scorer-demo.vercel.app';
const REPORT = '/reports/scorer-demo-2026-10-14';
const CANDIDATE = '/reports/scorer-attempt-cand-b-2026-10-13';
const W = 1440, H = 900;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// fake cursor + click ring (same approach as ../record.hdyai.mjs, blue accent)
const CURSOR = `(() => {
  if (window.__demoCursor) return; window.__demoCursor = true;
  const css = '#__cursor{position:fixed;left:0;top:0;width:22px;height:30px;z-index:2147483647;pointer-events:none;transition:transform .06s linear;transform:translate(-9999px,-9999px);filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}'
    + '.__ring{position:fixed;z-index:2147483646;pointer-events:none;border:3px solid #2f5bea;border-radius:9px;box-shadow:0 0 0 5px rgba(47,91,234,.2);animation:__ring .8s ease-out forwards}'
    + '@keyframes __ring{0%{opacity:1;transform:scale(1)}100%{opacity:0;transform:scale(1.08)}} nextjs-portal{display:none!important}';
  const host = () => document.querySelector('dialog[open]') || document.body;
  const install = () => {
    const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
    const c = document.createElement('div'); c.id = '__cursor';
    c.innerHTML = '<svg viewBox="0 0 22 30" width="22" height="30"><path d="M2 2 L2 23 L7.5 18 L11 27 L15 25.5 L11.5 17 L19 17 Z" fill="#0f172a" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    const rehost = () => { const h = host(); if (c.parentNode !== h) h.appendChild(c); };
    new MutationObserver(rehost).observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ['open'] });
    document.addEventListener('mousemove', (e) => { rehost(); c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)'; }, true);
    document.addEventListener('mousedown', (e) => {
      const t = e.target instanceof Element ? e.target.closest('button,a,input') || e.target : null; if (!t) return;
      const r = t.getBoundingClientRect(); const ring = document.createElement('div'); ring.className = '__ring';
      Object.assign(ring.style, { left: (r.left - 5) + 'px', top: (r.top - 5) + 'px', width: (r.width + 10) + 'px', height: (r.height + 10) + 'px' });
      host().appendChild(ring); setTimeout(() => ring.remove(), 850);
    }, true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();`;

rmSync(TMP, { recursive: true, force: true }); mkdirSync(TMP, { recursive: true });
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, colorScheme: 'light',
  recordVideo: { dir: TMP, size: { width: W, height: H } } });
await ctx.addInitScript(CURSOR);
const page = await ctx.newPage();
const t0 = Date.now();
const now = () => (Date.now() - t0) / 1000;
const beats = [];
let open = null;
const beat = (id, caption, speed = 1) => { end(); open = { id, caption, start: now(), speed }; log('beat', id); };
const end = () => { if (open) { open.end = now(); beats.push(open); open = null; } };
let mouse = { x: 700, y: 450 };

async function moveTo(loc) {
  await loc.scrollIntoViewIfNeeded();
  const b = await loc.boundingBox();
  const x = b.x + b.width / 2, y = b.y + b.height / 2;
  await page.mouse.move(x, y, { steps: 22 }); mouse = { x, y };
}
async function click(loc) { await moveTo(loc); await sleep(250); await loc.click(); }
async function smoothScrollTo(selector, offset = 90, ms = 1300) {
  await page.evaluate(async ({ selector, offset, ms }) => {
    const el = document.querySelector(selector); if (!el) return;
    const from = scrollY, to = Math.max(0, el.getBoundingClientRect().top + scrollY - offset), t = performance.now();
    await new Promise((done) => { const step = (n) => { const k = Math.min(1, (n - t) / ms), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      scrollTo(0, from + (to - from) * e); k < 1 ? requestAnimationFrame(step) : done(); }; requestAnimationFrame(step); });
  }, { selector, offset, ms });
}
async function closeInspector() {
  const x = page.locator('[data-testid=evidence-inspector] button[aria-label="Close evidence"]');
  await click(x); await page.waitForSelector('[data-testid=evidence-inspector]', { state: 'detached', timeout: 5000 }).catch(() => {});
}

const panel = page.locator('[data-testid=analyst-panel-root]');
const logText = () => panel.locator('.analyst-log').innerText();
let requests = 0;
// Send one question (chip or typed), wait until the streamed answer stops changing. Returns the answer text or null.
async function ask({ chip, text }, attempt = 0) {
  if (process.env.SKIP_CHAT) { await sleep(1500); return 'skipped'; }
  const before = await logText();
  requests += 1;
  if (chip) await click(panel.locator('.analyst-quick button', { hasText: chip }));
  else {
    const input = page.locator('[data-testid=analyst-input]');
    await click(input);
    await input.pressSequentially(text, { delay: 28 });
    await sleep(300);
    await click(panel.locator('button[aria-label="Send"]'));
  }
  const tStart = Date.now(); let last = '', stableSince = Date.now();
  while (Date.now() - tStart < 90_000) {
    await sleep(400);
    const cur = await logText();
    if (cur !== last) { last = cur; stableSince = Date.now(); }
    const busy = await panel.locator('input[data-testid=analyst-input]').isDisabled().catch(() => false);
    if (cur !== before && !busy && Date.now() - stableSince > 2500) break;
  }
  const added = last.slice(before.length);
  if (/\b(429|503)\b|rate limit|too many requests|not configured/i.test(added.slice(-300)) && attempt === 0 && requests < 4) {
    log('analyst error, retrying once in 20 s:', added.slice(-120).replace(/\s+/g, ' '));
    await sleep(20_000);
    return ask({ chip: null, text: text || chip }, 1);
  }
  return /\b(429|503|rate limit|too many)\b/i.test(added.slice(-300)) ? null : added;
}

// ---- flow ----
await page.goto(BASE + REPORT, { waitUntil: 'load' });
await page.waitForSelector('[data-testid=scorer-tile-V]', { timeout: 60_000 });
await page.mouse.move(mouse.x, mouse.y);
await sleep(800);

beat('overview', 'One day of Claude Code + Codex sessions, measured');
await page.mouse.move(520, 300, { steps: 20 });
await sleep(2600);

beat('scores', 'Every number links to the turn it came from');
await smoothScrollTo('[data-testid=scorer-kpis]');
await sleep(700);
await click(page.locator('[data-testid=scorer-tile-V] button', { hasText: 'Turn 1' }));
await page.waitForSelector('[data-testid=evidence-inspector]');
await sleep(2600);
await closeInspector();

beat('leverage', 'Leverage & diagnostics: tools by who chose them, parallel work, cost per model');
await smoothScrollTo('[data-testid=scorer-leverage]');
await page.mouse.move(400, 420, { steps: 20 });
await sleep(2800);

beat('skills', 'Skill profile, each rating backed by evidence');
await smoothScrollTo('[data-testid=report-tab-skills]', 140, 1000);
await click(page.locator('[data-testid=report-tab-skills]'));
await sleep(2400);
await click(page.locator('[data-testid=report-tab-overview]'));
await smoothScrollTo('body', 0, 900);

const chats = [];
beat('chat1-ask', "Ask the analyst: it queries the day's metrics and evidence");
await sleep(600);
beat('chat1-stream', "Ask the analyst: it queries the day's metrics and evidence", 2);
const a1 = await ask({ chip: 'What did I do well?' });
chats.push({ q: 'What did I do well?', a: a1 });
beat('chat1-read', 'Every claim cites a turn — click to open it');
await sleep(2200);
const cite = panel.locator('.analyst-cite').first();
if (a1 && await cite.count()) {
  await click(cite);
  await page.waitForSelector('[data-testid=evidence-inspector]', { timeout: 8000 }).catch(() => {});
  await sleep(2800);
  await closeInspector();
}

const q2 = 'Where did I skip verification, and what should I ask for next time?';
beat('chat2-ask', 'Concrete next step from your own sessions');
const a2p = ask({ text: q2 });
await sleep(2600);       // typing happens inside ask(); stream part is sped up below
beat('chat2-stream', 'Concrete next step from your own sessions', 2);
const a2 = await a2p;
chats.push({ q: q2, a: a2 });
beat('chat2-read', 'Concrete next step from your own sessions');
await sleep(3800);

const q3 = 'Which corrections could I have prevented?';
beat('chat3-ask', 'Which corrections were preventable, with the turns that show it');
const a3p = ask({ text: q3 });
await sleep(1800);
beat('chat3-stream', 'Which corrections were preventable, with the turns that show it', 2);
const a3 = await a3p;
chats.push({ q: q3, a: a3 });
beat('chat3-read', 'Which corrections were preventable, with the turns that show it');
await sleep(3800);

beat('candidate', "Same lens for a candidate's take-home attempt");
await page.goto(BASE + CANDIDATE, { waitUntil: 'load' });
await page.waitForSelector('[data-testid=scorer-tile-V]', { timeout: 60_000 });
await page.mouse.move(520, 320, { steps: 20 });
await sleep(3200);
end();

const video = page.video();
await ctx.close(); await browser.close();
const src = await video.path();
renameSync(src, join(OUT, 'raw.webm'));
rmSync(TMP, { recursive: true, force: true });
writeFileSync(join(OUT, 'beats.json'), JSON.stringify({ beats, chats, requests }, null, 1));
log('done; requests', requests, '; beats', beats.length);
for (const c of chats) log('Q:', c.q.slice(0, 80), '| A:', (c.a || 'NONE').replace(/\s+/g, ' ').slice(0, 80));
