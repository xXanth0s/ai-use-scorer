"""`scorer event` — the hook entry point. STDLIB ONLY, no click, no sqlite.

Contract (spec §5): read ALL of stdin, append exactly one JSON line to
`events_path()` with a single O_APPEND write, print nothing, exit 0 always,
finish well under 1 s. At Stop/SessionEnd, attach a git snapshot of the
payload's cwd (and at SessionStart, as the baseline the Stop deltas are diffed against).

Line schema:
  {"at": utc iso, "agent": "claude|codex", "event": <argv --event>,
   "hook_event_name": <payload's>, "payload": <parsed json | {"raw": ...}>,
   "git": {...}  # only at Stop / SessionEnd, when cwd is a git repo}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from .model import events_path

RAW_CLIP = 2000          # invalid stdin -> payload {"raw": first 2000 chars}
FIELD_CLIP = 2048        # per value inside tool_input / tool_response
MSG_CLIP = 2048          # last_assistant_message etc. (full text lives in the transcript)
GIT_BUDGET_S = 0.7       # total wall budget for the three git calls
GIT_CALL_TIMEOUT_S = 0.5
MAX_PATHS = 200
SNAPSHOT_EVENTS = ("SessionStart", "Stop", "SessionEnd")   # SessionStart = baseline for hand-edit deltas


def _parse_argv(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    agent = event = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--agent", "--event") and i + 1 < len(argv):
            if a == "--agent":
                agent = argv[i + 1]
            else:
                event = argv[i + 1]
            i += 2
            continue
        if a.startswith("--agent="):
            agent = a.split("=", 1)[1]
        elif a.startswith("--event="):
            event = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return agent, event, rest


def _read_stdin() -> bytes:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return b""
        return sys.stdin.buffer.read()
    except Exception:
        return b""


def _parse_payload(data: bytes):
    """-> dict. Invalid UTF-8 / JSON / non-object is kept under "raw" (clipped)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"raw": data.decode("utf-8", errors="replace")[:RAW_CLIP]}
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except ValueError:
        return {"raw": text[:RAW_CLIP]}
    return obj if isinstance(obj, dict) else {"raw": text[:RAW_CLIP]}


def _clip_value(v):
    if isinstance(v, str):
        return v if len(v) <= FIELD_CLIP else v[:FIELD_CLIP] + "…"
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = json.dumps(v, ensure_ascii=False)
    return v if len(s) <= FIELD_CLIP else s[:FIELD_CLIP] + "…"


def _clip_container(v):
    """tool_input / tool_response: dict -> clip each value; anything else -> clip whole."""
    if isinstance(v, dict):
        return {k: _clip_value(x) for k, x in v.items()}
    return _clip_value(v)


def _shape_payload(payload: dict) -> dict:
    """Keep every key; truncate the bulky ones so lines stay small."""
    out = {}
    for k, v in payload.items():
        if k in ("tool_input", "tool_response"):
            out[k] = _clip_container(v)
        elif isinstance(v, str) and len(v) > MSG_CLIP:
            out[k] = v[:MSG_CLIP] + "…"
        elif isinstance(v, (dict, list)):
            out[k] = _clip_value(v)
        else:
            out[k] = v
    return out


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


_STAT_RE = {
    "files_changed": re.compile(r"(\d+) files? changed"),
    "insertions": re.compile(r"(\d+) insertions?\(\+\)"),
    "deletions": re.compile(r"(\d+) deletions?\(-\)"),
}


def _git(cwd: str, args: list[str], deadline: float) -> str | None:
    left = deadline - time.monotonic()
    if left <= 0.02:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=min(GIT_CALL_TIMEOUT_S, left),
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def _parse_branch(line: str) -> str | None:
    s = line[3:].strip()
    for pre in ("No commits yet on ", "Initial commit on "):
        if s.startswith(pre):
            return s[len(pre):].strip() or None
    if s.startswith("HEAD (no branch)"):
        return None
    return s.split("...", 1)[0].split(" ", 1)[0] or None


def _git_snapshot(cwd, budget: float = GIT_BUDGET_S) -> dict | None:
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return None
    deadline = time.monotonic() + budget
    status = _git(cwd, ["status", "--porcelain=v1", "-b"], deadline)
    if status is None:
        return None  # not a repo (or git missing / too slow)
    head = _git(cwd, ["rev-parse", "HEAD"], deadline)
    stat = _git(cwd, ["diff", "--stat", "HEAD"], deadline)

    branch = None
    changed: list[str] = []
    untracked: list[str] = []
    for line in status.splitlines():
        if line.startswith("## "):
            branch = _parse_branch(line)
        elif line.startswith("?? "):
            untracked.append(line[3:])
        elif len(line) > 3:
            p = line[3:]
            changed.append(p.split(" -> ", 1)[1] if " -> " in p else p)

    snap = {
        "head": head.strip() if head else None,
        "branch": branch,
        "diff_stat_sha1": _sha1(stat) if stat is not None else None,
        "untracked_sha1": _sha1("\n".join(sorted(untracked))),
        "files_changed": 0,
        "insertions": 0,
        "deletions": 0,
        "untracked_count": len(untracked),
        # tracked changes + untracked files (a hand edit may create a new file), MAX_PATHS in total
        "changed_paths": (changed + untracked)[:MAX_PATHS],
        "untracked_paths": untracked[:max(0, MAX_PATHS - len(changed))],
    }
    if stat:
        last = stat.strip().splitlines()[-1] if stat.strip() else ""
        for key, rx in _STAT_RE.items():
            m = rx.search(last)
            if m:
                snap[key] = int(m.group(1))
    return snap


def build_line(agent, event, data: bytes, extra_args: list[str] | None = None) -> dict:
    payload = _parse_payload(data)
    if not payload and extra_args:
        # legacy Codex `notify` style passes JSON as the last argv
        for a in reversed(extra_args):
            if a.lstrip().startswith("{"):
                payload = _parse_payload(a.encode("utf-8", errors="replace"))
                break
    line = {
        "at": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "event": event,
        "hook_event_name": payload.get("hook_event_name"),
        "payload": _shape_payload(payload),
    }
    if event in SNAPSHOT_EVENTS or line["hook_event_name"] in SNAPSHOT_EVENTS:
        snap = _git_snapshot(payload.get("cwd"))
        if snap is not None:
            line["git"] = snap
    return line


def _write_line(obj: dict) -> None:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8", errors="replace")
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, buf)
    finally:
        os.close(fd)


def main_event(argv: list[str]) -> None:
    """Never raises, never prints, always exits 0."""
    try:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        agent, event, rest = _parse_argv(argv)
        data = _read_stdin()
        _write_line(build_line(agent, event, data, rest))
    except BaseException:
        pass
    finally:
        os._exit(0)
