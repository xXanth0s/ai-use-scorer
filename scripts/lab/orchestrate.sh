#!/usr/bin/env bash
# scorer test lab, driven through herdr. Run INSIDE a herdr pane (HERDR_ENV=1).
#
#   scripts/lab/orchestrate.sh setup      # ~/scorer-lab projects + worktree + project-local hooks
#   scripts/lab/orchestrate.sh launch     # 4 herdr workspaces, start claude x3 + codex x1
#   scripts/lab/orchestrate.sh drive      # scripted prompts: correction, clarification, interrupt,
#                                         #   queued prompt, hand edit, asked/not-asked to test
#   scripts/lab/orchestrate.sh measure    # ingest --cwd-prefix, check, report -> ~/.scorer-lab/reports/lab.md
#   scripts/lab/orchestrate.sh teardown   # stop lab agents, close lab workspaces (dirs are kept)
#   scripts/lab/orchestrate.sh all        # setup launch drive teardown measure
#   scripts/lab/orchestrate.sh reset      # delete ~/scorer-lab and ~/.scorer-lab (asks first)
#
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

cmd_setup() {
  [ -e "$LAB" ] && die "$LAB exists; run '$0 reset' first or set LAB_ROOT"
  mkdir -p "$LAB"
  # 1) todo CLI (claude) + a project slash command -> human-initiated tooling event
  py_project "$LAB/lab-todo-cli" lab-todo-cli
  mkdir -p "$LAB/lab-todo-cli/.claude/commands"
  printf -- '---\ndescription: run the test suite\n---\nRun `python3 -m pytest -q` in the project root and report pass/fail counts in one line.\n' \
    > "$LAB/lab-todo-cli/.claude/commands/runtests.md"
  commit_all "$LAB/lab-todo-cli" "init"
  # 2) csv stats (claude) + 3) its git worktree on branch feat/hist (claude, parallel)
  py_project "$LAB/lab-csv-stats" lab-csv-stats
  printf 'name,score\nann,3\nbob,5\ncid,8\ndee,\n' > "$LAB/lab-csv-stats/sample.csv"
  commit_all "$LAB/lab-csv-stats" "init"
  git -C "$LAB/lab-csv-stats" worktree add -q "$LAB/lab-csv-hist" -b feat/hist
  # 4) node kata (codex; measured from rollouts only, no hooks)
  local k="$LAB/lab-codex-kata"; mkdir -p "$k"; git -C "$k" init -q -b main
  printf '{\n  "name": "lab-codex-kata",\n  "version": "0.1.0",\n  "type": "commonjs",\n  "scripts": {"test": "node --test"}\n}\n' > "$k/package.json"
  printf '# lab-codex-kata\n' > "$k/README.md"; printf 'node_modules/\n' > "$k/.gitignore"
  commit_all "$k" "init"
  # project-local claude hooks (settings.local.json is untracked -> install into the worktree too)
  for p in lab-todo-cli lab-csv-stats lab-csv-hist; do
    "${SC[@]}" install --project "$LAB/$p" --agent claude --yes >/dev/null || die "install failed for $p"
  done
  log "setup projects=lab-todo-cli,lab-csv-stats,lab-csv-hist(worktree feat/hist),lab-codex-kata"
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
  need_herdr; : > "$STATE"; log "launch-begin"
  start_agent lab-todo-cli  "$LAB/lab-todo-cli"  lab-todo  "$(claude_cmd)"
  start_agent lab-csv-stats "$LAB/lab-csv-stats" lab-csv   "$(claude_cmd)"
  start_agent lab-csv-hist  "$LAB/lab-csv-hist"  lab-hist  "$(claude_cmd)"
  start_agent lab-codex-kata "$LAB/lab-codex-kata" lab-codex \
    "env -u SUPERSET_HOME_DIR codex -C $LAB/lab-codex-kata -s workspace-write -a never -c model_reasoning_effort=\"low\" --no-alt-screen"
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

cmd_drive() {
  need_herdr; load; log "drive-begin"
  # t0: all four in parallel (csv + hist are the parallel worktree pair -> overlap)
  P lab-csv  task "Write csvstats.py: a CLI that reads a CSV file and prints count, mean, median and stdev of one numeric column given by name (try it on sample.csv mentally). Just write the code; do not run tests or any commands. $NOQ"
  P lab-hist task "Add histogram.py: function histogram(values, bins=5) returning bin counts, and render(values) that prints ASCII bars; add a CLI reading a numeric column from a CSV. Also write tests in tests/test_histogram.py with 4-5 cases. $NOQ"
  P lab-todo task "Create todo.py: a tiny todo CLI with commands add, list, done that stores tasks in todo.json. Parse sys.argv by hand (no libraries). Keep it short. $NOQ"
  P lab-codex task "Create roman.js exporting toRoman(n) for 1..3999, with node:test tests in roman.test.js. Run npm test at the end."
  # queued prompt: typed while lab-hist is still working
  working_after_prompt lab-hist 20; sleep 15
  if [ "$(aget lab-hist | cut -d' ' -f1)" = working ]; then P lab-hist queued "When you are done, also add a --width option that scales the bars."; else log "skip queued (lab-hist not working)"; fi

  # todo: hand edit between turns (outside the agent), then a correction
  W lab-todo
  echo "Hand-edited by the lab orchestrator." >> "$LAB/lab-todo-cli/README.md"
  log "hand-edit lab-todo README.md (append 1 line, outside agent)"
  P lab-todo correction "No, use argparse instead of parsing sys.argv by hand. Keep the same commands."

  # csv: clarification, then a long task interrupted with Escape, then a re-scoped follow-up
  W lab-csv
  P lab-csv clarification "To clarify: skip empty cells, and for an even number of values the median is the mean of the two middle values. Don't run anything."
  W lab-csv
  P lab-csv long-task "Add a --group-by COLUMN option that prints the stats per group, a --format json|table option, and thorough docstrings and type hints throughout csvstats.py. Don't run anything."
  # Esc only after assistant output started: an Esc before any output makes Claude restore the prompt
  # into the input box with no interrupt marker (reproduces a parser gap: phantom 0-s turn, no interrupt).
  working_after_prompt lab-csv 20
  herdr pane wait-output "$(herdr agent get lab-csv | jq -r .result.agent.pane_id)" --match "⏺ Update" \
    --source visible --timeout 30000 >/dev/null 2>&1; sleep 2
  if [ "$(aget lab-csv | cut -d' ' -f1)" = working ]; then herdr agent send-keys lab-csv esc >/dev/null; log "interrupt lab-csv (esc while working)"; else log "skip interrupt (lab-csv not working)"; fi
  sleep 4; W lab-csv 60
  P lab-csv after-interrupt "Never mind that. Only add a --format json option. Don't run anything."

  # codex follow-up
  W lab-codex
  P lab-codex followup "Also add fromRoman(s) with tests, and run npm test again."

  # todo: asked to test, then the project slash command (human tooling event)
  W lab-todo
  P lab-todo asked-to-test "Add tests for add, list and done in tests/test_todo.py (use a temp dir for todo.json) and run pytest to verify they pass."
  W lab-todo
  P lab-todo slash-command "/runtests"

  for a in lab-hist lab-csv lab-codex lab-todo; do W "$a"; done
  log "drive-done"; echo "PHASE DONE drive"
}

# ---------------------------------------------------------------- teardown / measure
cmd_teardown() {
  need_herdr; load
  for a in lab-todo lab-csv lab-hist; do
    herdr agent send-keys "$a" esc >/dev/null 2>&1; sleep 0.5
    herdr agent prompt "$a" "/exit" >/dev/null 2>&1 && log "stop $a (/exit)"
  done
  herdr agent send-keys lab-codex ctrl+c >/dev/null 2>&1; sleep 1; herdr agent send-keys lab-codex ctrl+c >/dev/null 2>&1 && log "stop lab-codex (ctrl+c x2)"
  sleep 4
  for v in WS_lab_todo WS_lab_csv WS_lab_hist WS_lab_codex; do
    [ -n "${!v:-}" ] && herdr workspace close "${!v}" >/dev/null 2>&1 && log "close workspace ${!v}"
  done
  echo "PHASE DONE teardown"
}

cmd_measure() {
  "${SC[@]}" ingest --since 1d --cwd-prefix "$LAB" && "${SC[@]}" check
  "${SC[@]}" report --day today > "$SCORER_HOME/reports/lab.md" && log "report -> $SCORER_HOME/reports/lab.md"
  echo "PHASE DONE measure"
}

cmd_reset() {
  read -r -p "delete $LAB and $SCORER_HOME? [y/N] " a; [ "$a" = y ] || exit 1
  [ -d "$LAB" ] && for p in "$LAB"/lab-todo-cli "$LAB"/lab-csv-stats "$LAB"/lab-csv-hist; do
    [ -d "$p" ] && "${SC[@]}" uninstall --project "$p" --yes >/dev/null 2>&1; done
  rm -rf "$LAB" "$SCORER_HOME"
}

case "${1:-}" in
  setup) cmd_setup ;; launch) cmd_launch ;; drive) cmd_drive ;; teardown) cmd_teardown ;;
  measure) cmd_measure ;; reset) cmd_reset ;;
  all) cmd_setup && cmd_launch && cmd_drive; cmd_teardown; cmd_measure ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
