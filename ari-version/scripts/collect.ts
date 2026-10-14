import { collect } from '../lib/collector';
async function run() { console.log(JSON.stringify(await collect())); }
await run();
if (process.argv.includes('--watch')) {
  let running = false;
  setInterval(async () => { if (running) return; running = true; try { await run(); } catch { console.error('Collection failed; retrying next interval.'); } finally { running = false; } }, 15000);
}
