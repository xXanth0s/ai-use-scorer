// beats.txt -> out/beats.json: every text beat with its hold, plus per-anchor totals (what recorders hold for).
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const HERE = dirname(fileURLToPath(import.meta.url));
const GAP = 0.4;                                    // blank footage between two overlays of the same anchor
const beats = readFileSync(join(HERE, 'beats.txt'), 'utf8').split('\n').filter((l) => l && !l.startsWith('#')).map((l, i) => {
  const [anchor, style, text] = l.split('|');
  const words = text.trim().split(/\s+/).length;
  if (words > 8) throw new Error(`beat ${i} has ${words} words: ${text}`);
  const hold = +Math.min(3, Math.max(2, 0.35 * words + 1)).toFixed(2);
  return { id: `b${String(i + 1).padStart(2, '0')}`, anchor, style, text, words, hold };
});
const anchors = {};
for (const b of beats) anchors[b.anchor] = +((anchors[b.anchor] ?? -GAP) + GAP + b.hold).toFixed(2);
mkdirSync(join(HERE, 'out'), { recursive: true });
writeFileSync(join(HERE, 'out', 'beats.json'), JSON.stringify({ gap: GAP, beats, anchors }, null, 2));
console.log(`${beats.length} beats, text time ${beats.reduce((a, b) => a + b.hold, 0).toFixed(1)} s`, anchors);
