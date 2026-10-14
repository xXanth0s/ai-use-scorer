# Demo video brief — scorer (3:00, professional)

You own `demo/` only. Another agent (`builder`) owns `src/`, `tests/`, `STATUS.md`; never edit those. Commit only files under `demo/` and this brief. The dashboard runs at http://127.0.0.1:8811 and may restart while `builder` works; retry.

## Inputs (arrive while you set up)
- `demo/record.mjs`, `demo/titles/`, `demo/assemble.sh`, `demo/README.md`: a Playwright click-through recorder with fake cursor, title cards and ffmpeg assembly, created by a subagent. Wait until `demo/README.md` exists (poll every 30 s, max 20 min), then take ownership.
- `docs/spec/video-storyboard.md`: storyboard with timestamps, narration script (≤420 words), tooling recommendation. Will be written by the planner when ready; until then, set up tooling.
- `docs/spec/video-usecases.md`: six use cases + the meta twist (the tool recorded its own build day) + taglines.

## What to produce
`demo/out/demo.mp4`, 1920x1080, 30 fps, ≤3:00, with:
1. Cold open title (the problem: devs feel faster with agents, measured slower; nobody measures the human).
2. What we built (one title card + 10 s of the hooks firing in a terminal: `tail -f ~/.scorer/events.jsonl | jq -c '{at,event,tool:.payload.tool_name}'` next to one `claude -p` run; use `scripts/demo_e2e.sh` with `SCORER_HOME=/tmp/scorer-demo`; record with `vhs` if installed, else Playwright against a terminal in the browser is not possible, so use `asciinema` + `agg`, else a plain `screencapture -v` of a Herdr pane).
3. Dashboard walkthrough from the recorder: hero + scored cards → timeline → Sessions → Trends → Leverage → Ask AI with a real streamed answer (server needs `ANTHROPIC_API_KEY`; the builder's STATUS says the chat works).
4. The meta twist: the day shown is the hackathon build itself; call out 2–3 numbers from `docs/spec/video-usecases.md`.
5. Candidate mode use case: one title card + the three traps, 15 s.
6. Close: tagline + repo.

Narration: macOS `say -v` with a good voice (check `say -v '?'` for Siri/enhanced voices; prefer an enhanced English voice) unless `ELEVENLABS_API_KEY` is set; write `demo/narration.txt` per beat, generate `demo/out/vo/<beat>.aiff`, convert to m4a, align beats to the recorded pauses (adjust pause lengths in `record.mjs` to the VO durations, not the other way round). Burn captions from `demo/narration.srt`. Light theme. No music unless a royalty-free track is already on disk.

## Working rules
- Verify every step produced a file; `ffprobe` the final mp4 for duration and resolution and print it.
- Keep a `demo/PROGRESS.md` with what is done, what is missing, and how to re-render in one command (`bash demo/build.sh`).
- When the storyboard arrives, re-time the beats to it. If it never arrives in 20 min, write your own from the use-case file and the spec and proceed.
