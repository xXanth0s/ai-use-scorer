import { readFile, stat } from 'node:fs/promises';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { extname, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { runChat, type ChatBody, type ChatStats } from './chat';
import { config } from './config';
import { createScorerMastra, type ScorerMastra } from './mastra';

async function readBody(req: IncomingMessage, max = 1_000_000): Promise<string> {
  let size = 0;
  const parts: Buffer[] = [];
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > max) throw new Error('body too large');
    parts.push(c as Buffer);
  }
  return Buffer.concat(parts).toString('utf8');
}

function json(res: ServerResponse, code: number, obj: unknown) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body);
}

/** agent-mastra/public/ (owned by the QA page; this server only serves it). */
export const PUBLIC_DIR = resolve(fileURLToPath(new URL('.', import.meta.url)), '../public');

const TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
};

/** GET / -> public/index.html, GET /public/<path> -> public/<path>. Anything resolving outside
 *  public/ (traversal) or missing is a 404. */
async function serveStatic(res: ServerResponse, publicDir: string, urlPath: string): Promise<void> {
  let rel: string;
  try {
    rel = urlPath === '/' ? 'index.html' : decodeURIComponent(urlPath.slice('/public/'.length));
  } catch {
    return json(res, 400, { error: 'bad path' });
  }
  const root = resolve(publicDir);
  const file = resolve(join(root, rel));
  if (!file.startsWith(root + sep) || rel.includes('\0')) return json(res, 404, { error: 'not found' });
  try {
    if (!(await stat(file)).isFile()) throw new Error('not a file');
    const body = await readFile(file);
    res.writeHead(200, {
      'Content-Type': TYPES[extname(file).toLowerCase()] ?? 'application/octet-stream',
      'Content-Length': body.length,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    });
    res.end(body);
  } catch {
    json(res, 404, { error: 'not found' });
  }
}

/** Only loopback scorer URLs are accepted from the proxy header: the sidecar must not be turned
 *  into a fetcher for arbitrary hosts. */
function scorerUrlFor(req: IncomingMessage): string {
  const h = req.headers['x-scorer-url'];
  const v = Array.isArray(h) ? h[0] : h;
  if (v && /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(v)) return v;
  return config.scorerUrl;
}

/** Browser origins allowed to call /chat cross-origin: loopback only (any port). */
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/;

/** CORS for /chat. Returns false (and answers 403) when a browser origin is not loopback. */
function cors(req: IncomingMessage, res: ServerResponse): boolean {
  const origin = req.headers.origin;
  if (origin === undefined) return true; // not a browser cross-origin request (curl, the Python proxy)
  if (!LOCAL_ORIGIN.test(origin)) {
    json(res, 403, { error: 'origin not allowed' });
    return false;
  }
  res.setHeader('Access-Control-Allow-Origin', origin);
  res.setHeader('Vary', 'Origin');
  return true;
}

export function makeServer(
  scorer: ScorerMastra,
  opts: { onStats?: (s: ChatStats) => void; publicDir?: string } = {},
): Server {
  return createServer(async (req, res) => {
    const path = (req.url ?? '/').split('?')[0] ?? '/';
    if (req.method === 'GET' && path === '/health') {
      return json(res, 200, { ok: true, backend: 'mastra', model: config.analystModel, fallback: config.fallbackModel });
    }
    if ((req.method === 'GET' || req.method === 'HEAD') && (path === '/' || path.startsWith('/public/'))) {
      return serveStatic(res, opts.publicDir ?? PUBLIC_DIR, path);
    }
    if (path === '/chat' && req.method === 'OPTIONS') {
      if (!cors(req, res)) return;
      res.writeHead(204, {
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Max-Age': '600',
      });
      return res.end();
    }
    if (req.method !== 'POST' || path !== '/chat') return json(res, 404, { error: 'unknown endpoint' });
    if (!cors(req, res)) return;
    let body: ChatBody;
    try {
      const raw = await readBody(req);
      body = raw ? JSON.parse(raw) : {};
      if (!body || typeof body !== 'object' || (body.messages !== undefined && !Array.isArray(body.messages))) throw new Error();
    } catch {
      return json(res, 400, { error: 'body must be {messages: [{role, content}], day?}' });
    }
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Accel-Buffering': 'no',
      Connection: 'close',
    });
    const abort = new AbortController();
    res.on('close', () => {
      if (!res.writableFinished) abort.abort();
    });
    for await (const ev of runChat(scorer, body, {
      scorerUrl: scorerUrlFor(req),
      abortSignal: abort.signal,
      stats: (s) => {
        opts.onStats?.(s);
        console.log(`[chat] ${s.ms} ms · steps ${s.steps}+${s.subagentSteps} · tools ${s.toolCalls} · models ${s.models.join(',')}`);
      },
    })) {
      if (abort.signal.aborted) break;
      res.write(`data: ${JSON.stringify(ev)}\n\n`);
    }
    res.end();
  });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  if (!process.env.ANTHROPIC_API_KEY) console.warn('[agent-mastra] ANTHROPIC_API_KEY is not set; chats will fail');
  const scorer = createScorerMastra();
  const srv = makeServer(scorer);
  srv.listen(config.port, '127.0.0.1', () => {
    console.log(`agent-mastra sidecar: http://127.0.0.1:${config.port}  (scorer: ${config.scorerUrl}, traces: ${config.scorerHome}/traces)`);
  });
  const stop = () => {
    srv.close();
    void scorer.exporter.flush().finally(() => process.exit(0));
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}
