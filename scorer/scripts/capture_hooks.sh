#!/usr/bin/env bash
# Capture REAL hook payloads from Claude Code and Codex (spec §6c).
#
# Usage: scripts/capture_hooks.sh [OUT_DIR]
#   Writes $OUT/claude/hook_payloads.jsonl and $OUT/codex/hook_payloads.jsonl
#   (raw, NOT redacted -- redact before committing; see tests/fixtures/hook_payloads/).
#
# Never touches ~/.claude/settings.json or ~/.codex/{hooks.json,config.toml}:
# Claude gets --settings <tmp>, Codex gets CODEX_HOME=<tmp> with a copy of auth.json.
set -u

OUT="${1:-$(mktemp -d -t scorer-capture)}"
mkdir -p "$OUT/claude" "$OUT/codex"
REPO="$(mktemp -d -t scorer-capture-repo)"
EVENTS="SessionStart UserPromptSubmit PostToolUse Stop SessionEnd"

git -C "$REPO" init -q
printf 'print("hello")\n' > "$REPO/hello.py"
git -C "$REPO" add hello.py
git -C "$REPO" -c user.email=x@x -c user.name=x commit -qm init

# hooks file: {"hooks": {Event: [{"matcher"?, "hooks": [{"type":"command","command": "jq -c . >> F"}]}]}}
make_hooks() {  # $1 = payload file
  python3 - "$1" "$EVENTS" <<'PY'
import json, shlex, sys
out, events = sys.argv[1], sys.argv[2].split()
cmd = f"jq -c . >> {shlex.quote(out)}"
hooks = {}
for ev in events:
    g = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
    if ev == "PostToolUse":
        g = {"matcher": "*", **g}
    hooks[ev] = [g]
print(json.dumps({"hooks": hooks}, indent=2))
PY
}

echo "== claude (repo $REPO)"
make_hooks "$OUT/claude/hook_payloads.jsonl" > "$OUT/claude/settings.json"
(cd "$REPO" && env -u SUPERSET_HOME_DIR claude -p "say hi, then run 'ls' with the Bash tool" \
    --settings "$OUT/claude/settings.json" --allowedTools "Bash(ls)" ) || echo "claude exited $?"
wc -l "$OUT/claude/hook_payloads.jsonl" 2>/dev/null || echo "no claude payloads"

echo "== codex"
CH="$OUT/codex/home"
mkdir -p "$CH"
cp ~/.codex/auth.json "$CH/auth.json"
# Codex only runs hooks with [features] hooks = true. It also tracks hook trust
# (sha256 per hook in config.toml [hooks.state]); untrusted hooks may be skipped.
# (codex 0.160 dropped --full-auto; use -s workspace-write.)
# NOTE: with this fresh CODEX_HOME the hooks are untrusted, so this step currently
# yields 0 payloads. A human who wants a real capture must add
# --dangerously-bypass-hook-trust to the codex exec line below themselves.
printf '[features]\nhooks = true\n' > "$CH/config.toml"
make_hooks "$OUT/codex/hook_payloads.jsonl" > "$CH/hooks.json"
env -u SUPERSET_HOME_DIR CODEX_HOME="$CH" codex exec -C "$REPO" --skip-git-repo-check -s workspace-write \
    "say hi, then run 'ls' in the shell" </dev/null || echo "codex exited $?"
wc -l "$OUT/codex/hook_payloads.jsonl" 2>/dev/null || echo "no codex payloads"

echo "OUT=$OUT"
