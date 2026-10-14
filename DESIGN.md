---
# gstack: design-md-format=spec
name: scorer
description: An instrument log for one working day with coding agents; plain paper, two agent inks, numbers set in mono, one sentence that tells you what to change.
colors:
  surface: "#F5F6F3"
  surface-raised: "#FFFFFF"
  surface-sunk: "#ECEEEA"
  text: "#15181C"
  text-muted: "#566070"
  rule: "#D5D9D2"
  primary: "#15181C"
  on-primary: "#F5F6F3"
  focus: "#2F55D4"
  claude: "#B8740F"
  claude-soft: "#F1DFC2"
  codex: "#1D6A8A"
  codex-soft: "#CFE3EC"
  overlap: "#15181C"
  success: "#2D7A46"
  warning: "#9A6200"
  error: "#B3261E"
  dark-surface: "#121417"
  dark-surface-raised: "#1A1D21"
  dark-surface-sunk: "#0D0F11"
  dark-text: "#ECEDEA"
  dark-text-muted: "#A0A7B1"
  dark-rule: "#2E333A"
  dark-focus: "#86A4FF"
  dark-claude: "#E3A54A"
  dark-claude-soft: "#3A2C17"
  dark-codex: "#5BB4DA"
  dark-codex-soft: "#16313D"
  dark-success: "#62C383"
  dark-warning: "#E5B556"
  dark-error: "#F26B5E"
  chat-user-bubble: "#E4E7E1"
  chat-cite: "#E6ECFB"
  chat-cite-text: "#22409F"
  scrim: "#15181C66"
  dark-chat-user-bubble: "#262B31"
  dark-chat-cite: "#1F2A47"
  dark-chat-cite-text: "#B4C6FF"
  dark-scrim: "#000000A0"
typography:
  verdict:
    fontFamily: Hanken Grotesk
    fontWeight: 700
    fontSize: clamp(1.75rem, 1.2rem + 1.6vw, 2.5rem)
    lineHeight: 1.15
    letterSpacing: -0.015em
  heading:
    fontFamily: Hanken Grotesk
    fontWeight: 700
    fontSize: 1.375rem
    lineHeight: 1.25
    letterSpacing: -0.005em
  body:
    fontFamily: Hanken Grotesk
    fontWeight: 400
    fontSize: 1.0625rem
    lineHeight: 1.5
  label:
    fontFamily: Hanken Grotesk
    fontWeight: 600
    fontSize: 0.875rem
    lineHeight: 1.3
    letterSpacing: 0.01em
  metric-glyph:
    fontFamily: JetBrains Mono
    fontWeight: 700
    fontSize: 2.25rem
    lineHeight: 1
  mono:
    fontFamily: JetBrains Mono
    fontWeight: 500
    fontSize: 0.9375rem
    fontFeature: tnum
rounded:
  none: 0px
  sm: 3px
  md: 6px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  2xl: 64px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.sm}"
  button-quiet:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    borderColor: "{colors.rule}"
    rounded: "{rounded.sm}"
  input:
    borderColor: "{colors.text-muted}"
    rounded: "{rounded.sm}"
  lane-bar-claude:
    backgroundColor: "{colors.claude}"
    rounded: "{rounded.sm}"
  lane-bar-codex:
    backgroundColor: "{colors.codex}"
    rounded: "{rounded.sm}"
  detail-panel:
    backgroundColor: "{colors.surface-raised}"
    borderColor: "{colors.rule}"
    rounded: "{rounded.md}"
  evidence-link:
    textColor: "{colors.text}"
  chat-panel:
    backgroundColor: "{colors.surface-raised}"
    borderColor: "{colors.rule}"
    width: 400px
  chat-user-bubble:
    backgroundColor: "{colors.chat-user-bubble}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: 8px 12px
  chat-assistant-message:
    textColor: "{colors.text}"
    fontFamily: "{typography.body.fontFamily}"
  chat-thinking-steps:
    textColor: "{colors.text-muted}"
    fontFamily: "{typography.label.fontFamily}"
  chat-tool-card:
    backgroundColor: "{colors.surface}"
    borderColor: "{colors.rule}"
    rounded: "{rounded.sm}"
    fontFamily: "{typography.mono.fontFamily}"
  chat-citation-pill:
    backgroundColor: "{colors.chat-cite}"
    textColor: "{colors.chat-cite-text}"
    rounded: "{rounded.full}"
    fontFamily: "{typography.mono.fontFamily}"
    padding: 0 6px
  chat-peek-overlay:
    backgroundColor: "{colors.surface-raised}"
    borderColor: "{colors.text}"
    rounded: "{rounded.md}"
  chat-composer:
    backgroundColor: "{colors.surface-raised}"
    borderColor: "{colors.text-muted}"
    rounded: "{rounded.md}"
---

# scorer

## Overview

**Creative North Star:** an instrument log for one working day. The page reads like a flight-recorder printout: one strip shows the whole day, one sentence above it says the one thing to change, and every number can be traced back to the turn that produced it.

**Product context:** `scorer` is a local Python CLI that parses Claude Code and Codex transcripts plus hook events into SQLite and scores how a human drives coding agents. The dashboard (`scorer serve`) is the end-of-day report and, in candidate mode, the profile a hiring panel reads. Users: engineers reviewing their own day, and hiring managers reviewing a candidate session. Category peers: time trackers (RescueTime, WakaTime), observability timelines (Honeycomb traces, Chrome performance panel), sports telemetry.

**Mode per surface:** Operate (day picker, evidence drill-down) and Read (verdict, metrics, trend). No Persuade surface: there is no marketing page.

**Reference sites:** none. Competitive research was skipped (unattended run, 4-hour hackathon); the direction comes from design knowledge.

**Memorable thing (drives every decision):** "One strip shows where the whole day went, and the sentence above it tells you the one thing to change tomorrow."

**Key characteristics:**
- The timeline is the hero, at full width, above every number.
- The verdict is one sentence in large type with its number set in mono. It is never a card or a chip.
- The scored metrics form a ledger: a large mono letter (V, H, B, C), the value, a 7-day strip, and evidence links. They are rows, not cards.
- Two agent inks, ochre for Claude and petrol for Codex. Agent identity is never carried by color alone: every lane has a text label and Codex bars carry a hatch.
- Missing data reads as an em dash or as "judge pending", never as zero.

## Colors

**Strategy:** Restrained. The neutrals carry the page. Color appears only where it carries meaning: the two agent inks, the three semantic states (success means verified, warning means unverified, error means failed or blocked), and the focus ring. There is no brand accent; the primary button is ink on paper.

**Light or dark:** light by default. The use scene is a laptop on a projector in a lit meeting room, or a desk at the end of the day, and projectors wash out dark grounds and thin light text. Dark is fully specified (the `dark-*` tokens) and follows `prefers-color-scheme`, a header toggle, or `?theme=dark`.

Interaction and emphasis: links and evidence references are text-colored with an underline. Focus uses the `focus` cobalt ring (2px, offset 2px). Overlap is drawn with `overlap` ink as a band above the lanes plus a hatched underlay behind the overlapping minutes, so it reads even in grayscale. In dark mode surfaces step up in lightness (sunk < surface < raised) instead of adding shadows, and the agent inks are lifted for contrast against the dark ground. They are not inverted.

Contrast: in both themes `text` on `surface` is at least 14:1 and `text-muted` on `surface` at least 5.5:1. The agent inks on their own surface are at least 3:1 as non-text graphics.

## Typography

**Hanken Grotesk** (variable 400–800, SIL OFL, Google Fonts) carries the verdict, headings, body and labels. It is a plain grotesk with open apertures. It reads at projector distance without the template feel of Inter or Roboto, and its slightly condensed caps keep long session labels short.

**JetBrains Mono** (variable 400–700, SIL OFL, Google Fonts) carries every number and every quoted prompt or command, with tabular figures (`tnum`). It is the mono engineers already read in their editor, which suits the content: clipped prompts and bash commands.

Loading: both faces are vendored into `src/scorer/static/fonts/` as Latin-subset variable woff2 (no CDN at runtime), declared with `@font-face` and `font-display: swap`. Fallbacks are `ui-sans-serif, system-ui` and `ui-monospace, Menlo`.

Scale: verdict 28–40px fluid, heading 22px, body 17px (projector minimum), label 14px, mono 15px, metric glyph 36px. Nothing on screen is under 13px, including timeline axis ticks. Labels are sentence case, not tracked uppercase.

## Layout

- Max width 1440px with a 24px gutter (16px under 720px).
- Order, top to bottom: header bar (product mark, day picker, theme toggle), verdict block, day timeline (full width), then a two-column band with the scored ledger (about 7/12) and a sticky turn-detail panel (about 5/12), then **Leverage** (subagents and tooling use, labeled "not scored"), then diagnostics (full width, two-column definition grid), then the 7-day trend (small multiples, one row per metric).
- **Analyst chat panel (brief item 7b):** a 400px right-hand column beside the day timeline and ledger (from 1200px up). The report column keeps at least 760px. Under 1200px the panel becomes a slide-over drawer opened from an "Ask" button in the header, sitting over a `scrim`. The panel scrolls independently and the composer is pinned to its bottom.
- Under 960px the band stacks and the detail panel becomes inline below the ledger.
- Vertical rhythm: 40px between sections, 16px inside them. Sections are separated by a 1px `rule` line and a heading, not by boxed cards.

## Elevation & Depth

Flat. The only raised element is the turn-detail panel: `surface-raised` with a 1px `rule` border and in light mode a soft offset shadow (`0 2px 8px rgb(0 0 0 / 0.06)`). There is no shadow in dark mode and no zero-offset glows anywhere.

## Shapes

Radii stay small and technical: 3px on bars, buttons and inputs, 6px on the detail panel, full only on the 8px agent dot in the legend. Inside the panel, nested elements use radius 3px (6 − 3).

## Components

- **Timeline lane:** a 28px-tall row per session. The label column shows the agent name, repo basename and session short id. Bars span prompt_at to agent_finished_at. Claude bars are solid ochre; Codex bars are solid petrol with a 45° hatch. An open-ended turn (no finish time) fades out over its last 20%. An interruption is a 2px error-colored tick with a small triangle cap. Gaps are empty paper with a dotted baseline, and breaks over 15 minutes are labeled with their duration. Hover gives a 2px outline in `text`; focus-visible uses the focus ring; click opens the turn detail.
- **Overlap band:** a 6px ink band above all lanes covering every minute with ≥2 active sessions.
- **Ledger row:** glyph, name, value in mono, a short definition in muted text, a 7-day mini strip, and evidence links (`turn #id`). Hover underlines the row name. An empty metric shows "—" and the reason ("judge pending", "no edits today").
- **Turn detail panel:** session, agent and time span; the clipped prompt in mono (≤80 chars, server-clipped); a tool-call list with name, kind, bash_intent chip and error state; a verification tier label (verified pass / verified fail / inspected / none) colored with the semantic tokens and always paired with text.
- **Leverage table (not scored):** one row each for subagents, skills, slash commands, MCP tools and plugins. The columns are initiator (you asked / agent chose / default) and total, with numbers in mono, followed by a human-share row. Below the table, one muted mono line lists the top names (`skill:review (human) ×3`). It sits next to subagents and stays visually quieter than the scored ledger: no glyphs, no evidence pills, and a "not scored" label in muted text after the heading. Initiator columns are text-labeled, never color-only.
- **Chat panel (analyst agent):** the transcript reads like the report, not a messenger app.
  - *User message:* a right-aligned `chat-user-bubble` (max 85% width, 6px radius) in body type. This is the only bubble.
  - *Assistant message:* unboxed and full width, rendered markdown in body type (headings capped at 17px/700, lists, inline code in mono on `surface-sunk`). Streaming text appends in place with no typing cursor animation.
  - *Thinking steps:* a single collapsed line in label type and muted color ("Checked 3 sources ›") summarizing the tool calls before the answer. Expanding it reveals the tool cards. It is collapsed by default and while streaming shows the current step name.
  - *Tool card:* a flat `surface` box with a 1px rule border and 3px radius. It shows the tool name in mono, the clipped input (≤80 chars), and a one-line result summary with an ok/error word in a semantic color. There are no icons in circles.
  - *Citation pill:* `[n]` set inline in mono on `chat-cite` with a full radius. Hovering or focusing opens the *peek overlay*. Clicking jumps to the turn: it selects the timeline bar and opens turn detail, the same path as evidence links.
  - *Peek overlay:* a 320px popover anchored to the pill with a 1px `text` border and 6px radius. It shows the turn #, session/agent, time span, the clipped prompt and the verification tier. Esc closes it.
  - *Composer:* a multiline textarea (body type) with a `text-muted` border, 6px radius and a primary "Ask" button. Enter sends and Shift+Enter adds a newline. Disabled while streaming, with a "Stop" quiet button in its place.
  - *No-key state:* the panel shows one muted sentence ("Set ANTHROPIC_API_KEY and restart `scorer serve` to ask about your day.") and a disabled composer. Never hide the panel silently.
- **Day picker:** a native `<select>` of days that have data, plus previous/next buttons. Keyboard reachable.
- **Buttons:** quiet by default (raised surface, rule border). Hover darkens the border to `text`; active shifts down 1px; disabled is 50% opacity with no hover.

## Do's and Don'ts

- Do keep the timeline first and full width; never push it below the fold behind stat tiles.
- Do pair every color with text or a pattern (agent label, hatch, tier word).
- Do set every number in JetBrains Mono with tabular figures.
- Do show "—" with a reason for missing data; never render a fabricated zero.
- Do clip every human string server-side to 80 characters.
- Don't add stat-tile rows of big numbers with tiny labels; the ledger is the summary.
- Don't box sections in cards, and never nest a card inside a card.
- Don't use glow, gradients or a brand accent hue; color is reserved for agents and states.
- Don't use tracked uppercase micro-labels or a kicker above headings.
- Don't animate the timeline on load; the day is a record, not a show.

## Motion

- **Approach:** minimal-functional.
- **Easing:** enter(ease-out) exit(ease-in) move(ease-in-out)
- **Duration:** micro 80ms (hover outline), short 180ms (detail panel content swap), medium 300ms (scroll-to-turn highlight fade)
- **The one authored moment:** clicking an evidence link scrolls the timeline bar into view and pulses its outline once (300ms), so the number and the minute it came from connect.
- `prefers-reduced-motion`: all transitions off and the highlight becomes static.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-10-14 | Initial design system created | Created by /design-consultation (unattended subagent run) from the build brief and metrics spec; no competitive research. |
| 2026-10-14 | Light default, full dark spec | Use scene is a projector in a lit room; dark follows the OS or the toggle. |
| 2026-10-14 | Hanken Grotesk + JetBrains Mono, vendored | Verified on Google Fonts (OFL); vendored so the dashboard works offline with no CDN. |
| 2026-10-14 | Analyst chat panel added (brief 7b) | Unboxed assistant prose plus a single user bubble keeps the panel reading like the report; citations reuse the evidence-link path so chat answers and metrics point at the same turns. |
| 2026-10-14 | "Leverage" diagnostic (tooling use by initiator) | Shows whether the human or the agent reached for skills, commands, MCP and plugins; diagnostic only, so it is styled quieter than the scored ledger. |
| 2026-10-14 | Agents as ochre (Claude) and petrol hatched (Codex) | Distinguishable in color, grayscale and print; no vendor branding. |
