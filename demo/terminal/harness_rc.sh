# Sourced (hidden) by the bottom pane of the harness tmux session.
# Hooks phase writes to /tmp/scorer-demo; pipeline runs on a COPY of the demo DB in /tmp/scorer-harness.
export SCORER_HOME=/tmp/scorer-demo
export SCORER_CWD_PREFIX="$HARNESS_ROOT"
export PROMPT="add a docstring to hello.py, then run it with python"
export DB=/tmp/scorer-harness/scorer.db OUT=/tmp/scorer-harness/ari-sessions.sqlite
export URL=https://how-do-you-ai-analysis.vercel.app PAY=/tmp/scorer-harness/sync-payload.json
export PS1='\[\e[38;2;49;108;90m\]$\[\e[0m\] ' BASH_SILENCE_DEPRECATION_WARNING=1
STAMPS=/tmp/scorer-harness/stamps.txt
_stamp() { perl -MTime::HiRes=time -e 'printf "%.3f %s\n", time, $ARGV[0]' "$1" >> "$STAMPS"; }
# Enter time of each scorer command (DEBUG fires right before the command runs).
trap 'case "$BASH_COMMAND" in *demo_e2e*|*"scorer ingest"*|*"scorer judge"*|*"scorer export"*|*"scorer sync"*) _stamp "enter $BASH_COMMAND";; esac' DEBUG
PROMPT_COMMAND='_stamp prompt'
cd "$HARNESS_ROOT"
