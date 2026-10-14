#!/usr/bin/env bash
# Real end-to-end check of the scorer hooks with Claude Code (spec §6c).
#
#   scripts/demo_e2e.sh                  # prompt "say hi"
#   PROMPT="say hi, then run 'ls' with the Bash tool" scripts/demo_e2e.sh
#
# Uses scripts/demo.json (generate with:
#   uv run scorer install --agent claude --settings scripts/demo.json --no-schedule --yes)
# Never touches ~/.claude/settings.json. SCORER_HOME defaults to a fresh tmp dir.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$ROOT/scripts/demo.json"
PROMPT="${PROMPT:-say hi}"
# ALLOWED_TOOLS overrides the --allowedTools list (default lets the agent edit files and run pytest)
export SCORER_HOME="${SCORER_HOME:-$(mktemp -d -t scorer-home)}"
EV="$SCORER_HOME/events.jsonl"
FAIL=0
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAIL=1; }

[ -f "$SETTINGS" ] || { echo "missing $SETTINGS"; exit 2; }

REPO="$(mktemp -d -t scorer-e2e)"
git -C "$REPO" init -q
printf 'print("hello")\n' > "$REPO/hello.py"
git -C "$REPO" add hello.py
git -C "$REPO" -c user.email=e2e@x -c user.name=e2e commit -qm init
HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"

echo "repo=$REPO SCORER_HOME=$SCORER_HOME"
T0=$(python3 -c 'import time;print(time.time())')
REPLY="$(cd "$REPO" && env -u SUPERSET_HOME_DIR claude -p "$PROMPT" --settings "$SETTINGS" \
          --allowedTools ${ALLOWED_TOOLS:-Edit Write "Bash(ls)" "Bash(pytest*)" "Bash(python*)"} 2>/dev/null)"
RC=$?
T1=$(python3 -c 'import time;print(time.time())')
printf 'claude exit=%s wall=%.1fs reply(clipped)=%s\n' "$RC" "$(python3 -c "print($T1-$T0)")" \
  "$(printf '%s' "$REPLY" | tr '\n' ' ' | cut -c1-80)"

[ -s "$EV" ] || { bad "events.jsonl written"; exit 1; }
for e in SessionStart UserPromptSubmit Stop SessionEnd; do
  if [ -n "$(jq -c --arg e "$e" 'select(.event==$e)' "$EV")" ]; then ok "event $e present"; else bad "event $e present"; fi
done
NTOOL=$(jq -c 'select(.event=="PostToolUse")' "$EV" | wc -l | tr -d ' ')
echo "info  PostToolUse lines: $NTOOL"

STOP_HEAD="$(jq -r 'select(.event=="Stop") | .git.head // empty' "$EV" | tail -1)"
[ "$STOP_HEAD" = "$HEAD_SHA" ] && ok "Stop git.head == repo HEAD ($HEAD_SHA)" \
  || bad "Stop git.head ($STOP_HEAD) == repo HEAD ($HEAD_SHA)"

if printf '%s' "$REPLY" | grep -qiE 'scorer|traceback|events\.jsonl|hook'; then
  bad "reply has no injected hook text"; else ok "reply has no injected hook text"; fi

TRANSCRIPT="$(jq -r 'select(.event=="SessionStart") | .payload.transcript_path // empty' "$EV" | head -1)"
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  if [ -n "$(jq -c 'select(.type=="system" and .subtype=="stop_hook_summary")' "$TRANSCRIPT")" ]; then
    ok "transcript has system stop_hook_summary"
    jq -c 'select(.type=="system" and .subtype=="stop_hook_summary")
           | {hookCount, hookErrors, preventedContinuation, hasOutput}' "$TRANSCRIPT" | head -2
  else bad "transcript has system stop_hook_summary"; fi
else bad "transcript found ($TRANSCRIPT)"; fi

echo "--- events"
jq -c '{at,event,tool:.payload.tool_name}' "$EV"
[ "$FAIL" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$FAIL"
