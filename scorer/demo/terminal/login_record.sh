#!/usr/bin/env bash
# One command: bash demo/terminal/login_record.sh  -> demo/out/login-term.mp4 + /tmp/scorer-login/stamps.txt
# Verifies that ~/.claude/settings.json, ~/.codex/config.toml, ~/.codex/hooks.json are byte-identical before/after.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
sums() { for f in "$HOME/.claude/settings.json" "$HOME/.codex/config.toml" "$HOME/.codex/hooks.json"; do
  [ -e "$f" ] && shasum -a 256 "$f" || echo "absent  $f"; done; }
BEFORE="$(sums)"; echo "$BEFORE"
vhs demo/terminal/login.tape
tmux kill-session -t login 2>/dev/null || true
AFTER="$(sums)"
mkdir -p demo/out
# vhs 0.12 writes 25 fps regardless of Set Framerate; resample to 30 fps (timing unchanged)
ffmpeg -v error -y -i /tmp/scorer-login-raw.mp4 -vf fps=30 -c:v libx264 -crf 16 -pix_fmt yuv420p -movflags +faststart demo/out/login-term.mp4
cp /tmp/scorer-login/stamps.txt demo/out/login-term.stamps.txt
jq -c '{at,event,tool:.payload.tool_name}' /tmp/scorer-login/home/events.jsonl > demo/out/login-term.events.jsonl
if [ "$BEFORE" = "$AFTER" ]; then echo "CHECKSUMS UNCHANGED"; else echo "CHECKSUMS CHANGED"; echo "$AFTER"; exit 1; fi
