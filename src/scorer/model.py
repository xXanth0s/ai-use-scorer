"""Fixed interface between parsers, ingest, metrics, report and dashboard.

Parsers (parse_claude, parse_codex) are pure: `parse(path) -> Bundle | None`.
They never touch the DB. `ingest` maps a Bundle onto the DDL in `db.py`.

Keys inside a Bundle are parser-local: children point at their turn by
`turn_idx` (Turn.idx, 0-based within the session) and file edits point at
their tool call by `tool_use_id`. Ingest resolves these to row ids.

All timestamps are ISO-8601 strings in UTC with offset ("...+00:00").
Day bucketing is ALWAYS local time via `local_day()`.
Any human text (prompt, command, path) that leaves the DB for a log, report,
dashboard or test output MUST go through `clip()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PARSER_VERSION = "1"
DEFINITION_VERSION = "v1"

INTERRUPT_PREFIX = "[Request interrupted by user"


def scorer_home() -> Path:
    """Root for events.jsonl and scorer.db. Tests MUST set SCORER_HOME."""
    return Path(os.environ.get("SCORER_HOME") or os.path.expanduser("~/.scorer"))


def events_path() -> Path:
    return scorer_home() / "events.jsonl"


def db_path() -> Path:
    return scorer_home() / "scorer.db"


def clip(text: str | None, n: int = 80) -> str:
    """The only allowed way to surface human text. Single line, <= n chars."""
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat() if d else None


def local_tz():
    """Configured timezone (config.toml `timezone`), else the system tz."""
    from .config import load
    name = load().timezone
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def local_day(ts: str | datetime | None) -> str | None:
    """Report day YYYY-MM-DD: configured tz, shifted by day_cutoff (default 04:00 ->
    a turn at 02:30 belongs to the previous day). The ONLY day-bucketing function."""
    from datetime import timedelta
    from .config import load
    d = parse_ts(ts) if isinstance(ts, str) else ts
    if not d:
        return None
    c = load().day_cutoff
    d = d.astimezone(local_tz()) - timedelta(hours=c.hour, minutes=c.minute)
    return d.strftime("%Y-%m-%d")


# --- vocabularies -----------------------------------------------------------
AGENTS = ("claude", "codex")
PROMPT_KINDS = ("prompt", "command", "queued", "interrupt")  # interrupt = marker only
END_SOURCES = ("stop_hook", "turn_duration", "last_entry", "crash", "interrupt", "task_complete")
INITIATORS = ("human", "agent", "default")
TOOL_KINDS = ("edit", "bash", "read", "search", "subagent", "web", "other")
BASH_INTENTS = ("test", "build", "edit", "git", "run", "search", "read", "other")
EVENT_KINDS = (
    "interrupt", "compaction", "queue_enqueue", "queue_dequeue", "permission_wait",
    "subagent", "hook", "git_snapshot", "error", "tooling",
)
# Event(kind="tooling", data={"category": TOOLING_CATEGORIES, "name": str, "initiator": INITIATORS,
#                             "rule": str, "tool_use_id": str | None})
# category: skill | command (slash command) | mcp (data.name = server) | plugin (data.name = plugin)
# initiator: Claude <command-name>/x in user entry -> human; Skill tool_use / attributionSkill -> agent;
#            hook-triggered -> default. Codex: /skill or $skill in user message -> human; skill items -> agent.
TOOLING_CATEGORIES = ("skill", "command", "mcp", "plugin")


@dataclass
class Session:
    id: str
    agent: str                      # claude | codex
    cwd: str | None = None
    repo: str | None = None         # git toplevel if known, else cwd
    branch: str | None = None
    transcript_path: str | None = None
    forked_from: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    ended_by: str | None = None


@dataclass
class Turn:
    idx: int
    prompt_at: str                  # prompt ts (dequeue ts if queued)
    prompt_text: str | None = None  # raw; clip() before surfacing
    prompt_kind: str = "prompt"
    initiator: str = "human"
    agent_started_at: str | None = None   # first assistant entry
    agent_finished_at: str | None = None  # per end precedence
    end_source: str | None = None
    first_uuid: str | None = None
    last_uuid: str | None = None
    interrupted: bool = False       # stored as event(kind=interrupt), not a column
    enqueued_at: str | None = None  # for queued prompts


@dataclass
class ApiCall:
    turn_idx: int | None
    request_id: str                 # Claude requestId | Codex response_id; dedupe key
    model: str | None = None
    effort: str | None = None
    input_tok: int = 0              # uncached input
    cached_tok: int = 0             # cache read
    output_tok: int = 0
    cache_write_tok: int = 0        # not a column; folded into input_tok for context
    context_window: int | None = None
    cost_usd: float | None = None   # None = unpriced, never 0
    duration_ms: int | None = None
    at: str | None = None
    sidechain_id: str | None = None

    @property
    def context_tokens(self) -> int:
        return self.input_tok + self.cached_tok + self.cache_write_tok


@dataclass
class ToolCall:
    turn_idx: int | None
    tool_use_id: str
    name: str
    kind: str = "other"             # TOOL_KINDS
    bash_intent: str | None = None
    initiator: str = "agent"
    initiator_rule: str | None = None   # e.g. "3:prompt:/test/" (matched span clipped)
    args_summary: str | None = None     # raw command / path; clip() before surfacing
    started_at: str | None = None
    duration_ms: int | None = None
    is_error: bool = False
    has_result: bool = False
    sidechain_id: str | None = None     # subagent id if run inside a subagent
    cwd: str | None = None              # not a column; for relevance checks


@dataclass
class FileEdit:
    tool_use_id: str
    path: str
    manifest_dir: str | None = None
    lines_added: int = 0
    lines_removed: int = 0
    user_modified: bool = False


@dataclass
class Event:
    kind: str                       # EVENT_KINDS
    at: str
    turn_idx: int | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Bundle:
    session: Session
    turns: list[Turn] = field(default_factory=list)
    api_calls: list[ApiCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    file_edits: list[FileEdit] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
