# how do you ai

Live hackathon app: https://how-do-you-ai.vercel.app

A hosted Next.js dashboard for evidence-backed reports on how someone works with AI. Codex and Claude Code plugins collect local sessions; a Vercel-hosted Eve service analyzes bounded evidence through AI Gateway. Neon Postgres stores access controls, synced summaries, and reports.

## Architecture

`Codex / Claude plugin → local transcript snapshots + SQLite → bounded authenticated sync → Eve + Neon → hosted Next.js dashboard`

The web app runs on Vercel with `HDYAI_HOSTED=1`. It never reads the visitor's local filesystem or uses SQLite as hosted storage. Its APIs require Clerk sign-in and a separately authorized platform connection. The platform credential is encrypted in an HttpOnly cookie bound to the current Clerk user; switching accounts does not inherit another user's reports.

The analysis service is a separate Vercel project in `services/analysis`. Its Neon database is provisioned and linked via Vercel Marketplace. The web app talks to its authenticated APIs rather than receiving the database or Gateway credentials. Clerk is also provisioned via Vercel Marketplace on its free plan. No paid auth features are required. Model calls use the configured AI Gateway account and can consume credits; free infrastructure does not make model inference free.

## User setup

1. Open a platform invitation and sign in using Clerk.
2. Select **Connect Codex** or **Connect Claude Code**, then paste the generated setup message into that agent.
3. Let the agent register/install the plugin and complete its native hook review. The setup message pairs a private, revocable device credential with the user's existing platform account.

Users do not enter API keys, workspace paths, or Git settings. This MVP uses a guided setup message; it is not yet a published universal one-click marketplace install. Codex still requires native plugin installation and hook trust review. Plugin runtime currently supports macOS/Linux with Node.js 24+. The agent can help install that runtime from its official distribution.

Invitations expire in 15 minutes and can be used once. Platform credentials expire after 30 days. Accounts are invitation gated to prevent arbitrary signups from consuming model quota. Browser and plugin credentials are independent; signing out does not stop the plugin. Ask the agent to run the plugin's disconnect action to pause capture and revoke its credential.

## Capture and daily reports

We adapted Entire's MIT-licensed lifecycle approach into our own lightweight Node adapter. **Entire CLI and Git are not required.** Attribution is in `THIRD_PARTY_NOTICES.md`. Native plugin manifests register lifecycle hooks without modifying global agent settings. `scripts/capture-hook.mjs` saves bounded, private transcript snapshots; separate subagent transcripts are captured when provided. The packaged worker needs no npm, tsx, or local web server after installation.

The collector reads supported Codex session/archive JSONL and Claude Code project JSONL, including concurrently active threads. It normalizes candidate/agent/tool records, excludes injected system wrappers and approval-review sessions, and performs best-effort credential redaction. Native logs fill gaps after a snapshot. Session IDs and evidence-line IDs survive migration to snapshots, so sessions are not duplicated and old reports remain frozen.

Snapshots and local credentials use owner-only permissions. Snapshot writes are atomic and use per-session locks. Input is capped at 1 MB and transcripts at 50 MB. Redaction is best-effort; it cannot guarantee that every sensitive value is detected. Full original transcripts stay local. The cloud receives session summaries, descriptive metrics, a bounded analysis sample, and exact cited excerpts for the report.

Hooks trigger background sync, throttled to at most once per 15 seconds. The plugin automatically assesses the latest **completed calendar day** with candidate activity once per day, when a hook next runs. This is an activity-triggered MVP, not an always-on 9 a.m. cloud scheduler. An interrupted assessment or failed publication retries with its existing request ID; completed publication does not spend quota again. The local legacy `daily` script/automation remains available separately.

The rubric interprets framing, context, iteration, verification, coordination, tool judgment, and outcome evidence. It produces cited findings rather than a numerical hiring score. KPIs include explicit interruptions, overlapping completed Codex tasks, longest uninterrupted completed task, recorded tokens, and tokens per final output. Token counters use recorded native usage, exclude duplicate/cumulative counts, show K/M/B, and disclose missing coverage. Lower token use alone does not establish proficiency. Historical snapshots without usage data show unavailable.

## Deploy

The root `vercel.json` selects hosted mode and the existing analysis-service origin. Deploy the dashboard and the separate `services/analysis` project. Marketplace provides Clerk keys to the dashboard and Neon `DATABASE_URL` to the analysis project; model and admin keys remain only in the analysis service.

```bash
npm ci
npm test
npm run build
npm run analysis:migrate
```

`npm run build` packages the plugin into `public/plugins/how-do-you-ai.zip` and publishes its SHA-256 in `release.json`. The archive contains dual native manifests, a connect skill, local marketplace catalogs, and a bundled worker. It contains no credentials or user data.

The additive schema is `services/analysis/schema.sql`. Admission is atomic: by default 6 analyses/account/day, 40 across the platform/day, one fresh active job/account, and up to 3 active credentials/account. The hosted service validates output and exact evidence hashes before accepting a completed report. Retain database backups and use a Neon branch for future destructive migrations.

Owner administration uses private `services/analysis/.env.local` (never distribute it):

```bash
npm run platform:admin -- invite https://how-do-you-ai-analysis.vercel.app ari https://how-do-you-ai.vercel.app
npm run platform:admin -- revoke https://how-do-you-ai-analysis.vercel.app ari
```

Invitations are written to owner-only files in `.local`, never printed. Share only with the approved recipient. No automatic emails are sent.

## Local standalone development

```bash
npm install
npm run dev
```

Without `HDYAI_HOSTED=1`, the app binds to loopback, uses private local SQLite, and offers manual/custom assessment plus legacy Entire checkpoint import. New local-device setup can install our own capture hooks additively and back up existing settings; it never bypasses agent trust. Hosted mode protects private routes through Clerk and reads only the authenticated cloud data.

## Verification

Tests cover transcript attribution, token usage/final-output semantics, date scoping, concurrency, citation validation, tenant-scoped sync, encrypted user-bound auth credentials, CSRF, native snapshot recovery, and the actual extracted plugin package using synthetic localhost data. Native marketplace installation/trust still needs verification inside each agent. Tests do not require paid model calls.
