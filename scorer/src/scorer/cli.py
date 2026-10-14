"""click CLI. Each command imports its module lazily.

Module contracts (owners implement these exact signatures):
  hookio.main_event(argv: list[str]) -> None            # never raises, exit 0
  install.run_install(*, agents, claude_settings, codex_home, yes, dry_run, uninstall) -> int
  ingest.run_ingest(conn, *, since_days, claude_root, codex_root, events_file) -> int  # run_id
  metrics.compute_all(conn, run_id) -> None
  check.run_checks(conn) -> list[str]                   # failures, empty = ok
  report.render(conn, *, day: str | None, days: int | None) -> str   # markdown
  serve.serve(db: str | None, port: int) -> None
  judge.run_judge(conn, run_id, *, limit: int | None) -> None
"""
from __future__ import annotations

import os
import sys

import click


@click.group()
def cli() -> None:
    """scorer — how well does a human drive coding agents?"""


@cli.command()
@click.option("--agent", "agents", multiple=True, type=click.Choice(["claude", "codex"]), default=("claude", "codex"))
@click.option("--settings", "claude_settings", default=os.path.expanduser("~/.claude/settings.json"), show_default=True)
@click.option("--codex-home", default=lambda: os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
@click.option("--yes", is_flag=True, help="Apply without asking.")
@click.option("--dry-run", is_flag=True, help="Print the diff only.")
@click.option("--no-schedule", is_flag=True, help="Skip the daily ingest+report job.")
@click.option("--project", default=None, type=click.Path(file_okay=False),
              help="Lab mode: hooks only in <dir>/.claude/settings.local.json (+ .codex/hooks.json), "
                   "pinned to the current SCORER_HOME; no schedule, no global files.")
def install(agents, claude_settings, codex_home, yes, dry_run, no_schedule, project):
    """Merge scorer hooks into Claude/Codex hook configs (shows diff first)."""
    from .install import run_install
    sys.exit(run_install(agents=agents, claude_settings=claude_settings, codex_home=codex_home,
                         yes=yes, dry_run=dry_run, uninstall=False,
                         schedule=False if no_schedule else None, project=project))


@cli.command()
@click.option("--agent", "agents", multiple=True, type=click.Choice(["claude", "codex"]), default=("claude", "codex"))
@click.option("--settings", "claude_settings", default=os.path.expanduser("~/.claude/settings.json"), show_default=True)
@click.option("--codex-home", default=lambda: os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--no-schedule", is_flag=True, help="Skip the daily ingest+report job.")
@click.option("--project", default=None, type=click.Path(file_okay=False),
              help="Lab mode: hooks only in <dir>/.claude/settings.local.json (+ .codex/hooks.json), "
                   "pinned to the current SCORER_HOME; no schedule, no global files.")
def uninstall(agents, claude_settings, codex_home, yes, dry_run, no_schedule, project):
    """Remove scorer hooks, leaving all other hooks untouched."""
    from .install import run_install
    sys.exit(run_install(agents=agents, claude_settings=claude_settings, codex_home=codex_home,
                         yes=yes, dry_run=dry_run, uninstall=True,
                         schedule=False if no_schedule else None, project=project))


def _days(s: str) -> int:
    return int(s.rstrip("d"))


@cli.command()
@click.option("--since", default="30d", show_default=True)
@click.option("--claude-root", default=os.path.expanduser("~/.claude/projects"))
@click.option("--codex-root", default=os.path.expanduser("~/.codex/sessions"))
@click.option("--events", "events_file", default=None)
@click.option("--db", default=None)
@click.option("--no-metrics", is_flag=True)
@click.option("--cwd-prefix", "cwd_prefixes", multiple=True,
              help="Keep only sessions whose cwd is under this dir (repeatable; default: env SCORER_CWD_PREFIX).")
@click.option("--include-lab", is_flag=True,
              help="Also ingest lab/scratch/temp sessions (excluded and purged by default; config: exclude_cwd).")
def ingest(since, claude_root, codex_root, events_file, db, no_metrics, cwd_prefixes, include_lab):
    """Parse transcripts + hook events into SQLite, then compute metrics."""
    from .db import connect
    from .ingest import default_exclude_patterns, run_ingest
    from .model import events_path
    conn = connect(db)
    run_id = run_ingest(conn, since_days=_days(since), claude_root=claude_root, codex_root=codex_root,
                        events_file=events_file or str(events_path()),
                        cwd_prefixes=list(cwd_prefixes) or None,
                        exclude_cwd=None if include_lab else default_exclude_patterns())
    if not no_metrics:
        from .metrics import compute_all
        compute_all(conn, run_id)
    n = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
         for t in ("session", "turn", "api_call", "tool_call", "metric_value")}
    click.echo(f"run {run_id}: " + " ".join(f"{k}={v}" for k, v in n.items()))


@cli.command()
@click.option("--db", default=None)
def check(db):
    """Replay invariants on the ingested DB (spec §6e)."""
    from .check import run_checks
    from .db import connect
    fails = run_checks(connect(db))
    for f in fails:
        click.echo(f"FAIL {f}")
    click.echo("ok" if not fails else f"{len(fails)} invariant(s) failed")
    sys.exit(1 if fails else 0)


def _pin_db(db):
    """`--db PATH` == `SCORER_DB=PATH`: export the absolute path so every later connect(None),
    the analyst's read-only connections and child processes use the same file."""
    import os
    from pathlib import Path
    if db:
        os.environ["SCORER_DB"] = str(Path(db).expanduser().resolve())
    return os.environ.get("SCORER_DB") or db


@cli.command()
@click.option("--day", default=None, help="today | YYYY-MM-DD")
@click.option("--days", type=int, default=None)
@click.option("--db", default=None)
@click.option("--no-judge", is_flag=True, help="Skip the (cached) correction-episode judge.")
def report(day, days, db, no_judge):
    """Markdown daily report (spec §2). Runs the cached judge for the day first."""
    from .db import connect, latest_run_id
    if not day and not days:
        day = "today"
    db = _pin_db(db)
    conn = connect(db)
    judge_note = None
    if not no_judge:
        try:
            # Bounded for interactive use: <= REPORT_MAX_CALLS uncached turns (most recent first),
            # REPORT_WORKERS threads, REPORT_TIMEOUT_S overall. `scorer judge` pre-warms the rest.
            from datetime import datetime, timedelta, timezone
            from .judge import REPORT_MAX_CALLS, REPORT_TIMEOUT_S, REPORT_WORKERS, resolve_day, run_judge
            from .model import local_day
            import time
            run_id = latest_run_id(conn)
            if run_id is not None:
                now = datetime.now(timezone.utc)
                jdays = ([local_day(now - timedelta(days=i)) for i in range(days)] if days
                         else [resolve_day(day)])
                t_end = time.monotonic() + REPORT_TIMEOUT_S
                labelled = total = 0
                budget = REPORT_MAX_CALLS
                for d in jdays:
                    left = t_end - time.monotonic()
                    st = run_judge(conn, run_id, day=d, max_calls=max(0, budget) if left > 0 else 0,
                                   workers=REPORT_WORKERS, timeout=max(0.0, left))
                    budget -= st["called"]
                    labelled += st["labelled"]
                    total += st["n"]
                judge_note = f"judge: {labelled} of {total} turns labelled"
        except Exception as e:  # noqa: BLE001 - the report must render regardless
            click.echo(f"judge skipped: {type(e).__name__}", err=True)
    from .report import render
    click.echo(render(conn, day=day, days=days))
    if judge_note:
        click.echo(f"\n_{judge_note}_")


@cli.command()
@click.option("--port", default=8765, show_default=True)
@click.option("--db", default=None)
def serve(port, db):
    """Local dashboard."""
    from .serve import serve as _serve
    _serve(_pin_db(db), port)


@cli.command()
@click.option("--day", default="today", show_default=True, help="today | YYYY-MM-DD | all")
@click.option("--limit", type=int, default=None, help="Judge at most N (most recent) turns.")
@click.option("--db", default=None)
def judge(day, limit, db):
    """Label correction episodes with the LLM judge (cached). Prints counts only."""
    import time
    from .db import connect, latest_run_id
    from .judge import resolve_day, run_judge
    db = _pin_db(db)
    conn = connect(db)
    t0 = time.monotonic()
    st = run_judge(conn, latest_run_id(conn), limit=limit, day=None if day == "all" else resolve_day(day))
    dt = time.monotonic() - t0
    click.echo(f"judged {st['n']} turns ({st['cached']} cached, {st['called']} labelled, "
               f"{st['errors']} with backend errors) in {dt:.1f}s; model={st['model']} chain={','.join(st['chain'])}")
    click.echo("events: " + " ".join(f"{k}={v}" for k, v in sorted(st["dist"].items())))
    click.echo("sources: " + " ".join(f"{k}={v}" for k, v in sorted(st["sources"].items()))
               + ("  (regex = stand-in, confidence 0.3)" if "regex" in st["sources"] else ""))


@cli.command("export-hdyai")
@click.option("--day", required=True, help="YYYY-MM-DD (report day, our tz)")
@click.option("--db", default=None)
@click.option("--out", default="../.local/sessions.sqlite", show_default=True)
@click.option("--tz", default=None, help="Label tz (default: $REPORT_TIMEZONE, else config tz).")
def export_hdyai(day, db, out, tz):
    """Write our day as Ari's Session + Report rows into their local SQLite (bridge-contract.md)."""
    import os
    os.environ.setdefault("SCORER_NO_LLM", "1")
    from .db import connect
    from .export_hdyai import build, write_local
    from .judge import resolve_day
    db = _pin_db(db)
    b = build(connect(db), resolve_day(day), tz=tz)
    if not b["reports"]:
        click.echo(f"no sessions on {day}")
        sys.exit(1)
    write_local(b, out)
    from .config import load
    click.echo(f"label tz {b['tz']} · day {b['day']} (day bucketing: {load().timezone or 'system tz'}) → {out}")
    for r in b["reports"]:
        rep = r["local"]
        tiles = " ".join(f"{t['key']}={t['display']}" for t in rep["scorerMetrics"]["tiles"])
        click.echo(f"  {rep['id']}: {len(rep['snapshot'])} sessions, "
                   f"{sum(len(s['events']) for s in rep['snapshot'])} events, "
                   f"{len(rep['content']['findings'])} findings, {len(rep['content']['moments'])} moments · {tiles}")


@cli.command()
@click.option("--day", default=None, help="YYYY-MM-DD")
@click.option("--db", default=None)
@click.option("--url", default="https://how-do-you-ai-analysis.vercel.app", show_default=True)
@click.option("--token", default=None, help="Device token (default: SCORER_HOME/hdyai.json)")
@click.option("--activate", "activation", default=None, help="Redeem an invite_… code from the owner.")
@click.option("--dry-run", "dry_run", default=None, help="Write payloads to this file; no HTTP.")
def sync(day, db, url, token, activation, dry_run):
    """Upload our day to the hosted analysis service (/api/sync + /api/analyze)."""
    import os
    os.environ.setdefault("SCORER_NO_LLM", "1")
    from .export_hdyai import activate, build, sync as _sync
    if activation:
        r = activate(url, activation)
        click.echo(f"activated as {r['userId']} until {r['expiresAt']}")
        if not day:
            return
    if not day:
        raise click.UsageError("--day is required")
    from .db import connect
    from .judge import resolve_day
    b = build(connect(_pin_db(db)), resolve_day(day))
    for r in _sync(b, url, token, dry_run=dry_run, echo=click.echo):
        click.echo("  " + " ".join(f"{k}={v}" for k, v in r.items()))
