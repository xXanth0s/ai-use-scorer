# Design consultation: unattended decisions

`/design-consultation` was run by a subagent with no human present. Where the skill would have asked a question, this run chose the option below. `DESIGN.md` at the repo root is the resulting system; `preview.html` in this folder is the Path B preview.

| Skill step | Choice | Why |
|---|---|---|
| Phase 0: existing DESIGN.md | None found → new spec-format file | Fresh repo. |
| gstack upgrade prompt / CLAUDE.md routing injection | Skipped | Out of scope for a subagent that may not touch git or CLAUDE.md. |
| Q1: context + research | Context as briefed; **design knowledge only**, no competitive research | 4-hour hackathon; the product is a local tool with no competitors to differentiate against visually. |
| Memorable thing | "One strip shows where the whole day went, and the sentence above it tells you the one thing to change tomorrow." | Restates the brief's one job ("where the day went / what to change"). |
| Outside voices (Codex / Claude subagent) | Declined | Time budget; unattended. |
| Q2: proposal | Approved as drafted (Industrial/utilitarian instrument log, restrained palette, Hanken Grotesk + JetBrains Mono, minimal-functional motion) | See risks below. |
| Phase 5 preview | **Path B (HTML)**, written to `docs/design/preview.html` | Path A (AI mockups) needs a human to choose on a comparison board, and it is paid. |
| `$D extract` | Not run | It writes DESIGN.md itself; Path B uses the preview's CSS values. |
| Q-final | Approved: wrote `DESIGN.md` | |
| CLAUDE.md "Design System" section | Not written | Not owned by this subagent. The lead may append it. |

## Proposal summary

- **Aesthetic:** industrial/utilitarian "instrument log" with minimal decoration and a grid-disciplined layout.
- **Color:** restrained. Paper `#F5F6F3`, ink `#15181C`, two agent inks (Claude ochre `#B8740F`, Codex petrol `#1D6A8A` with hatch), and the semantic success/warning/error tokens. There is no brand accent.
- **Type:** Hanken Grotesk (verdict, headings, body, labels) and JetBrains Mono (all numbers, prompts, commands). Both are vendored as woff2.
- **Spacing:** 4px base. Comfortable density: 17px body for the projector, 40px between sections.
- **Motion:** minimal-functional. One authored moment: an evidence link scrolls to its timeline bar and pulses it.

**Safe choices:** a time axis left to right with one lane per session (the convention of every trace viewer), a light default, and a native day `<select>`.

**Risks taken:**
1. The timeline sits above all numbers as the hero. There is no stat-tile row. The cost is that the scored numbers sit one scroll lower on small screens.
2. The scored metrics are a ledger with large mono letter glyphs rather than cards. It reads as a report, but it is less skimmable than tiles.
3. Agent identity is encoded as ink plus pattern (Codex is hatched), so the page works in grayscale on a washed-out projector. Bars get busier.

## Plan changes during the run

- **Coordinator, mid-run:** the final dashboard visuals will be chosen from 3+ mockups by another agent, so `/design-html` was **not run**. `src/scorer/static/index.html` is a minimal page that proves the endpoints and follows the DESIGN.md tokens.
- **Brief 7b:** the analyst chat panel is added to the system (tokens and component specs in DESIGN.md; specimen in `preview.html`).
- **Tooling use:** a "Leverage" diagnostic section (subagents plus skills/commands/MCP/plugins by initiator) is added to DESIGN.md. It is not scored.
- Screenshots: `screenshots/dashboard-minimal-{light,dark}.png` (fixture DB from `tests/test_serve.py`) and `screenshots/design-preview-light.png`.
