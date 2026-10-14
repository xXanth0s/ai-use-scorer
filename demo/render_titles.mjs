// 1. Screenshot each title card in titles/*.html to out/titles/<name>.png (1920x1080, 1x so ffmpeg gets exact pixels).
// 2. Render every cue of narration.srt as a transparent 1920x160 PNG strip in out/subs/NN.png, so assemble.sh can
//    burn subtitles with ffmpeg's overlay filter when the build has no libass (Homebrew ffmpeg often does not).
import { chromium } from 'playwright';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, basename } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const TITLES = join(HERE, 'titles');
const OUT = join(HERE, 'out', 'titles');
const SUBS = join(HERE, 'out', 'subs');
mkdirSync(OUT, { recursive: true });
mkdirSync(SUBS, { recursive: true });

const browser = await chromium.launch({ headless: true });

const SUBS_ONLY = process.argv.includes('--subs-only');
// card placeholders: __REPO__ (env REPO_URL) and __KEY__ from meta.json (build-day numbers, filled from `scorer report`)
const META = existsSync(join(HERE, 'meta.json')) ? JSON.parse(readFileSync(join(HERE, 'meta.json'), 'utf8')) : {};
const fill = (html) => html.replace(/__REPO__/g, process.env.REPO_URL || 'ai-use-scorer')
  .replace(/__([A-Z_]+)__/g, (m, k) => (META[k.toLowerCase()] ?? m));

// ---- title cards ----
if (!SUBS_ONLY) {
const files = readdirSync(TITLES).filter((f) => f.endsWith('.html') && !f.startsWith('_')).sort();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1, colorScheme: 'light' });
for (const f of files) {
  const html = fill(readFileSync(join(TITLES, f), 'utf8'));
  if (/__[A-Z_]+__/.test(html)) { console.warn(`skip ${f}: unfilled placeholder (meta.json)`); continue; }
  const tmp = join(TITLES, `_render_${f}`); writeFileSync(tmp, html);
  await page.goto(pathToFileURL(tmp).href, { waitUntil: 'load' });
  rmSync(tmp);
  await page.evaluate(() => document.fonts.ready);
  const png = join(OUT, `${basename(f, '.html')}.png`);
  await page.screenshot({ path: png, clip: { x: 0, y: 0, width: 1920, height: 1080 } });
  console.log('wrote', png);
}
// corner badges (transparent full-frame PNGs, overlaid on sped-up clips / previews)
const badge = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
for (const [name, text] of [['badge-speed', '▶▶ sped up'], ['badge-wait', '▶▶ waiting for the answer · sped up'], ['badge-preview', 'Preview · not shipped']]) {
  await badge.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:transparent;width:1920px;height:1080px}
    .b{position:absolute;top:28px;right:32px;background:#1c2d22;color:#fff;font:600 26px/1 ui-sans-serif,system-ui,-apple-system,Helvetica,Arial,sans-serif;padding:12px 20px;border-radius:9999px}</style></head>
    <body><div class="b">${text}</div></body></html>`);
  await badge.screenshot({ path: join(OUT, `${name}.png`), omitBackground: true });
}
}

// ---- subtitle strips ----
const srt = readFileSync(join(HERE, 'narration.srt'), 'utf8').replace(/\r/g, '');
const toSec = (t) => { const m = t.match(/(\d+):(\d+):(\d+)[,.](\d+)/); return +m[1] * 3600 + +m[2] * 60 + +m[3] + +m[4] / 1000; };
const cues = [];
for (const block of srt.split(/\n{2,}/)) {
  const lines = block.trim().split('\n');
  const ti = lines.findIndex((l) => l.includes('-->'));
  if (ti < 0) continue;
  const [a, b] = lines[ti].split('-->').map((s) => toSec(s.trim()));
  cues.push({ start: a, end: b, text: lines.slice(ti + 1).join('\n') });
}
const sub = await browser.newPage({ viewport: { width: 1920, height: 160 }, deviceScaleFactor: 1 });
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const index = [];
for (const f of readdirSync(SUBS)) if (f.endsWith('.png')) rmSync(join(SUBS, f));
for (let i = 0; i < cues.length; i++) {
  const c = cues[i];
  await sub.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;background:transparent;width:1920px;height:160px;overflow:hidden}
    body{display:flex;align-items:flex-end;justify-content:center;padding-bottom:28px;box-sizing:border-box;
      font:500 34px/1.35 "GT America Standard","GT America",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
    .pill{background:rgba(28,45,34,.82);color:#fff;padding:12px 28px;border-radius:12px;max-width:1500px;text-align:center;white-space:pre-wrap}
  </style></head><body><div class="pill">${esc(c.text)}</div></body></html>`);
  await sub.evaluate(() => document.fonts.ready);
  const png = join(SUBS, `${String(i + 1).padStart(2, '0')}.png`);
  await sub.screenshot({ path: png, omitBackground: true });
  index.push(`${png}\t${c.start}\t${c.end}`);
}
writeFileSync(join(SUBS, 'index.tsv'), index.join('\n') + '\n');
console.log(`wrote ${cues.length} subtitle strips to ${SUBS} (index.tsv: path, start s, end s)`);
await browser.close();
