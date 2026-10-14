import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import type { Server } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { createScorerMastra } from '../src/mastra';
import { makeServer } from '../src/server';
import { freePort } from './helpers';
import { scriptedModel } from './mock-model';

let srv: Server;
let url: string;

beforeAll(async () => {
  const dir = mkdtempSync(join(tmpdir(), 'mastra-public-'));
  const pub = join(dir, 'public');
  mkdirSync(join(pub, 'css'), { recursive: true });
  writeFileSync(join(pub, 'index.html'), '<!doctype html><title>t</title>');
  writeFileSync(join(pub, 'app.js'), 'console.log(1)');
  writeFileSync(join(pub, 'css', 'app.css'), 'body{}');
  writeFileSync(join(dir, 'secret.txt'), 'SECRET');
  const m = scriptedModel('m', () => [{ text: 'x' }]);
  srv = makeServer(createScorerMastra({ models: { analyst: m, turnInvestigator: m, sqlAnalyst: m }, traceDir: join(dir, 'traces') }),
    { publicDir: pub });
  const port = await freePort();
  await new Promise<void>((ok) => srv.listen(port, '127.0.0.1', ok));
  url = `http://127.0.0.1:${port}`;
});

afterAll(() => new Promise<void>((ok) => srv.close(() => ok())));

describe('static files from public/', () => {
  it('serves / as index.html and /public/* with content types', async () => {
    const root = await fetch(`${url}/`);
    expect(root.status).toBe(200);
    expect(root.headers.get('content-type')).toBe('text/html; charset=utf-8');
    expect(await root.text()).toContain('<title>t</title>');
    const js = await fetch(`${url}/public/app.js`);
    expect(js.headers.get('content-type')).toBe('text/javascript; charset=utf-8');
    const css = await fetch(`${url}/public/css/app.css`);
    expect(css.status).toBe(200);
    expect(css.headers.get('content-type')).toBe('text/css; charset=utf-8');
  });

  it('404s missing files, directories and traversal; keeps /health and POST /chat', async () => {
    expect((await fetch(`${url}/public/nope.js`)).status).toBe(404);
    expect((await fetch(`${url}/public/css`)).status).toBe(404);
    const trav = await fetch(`${url}/public/..%2Fsecret.txt`);
    expect(trav.status).toBe(404);
    expect(await trav.text()).not.toContain('SECRET');
    expect((await fetch(`${url}/health`)).status).toBe(200);
    expect((await fetch(`${url}/chat`, { method: 'POST', body: 'not json' })).status).toBe(400);
    expect((await fetch(`${url}/other`)).status).toBe(404);
  });
});
