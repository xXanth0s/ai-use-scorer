#!/usr/bin/env bash
# Builds the tmux session recorded by harness.tape (top: live hook events, bottom: commands).
# Fresh copy of the demo DB each time; never touches ~/.scorer.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
H=/tmp/scorer-harness
WARM=/tmp/scorer-harness-warm/scorer.db
rm -rf /tmp/scorer-demo "$H"; mkdir -p /tmp/scorer-demo "$H"
: > /tmp/scorer-demo/events.jsonl; : > "$H/stamps.txt"
cp "${HARNESS_DB:-$WARM}" "$H/scorer.db"; cp ~/.scorer/config.toml "$H/" 2>/dev/null
# hooks_end: when SessionEnd lands in events.jsonl (tail -f shows it within ms)
( for i in $(seq 1 1800); do
    if grep -q '"SessionEnd"' /tmp/scorer-demo/events.jsonl 2>/dev/null; then
      perl -MTime::HiRes=time -e 'printf "%.3f sessionend\n", time' >> "$H/stamps.txt"; break; fi
    sleep 0.05; done ) >/dev/null 2>&1 &
tmux kill-session -t harness 2>/dev/null
tmux -f "$ROOT/demo/terminal/tmux.conf" new-session -d -s harness -x 56 -y 50 -c "$ROOT" \
  -e HARNESS_ROOT="$ROOT"
tmux split-window -v -t harness -l 52% -c "$ROOT" -e HARNESS_ROOT="$ROOT"
tmux send-keys -t harness:0.0 "export PS1=''; clear; printf '\\033[2m# tail -f events.jsonl (hooks, live)\\033[0m\\n'; tail -f /tmp/scorer-demo/events.jsonl | jq -r '\"\\(.at[11:19])  \\(.event)  \\(.payload.tool_name // \"\")\"'" Enter
tmux send-keys -t harness:0.1 "source demo/terminal/harness_rc.sh; clear" Enter
tmux select-pane -t harness:0.1
