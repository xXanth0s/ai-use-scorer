#!/usr/bin/env bash
# Demo fallback (spec §7): render the current report into report_fallback.txt at the repo root.
#
#   scripts/report_fallback.sh            # last 7 days (default)
#   DAYS=1 scripts/report_fallback.sh     # today only
#   SCORER_NO_LLM=1 scripts/report_fallback.sh   # skip the LLM narrative
#   scripts/report_fallback.sh --no-judge        # extra args go to `scorer report`
#
# Reads the DB under $SCORER_HOME (default ~/.scorer). Run `scorer ingest` first for fresh data.
set -euo pipefail
cd "$(dirname "$0")/.."
DAYS="${DAYS:-7}"
out="report_fallback.txt"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if [ "$DAYS" = "1" ]; then
  uv run scorer report --day today "$@" > "$tmp"
else
  uv run scorer report --days "$DAYS" "$@" > "$tmp"
fi
mv "$tmp" "$out"
trap - EXIT
echo "wrote $out ($(wc -l < "$out" | tr -d ' ') lines)" >&2
