#!/usr/bin/env bash
# scorer test lab, driven through herdr. Run INSIDE a herdr pane (HERDR_ENV=1).
#
#   scripts/lab/orchestrate.sh setup      # ~/scorer-lab projects + worktree + project-local hooks
#   scripts/lab/orchestrate.sh launch     # 4 herdr workspaces, start claude x3 + codex x1
#   scripts/lab/orchestrate.sh drive      # scripted prompts: correction, clarification, interrupt,
#                                         #   queued prompt, hand edit, asked/not-asked to test
#   scripts/lab/orchestrate.sh measure    # ingest --cwd-prefix, check, report -> ~/.scorer-lab/reports/lab.md, compare
#   scripts/lab/orchestrate.sh compare    # ground truth (lab-actions.log) vs measured (scorer.db) per project
#   scripts/lab/orchestrate.sh teardown   # stop lab agents, close lab workspaces (dirs are kept)
#   scripts/lab/orchestrate.sh all        # setup launch drive teardown measure
#   scripts/lab/orchestrate.sh reset      # delete ~/scorer-lab and ~/.scorer-lab (asks first)
#
# Project sets (scripts/lab/sets/<set>.sh): LAB_SET=v1 (default: todo, csv+worktree, codex kata) or
#   LAB_SET=v2 (md link checker, rate limiter, json-schema diff, retry util w/ flaky tests; claude only,
#   adds positive episodes: good spec, verification catch, early redirect, scoped turns, trivial hand edit).
#   setup records the set in $LAB_ROOT/.lab-set, so later phases pick it up from LAB_ROOT alone.
#   e.g. LAB_ROOT=$HOME/scorer-lab/run4 LAB_SET=v2 scripts/lab/orchestrate.sh all
# Re-run in a fresh root (keeps earlier runs):  LAB_ROOT=$HOME/scorer-lab/run3 scripts/lab/orchestrate.sh all
# (the reported lab used LAB_ROOT=$HOME/scorer-lab/run2; measure filters --cwd-prefix "$LAB_ROOT").
# Isolation: SCORER_HOME=~/.scorer-lab, hooks only in <proj>/.claude/settings.local.json. Nothing global.
# Ground truth for expected-vs-measured: ~/.scorer-lab/lab-actions.log (one line per orchestrator action).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
export SCORER_HOME="${SCORER_HOME_LAB:-$HOME/.scorer-lab}"
LAB="${LAB_ROOT:-$HOME/scorer-lab}"
MODEL="${LAB_MODEL:-claude-sonnet-5-5}"
STATE="$SCORER_HOME/lab-state.env"
ACTLOG="$SCORER_HOME/lab-actions.log"
SC=(uv run --quiet --project "$REPO" scorer)
mkdir -p "$SCORER_HOME/reports"
SET="${LAB_SET:-$(cat "$LAB/.lab-set" 2>/dev/null || echo v1)}"
SETFILE="$REPO/scripts/lab/sets/$SET.sh"
[ -f "$SETFILE" ] || { echo "[lab] ERROR: unknown LAB_SET=$SET ($SETFILE)" >&2; exit 1; }

log() { local l; l="$(date -u +%FT%TZ) $*"; echo "$l" >> "$ACTLOG"; echo "[lab] $l"; }
die() { echo "[lab] ERROR: $*" >&2; exit 1; }
need_herdr() { [ "${HERDR_ENV:-}" = 1 ] || die "not inside a herdr pane"; }
save() { touch "$STATE"; grep -v "^$1=" "$STATE" > "$STATE.tmp" 2>/dev/null; echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
load() { [ -f "$STATE" ] && . "$STATE"; return 0; }

# ---------------------------------------------------------------- setup
commit_all() { git -C "$1" add -A && git -C "$1" -c user.name=lab -c user.email=lab@local commit -qm "$2"; }

claude_settings() {  # committed project settings: acceptEdits + a dev-command allow list (no global change)
  mkdir -p "$1/.claude"
  cat > "$1/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": ["Bash(pytest:*)", "Bash(python3:*)", "Bash(python:*)", "Bash(uv:*)", "Bash(git:*)",
              "Bash(ls:*)", "Bash(cat:*)", "Bash(mkdir:*)", "Bash(node:*)", "Bash(npm:*)",
              "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(echo:*)",
              "Bash(sed:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(cd:*)", "Bash(touch:*)", "Bash(pgrep:*)"]
  }
}
JSON
}

py_project() {
  local d="$1" name="$2"
  mkdir -p "$d/tests"
  git -C "$d" init -q -b main 2>/dev/null || git -C "$d" init -q
  printf '# %s\n\nscorer lab project.\n' "$name" > "$d/README.md"
  cat > "$d/pyproject.toml" <<EOF
[project]
name = "$name"
version = "0.1.0"
requires-python = ">=3.9"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF
  printf 'def test_smoke():\n    assert True\n' > "$d/tests/test_smoke.py"
  printf '__pycache__/\n.pytest_cache/\n*.json\n!package.json\n.claude/settings.local.json\n' > "$d/.gitignore"
  claude_settings "$d"
}

runtests_cmd() {  # project slash command /runtests -> human-initiated tooling event
  mkdir -p "$1/.claude/commands"
  printf -- '---\ndescription: run the test suite\n---\nRun `python3 -m pytest -q` in the project root and report pass/fail counts in one line.\n' \
    > "$1/.claude/commands/runtests.md"
}
install_hooks() {  # project-local claude hooks only (<proj>/.claude/settings.local.json)
  local p; for p in "$@"; do
    "${SC[@]}" install --project "$LAB/$p" --agent claude --yes >/dev/null || die "install failed for $p"
  done
}

cmd_setup() {
  [ -e "$LAB" ] && die "$LAB exists; run '$0 reset' first or set LAB_ROOT"
  mkdir -p "$LAB"; echo "$SET" > "$LAB/.lab-set"
  log "run-begin root=$LAB set=$SET model=$MODEL"
  set_setup
}

# ---------------------------------------------------------------- launch
pane_text() { herdr pane read "$1" --source recent-unwrapped --lines "${2:-60}" 2>/dev/null; }
agent_status() { herdr agent list 2>/dev/null | jq -r --arg p "$1" '.result.agents[] | select(.pane_id==$p) | .agent_status' | head -1; }

start_agent() {  # <workspace label> <dir> <agent name> <shell command>
  local label="$1" dir="$2" name="$3" cmd="$4" out ws pane st i
  out="$(herdr workspace create --cwd "$dir" --label "$label" --no-focus)" || die "workspace create $label"
  ws="$(jq -r '.result.workspace.workspace_id' <<<"$out")"; pane="$(jq -r '.result.root_pane.pane_id' <<<"$out")"
  save "WS_${name//-/_}" "$ws"; save "PANE_${name//-/_}" "$pane"
  sleep 1
  herdr pane run "$pane" "$cmd" >/dev/null
  log "launch $name ws=$ws pane=$pane"
  for i in $(seq 1 60); do
    sleep 2
    # first-run folder-trust screens (claude / codex) for the lab dir we just created
    local vis; vis="$(herdr pane read "$pane" --source visible 2>/dev/null)"
    if grep -qiE 'trust this folder|do you trust|trust the contents' <<<"$vis"; then
      # claude defaults to "No, exit" when the folder pre-approves permissions -> move to "Yes" first
      if grep -qE '❯ *(1\. *)?No' <<<"$vis"; then herdr pane send-keys "$pane" down >/dev/null; sleep 0.5; fi
      herdr pane send-keys "$pane" enter >/dev/null; log "trust-accept $name (lab dir)"; sleep 4; continue
    fi
    if grep -q "Update available" <<<"$vis" && grep -q "esc skip" <<<"$vis"; then
      herdr pane send-keys "$pane" esc >/dev/null; log "skip codex update prompt $name"; sleep 3; continue
    fi
    st="$(agent_status "$pane")"
    case "$st" in
      idle|done) herdr agent rename "$pane" "$name" >/dev/null && { log "ready $name"; return 0; } ;;
      blocked) pane_text "$pane" 15 | tail -8 ;;
    esac
  done
  die "$name did not become ready (status=$st)"
}

claude_cmd() { echo "env -u SUPERSET_HOME_DIR claude --model $MODEL"; }

cmd_launch() {
  need_herdr; : > "$STATE"; log "launch-begin set=$SET"
  set_launch
}

# ---------------------------------------------------------------- drive
aget() { herdr agent get "$1" 2>/dev/null | jq -r '.result.agent | "\(.agent_status) \(.state_change_seq)"'; }
P() {  # <agent> <category> <text>   send a prompt (no wait); remember the lifecycle seq it was sent at
  local seq; seq="$(aget "$1" | cut -d' ' -f2)"; echo "${seq:-0}" > "$SCORER_HOME/.seq_$1"
  herdr agent prompt "$1" "$3" >/dev/null 2>&1 || { sleep 2; herdr agent prompt "$1" "$3" >/dev/null 2>&1; } \
    || log "prompt-FAILED $1 $2"
  log "prompt $1 $2 :: ${3:0:70}"
}
W() {  # <agent> [timeout_s]  wait until the agent settled AFTER the last P (seq moved on, status idle/done)
  local a="$1" t="${2:-420}" s0 st seq vis n=0 start=$SECONDS
  s0="$(cat "$SCORER_HOME/.seq_$a" 2>/dev/null || echo 0)"
  while [ $((SECONDS-start)) -lt "$t" ]; do
    read -r st seq <<<"$(aget "$a")"
    if [ "$st" = blocked ]; then
      vis="$(herdr pane read "$(herdr agent get "$a" | jq -r .result.agent.pane_id)" --source visible 2>/dev/null)"
      if grep -q "Do you want" <<<"$vis" && [ $n -lt 8 ]; then
        n=$((n+1)); log "blocked $a permission -> approve once :: $(grep -m1 -E 'Bash|Write|Edit|Fetch' <<<"$vis" | tr -s ' ' | cut -c1-70)"
        herdr agent send-keys "$a" enter >/dev/null; sleep 3; continue
      fi
      log "blocked $a (not a permission prompt) -> leaving it"; return 1
    fi
    if [ "${seq:-0}" -gt "$s0" ] && { [ "$st" = idle ] || [ "$st" = done ]; }; then log "settled $a"; return 0; fi
    sleep 3
  done
  log "timeout $a status=$st"; return 1
}
working_after_prompt() {  # <agent> <max_s>  wait until the agent reports working
  local i; for i in $(seq 1 "$2"); do [ "$(aget "$1" | cut -d' ' -f1)" = working ] && return 0; sleep 1; done; return 1
}
NOQ="Do not ask clarifying questions; make reasonable choices and proceed."

interrupt_when_output() {  # <agent> <literal>  Esc once assistant output (literal) is visible
  # Esc only after assistant output started: an Esc before any output makes Claude restore the prompt
  # into the input box with no interrupt marker (reproduces a parser gap: phantom 0-s turn, no interrupt).
  working_after_prompt "$1" 20
  herdr pane wait-output "$(herdr agent get "$1" | jq -r .result.agent.pane_id)" --match "$2" \
    --source visible --timeout 30000 >/dev/null 2>&1; sleep 2
  if [ "$(aget "$1" | cut -d' ' -f1)" = working ]; then herdr agent send-keys "$1" esc >/dev/null; log "interrupt $1 (esc while working)"; else log "skip interrupt ($1 not working)"; fi
  sleep 4; W "$1" 60
}

cmd_drive() {
  need_herdr; load; log "drive-begin set=$SET"
  set_drive
  log "drive-done"; echo "PHASE DONE drive"
}

# ---------------------------------------------------------------- teardown / measure
agent_names() { local e; for e in "${AGENT_DIRS[@]}"; do echo "${e%%:*}"; done; }

cmd_teardown() {
  need_herdr; load
  local a v
  for a in $(agent_names); do
    if [[ "$a" == *codex* ]]; then
      herdr agent send-keys "$a" ctrl+c >/dev/null 2>&1; sleep 1; herdr agent send-keys "$a" ctrl+c >/dev/null 2>&1 && log "stop $a (ctrl+c x2)"
    else
      herdr agent send-keys "$a" esc >/dev/null 2>&1; sleep 0.5
      herdr agent prompt "$a" "/exit" >/dev/null 2>&1 && log "stop $a (/exit)"
    fi
  done
  sleep 4
  for a in $(agent_names); do
    v="WS_${a//-/_}"
    [ -n "${!v:-}" ] && herdr workspace close "${!v}" >/dev/null 2>&1 && log "close workspace ${!v}"
  done
  echo "PHASE DONE teardown"
}

cmd_measure() {
  "${SC[@]}" ingest --since 1d --cwd-prefix "$LAB" && "${SC[@]}" check
  "${SC[@]}" report --day today > "$SCORER_HOME/reports/lab.md" && log "report -> $SCORER_HOME/reports/lab.md"
  cmd_compare
  echo "PHASE DONE measure"
}

cmd_compare() {  # ground truth from lab-actions.log (this run only) vs scorer.db, per project dir
  local DB="$SCORER_HOME/scorer.db" e a dir exp_p exp_i exp_q exp_h ses turns intr gt
  gt="$(awk -v r="run-begin root=$LAB " 'index($0, r) {buf=""} {buf = buf $0 "\n"} END {printf "%s", buf}' "$ACTLOG")"
  grep -q "run-begin root=$LAB " <<<"$gt" || { echo "[lab] no run-begin for $LAB in $ACTLOG"; return 1; }
  printf '\n%-16s %-26s %8s %8s | %8s %8s %8s %8s\n' agent project exp_turn got_turn exp_intr got_intr queued handedit
  for e in "${AGENT_DIRS[@]}"; do
    a="${e%%:*}"; dir="$LAB/${e#*:}"
    exp_p=$(grep -c " prompt $a " <<<"$gt"); exp_i=$(grep -c " interrupt $a " <<<"$gt")
    exp_q=$(grep -c " prompt $a queued" <<<"$gt"); exp_h=$(grep -cE " hand-edit(-trivial)? $a " <<<"$gt")
    ses=$(sqlite3 "$DB" "select count(*) from session where cwd='$dir'")
    turns=$(sqlite3 "$DB" "select count(*) from turn t join session s on s.id=t.session_id where s.cwd='$dir'")
    intr=$(sqlite3 "$DB" "select count(*) from turn t join session s on s.id=t.session_id where s.cwd='$dir' and t.end_source='interrupt'")
    printf '%-16s %-26s %8s %8s | %8s %8s %8s %8s   sessions=%s\n' "$a" "${e#*:}" "$exp_p" "$turns" "$exp_i" "$intr" "$exp_q" "$exp_h" "$ses"
  done
  echo "(exp_turn = prompts sent incl. queued; a queued prompt may legitimately merge into the running turn)"
  echo "measured per-turn detail (cwd prefix $LAB):"
  sqlite3 -separator ' | ' "$DB" "select replace(s.cwd,'$LAB/',''), t.idx, t.prompt_kind, t.initiator, coalesce(t.end_source,'-'), substr(replace(t.prompt_text,char(10),' '),1,60)
    from turn t join session s on s.id=t.session_id where s.cwd like '$LAB/%' order by s.cwd, t.idx"
}

cmd_reset() {
  read -r -p "delete $LAB and $SCORER_HOME? [y/N] " a; [ "$a" = y ] || exit 1
  local e p; [ -d "$LAB" ] && for e in "${AGENT_DIRS[@]}"; do
    p="$LAB/${e#*:}"; [ -d "$p/.claude" ] && "${SC[@]}" uninstall --project "$p" --yes >/dev/null 2>&1; done
  rm -rf "$LAB" "$SCORER_HOME"
}

. "$SETFILE"

case "${1:-}" in
  setup) cmd_setup ;; launch) cmd_launch ;; drive) cmd_drive ;; teardown) cmd_teardown ;;
  measure) cmd_measure ;; compare) cmd_compare ;; reset) cmd_reset ;;
  all) cmd_setup && cmd_launch && cmd_drive; cmd_teardown; cmd_measure ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
