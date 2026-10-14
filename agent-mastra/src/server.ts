import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { pathToFileURL } from 'node:url';

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

/** Only loopback scorer URLs are accepted from the proxy header: the sidecar must not be turned
 *  into a fetcher for arbitrary hosts. */
function scorerUrlFor(req: IncomingMessage): string {
  const h = req.headers['x-scorer-url'];
  const v = Array.isArray(h) ? h[0] : h;
  if (v && /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(v)) return v;
  return config.scorerUrl;
}

export function makeServer(scorer: ScorerMastra, opts: { onStats?: (s: ChatStats) => void } = {}): Server {
  return createServer(async (req, res) => {
    const path = (req.url ?? '/').split('?')[0];
    if (req.method === 'GET' && path === '/health') {
      return json(res, 200, { ok: true, backend: 'mastra', model: config.analystModel });
    }
    if (req.method !== 'POST' || path !== '/chat') return json(res, 404, { error: 'unknown endpoint' });
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
