// Static end card for the harness pane: demo/out/harness_card.png (576x1080, Sherpa light).
// Stands in for the real hosted sync clip until the device is paired (see HARNESS.md).
import { chromium } from 'playwright';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'out', 'harness_card.png');
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:576px;height:1080px;background:#fbf6ee;color:#1c2d22;font-family:"JetBrains Mono",monospace}
.wrap{box-sizing:border-box;height:100%;padding:0 44px;display:flex;flex-direction:column;justify-content:center;gap:22px}
.k{font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:#316c5a}
h1{margin:0;font-size:34px;line-height:1.2;font-weight:600}
.url{font-size:19px;color:#0369a1}
.note{font-size:15px;line-height:1.5;color:#4d5a4d;border-top:1px solid #ddd9d3;padding-top:18px}
</style></head><body><div class="wrap">
<div class="k">next: scorer sync</div>
<h1>&rarr; hosted analysis on Vercel</h1>
<div class="url">https://how-do-you-ai.vercel.app</div>
<div class="note">The dry run just built the real /api/sync payload.<br>Real sync clip pending device pairing.</div>
</div></body></html>`;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 576, height: 1080 }, deviceScaleFactor: 1, colorScheme: 'light' });
await page.setContent(html); await page.evaluate(() => document.fonts.ready);
await page.screenshot({ path: OUT }); await browser.close();
console.log(OUT);
