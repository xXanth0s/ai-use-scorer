#!/usr/bin/env bash
# QA stack: the same processes scripts/serve_with_mastra.sh starts, but on QA ports, against a
# read-only *copy* of the DB (scorer serve's dbm.connect runs DDL + WAL pragma, i.e. it can write),
# with SCORER_HOME pointing at the QA work dir (so sidecar traces don't land in ~/.scorer).
#
#   agent-mastra/qa/stack.sh start     # copy DB, start sidecar :4190, scorer(mastra) :8790, scorer(python) :8791
#   agent-mastra/qa/stack.sh stop      # kill everything recorded in $QA_DIR/pids
#
# Env: QA_DIR (default /tmp/scorer-qa), SRC_DB (default ~/.scorer/scorer.db).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
QA_DIR="${QA_DIR:-/tmp/scorer-qa}"
SRC_DB="${SRC_DB:-$HOME/.scorer/scorer.db}"
MASTRA_PORT="${MASTRA_PORT:-4190}"
SCORER_PORT="${SCORER_PORT:-8790}"
PY_PORT="${PY_PORT:-8791}"
PIDS="$QA_DIR/pids"

wait_for() { for _ in $(seq 1 150); do curl -fsS -m 1 "$1" >/dev/null 2>&1 && return 0; sleep 0.2; done; echo "timeout: $1" >&2; return 1; }

case "${1:-}" in
  start)
    mkdir -p "$QA_DIR/home"
    if [ ! -f "$QA_DIR/scorer.db" ]; then
      sqlite3 -readonly "$SRC_DB" ".backup '$QA_DIR/scorer.db'"
    fi
    : > "$PIDS"
    (cd "$ROOT/agent-mastra" && PORT="$MASTRA_PORT" SCORER_URL="http://127.0.0.1:$SCORER_PORT" SCORER_HOME="$QA_DIR/home" \
      exec ./node_modules/.bin/tsx src/server.ts) > "$QA_DIR/sidecar.log" 2>&1 &
    echo $! >> "$PIDS"
    wait_for "http://127.0.0.1:$MASTRA_PORT/health"
    (cd "$ROOT" && SCORER_MASTRA_URL="http://127.0.0.1:$MASTRA_PORT" exec uv run scorer serve --port "$SCORER_PORT" --db "$QA_DIR/scorer.db") \
      > "$QA_DIR/scorer-mastra.log" 2>&1 &
    echo $! >> "$PIDS"
    (cd "$ROOT" && env -u SCORER_MASTRA_URL uv run scorer serve --port "$PY_PORT" --db "$QA_DIR/scorer.db") \
      > "$QA_DIR/scorer-python.log" 2>&1 &
    echo $! >> "$PIDS"
    wait_for "http://127.0.0.1:$SCORER_PORT/api/chat/status"
    wait_for "http://127.0.0.1:$PY_PORT/api/chat/status"
    echo "sidecar  http://127.0.0.1:$MASTRA_PORT/   (dummy chat page)"
    echo "scorer   http://127.0.0.1:$SCORER_PORT/   $(curl -s "http://127.0.0.1:$SCORER_PORT/api/chat/status")"
    echo "python   http://127.0.0.1:$PY_PORT/   $(curl -s "http://127.0.0.1:$PY_PORT/api/chat/status")"
    ;;
  stop)
    [ -f "$PIDS" ] || exit 0
    while read -r p; do
      [ -n "$p" ] || continue
      pkill -TERM -P "$p" 2>/dev/null || true
      kill -TERM "$p" 2>/dev/null || true
    done < "$PIDS"
    sleep 1
    for port in "$MASTRA_PORT" "$SCORER_PORT" "$PY_PORT"; do
      for p in $(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null); do kill -TERM "$p" 2>/dev/null || true; done
    done
    rm -f "$PIDS"
    echo stopped
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
