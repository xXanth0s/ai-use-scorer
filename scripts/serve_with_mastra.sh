#!/usr/bin/env bash
# Start `scorer serve` with the optional Mastra sidecar (agent-mastra/) as its chat backend.
# Both are stopped on exit (Ctrl-C, error, or either one dying).
#
#   scripts/serve_with_mastra.sh [--db PATH]          # extra args go to `scorer serve`
#   SCORER_PORT=8765 MASTRA_PORT=4111 scripts/serve_with_mastra.sh
#
# Without the sidecar (or if it is down) `scorer serve` falls back to the Python agent.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCORER_PORT="${SCORER_PORT:-8765}"
MASTRA_PORT="${MASTRA_PORT:-4111}"

if [ ! -d "$ROOT/agent-mastra/node_modules" ]; then
  (cd "$ROOT/agent-mastra" && pnpm install --frozen-lockfile)
fi

pids=()
cleanup() {
  trap - EXIT INT TERM
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd "$ROOT/agent-mastra" && PORT="$MASTRA_PORT" SCORER_URL="http://127.0.0.1:$SCORER_PORT" exec ./node_modules/.bin/tsx src/server.ts) &
pids+=($!)

for _ in $(seq 1 100); do
  curl -fsS -m 1 "http://127.0.0.1:$MASTRA_PORT/health" >/dev/null 2>&1 && break
  sleep 0.2
done

(cd "$ROOT" && SCORER_MASTRA_URL="http://127.0.0.1:$MASTRA_PORT" exec uv run scorer serve --port "$SCORER_PORT" "$@") &
pids+=($!)

# exit (and clean up both) as soon as either process stops
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do sleep 1; done
