---
# gstack: design-md-format=spec
name: scorer
description: Sherpa theme = Sherpa colors and the Sherpa/Lex copilot sidebar; the page layout is scorer's own (fluid, full width).
source: sherpa-vms apps/admin/app/globals.css + packages/ui/src/styles/tokens.css (read-only reference)
colors:
  background: "#ffffff"
  foreground: "#1c2d22"        # Pine
  app-background: "#fbf6ee"    # Cloud, lightened (page behind content)
  card: "#fefcf8"
  primary: "#1c2d22"
  primary-foreground: "#ffffff"
  muted: "#f7f7f5"
  muted-foreground: "#4d5a4d"
  accent: "#f0efed"
  border: "#ddd9d3"
  input: "#ddd9d3"
  ring: "#1c2d22"
  salmon: "#ffb299"            # only as a 2px quote rule (peek, prompt, tomorrow's change)
  success: "#316c5a"           # Grass, on success-muted #e6edeb, border #b7ccc5  = verified
  destructive: "#802d11"       # Oak, on #f1e7e2, border #d6b5a8                 = check failed / interrupted
  lane-claude: "#b36813"       # Clay (Sherpa chart-2)
  lane-codex: "#0369a1"        # Sherpa info blue
  dark-background: "oklch(0.145 0 0)"
  dark-app-background: "oklch(0.12 0 0)"
  dark-card: "oklch(0.205 0 0)"
  dark-foreground: "oklch(0.985 0 0)"
  dark-primary: "oklch(0.922 0 0)"
  dark-muted: "oklch(0.269 0 0)"
  dark-muted-foreground: "oklch(0.708 0 0)"
  dark-border: "oklch(1 0 0 / 10%)"
  dark-success: "#4ade80"
  dark-destructive: "#f0a58a"
  dark-lane-claude: "oklch(0.769 0.188 70.08)"
  dark-lane-codex: "#38bdf8"
typography:
  sans: '"GT America Standard", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'
  display: '"Oceanic Text", <sans>'
  numbers: 'sans with font-variant-numeric: tabular-nums'
  code: '"GT America Mono", ui-monospace, SFMono-Regular, Menlo, monospace  # commands and paths only'
  scale: { stat: 32px, verdict: 24px, page-title: 22px, section: 17px, body: 15px/1.6, table: 14px, label: 13px, meta: 12px, floor: 11px }
rounded: { sm: 4px, md: 6px, lg: 8px, full: 9999px }
---

# scorer: Sherpa theme

"Sherpa style" means Sherpa's **colors** and its **copilot sidebar**. The page layout is scorer's own. Tokens are plain CSS custom properties in `src/scorer/static/app.css`, with no Tailwind build and no CDN. Light and dark values are copied from Sherpa `apps/admin/app/globals.css` and `packages/ui/src/styles/tokens.css`.

**Fonts.** Sherpa's brand faces (Oceanic Text, GT America) are licensed and are not vendored. The stacks name them first and fall back to the system sans. Every number uses the sans face with tabular figures. Mono is only for commands and file paths. Headings are sentence case at normal sizes, with no small-caps or tracked labels. The old Hanken/JetBrains files stay in `static/fonts/` because `tests/test_serve.py` fetches one.

**Color rules.** Pine is text and the primary fill ("Ask AI" toggles from outline to filled). Salmon is only a 2px rule beside quoted text. There are two lane colors: Claude is Clay and Codex is info blue. Status color is only for verification: verified is Grass, check failed is Oak, and unverified is neutral. Deltas against the median are green when better and Oak when worse. Dark mode is Sherpa's `.dark` block.

## Layout

- A 48px header (wordmark, tab name, day picker, theme, "Ask AI"). Under it, an underline tab bar with hash routes: Today `#/today`, Sessions `#/sessions`, Trends `#/trends`, Leverage `#/leverage`. There is no left rail and no Candidate tab.
- The main area is fluid: the viewport minus the sidebar, with content up to 1400px and 32px padding. The sidebar is 26rem, collapsible, and open by default from 1200px. It overlays the page under 1100px, and its conversation survives tab switches.
- **Today:** the analyst narrative (fallback: the deterministic verdict) and "Tomorrow", then a band of 4 numbers (unverified edits, blocked min, corrections, active h), each with its delta against the 7-day median and evidence turn links. Then the full-width timeline (lanes grouped by worktree, sessions on demand, hover summary with "Ask about this in chat"). Then a 2-column grid: on the left, "Right now: unverified" and "Corrections by cause"; on the right, "Worst attention window" (with the hourly working/waiting/idle strip) and "Pattern to repeat" (with the top tools). Last, a diagnostics row with cost by model behind a disclosure.
- **Sessions:** one full-width table: worktree, an inline activity mini-timeline across the day, time, turns, active, a verified/failed/unverified stacked bar, blocked, cost. Selecting a row opens its turns (clipped prompt, tools, verification, gap after) and each turn's detail.
- **Trends:** small multiples in a responsive grid (V, B, C, active h, $/day, and C by cause), with 7/14/30 days and a dashed median line.
- **Leverage:** two-tone bars per skill / command / MCP server / plugin (by you / by the agent) with a category filter, plus subagents and a category table.

## Sidebar (Sherpa copilot pattern)

The empty state has an icon tile, "Ask scorer", one muted line and four suggested prompts. The user message is a Pine bubble on the right. The assistant's reply is unboxed, safe markdown. A shimmer line names the running step and then collapses to "N steps completed", with subagent steps nested. Result cards list the cited turns per tool. `[n]` pills open a peek overlay inside the sidebar with "Show in timeline". The composer is an auto-grow textarea where Enter sends, with a round send/stop button. Without an API key, the sidebar says how to set one.

## Don'ts

No gradients, glow, emoji, shadow stacks, sankey diagrams, decorative charts, or content stranded in a narrow column. Missing data shows "—" with a reason. Human text arrives clipped to 80 characters by the server.
