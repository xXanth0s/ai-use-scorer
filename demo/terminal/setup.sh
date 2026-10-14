#!/usr/bin/env bash
# Builds the two-pane tmux session recorded by hooks.tape (left: event stream, right: claude run).
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export SCORER_HOME=/tmp/scorer-demo
rm -rf "$SCORER_HOME"; mkdir -p "$SCORER_HOME"; : > "$SCORER_HOME/events.jsonl"
export PS1='\[\e[38;2;49;108;90m\]$\[\e[0m\] ' BASH_SILENCE_DEPRECATION_WARNING=1
tmux kill-session -t demo 2>/dev/null
tmux -f "$ROOT/demo/terminal/tmux.conf" new-session -d -s demo -x 160 -y 40 -c "$ROOT"
tmux split-window -h -t demo -l 48% -c "$ROOT"
tmux send-keys -t demo:0.0 "clear; tail -f \$SCORER_HOME/events.jsonl | jq -c '{at:.at[11:19],event,tool:.payload.tool_name}'" Enter
tmux send-keys -t demo:0.1 "clear" Enter
tmux select-pane -t demo:0.1
