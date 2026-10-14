// Assemble out/demo.mp4 from: out/vo/*.m4a + durations.json, out/marks.json + walkthrough.webm, out/hooks.mp4,
// out/install.mp4, out/titles/*.png. Writes narration.srt (from narration.txt, timed to the real timeline), renders
// caption strips via render_titles.mjs, then cuts/normalizes segments, mixes VO, overlays captions.
// Homebrew ffmpeg has no drawtext/libass, so every text on screen is a PNG.
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'out');
const SEG = join(OUT, 'seg');
const TITLES = join(OUT, 'titles');
const FPS = 30;
const PAD = 0.6;                 // silence after a VO line before the next segment
const STREAM_MAX = 2.5;          // Ask-AI wait (after the VO line) is sped up to at most this many seconds

const vo = JSON.parse(readFileSync(join(OUT, 'vo', 'durations.json'), 'utf8'));
const marks = JSON.parse(readFileSync(join(OUT, 'marks.json'), 'utf8'));
const lines = Object.fromEntries(readFileSync(join(HERE, 'narration.txt'), 'utf8').split('\n')
  .filter((l) => l && !l.startsWith('#')).map((l) => { const [id, cap] = l.split('|'); return [id, cap]; }));

const run = (args, label) => {
  const r = spawnSync('ffmpeg', ['-nostdin', '-y', '-loglevel', 'error', ...args], { stdio: 'inherit' });
  if (r.status !== 0) { console.error(`ffmpeg failed: ${label}`); process.exit(1); }
};
const probe = (f) => +spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f]).stdout.toString().trim();
const need = (f) => { if (!existsSync(f)) { console.error(`missing ${f}`); process.exit(1); } return f; };
const V = `scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0xfbf6ee,setsar=1,fps=${FPS},format=yuv420p`;
const ENC = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-video_track_timescale', '15360', '-an'];

rmSync(SEG, { recursive: true, force: true }); mkdirSync(SEG, { recursive: true });
const segs = [];           // { file, dur }
const cues = [];           // { id, at }  VO start on the global timeline
let clock = 0;
const addSeg = (file) => { const d = probe(file); segs.push({ file, dur: d }); clock += d; return d; };

// still PNG -> clip (optional slow push-in)
function still(png, dur, name, zoom = false) {
  const f = join(SEG, `${String(segs.length).padStart(2, '0')}-${name}.mp4`);
  const vf = zoom
    ? `scale=3840:2160,zoompan=z='min(1+0.06*on/${Math.round(dur * FPS)},1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=${Math.round(dur * FPS)}:s=1920x1080:fps=${FPS},format=yuv420p`
    : V;
  run(['-loop', '1', '-framerate', String(FPS), '-t', dur.toFixed(3), '-i', need(png), '-vf', vf, '-t', dur.toFixed(3), ...ENC, f], name);
  return f;
}
// video window [a,b) of src, sped up so it lasts `dur` (speed = (b-a)/dur), optional overlay PNG
function clip(src, a, b, dur, name, overlay) {
  const f = join(SEG, `${String(segs.length).padStart(2, '0')}-${name}.mp4`);
  const k = dur / (b - a);
  const base = `[0:v]trim=start=${a.toFixed(3)}:end=${b.toFixed(3)},setpts=(PTS-STARTPTS)*${k.toFixed(5)},${V}`;
  const args = ['-i', need(src)];
  let fc = `${base}[v]`;
  if (overlay) { args.push('-i', need(overlay)); fc = `${base}[b];[b][1:v]overlay=0:0[v]`; }
  run([...args, '-filter_complex', fc, '-map', '[v]', '-t', dur.toFixed(3), ...ENC, f], name);
  return f;
}
const say = (id, offset = 0) => { if (vo[id] == null) { console.error(`no VO for ${id}`); process.exit(1); } cues.push({ id, at: clock + offset }); };

// ---- 1. cold open: one card per line ----
for (const [id, card] of [['p1a', '01-every-day'], ['p1b', '02-measures-agent'], ['p1c', '03-measures-you'], ['p1d', '04-scorer']]) {
  const d = vo[id] + PAD + (id === 'p1d' ? 0.8 : 0) + (id === 'p1a' ? 0.4 : 0);
  const at = id === 'p1a' ? 0.4 : 0;
  say(id, at); addSeg(still(join(TITLES, `${card}.png`), d, card));
}

// ---- 2. hooks firing (vhs, sped up to the VO length) ----
{
  const src = join(OUT, 'hooks.mp4'); const raw = probe(need(src));
  const d = vo.p2 + PAD + 0.3;
  const a = 0.8, b = raw - 1.0;
  say('p2', 0.3);
  addSeg(clip(src, a, b, d, 'hooks', join(TITLES, `badge-speed.png`)));
  console.log(`hooks: ${(b - a).toFixed(1)}s -> ${d.toFixed(1)}s (${((b - a) / d).toFixed(1)}x)`);
}

// ---- 3+4. dashboard walkthrough: real time, except the Ask-AI wait which is compressed ----
{
  const src = join(OUT, 'walkthrough.webm');
  const ids = ['p3a', 'p3b', 'p3c', 'p3d', 'p3e', 'p3f', 'p3g', 'p4a', 'p4b', 'p4c'];
  const s0 = marks.start, w0 = marks.streamWait, w1 = marks.answered, e = marks.end;
  const wait = w1 - w0, waitOut = Math.min(wait, STREAM_MAX);
  const base = clock;
  for (const id of ids) {
    const t = marks[id]; if (t == null) { console.error(`no mark for ${id}`); process.exit(1); }
    const rel = t < w1 ? t - s0 : (w0 - s0) + waitOut + (t - w1);
    cues.push({ id, at: base + rel });
  }
  addSeg(clip(src, s0, w0, w0 - s0, 'walk-a'));
  if (wait > 0.05) addSeg(clip(src, w0, w1, waitOut, 'walk-wait', wait / waitOut > 1.3 ? join(TITLES, 'badge-wait.png') : null));
  addSeg(clip(src, w1, e, e - w1, 'walk-b'));
}

// ---- 5. coming next (card, optional preview still) ----
{
  const d = vo.p5 + PAD + 0.4;
  say('p5', 0.3);
  const prev = join(TITLES, '05b-preview.png');
  if (existsSync(prev)) {
    const d1 = Math.min(d * 0.55, 15);
    addSeg(still(join(TITLES, '05-coming-next.png'), d1, 'coming-next'));
    addSeg(still(prev, d - d1, 'preview', true));
  } else addSeg(still(join(TITLES, '05-coming-next.png'), d, 'coming-next'));
}

// ---- 6. meta twist: build-day dashboard, then the numbers ----
if (vo.p6 != null) {
  const d = vo.p6 + PAD + 0.6;
  say('p6', 0.3);
  const d1 = d * 0.5;
  addSeg(still(need(join(OUT, 'stills', 'p3c.png')), d1, 'buildday', true));
  addSeg(still(join(TITLES, '06-meta.png'), d - d1, 'meta'));
} else console.warn('WARNING: p6 has no VO (unfilled [N]); meta beat skipped');

// ---- 7. close: install typed (not run), end card ----
{
  const src = join(OUT, 'install.mp4'); const raw = probe(need(src));
  const d = Math.min(vo.p7 * 0.6, raw - 0.5);
  say('p7', 0.2);
  addSeg(clip(src, 0.3, 0.3 + d, d, 'install'));
  addSeg(still(join(TITLES, '07-end.png'), Math.max(vo.p7 + 0.2 - d, 0) + 3.0, 'end'));
}
const total = clock;

// ---- narration.srt: each VO line split into sentence-ish chunks, timed by character share ----
const fmt = (s) => { const ms = Math.round(s * 1000); const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, ss = Math.floor(ms / 1000) % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`; };
function chunks(text) {
  const parts = text.match(/[^.:;?!]+[.:;?!]*\s*/g).map((s) => s.trim()).filter(Boolean);
  const out = []; let cur = '';
  for (const p of parts) {
    if (!cur) cur = p; else if ((cur + ' ' + p).length <= 95) cur += ' ' + p; else { out.push(cur); cur = p; }
  }
  if (cur) out.push(cur);
  return out.flatMap((c) => c.length <= 110 ? [c] : c.split(/,\s+/).reduce((a, w, i, arr) => {   // long clause: split at commas
    const last = a[a.length - 1]; const piece = w + (i < arr.length - 1 ? ',' : '');
    if (last && (last + ' ' + piece).length <= 95) a[a.length - 1] = last + ' ' + piece; else a.push(piece); return a; }, []));
}
const srt = []; let n = 0;
for (const c of cues) {
  const cs = chunks(lines[c.id]); const tot = cs.reduce((a, s) => a + s.length, 0); let t = c.at;
  for (const s of cs) { const d = vo[c.id] * s.length / tot; srt.push(`${++n}\n${fmt(t)} --> ${fmt(t + d)}\n${s}\n`); t += d; }
}
writeFileSync(join(HERE, 'narration.srt'), srt.join('\n'));
console.log(`narration.srt: ${n} cues`);

// ---- caption strips (render_titles.mjs reads narration.srt) ----
{ const r = spawnSync('node', [join(HERE, 'render_titles.mjs'), '--subs-only'], { stdio: 'inherit' }); if (r.status) process.exit(1); }

// ---- concat video ----
const list = join(SEG, 'list.txt');
writeFileSync(list, segs.map((s) => `file '${s.file}'`).join('\n') + '\n');
const video = join(OUT, '_video.mp4');
run(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', video], 'concat');

// ---- VO mix ----
const audio = join(OUT, '_vo.m4a');
{
  const args = []; const fc = [];
  cues.forEach((c, i) => { args.push('-i', join(OUT, 'vo', `${c.id}.m4a`)); const ms = Math.round(c.at * 1000); fc.push(`[${i}:a]aresample=48000,adelay=${ms}|${ms}[a${i}]`); });
  fc.push(`${cues.map((_, i) => `[a${i}]`).join('')}amix=inputs=${cues.length}:normalize=0,apad,atrim=0:${total.toFixed(3)}[out]`);
  run([...args, '-filter_complex', fc.join(';'), '-map', '[out]', '-ac', '2', '-c:a', 'aac', '-b:a', '192k', audio], 'vo mix');
}

// ---- captions overlay + mux ----
const idx = readFileSync(join(OUT, 'subs', 'index.tsv'), 'utf8').trim().split('\n').map((l) => l.split('\t'));
{
  const args = ['-i', video, '-i', audio]; const fc = ['[0:v]setpts=N/(30*TB)[base]']; let last = 'base';
  idx.forEach(([png, a, b], i) => {
    args.push('-loop', '1', '-framerate', String(FPS), '-t', (+b + 0.1).toFixed(3), '-i', png);
    const o = i === idx.length - 1 ? 'v' : `v${i}`;
    fc.push(`[${last}][${i + 2}:v]overlay=0:H-h-24:eof_action=pass:enable='between(t,${(+a).toFixed(3)},${(+b).toFixed(3)})'[${o}]`);
    last = o;
  });
  const outFile = join(OUT, 'demo.mp4');
  run([...args, '-filter_complex', fc.join(';'), '-map', '[v]', '-map', '1:a', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-c:a', 'copy', '-movflags', '+faststart', outFile], 'final');
  console.log(`demo.mp4: ${probe(outFile).toFixed(2)} s (timeline ${total.toFixed(2)} s)`);
}
