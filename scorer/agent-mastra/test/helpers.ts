import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

export const REPO = resolve(__dirname, '../..');
export const DAY = '2026-10-13';

export async function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer();
    s.once('error', fail);
    s.listen(0, '127.0.0.1', () => {
      const port = (s.address() as { port: number }).port;
      s.close(() => ok(port));
    });
  });
}

export type ScorerProc = { url: string; dir: string; turnIds: Record<string, number>; stop: () => Promise<void> };

/** Build tests/test_serve.py's fixture DB in a temp dir and run a real `scorer serve` on it. */
export async function startScorer(env: Record<string, string> = {}): Promise<ScorerProc> {
  const dir = mkdtempSync(join(tmpdir(), 'scorer-mastra-'));
  const db = join(dir, 'scorer.db');
  const out = execFileSync('uv', ['run', '--project', REPO, 'python', '-c',
    'import json, sys; sys.path.insert(0, "tests"); from test_serve import build_fixture; ' +
    `print(json.dumps(build_fixture(${JSON.stringify(db)})))`],
  { cwd: REPO, env: { ...process.env, SCORER_HOME: dir }, encoding: 'utf8' });
  const turnIds = JSON.parse(out.trim().split('\n').pop()!).turn_ids as Record<string, number>;
  const port = await freePort();
  const proc: ChildProcess = spawn('uv', ['run', '--project', REPO, 'scorer', 'serve', '--db', db, '--port', String(port)], {
    cwd: REPO,
    env: { ...process.env, SCORER_HOME: dir, ANTHROPIC_API_KEY: '', SCORER_MASTRA_URL: '', ...env },
    stdio: 'ignore',
  });
  const url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 20_000;
  for (;;) {
    try {
      if ((await fetch(`${url}/api/days`)).ok) break;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) throw new Error('scorer serve did not start');
    await new Promise((r) => setTimeout(r, 100));
  }
  return {
    url, dir, turnIds,
    stop: () => new Promise<void>((ok) => {
      if (proc.exitCode !== null) return ok();
      proc.once('exit', () => ok());
      proc.kill('SIGTERM');
      setTimeout(() => proc.kill('SIGKILL'), 2000).unref();
    }),
  };
}

export function sse(raw: string): any[] {
  return raw.split('\n\n').filter((c) => c.startsWith('data: ')).map((c) => JSON.parse(c.slice(6)));
}

/** Text of the last user message in a V3 prompt. */
export function lastUserText(prompt: any[]): string {
  const u = [...prompt].reverse().find((m) => m.role === 'user');
  if (!u) return '';
  return typeof u.content === 'string' ? u.content : u.content.map((p: any) => p.text ?? '').join('');
}

/** All tool-result outputs in a V3 prompt, JSON-stringified. */
export function toolResultsText(prompt: any[]): string {
  return JSON.stringify(prompt.filter((m) => m.role === 'tool'));
}
