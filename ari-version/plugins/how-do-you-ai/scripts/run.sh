#!/usr/bin/env bash
set -eu
plugin_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AI_SESSION_DATA_DIR="${HDYAI_DATA_DIR:-${HOME}/.local/share/how-do-you-ai}"
node_path="$(command -v node || true)"
if [ -z "$node_path" ] || ! "$node_path" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 24 ? 0 : 1)' >/dev/null 2>&1; then
  if [ "${1:-}" = hook ]; then printf '{}\n'; exit 0; fi
  printf 'This plugin needs Node.js 24 or newer. Ask your agent to set up Node.js from its official distribution, then retry.\n' >&2
  exit 1
fi
cd "$plugin_root"
if [ "${1:-}" = hook ]; then
  "$node_path" scripts/worker.mjs "$@" 2>/dev/null || printf '{}\n'
  exit 0
fi
exec "$node_path" scripts/worker.mjs "$@"
