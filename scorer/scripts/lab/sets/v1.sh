# project set v1 (the original four): todo CLI, csv stats + its worktree, codex node kata.
# Sourced by orchestrate.sh. Defines AGENT_DIRS, set_setup, set_launch, set_drive.
# Note: launching codex accepts codex's folder trust, which writes ~/.codex/config.toml.
AGENT_DIRS=(lab-todo:lab-todo-cli lab-csv:lab-csv-stats lab-hist:lab-csv-hist lab-codex:lab-codex-kata)

set_setup() {
  # 1) todo CLI (claude) + a project slash command -> human-initiated tooling event
  py_project "$LAB/lab-todo-cli" lab-todo-cli
  runtests_cmd "$LAB/lab-todo-cli"
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
  install_hooks lab-todo-cli lab-csv-stats lab-csv-hist
  log "setup projects=lab-todo-cli,lab-csv-stats,lab-csv-hist(worktree feat/hist),lab-codex-kata"
}

set_launch() {
  start_agent lab-todo-cli  "$LAB/lab-todo-cli"  lab-todo  "$(claude_cmd)"
  start_agent lab-csv-stats "$LAB/lab-csv-stats" lab-csv   "$(claude_cmd)"
  start_agent lab-csv-hist  "$LAB/lab-csv-hist"  lab-hist  "$(claude_cmd)"
  start_agent lab-codex-kata "$LAB/lab-codex-kata" lab-codex \
    "env -u SUPERSET_HOME_DIR codex -C $LAB/lab-codex-kata -s workspace-write -a never -c model_reasoning_effort=\"low\" --no-alt-screen"
}

set_drive() {
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
  interrupt_when_output lab-csv "⏺ Update"
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
}
