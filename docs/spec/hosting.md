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

Order matters: steps 1–3 can happen now. Step 4 waits for our push (the additive schema/zod changes and the analyst route land in `ari-version/`).

### 1. Env vars

**web project** (`ari-version/`):

| Var | Value | Status |
|---|---|---|
| `HDYAI_HOSTED` | `1` | already in `vercel.json` |
| `HDYAI_ANALYSIS_URL` | analysis project origin | already in `vercel.json`; change only if the analysis URL changes |
| Clerk keys (`CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`) | from Vercel Marketplace Clerk integration | already provisioned per README |
| **`ANTHROPIC_API_KEY`** | an Anthropic key with access to `claude-opus-5-5` | **NEW**, used by the analyst chat route `app/api/analyst/route.ts` (server-only, never `NEXT_PUBLIC_`) |
| **`REPORT_TIMEZONE`** | `Europe/Berlin` | **NEW** (else America/Los_Angeles, which shifts day boundaries). Same value in local dev env |

**analysis project** (`ari-version/services/analysis/`), no new vars:

| Var | Value | Status |
|---|---|---|
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | Neon via Vercel Marketplace | already provisioned |
| `AI_GATEWAY_API_KEY` | AI Gateway key (Eve model calls) | already; `/api/analyze` returns 503 without it |
| `PLATFORM_ADMIN_KEY` | owner secret | already; needed for the invite in step 3 |

The analyst route does **not** get `DATABASE_URL`. It reads the report through the analysis service's authenticated
`/api/published/:id` / `/api/library` using the signed-in user's platform credential, the same way the dashboard does.
So no Neon credential moves to the web project.

### 2. Neon migrate: **not needed for our change**
Verified: our additions are zod-only (`services/analysis/agent/lib/contract.ts`, `library.ts`). `scorerMetrics` is stored
inside the existing `published_reports.data jsonb`, and `metric` evidence ids live in the existing `analysis_jobs.evidence_ids`
/ `evidence_digests` jsonb columns. `services/analysis/schema.sql` is unchanged. Run the migration only if the Neon DB was never
migrated at all (fresh project); it is idempotent:
```bash
cd ari-version
npm run analysis:migrate          # needs services/analysis/.env.local → DATABASE_URL_UNPOOLED
```

### 3. Device invite for us (owner only)
We need one platform user plus one device credential for `scorer sync`. The activation code **expires in 15 minutes and
is single-use**, so do this when we are online:
```bash
cd ari-version
npm run platform:admin -- invite https://how-do-you-ai-analysis.vercel.app max https://how-do-you-ai.vercel.app
# → writes .local/activation-<uuid>.json (owner-only); send us the activationCode ("invite_…") privately (not Slack in clear if avoidable)
```
`max` = the platform user id (any id ≤100 chars; default daily limit 6 analyses). If we need more demo rehearsals, re-invite with a higher
limit is not exposed by the script. Instead raise it in Neon: `UPDATE platform_users SET daily_limit=12 WHERE id='max';` (max 20).
We redeem the code with `scorer sync --activate invite_…` (calls `POST /api/activate`, stores the 30-day `hdyai_…` token
in `~/.scorer/hdyai.json`, mode 0600). We never run `platform:admin` ourselves.

For the browser: the same invite's `setupLink` signs the Clerk user into the same platform account, so the synced report
shows on the dashboard. If Ari prefers to view under his own account, invite `ari` for the browser and we'll sync to `max`.
Reports are per platform user, so the viewer must be linked to the user we sync as.

### 4. Deploy both projects (after our push)
```bash
cd ari-version && npm ci && npm test && npm run typecheck
# Vercel: redeploy "analysis" (root services/analysis) FIRST, then "web" (root ari-version)
```
Analysis first: the web app's new fields are optional, but the analysis service must accept evidence kind `metric` and
the `scorerMetrics` field before we sync. Otherwise `/api/analyze` returns 400 "Invalid evidence payload", and zod strips
`scorerMetrics` on `/api/sync`.

### Checklist to send back to us
- [ ] `ANTHROPIC_API_KEY` + `REPORT_TIMEZONE=Europe/Berlin` set on web
- [ ] (only for a fresh Neon DB) `npm run analysis:migrate`
- [ ] activation code sent (we redeem within 15 min)
- [ ] both projects redeployed (analysis first), URLs confirmed

---

## We do

Report ids (contract): hosted id = `requestId` = `scorer-<day>-<8 hex of sha1(bundle)>`. A complete published report is
immutable and the analysis job is keyed by `hash(userId:requestId)`. So **identical data re-syncs for free** (same id, no new
analysis), while any data change (new ingest, judge labels) gives a new id and costs 1 analysis.

1. Push the additive changes: `services/analysis` zod (evidence kind `metric`, `publishedReport.scorerMetrics`), KPI tiles,
   `app/api/analyst/route.ts`. Tell Ari to redeploy (analysis first).
2. Redeem the invite within 15 min of Ari creating it:
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

- **T-24h**: Ari's checklist done (env, redeploy, invite redeemed). Run one real `scorer sync --day <yesterday>` and verify the
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
