# Hosting runbook — scorer data in Ari's app on Vercel

Product = Ari's Next.js app (`ari-version/`) in hosted mode (`HDYAI_HOSTED=1`): Clerk + Neon + the Eve analysis service.
Our Python harness stays local (hooks, parser, judge, metrics); only `scorer sync` goes up, through the analysis
service's authenticated APIs (`/api/activate`, `/api/sync`, `/api/analyze`). Nothing reads local files in hosted mode.

Two Vercel projects, both from the `ari-version/` tree:

| Project | Root dir | Today's URL (README) |
|---|---|---|
| web (dashboard) | `ari-version/` | https://how-do-you-ai.vercel.app |
| analysis (Eve service) | `ari-version/services/analysis/` | https://how-do-you-ai-analysis.vercel.app |

---

## Ari does (hand this over first)

PR #1 (self-service onboarding, `ari/automatic-account-onboarding`) is **merged** into our branch. Order matters:
env vars (1) → migrate (2) → deploy analysis, then web (4). No invite is needed any more (3).

### 1. Env vars

**web project** (`ari-version/`):

| Var | Value | Status |
|---|---|---|
| `HDYAI_HOSTED` | `1` | already in `vercel.json` |
| `HDYAI_ANALYSIS_URL` | analysis project origin | already in `vercel.json`; change only if the analysis URL changes |
| Clerk keys (`CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`) | from Vercel Marketplace Clerk integration | already provisioned per README |
| **`WEB_PROVISIONING_KEY`** | random server-only secret, 32+ chars, **identical on web and analysis** | **NEW (PR #1)**: HMAC for web→analysis account provisioning |
| **`ANTHROPIC_API_KEY`** | an Anthropic key with access to `claude-opus-5-5` | **NEW**, used by the analyst chat route `app/api/analyst/route.ts` (server-only, never `NEXT_PUBLIC_`) |
| **`REPORT_TIMEZONE`** | `Europe/Berlin` | **NEW** (else America/Los_Angeles, which shifts day boundaries). Same value in local dev env |

**analysis project** (`ari-version/services/analysis/`):

| Var | Value | Status |
|---|---|---|
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | Neon via Vercel Marketplace | already provisioned |
| `AI_GATEWAY_API_KEY` | AI Gateway key (Eve model calls) | already; `/api/analyze` returns 503 without it |
| `PLATFORM_ADMIN_KEY` | owner secret | already; only needed for managed invites / revoke |
| **`WEB_PROVISIONING_KEY`** | the same value as on web | **NEW (PR #1)** |

The analyst route does **not** get `DATABASE_URL`. It reads the report through the analysis service's authenticated
`/api/published/:id` using the signed-in user's browser credential (`hostedRequest`, Clerk-bound HttpOnly cookie), the same
way the dashboard does. `/api/analyst` sits under `/api/*`, so in hosted mode Clerk sign-in is required. No Neon credential moves to the web project.

### 2. Neon migrate: **REQUIRED** (PR #1)
PR #1's `services/analysis/schema.sql` adds `device_credentials.kind` (collector | browser) and `provision_web_account()`.
Our own additions need no migration: `scorerMetrics` and `metric` evidence live in existing jsonb columns. Idempotent:
```bash
cd ari-version
npm run analysis:migrate          # needs services/analysis/.env.local → DATABASE_URL_UNPOOLED
```

### 3. Our account: self-service, no invite
1. We open the dashboard, sign up with Clerk. The first authenticated setup visit auto-provisions a restricted account
   (`platform_users.id = 'clerk:user_…'`, daily limit **2** analyses, 3 collector connections, 5 browser sessions).
2. The setup flow's "copy setup message" step creates a pairing via `POST /api/pairings` (browser credential). It returns
   an `activationCode` of the same format as before, `invite_` + 43 chars, single-use, valid 15 min.
3. We redeem it with `scorer sync --activate invite_…` (`POST /api/activate`, unchanged), which stores the 30-day `hdyai_…`
   collector token in `~/.scorer/hdyai.json` (mode 0600). The synced report shows on the dashboard of that same Clerk user.

**Ari does:** raise our quota for demo rehearsals (self-service default is 2/day):
`UPDATE platform_users SET daily_limit=6 WHERE id='clerk:user_…';` (max 20; we send the id after first sign-in).
The managed `platform:admin -- invite` path still works and keeps its configured limits. Use it only if Ari prefers a managed account.

### 4. Deploy both projects (after our push)
```bash
cd ari-version && npm ci && npm test && npm run typecheck
# 1) npm run analysis:migrate   2) Vercel: deploy "analysis" (root services/analysis)   3) deploy "web" (root ari-version)
```
`scorer sync` uses the **A-lite** path (merge-design §0–2): it sends only evidence kinds the deployed service already
accepts, puts our numbers in `metrics` (`scorer_*` keys), and patches TaskSpans onto session summaries. The analysis redeploy
also adds the optional `metric` evidence kind and the stored `scorerMetrics` block (Scores tiles on the hosted report).

### Checklist to send back to us
- [ ] `WEB_PROVISIONING_KEY` (same value) on web **and** analysis
- [ ] `ANTHROPIC_API_KEY` + `REPORT_TIMEZONE=Europe/Berlin` on web
- [ ] `npm run analysis:migrate` done
- [ ] analysis deployed, then web; URLs confirmed
- [ ] after we sign up: our `clerk:user_…` `daily_limit` raised to 6

---

## We do

Report ids (contract): hosted id = `requestId` = `scorer-<day>-<8 hex of sha1(bundle)>`. A complete published report is
immutable and the analysis job is keyed by `hash(userId:requestId)`. So **identical data re-syncs for free** (same id, no new
analysis), while any data change (new ingest, judge labels) gives a new id and costs 1 analysis.

1. Push the additive changes: `services/analysis` zod (evidence kind `metric`, `publishedReport.scorerMetrics`), KPI tiles,
   `app/api/analyst/route.ts`. Tell Ari to redeploy (analysis first).
2. Redeem the pairing code from the dashboard setup message within 15 min (section 3):
   ```bash
   uv run scorer sync --activate invite_XXXXXXXX --url https://how-do-you-ai-analysis.vercel.app
   # POST /api/activate → ~/.scorer/hdyai.json (0600): {url, deviceToken: hdyai_…, userId, expiresAt (+30 d)}
   ```
3. Dry run, no network and no quota: builds the exact `/api/sync` + `/api/analyze` bodies, checks the caps (prompt ≤125k chars,
   ≤200 sessions, ≤2000 evidence/session, text ≤1800, metric keys ≤40, all evidence ids unique), prints sizes:
   ```bash
   uv run scorer sync --day 2026-10-14 --dry-run
   ```
4. Real sync (1 of 6 analyses/day; Eve takes minutes):
   ```bash
   uv run scorer sync --day 2026-10-14 --url https://how-do-you-ai-analysis.vercel.app
   # 1. POST /api/sync     {sessions: summaries, reportName}
   # 2. POST /api/analyze  {requestId, name, role, metrics: flat record, input: sessions + evidence incl. kind 'metric'}
   # 3. poll GET /api/analyses/:jobId until complete|failed
   # 4. POST /api/sync     {sessions, reportName, report: {…Eve content verbatim, evidenceIndex = cited evidence (byte-identical
   #    to what analyze got), scorerMetrics}}
   ```
5. Verify: `GET /api/device` (quota/user), then open `https://how-do-you-ai.vercel.app/reports/<hosted id>` signed in as the
   linked Clerk user. Check that the KPI tiles show our numbers, `Source n` links open excerpts (including `metric:` records),
   and the analyst panel answers with citations.

Quota math: 6 analyses/account/day (40 platform-wide), one active job at a time. Budget for demo day:
1 verify run (today) + 1 final run on demo morning + ≥3 spare. Re-running unchanged data costs 0. If more are needed, Ari raises
`platform_users.daily_limit` (≤20).

## Analyst chat on Vercel

Built as `ari-version/app/api/analyst/route.ts`: `ai` SDK `streamText` with `@ai-sdk/anthropic` (`claude-opus-5-5`), tools
`list_sessions`, `query_metrics`, `get_evidence`. It reads the **report object**:
- hosted: the published report via the analysis service (`/api/published/:id` through `lib/hosted.ts`, with the signed-in user's
  sealed platform credential). The tools only see `scorerMetrics`, `content`, `activity` and the cited `evidenceIndex`
  (full transcripts never leave the laptop).
- local: `getReport()` from `.local/sessions.sqlite`, with the full snapshot.

The web project needs **only `ANTHROPIC_API_KEY`**. No `DATABASE_URL`, and no Python process on Vercel. The Python `/api/chat` (`scorer serve
--port 8811`) stays dev-only; no proxy is built. If the key is missing, the route returns 503 and the panel says so.

## Demo-day runbook

- **T-24h**: Ari's checklist done (env incl. `REPORT_TIMEZONE=Europe/Berlin` on web, migrate + redeploy, pairing code redeemed); for the local fallback export with `REPORT_TIMEZONE=Europe/Berlin` in the dev-server env too, so day boundaries match. Run one real `scorer sync --day <yesterday>` and verify the
  hosted report end to end (step 5 above).
- **T-60 min**: `uv run scorer ingest && uv run scorer judge --day today`. `uv run scorer sync --day <demo day> --dry-run` to check caps and sizes.
  Start the local fallback now: `uv run scorer export-hdyai --day <demo day>` (and `--db demo/scorer-demo.db` for
  `scorer-demo-<day>`), then `cd ari-version && PORT=3100 npm run dev`, then open `http://127.0.0.1:3100/reports/scorer-<day>`.
- **T-45 min**: real `scorer sync --day <demo day> --url …` (1 analysis). Wait for complete (poll prints the stage).
- **T-30 min**: open the hosted report and click through the demo flow: Sessions → report overview (KPI tiles) → evidence inspector →
  analyst panel (ask 2 rehearsed questions, check citations open).
- **T-10 min**: do not re-sync unless the data changed. An unchanged re-sync is free, and a changed one costs 1 of the spares.
- **T-0**: present on hosted. **Fallback**: if hosted fails (Eve 503, auth, quota), switch the tab to the local standalone
  `http://127.0.0.1:3100/reports/scorer-<day>` (same component, same KPI tiles; analyst panel uses the same route locally with
  `ANTHROPIC_API_KEY` in the shell env). Stop the dev server afterwards.
