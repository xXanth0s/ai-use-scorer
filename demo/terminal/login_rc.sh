# Sourced (hidden) in the top pane of the login tmux session.
export SCORER_HOME=/tmp/scorer-login/home                # hooks write events here (not ~/.scorer)
export CLAUDE_CONFIG_DIR=/tmp/scorer-login/claude-config # sandboxed claude: no ~/.claude settings/hooks/transcripts
unset SUPERSET_HOME_DIR
export PATH="$LOGIN_ROOT/.venv/bin:$PATH"                # `scorer` = this repo's real CLI entry point
export PS1='\[\e[38;2;49;108;90m\]$\[\e[0m\] ' BASH_SILENCE_DEPRECATION_WARNING=1
STAMPS=/tmp/scorer-login/stamps.txt
_stamp() { perl -MTime::HiRes=time -e 'printf "%.3f %s\n", time, $ARGV[0]' "$1" >> "$STAMPS"; }
trap 'case "$BASH_COMMAND" in *"scorer install"*|*"claude -p"*) _stamp "enter $BASH_COMMAND";; esac' DEBUG
PROMPT_COMMAND='_stamp prompt'
cd /tmp/scorer-login/repo
