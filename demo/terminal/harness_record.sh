#!/usr/bin/env bash
# One command: bash demo/terminal/harness_record.sh
# -> demo/out/harness.mp4 + demo/out/harness.marks.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
WARM=/tmp/scorer-harness-warm
# Warm template: demo DB + ingest + judge once, so the recorded judge only labels new turns.
if [ ! -s "$WARM/scorer.db" ] || [ "${REWARM:-0}" = 1 ]; then
  rm -rf "$WARM"; mkdir -p "$WARM"; cp demo/scorer-demo.db "$WARM/scorer.db"; cp ~/.scorer/config.toml "$WARM/"
  SCORER_HOME="$WARM" SCORER_CWD_PREFIX="$ROOT" uv run scorer ingest --db "$WARM/scorer.db" --since 1d
  SCORER_HOME="$WARM" uv run scorer judge --day 2026-10-14 --db "$WARM/scorer.db"
fi
vhs demo/terminal/harness.tape
# vhs 0.12 writes 25 fps regardless of Set Framerate; resample to 30 fps (timing unchanged)
# then append the static Vercel card (4 s) in the same encode
node demo/terminal/harness_card.mjs >/dev/null
CARD_S=4
ffmpeg -v error -y -i /tmp/scorer-harness-raw.mp4 -loop 1 -t "$CARD_S" -framerate 30 -i demo/out/harness_card.png \
  -filter_complex "[0:v]fps=30,format=yuv420p,setsar=1[a];[1:v]fps=30,format=yuv420p,setsar=1[b];[a][b]concat=n=2:v=1[v]" \
  -map "[v]" -c:v libx264 -crf 16 -pix_fmt yuv420p -r 30 -movflags +faststart demo/out/harness.mp4
tmux kill-session -t harness 2>/dev/null || true
python3 demo/terminal/harness_marks.py
