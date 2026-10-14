# Demo video — progress

Re-render (once demo/build.sh lands): `bash demo/build.sh`

## Direction (final, from the user)
Ari's Next.js app (ari-version, standalone :3100) is the main frame (left 1344x1080), our harness terminal is the
right pane (576x1080): hooks firing → `scorer ingest` → `scorer judge --day` → export into Ari's app → report appears.
VO is the master clock. Our :8811 dashboard footage is fallback only (MODE=fallback).

## Beats (VO ids in demo/narration.txt)
| beat | ids | frame |
|---|---|---|
| Cold open | p1a–p1d | 4 cards: every day / measures the agent / measures you / scorer (METR cut) |
| Hooks | p2 | split: Ari left, harness right (hooks phase, sped up, badge) |
| Report in Ari's app | p3a–p3e | split: Sessions → report KPIs (ours + theirs) → timeline → evidence; right: ingest/judge/export |
| Ask the analyst | p4a–p4c | split; streaming wait compressed ≤2.5 s with badge |
| Coming next | p5 | card (future tense) + candidate-day still labelled "Preview · not shipped" |
| Meta twist | p6 | build-day still + numbers card from real `scorer report --day 2026-10-14` (meta.json) |
| Close | p7 | `uv run scorer install` typed, never run; end card |

## Done
- Tooling: ffmpeg 7.1.1 (no drawtext/libass → all text is PNG overlays), node 23, vhs 0.12.1 + ttyd + tmux, jq, claude.
  `say` has no enhanced voices installed → Samantha @150 (VOICE env). No ElevenLabs key. No royalty-free music → none.
- demo/terminal/hooks.tape (+ setup.sh, tmux.conf): real e2e run, ALL PASS → out/hooks.mp4 (fallback right pane).
- demo/terminal/install.tape → out/install.mp4.
- demo/vo.sh: narration.txt → out/vo/*.m4a + durations.json.
- demo/record.mjs (fallback :8811/:8812 walkthrough): VO-driven holds, writes out/marks.json.
- demo/assemble.mjs: VO-timed timeline, narration.srt generated, caption strips, VO mix. Fixed: concat timebase mismatch
  and caption overlay frame drops (looped strip inputs).

## In progress (parallel subagents)
- harness pane clip (demo/terminal/harness.tape → out/harness.mp4 + marks)
- narration ¶3/¶4 rewrite for Ari's flow + p6 numbers from the real report (meta.json)
- demo/record.hdyai.mjs (waits for STATUS.md '## Bridge')
- split-screen compositor, placeholder left clip, candidate preview still, build.sh

## Open
- Repo URL for the end card: no git remote configured; card shows "ai-use-scorer" (REPO_URL env overrides).
