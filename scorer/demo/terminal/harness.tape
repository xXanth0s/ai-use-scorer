# Right-hand "harness" pane (576x1080): hooks firing live (top) + ingest/judge/export (bottom).
# Re-record: bash demo/terminal/harness_record.sh   (warms the DB copy, records, writes marks)
Output "/tmp/scorer-harness-raw.mp4"
Set Shell bash
Set FontSize 16
Set FontFamily "JetBrains Mono"
Set Width 576
Set Height 1080
Set Padding 18
Set Framerate 30
Set TypingSpeed 40ms
Set Theme { "name": "sherpa-light", "background": "#fbf6ee", "foreground": "#1c2d22", "cursor": "#1c2d22", "selection": "#ddd9d3", "black": "#1c2d22", "red": "#802d11", "green": "#316c5a", "yellow": "#b36813", "blue": "#0369a1", "magenta": "#7a3e6d", "cyan": "#2f6f73", "white": "#4d5a4d", "brightBlack": "#4d5a4d", "brightRed": "#802d11", "brightGreen": "#316c5a", "brightYellow": "#b36813", "brightBlue": "#0369a1", "brightMagenta": "#7a3e6d", "brightCyan": "#2f6f73", "brightWhite": "#1c2d22" }

Hide
Type "bash demo/terminal/harness_setup.sh && tmux attach -t harness" Enter
Sleep 3s
# stamp the moment the recording becomes visible (maps wall clock -> video time)
Type@1ms "_stamp show; clear" Enter
Show
Sleep 1500ms

# 1. hooks: one real claude -p run, events stream in above
Type "bash scripts/demo_e2e.sh | grep -E '^(PASS|FAIL|ALL|SOME)'"
Sleep 400ms
Enter
Wait+Screen@300s /ALL PASS|SOME FAILED/
Sleep 3s

# 2. pipeline on a copy of the demo DB
Ctrl+L
Sleep 500ms
Type "uv run scorer ingest --db $DB --since 1d"
Sleep 400ms
Enter
Wait+Screen@300s /run \d+: session=/
Sleep 2s
Type "uv run scorer judge --day 2026-10-14 --db $DB"
Sleep 400ms
Enter
Wait+Screen@300s /sources: /
Sleep 2s
Type "uv run scorer export-hdyai --day 2026-10-14 --db $DB --out $OUT"
Sleep 400ms
Enter
Wait+Screen@300s /scorer-demo-2026-10-14: /
Sleep 2s

# 3. hosted side: real dry run of the Vercel sync (builds the exact payload, no HTTP; device not paired yet)
Type "uv run scorer sync --day 2026-10-14 --db $DB --url $URL --dry-run $PAY"
Sleep 400ms
Enter
Wait+Screen@300s /dry_run=/
Sleep 3s
# stop capturing, then stamp the wall time of the last frame (linear wall->video calibration)
Hide
Type@1ms "_stamp end" Enter
Sleep 500ms
