#!/usr/bin/env bash
# Builds the tmux session recorded by login.tape. Everything lives in a throwaway lab dir:
#   /tmp/scorer-login/.claude/settings.json   <- the ONLY settings file `scorer install` writes
#   /tmp/scorer-login/home                    <- SCORER_HOME (events.jsonl)
#   /tmp/scorer-login/claude-config           <- CLAUDE_CONFIG_DIR for the sandboxed `claude -p`
#   /tmp/scorer-login/repo                    <- throwaway git repo (cwd of the agent run)
# Never touches ~/.claude, ~/.codex or ~/.scorer.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAB=/tmp/scorer-login
rm -rf "$LAB"; mkdir -p "$LAB/.claude" "$LAB/home" "$LAB/claude-config" "$LAB/repo"
: > "$LAB/home/events.jsonl"
git -C "$LAB/repo" init -q
printf 'def hello():\n    print("hello")\n\nhello()\n' > "$LAB/repo/hello.py"
git -C "$LAB/repo" add hello.py
git -C "$LAB/repo" -c user.email=demo@x -c user.name=demo commit -qm init
tmux kill-session -t login 2>/dev/null || true
tmux -f "$ROOT/demo/terminal/tmux.conf" new-session -d -s login -x 160 -y 40 -c "$LAB/repo" \
  -e LOGIN_ROOT="$ROOT"
# bottom pane: live event stream from the lab SCORER_HOME only
tmux split-window -v -t login -l 38% -c "$LAB/repo"
tmux send-keys -t login:0.1 "export PS1='' BASH_SILENCE_DEPRECATION_WARNING=1; clear; tail -n0 -F $LAB/home/events.jsonl 2>/dev/null | jq -c '{at:.at[11:19],agent,event,tool:.payload.tool_name}'" Enter
tmux send-keys -t login:0.0 "source $ROOT/demo/terminal/login_rc.sh; clear" Enter
tmux select-pane -t login:0.0
