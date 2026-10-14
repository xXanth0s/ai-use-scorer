// Build live/out/hdyai-live-demo.mp4 from live/out/raw.webm + live/out/beats.json (written by record.mjs).
// Captions and title cards are rendered to PNG with Playwright (Homebrew ffmpeg has no drawtext/libass) and
// overlaid per beat; stream waits play at the beat's speed. No audio track.
import { chromium } from 'playwright';
import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const SEG = join(OUT, 'seg');
const PNG = join(OUT, 'png');
const W = 1440, H = 900, FPS = 30;
const URL = 'hdyai-scorer-demo.vercel.app';
const { beats } = JSON.parse(readFileSync(join(OUT, 'beats.json'), 'utf8'));

const run = (args, label) => {
  const r = spawnSync('ffmpeg', ['-nostdin', '-y', '-loglevel', 'error', ...args], { stdio: 'inherit' });
  if (r.status !== 0) { console.error(`ffmpeg failed: ${label}`); process.exit(1); }
};
const ENC = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-an'];

rmSync(SEG, { recursive: true, force: true }); mkdirSync(SEG, { recursive: true }); mkdirSync(PNG, { recursive: true });

// ---- PNGs: title, end card, one caption per distinct text ----
const BASE_CSS = `*{box-sizing:border-box}html,body{margin:0;width:${W}px;height:${H}px;overflow:hidden;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}`;
const card = (title, sub, foot) => `<style>${BASE_CSS}body{background:#f6f7fb;display:flex;align-items:center;justify-content:center}
.c{background:#fff;border:1px solid #e3e7ef;border-radius:22px;padding:64px 80px;width:1100px;box-shadow:0 20px 60px rgba(15,23,42,.08)}
.k{display:flex;gap:10px;align-items:center;color:#2f5bea;font-weight:700;letter-spacing:.14em;font-size:15px;text-transform:uppercase}
.k i{width:9px;height:9px;border-radius:50%;background:#2f5bea}h1{font-size:54px;line-height:1.12;margin:22px 0 18px;color:#0f172a;letter-spacing:-.02em}
p{font-size:26px;color:#475569;margin:0}.f{margin-top:40px;font-size:20px;color:#2f5bea;font-weight:600}</style>
<div class="c"><div class="k"><i></i>how do you ai × scorer</div><h1>${title}</h1><p>${sub}</p>${foot ? `<div class="f">${foot}</div>` : ''}</div>`;
// Caption: lower third over the report column (the analyst sidebar on the right stays uncovered).
const caption = (text, right) => `<style>${BASE_CSS}body{background:transparent}
.b{position:absolute;left:${right ? 'auto' : '36px'};right:${right ? '400px' : 'auto'};bottom:34px;max-width:${right ? 640 : 960}px;background:#fff;border:1px solid #dfe5f2;
border-left:6px solid #2f5bea;border-radius:14px;padding:18px 26px;box-shadow:0 14px 40px rgba(15,23,42,.18);color:#0f172a;font-size:30px;font-weight:650;line-height:1.25;letter-spacing:-.01em}</style>
<div class="b">${text}</div>`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
async function shot(html, file, transparent) {
  await page.setContent(html, { waitUntil: 'load' });
  await page.screenshot({ path: file, omitBackground: !!transparent });
}
await shot(card('How you work with coding agents', 'One day of Claude Code + Codex sessions: scored, cited, and explained by an analyst you can ask.'), join(PNG, 'title.png'));
await shot(card(URL, 'Synthetic demo data · real use: the plugin on your machine', 'Scores · Leverage &amp; diagnostics · Ask the analyst'), join(PNG, 'end.png'));
const capFile = new Map();
for (const b of beats) {
  if (capFile.has(b.caption)) continue;
  const f = join(PNG, `cap-${capFile.size}.png`);
  await shot(caption(b.caption), f, true);
  capFile.set(b.caption, f);
}
await browser.close();

// ---- segments ----
const segs = [];
const still = (png, dur, name) => {
  const f = join(SEG, `${String(segs.length).padStart(2, '0')}-${name}.mp4`);
  run(['-loop', '1', '-t', String(dur), '-i', png, '-vf', `scale=${W}:${H},fps=${FPS},format=yuv420p,fade=in:0:8,fade=out:st=${dur - 0.3}:d=0.3`, ...ENC, f], name);
  segs.push(f);
};
const clip = (b) => {
  const f = join(SEG, `${String(segs.length).padStart(2, '0')}-${b.id}.mp4`);
  const dur = b.end - b.start;
  const vf = `[0:v]trim=start=${b.start.toFixed(3)}:duration=${dur.toFixed(3)},setpts=(PTS-STARTPTS)/${b.speed},scale=${W}:${H},fps=${FPS}[v];[v][1:v]overlay=0:0,format=yuv420p[o]`;
  run(['-i', join(OUT, 'raw.webm'), '-loop', '1', '-i', capFile.get(b.caption), '-filter_complex', vf, '-map', '[o]', '-t', (dur / b.speed).toFixed(3), ...ENC, f], b.id);
  segs.push(f);
};
still(join(PNG, 'title.png'), 3, 'title');
for (const b of beats) clip(b);
still(join(PNG, 'end.png'), 3, 'end');

const list = join(SEG, 'list.txt');
writeFileSync(list, segs.map((f) => `file '${f}'`).join('\n'));
const mp4 = join(OUT, 'hdyai-live-demo.mp4');
run(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', mp4], 'concat');
run(['-ss', '20', '-i', mp4, '-frames:v', '1', '-vf', 'scale=640:-2', join(OUT, 'poster.png')], 'poster');
console.log('wrote', mp4);
