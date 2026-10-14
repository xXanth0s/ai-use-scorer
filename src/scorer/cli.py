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
def install(agents, claude_settings, codex_home, yes, dry_run, no_schedule):
    """Merge scorer hooks into Claude/Codex hook configs (shows diff first)."""
    from .install import run_install
    sys.exit(run_install(agents=agents, claude_settings=claude_settings, codex_home=codex_home,
                         yes=yes, dry_run=dry_run, uninstall=False,
                         schedule=False if no_schedule else None))


@cli.command()
@click.option("--agent", "agents", multiple=True, type=click.Choice(["claude", "codex"]), default=("claude", "codex"))
@click.option("--settings", "claude_settings", default=os.path.expanduser("~/.claude/settings.json"), show_default=True)
@click.option("--codex-home", default=lambda: os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--no-schedule", is_flag=True, help="Skip the daily ingest+report job.")
def uninstall(agents, claude_settings, codex_home, yes, dry_run, no_schedule):
    """Remove scorer hooks, leaving all other hooks untouched."""
    from .install import run_install
    sys.exit(run_install(agents=agents, claude_settings=claude_settings, codex_home=codex_home,
                         yes=yes, dry_run=dry_run, uninstall=True,
                         schedule=False if no_schedule else None))


def _days(s: str) -> int:
    return int(s.rstrip("d"))


@cli.command()
@click.option("--since", default="30d", show_default=True)
@click.option("--claude-root", default=os.path.expanduser("~/.claude/projects"))
@click.option("--codex-root", default=os.path.expanduser("~/.codex/sessions"))
@click.option("--events", "events_file", default=None)
@click.option("--db", default=None)
@click.option("--no-metrics", is_flag=True)
def ingest(since, claude_root, codex_root, events_file, db, no_metrics):
    """Parse transcripts + hook events into SQLite, then compute metrics."""
    from .db import connect
    from .ingest import run_ingest
    from .model import events_path
    conn = connect(db)
    run_id = run_ingest(conn, since_days=_days(since), claude_root=claude_root, codex_root=codex_root,
                        events_file=events_file or str(events_path()))
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


@cli.command()
@click.option("--day", default=None, help="today | YYYY-MM-DD")
@click.option("--days", type=int, default=None)
@click.option("--db", default=None)
def report(day, days, db):
    """Markdown daily report (spec §2)."""
    from .db import connect
    from .report import render
    if not day and not days:
        day = "today"
    click.echo(render(connect(db), day=day, days=days))


@cli.command()
@click.option("--port", default=8765, show_default=True)
@click.option("--db", default=None)
def serve(port, db):
    """Local dashboard."""
    from .serve import serve as _serve
    _serve(db, port)


@cli.command()
@click.option("--limit", type=int, default=None)
@click.option("--db", default=None)
def judge(limit, db):
    """Label correction episodes with the LLM judge (cached)."""
    from .db import connect, latest_run_id
    from .judge import run_judge
    conn = connect(db)
    run_judge(conn, latest_run_id(conn), limit=limit)
