# Harness pane (right 576x1080 of the final frame)

Re-record (one command, ~2 min): `bash demo/terminal/harness_record.sh`
Outputs `demo/out/harness.mp4` (576x1080, 30 fps) and `demo/out/harness.marks.json` (seconds in harness.mp4 time).

Sections, all real commands, nothing faked:
1. hooks: `bash scripts/demo_e2e.sh` (one real `claude -p` run, SCORER_HOME=/tmp/scorer-demo); top pane = live `tail -f events.jsonl`.
2. pipeline on a COPY of the demo DB in /tmp/scorer-harness (never ~/.scorer): `scorer ingest --since 1d` (cwd filter = this repo),
   `scorer judge --day 2026-10-14`, `scorer export-hdyai ... --out /tmp/scorer-harness/ari-sessions.sqlite`.
   The copy comes from a warm template (/tmp/scorer-harness-warm: demo DB + one ingest + judge, built on first run, `REWARM=1` rebuilds),
   so the recorded judge only labels turns that are new since then (keeps the clip short).
3. hosted: `scorer sync ... --url https://how-do-you-ai-analysis.vercel.app --dry-run $PAY` (real dry run, no HTTP; device not paired yet).
4. static card `demo/out/harness_card.png` (rendered by harness_card.mjs), 4 s, starts at mark `vercel_card`.

All printed output is counts/summary lines only (no prompt text, no session titles).

## Swapping in the real hosted sync clip
Drop the real clip at `demo/out/harness-sync-real.mp4` (576x1080, 30 fps). The assembler should then cut harness.mp4 at
mark `sync` and append harness-sync-real.mp4 instead of the dry-run + card section (`sync` .. `end`).

Marks: hooks_start, hooks_end (SessionEnd visible), ingest, judge, export, export_done, sync, sync_done (= done), vercel_card, end.
Enter marks come from a bash DEBUG trap stamp, done marks from PROMPT_COMMAND, hooks_end from a watcher on events.jsonl;
wall clock is mapped to video time linearly between the `show` and `end` stamps (`scale` in the JSON).
