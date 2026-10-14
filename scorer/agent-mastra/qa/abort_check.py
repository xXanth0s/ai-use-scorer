#!/usr/bin/env python3
"""Client abort: open POST /chat, hang up after the first tool-start, then confirm from the sidecar's JSONL
traces that the analyst's model generation ended with finish_reason "aborted" (server.ts res.on('close')
-> AbortController) and that /health still answers. The [chat] stats log line is NOT reliable evidence:
it is skipped when the generator is closed early.

    python3 agent-mastra/qa/abort_check.py http://127.0.0.1:4190 $QA_DIR/home/traces
"""
import json, socket, sys, time, urllib.request
from urllib.parse import urlparse

import glob
from datetime import datetime, timezone
base, tdir = sys.argv[1], sys.argv[2]
u = urlparse(base)
t_start = datetime.now(timezone.utc).isoformat()[:23]
body = json.dumps({"messages": [{"role": "user", "content": "Why was verification low this week? Investigate the worst turns."}], "day": "2026-10-14"}).encode()
sock = socket.create_connection((u.hostname, u.port), timeout=120)
sock.sendall(b"POST /chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
f = sock.makefile("rb")
seen = []
while True:
    line = f.readline().decode()
    if not line:
        break
    if line.startswith("data:"):
        ev = json.loads(line[5:]); seen.append(ev["type"])
        if ev["type"] == "tool-start":
            break
f.close(); sock.shutdown(socket.SHUT_RDWR); sock.close()
t_abort = time.monotonic()
ended = None
for _ in range(60):
    time.sleep(1)
    rows = [json.loads(l) for f in glob.glob(tdir + "/*.jsonl") for l in open(f) if l.strip()]
    ab = [r for r in rows if r.get("ts", "") >= t_start and r.get("type") == "model_generation" and r.get("finish_reason") == "aborted"]
    if ab:
        ended = round(time.monotonic() - t_abort, 1); break
health = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
print(json.dumps({"events_before_hangup": seen, "aborted_generation_seen_after_s": ended, "health_ok": health.get("ok")}))
sys.exit(0 if ended is not None and health.get("ok") else 1)
