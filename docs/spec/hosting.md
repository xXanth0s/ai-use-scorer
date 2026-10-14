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
| `REPORT_TIMEZONE` | `America/Los_Angeles` | optional: our config tz is unset → system tz = America/Los_Angeles, which is already their default. Set it explicitly only if our tz changes |

**analysis project** (`ari-version/services/analysis/`), no new vars:

| Var | Value | Status |
|---|---|---|
| `DATABASE_URL` / `DATABASE_URL_UNPOOLED` | Neon via Vercel Marketplace | already provisioned |
| `AI_GATEWAY_API_KEY` | AI Gateway key (Eve model calls) | already; `/api/analyze` returns 503 without it |
| `PLATFORM_ADMIN_KEY` | owner secret | already; needed for the invite in step 3 |

The analyst route does **not** get `DATABASE_URL`. It reads the report through the analysis service's authenticated
`/api/published/:id` / `/api/library` using the signed-in user's platform credential, the same way the dashboard does.
So no Neon credential moves to the web project.

### 2. Neon migrate (after our push)
Our schema change is additive only (no new tables expected; if one is needed it is `ADD COLUMN IF NOT EXISTS` in
`services/analysis/schema.sql`). Run it with the analysis env loaded:
```bash
cd ari-version
npm run analysis:migrate          # reads services/analysis/.env.local → DATABASE_URL_UNPOOLED
```
Idempotent; safe to re-run. Use a Neon branch first if you want to be careful.

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
Analysis first: the web app's new fields are optional, but the analysis service must accept our extended
`metrics` record and `moments` before we sync.

### Checklist to send back to us
- [ ] `ANTHROPIC_API_KEY` set on web
- [ ] `npm run analysis:migrate` done (after our push)
- [ ] activation code sent (we redeem within 15 min)
- [ ] both projects redeployed (analysis first), URLs confirmed

---

## We do

_In progress. Filled in once `scorer sync` and the analyst route are built and verified._

1. Push the additive changes (`services/analysis` zod/schema, KPI tiles, `app/api/analyst/route.ts`).
2. `uv run scorer sync --activate invite_…` → stores the device token.
3. `uv run scorer sync --day 2026-10-14 --url https://how-do-you-ai-analysis.vercel.app` → `/api/sync` (session summaries) + `/api/analyze` (evidence sample + metrics record). That uses 1 of 6 daily analyses.
4. Verify the hosted report shows our KPI block, moments, and the analyst chat answers with citations.
5. Demo-day rehearsal within the 6/day quota (see below).

## Demo-day runbook
_TBD after verification._
