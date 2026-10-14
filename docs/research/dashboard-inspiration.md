# Dashboard inspiration for scorer

Research date: 2026-10-14. Scope: UI references for scorer's Today / Sessions / Trends / Leverage tabs and the analyst chat sidebar, under the fixed Sherpa palette (Pine #1c2d22 text, Clay #b36813, Grass #316c5a, Oak #802d11, warm neutrals, white background; see `DESIGN.md`).

**How this was sourced.** All references come from primary product docs, help centres and changelogs, read through a text fetcher. **I could not see any screenshots or videos**, only the page text and, where present, image file names or alt text. Each entry says what is documented. Visual claims beyond that come from general product knowledge and are marked *(from memory, not verified visually)*. Before borrowing a pattern, open the URL and check the visuals.

Colour shorthand used in the spec lines: **Pine** = text/primary, **Clay** = Claude lane, **Blue** = Codex lane (#0369a1), **Grass** = verified/better, **Oak** = failed/worse, **muted** = #4d5a4d text on #f7f7f5.

---

## 1. References

### 1.1 Timing (Mac): Activities screen
- URL: https://timingapp.com/help/activities (also https://timingapp.com/blog/timing-2021-6-all-new-activities-screen-and-plenty-of-usability-improvements/)
- **What it does well.** One screen pairs a day timeline with a list. The timeline can sit on top or to the right (there is a toolbar toggle). It shows when you worked, which apps you used, and which project each block went to. Hovering shows the exact document or site at that moment. **Dragging across the timeline selects a time range and filters the list below to that range.** The list is ordered by time spent, largest first, and expands to show apps, documents and sites. A "Chronological" mode turns the list into time entries in order. Timing merged its old separate Review and Details screens into this one screen, which is the same choice we face between a timeline and evidence lists. Screenshots exist on the page but I could not view them.
- **Borrow:** `Today timeline: click-drag on the time axis selects a range; every list below (unverified, corrections, top tools) re-filters to that range; a muted chip "14:10–15:40 ×" sits above the lists; Esc clears.`

### 1.2 Toggl Track: Timeline in the calendar view
- URL: https://support.toggl.com/the-timeline-feature and https://support.toggl.com/tracking-time-in-the-calendar-view
- **What it does well.** It sets recorded background activity beside the user's logged entries and colours the gap between them: activity that matches an entry, activity with no entry, and an entry with no activity behind it (per the docs, green / red / orange pills). This is the closest analogue to our "agent was working but the human wasn't" and "edits with no verification behind them". The web app shows 7 days at 15-minute granularity.
- **Borrow:** `Timeline lane = two stacked tracks per session: top track agent-active (Clay/Blue fill), bottom hairline track human attention; where the agent sits waiting and the human is elsewhere, draw the gap as a 1px Oak-outlined empty box (blocked), not a filled colour.`

### 1.3 WakaTime: personal coding dashboard
- URL: https://wakatime.com/faq and https://wakatime.com/blog/27-fill-the-gaps-in-your-coding-activity
- **What it does well.** It shows a day-activity chart at adjustable granularity, with time split by project, language and editor. It exposes the **idle timeout as a user-visible setting** ("the maximum time allowed between durations"), so the definition of "active" can be read and changed instead of being hidden. scorer has the same problem with idle and waiting thresholds.
- **Borrow:** `Timeline header right: muted 12px "idle after 5 min · waiting = agent done, no reply" with an info popover; the threshold is a query param so the analyst can cite it.`

### 1.4 Honeycomb: trace waterfall and trace summary
- URL: https://docs.honeycomb.io/reference/honeycomb-ui/query/trace-waterfall
- **What it does well.** A condensed **trace summary** strip spans the whole duration and shows up to 6 depth levels. Hovering it names the longest-running span at that depth and time, and clicking it jumps the waterfall below. Error spans turn red behind a "Highlight errors" toggle, with next/previous navigation between the highlighted errors. The selected row's details open in a right sidebar. Rows with children show a count box.
- **Borrow:** `Sessions detail: a 24px overview strip of the whole day above the per-turn waterfall; unverified/failed turns are marked with Oak ticks on the strip; "Next unverified ↓" button steps through them (keyboard j/k).`

### 1.5 Langfuse: responsive timeline and trace view
- URL: https://langfuse.com/changelog/2026-08-28-responsive-timeline, https://langfuse.com/changelog/2025-03-19-new-trace-view, https://langfuse.com/docs/observability/features/agent-graphs
- **What it does well.** **One timeline renderer adapts to the space it gets.** The same view runs in a wide pane, a narrow side peek and on a phone. Zoomed out, rows collapse to one line, and labels and durations come back only when they fit. Colour encodes observation *type*. Double-click zooms to a span, drag selects a range, and "Fit" resets. A Tree/Timeline toggle keeps the same metrics in both modes. Agent graphs offer Aggregated mode (same-name steps collapse into one node with a counter) and Expanded mode. The page is text plus a video, which I could not view.
- **Borrow:** `Build one timeline component with density levels (full: labels + durations; compact: 1-line rows, no labels; mini: 16px row for the Sessions table and the chat peek); "Show in timeline" from chat opens the compact density inside the sidebar.`

### 1.6 Linear Insights
- URL: https://linear.app/docs/insights
- **What it does well.** The chart is **linked to a table underneath**: hovering a bar highlights the matching rows, and clicking a bar or segment *temporarily filters* the issue view. It has three controls only: Measure (y), Slice (x) and optional Segment (colour). Percentile lines (p25/50/75/95) on scatterplots can be clicked to zoom the y-axis. It opens as a right panel with `⌘⇧I` and an expand-to-full-screen button.
- **Borrow:** `Leverage: chart and table are one unit; hovering a skill bar highlights its table row and vice versa; clicking a bar segment ("by agent") filters the turn list below; one control row "Measure: uses | turns | cost · Segment: by you/by agent".`

### 1.7 Plausible Analytics: click-to-filter on a single page
- URL: https://plausible.io/docs/filters-segments
- **What it does well.** **Any value you click becomes a filter**, filters stack, active filters show as removable chips at the top, and Esc clears them all. Everything sits on one scrolling page without sub-reports. *(From memory, not verified visually:)* the top row is a set of plain numbers with a small change percentage, and clicking one swaps the main chart's metric.
- **Borrow:** `KPI band: 4 numbers, label above in muted 13px, value 32px tabular Pine, delta below as signed text ("+3 vs median") in Grass/Oak, no boxes or borders; clicking a number makes it the timeline's highlight metric (e.g. "blocked" outlines blocked spans) and adds a filter chip.`

### 1.8 Fathom Analytics
- URL: https://usefathom.com/features
- **What it does well.** The product rule is stated outright: "No training needed, it's just a single page." **Milestones** are drawn on the time chart (launches, press). Comparison periods are previous week, month, year or custom. Named saved views cover common filter combinations.
- **Borrow:** `Trends: annotate small multiples with vertical hairline milestones from git/config events ("CLAUDE.md changed", "new skill added") in muted 11px, so a step change in V/B/C can be traced to a cause.`

### 1.9 GitHub Copilot usage metrics: code generation dashboard
- URL: https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics
- **What it does well.** It splits **user-initiated vs agent-initiated** changes. User-initiated means completions and chat insert/copy/apply. Agent-initiated means edit, agent and custom modes. Each side breaks down by model and by language. It also has "agent contribution %", a daily added/deleted series, and requests per chat mode (ask/edit/plan/agent). This is the same human-vs-agent split as our Leverage tab. The docs are text-only and say nothing about the visual layout.
- **Borrow:** `Leverage header line: "You invoked 14 · agent invoked 41 (75%)", with the agent share as a single thin two-tone bar (Pine = you, Clay = agent) beneath; same two-tone used for every per-tool row.`

### 1.10 ccusage (Claude Code usage CLI)
- URL: https://www.npmjs.com/package/ccusage (repo github.com/ryoppippi/ccusage)
- **What it does well.** A plain table by day, month, session or **5-hour billing block**. Columns are input, output, cache-create and cache-read tokens, the models used and estimated USD, with a per-model breakdown flag. It openly says costs are estimates and points to the billing console for the real numbers. It treats cost as a dense table, not a chart.
- **Borrow:** `Cost by model: a right-aligned tabular table (model · turns · in · out · cache read · $), "estimated" in muted 12px in the header, behind a disclosure on Today; no pie or donut.`

### 1.11 Braintrust: compare experiments
- URL: https://www.braintrust.dev/docs/evaluate/compare-experiments
- **What it does well.** **Baseline deltas on every row**, green for improvements and red for regressions. You can **sort by regressions**, and a summary panel orders scorers by regression count, so the worst comes first. Diff mode shows what changed.
- **Borrow:** `Trends table under the multiples: one row per metric, today vs 7-day median vs 30-day median, delta signed in Grass/Oak; default sort = "most worsened first".`

### 1.12 PostHog AI (formerly Max)
- URL: https://posthog.com/docs/posthog-ai/surfaces/web-app and https://posthog.com/docs/posthog-ai/platform-and-chat-ui
- **What it does well.** A full chat lives in the **right sidebar on every page**, plus **inline entry points** built into features ("hand off a question without leaving the page"). The assistant reads the current page's context and **can edit the filters on the page you're on**. Sensitive steps stop behind an **approval card**. History and threads persist across pages. The docs are text-only and do not give the sidebar's width or whether it overlays the page.
- **Borrow:** `Chat can set dashboard state: the agent returns a "view" action (date, range, session filter) rendered as a muted card "Show 14:10–15:40 on timeline →"; clicking it applies the filter chip on the page, never silently.`

### 1.13 Hex: Notebook agent / Threads
- URL: https://learn.hex.tech/docs/explore-data/notebook-view/notebook-agent
- **What it does well.** A "+" in the agent sidebar header starts a **new thread, which clears the context**. Each thread start also saves a project version. A later changelog says the conversation **persists when you switch views**. The agent's work stays editable in the main canvas.
- **Borrow:** `Sidebar header: thread title (auto from first question) + "New" (+) icon button; threads are per-day and survive tab switches; a thread list sits behind the title as a dropdown, not a second rail.`

### 1.14 Mixpanel Agent (formerly Spark) / Amplitude Global Agent
- URL: https://mixpanel.com/blog/spark-bringing-generative-ai-to-mixpanel/ and https://amplitude.com/docs/amplitude-ai/agents-overview
- **What it does well.** The AI builds a **normal report** that you can then open in the regular query builder. It is not a dead-end chat picture. Amplitude's agent answers with a chart, a cohort or a follow-up investigation.
- **Borrow:** `Chat answers never embed bespoke charts; they cite turns [n] and link to existing views ("Open in Sessions: wt-auth, 3 turns"), so every claim lands on a real page the user already knows.`

### 1.15 Vercel Observability: overview and anomaly alerts
- URL: https://vercel.com/products/observability and https://vercel.com/changelog/anomaly-alerts-ga
- **What it does well.** Each overview panel is a drill-in entry point (Functions, Compute and others). Anomalies show in the dashboard, and an agent can then investigate them and suggest causes. Detection and explanation are kept separate.
- **Borrow:** `Today "needs attention" items are deterministic (computed), each with an "Ask why" link that seeds the chat with that item's evidence IDs; the agent explains, the page detects.`

### Also checked, with less to borrow
- **Swarmia Investment balance** (https://help.swarmia.com/features/focus/balance-engineering-investments): a 100% stacked share over time with target bands (for example "productivity 10–15%"). It could work for "share of agent minutes blocked" with a target band, but it is mostly a team view.
- **DX AI Measurement Framework** (https://getdx.com/ai-measurement-framework/): its utilization / impact / cost trio is a useful framing for the order of Trends sections, but the docs show no UI.
- **Claude Code analytics** (https://code.claude.com/docs/en/analytics): per-user lines accepted and average daily spend for the current month. It is an admin leaderboard, which is the wrong frame for one engineer.
- **LangSmith dashboards** (https://docs.langchain.com/langsmith/dashboards): prebuilt grids of trace count, errors, tokens and cost. It is a generic chart-tile grid (see anti-patterns).

---

## 2. Recommended layout per tab

These spec lines refine `DESIGN.md` and do not contradict it. They use the same 48px header, the underline tabs, the 26rem sidebar and fluid content up to 1400px.

### Today
1. Verdict first: the narrative paragraph at 17–24px Pine, max 72ch, left-aligned; "Tomorrow" as one line with the 2px Salmon quote rule. No card around it.
2. KPI band: 4 numbers (unverified edits, blocked min, corrections, active h), label above in muted 13px, value 32px tabular, signed delta vs the 7-day median below in Grass/Oak. There are no boxes, only 32px gaps. Clicking a number highlights that metric on the timeline and adds a filter chip (Plausible, Toggl).
3. The full-width timeline is the page's anchor. Lanes are grouped by worktree with a sticky 160px label column. The agent-active fill uses the lane colour (Clay/Blue), waiting is a hatched neutral, idle is empty, and blocked is an empty box with a 1px Oak outline.
4. An overview strip (24px) above the lanes shows the whole day, with Oak ticks for unverified and failed turns and Grass ticks for verified ones (Honeycomb).
5. Dragging on the time axis selects a range. Every section below re-filters, and a removable chip shows the range (Timing).
6. Below the timeline is a 2-column grid. The left column holds "Unverified right now" (rows: worktree · file · minutes since edit · Ask why) and "Corrections by cause" (horizontal bars, count right-aligned). The right column holds "Worst attention window" and "Pattern to repeat".
7. "Needs attention" rows are text-first. A 2px Oak left rule appears only on failed checks. Unverified stays neutral, per the DESIGN.md status rule.
8. Last on the page: diagnostics and cost by model as a tabular table behind a disclosure, headed "estimated" (ccusage).

### Sessions
1. One full-width table, one row per session. Columns: worktree, mini timeline (Langfuse "mini" density, 16px, shared time axis across all rows), duration, turns, active, verification stacked bar (Grass/Oak/neutral), blocked min, $.
2. A shared time axis across rows lets you see parallelism by scanning down the column. A vertical hover line shows the same time in every row.
3. Default sort is "most unverified first", and column headers sort (Braintrust).
4. Selecting a row expands it inline, not in a modal. Inside are the session's overview strip and then the turn waterfall: the clipped prompt, tool chips, a verification badge and the gap after each turn.
5. "Next unverified ↓" and j/k step through the problem turns (Honeycomb).
6. Turn detail opens in a right detail pane inside the expanded row (60/40 split), so the table stays visible.
7. The filter chips from Today (date, range, worktree) carry over, so drilling down from Today lands here already filtered.

### Trends
1. Window switch 7 / 14 / 30 days at the top left. Groups follow DX's order: Utilization (active h, sessions), Quality (V, C, C by cause), Cost ($/day, by model).
2. Small multiples in an auto-fill grid (min 280px), all sharing the same x-axis. Each has a 1px line in Pine, a dashed median line, and today's dot in Grass/Oak according to its side of the median.
3. Each multiple's title row shows the metric name, today's value and the signed delta, so the chart supports the number and never replaces it.
4. Milestone hairlines mark config events (new skill, CLAUDE.md edit, model switch), with muted 11px labels on hover (Fathom).
5. Under the grid is a delta table (metric · today · 7d median · 30d median · change), sorted most-worsened first (Braintrust).
6. Clicking a day on any multiple sets the global day and jumps to Today.

### Leverage
1. Summary line: "You invoked 14 · agent invoked 41", with one thin two-tone bar under it (Pine = you, Clay = agent) (GitHub Copilot).
2. Control row: category segmented control (skills / commands / MCP / plugins / subagents) and Measure (uses · turns touched · cost).
3. A ranked list of horizontal two-tone bars with the name in mono only for command and tool identifiers. Counts are right-aligned and tabular.
4. Chart and table are linked. Hovering a bar highlights its table row, and clicking a segment filters the turn list below it to "by agent" or "by you" (Linear Insights).
5. A turn list under the chart shows where the selected tool was used, with links into Sessions.
6. "Unused but installed" goes in a muted footer list, not a red warning.

### Chat sidebar integration
1. The sidebar is a sibling column (26rem), not an overlay, from 1200px up. The page reflows and never sits behind it. Below 1100px it overlays, as DESIGN.md says.
2. Context chip at the top of the composer: "Today · 14:10–15:40 · wt-auth". It reflects the current tab and filters, can be removed, and is sent with each question (PostHog).
3. Inline entry points: every "needs attention" row and every timeline hover card has an "Ask why" link that seeds the composer with the evidence IDs (PostHog, Vercel).
4. Answers cite turns as `[n]` pills. A "Show on timeline" action applies a filter chip to the page through an explicit card the user clicks, never silently (PostHog, Mixpanel).
5. Threads: the title plus a "+" for a new thread in the header. Threads persist across tabs and are keyed per day (Hex).
6. The peek overlay inside the sidebar uses the compact timeline density, so "show me" works without leaving the chat (Langfuse).

---

## 3. Anti-patterns to avoid

1. **A generic chart-tile grid as the home page.** Every metric gets an equal box with its own mini chart and nothing is ranked or explained. *Seen in:* the LangSmith prebuilt dashboards (traces, LLM calls, cost and tokens, tools, run types, feedback scores, each as its own section of charts), and Copilot's 28-day usage dashboard in the same way. For one engineer at the end of the day, a verdict and 4 numbers beat 12 tiles.
2. **Leaderboards and per-user tables in a personal tool.** *Seen in:* the Claude Code analytics "Team insights", the top-contributors leaderboard and the per-user lines-accepted table. These rank people instead of explaining a day, and "lines accepted" rewards volume, not verified work.
3. **Opaque scores.** *Seen in:* Timing's and Rize's "productivity score" / "daily scores". A single number that hides how it is calculated invites gaming and distrust. scorer should show the 4 raw counts with their definitions (WakaTime's visible timeout is the counter-example).
4. **AI answers that produce dead-end bespoke charts.** *Seen in:* the early Mixpanel Spark and Amplitude agent marketing, where the AI "returns a chart". The fix Mixpanel adopted was to make the generated report editable in the normal query builder. In scorer, the chat should link to existing views and not draw new charts in the sidebar.
5. **Colour as the only carrier of meaning, with alarm red overused.** *Seen in:* the Toggl calendar timeline (green / red / orange pills), and in error-red highlighting in trace waterfalls when it is always on (Honeycomb makes it a toggle for this reason). Keep Oak for failed checks only. Pair every colour with text or shape (outline vs fill, tick vs none), and leave unverified neutral.

---

## Sources

- https://timingapp.com/help/activities
- https://timingapp.com/blog/timing-2021-6-all-new-activities-screen-and-plenty-of-usability-improvements/
- https://timingapp.com/presskit/
- https://support.toggl.com/the-timeline-feature
- https://support.toggl.com/tracking-time-in-the-calendar-view
- https://wakatime.com/faq
- https://wakatime.com/blog/27-fill-the-gaps-in-your-coding-activity
- https://docs.honeycomb.io/reference/honeycomb-ui/query/trace-waterfall
- https://langfuse.com/changelog/2026-08-28-responsive-timeline
- https://langfuse.com/changelog/2025-03-19-new-trace-view
- https://langfuse.com/docs/observability/features/agent-graphs
- https://linear.app/docs/insights
- https://plausible.io/docs/filters-segments
- https://usefathom.com/features
- https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics
- https://www.npmjs.com/package/ccusage
- https://www.braintrust.dev/docs/evaluate/compare-experiments
- https://posthog.com/docs/posthog-ai/surfaces/web-app
- https://posthog.com/docs/posthog-ai/platform-and-chat-ui
- https://learn.hex.tech/docs/explore-data/notebook-view/notebook-agent
- https://mixpanel.com/blog/spark-bringing-generative-ai-to-mixpanel/
- https://amplitude.com/docs/amplitude-ai/agents-overview
- https://vercel.com/products/observability
- https://vercel.com/changelog/anomaly-alerts-ga
- https://help.swarmia.com/features/focus/balance-engineering-investments
- https://getdx.com/ai-measurement-framework/
- https://code.claude.com/docs/en/analytics
- https://docs.langchain.com/langsmith/dashboards
- https://rize.io/features
