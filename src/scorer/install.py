"""`scorer install` / `scorer uninstall`: merge scorer hook groups into
Claude's settings.json and Codex's <CODEX_HOME>/hooks.json (same schema):

  {"hooks": {Event: [{"matcher"?: "*", "hooks": [{"type": "command", "command": ..., "timeout": 5}]}]}}

Rules: exactly one scorer group per event (identified by "-m scorer event" in a
command), other groups/keys untouched and in order, idempotent, atomic write
with a .bak of the previous file, unified diff shown before applying.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
MARKER = "-m scorer event"
TIMEOUT = 5
CODEX_TIMEOUTS = {"SessionEnd": 3}  # codex clamps SessionEnd to 3 s and warns otherwise


def hook_command(agent: str, event: str) -> str:
    return f'"{os.path.abspath(sys.executable)}" -m scorer event --agent {agent} --event {event}'


def _is_scorer_group(group) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks") or []:
        if isinstance(h, dict) and MARKER in str(h.get("command", "")):
            return True
    return False


def _group(agent: str, event: str) -> dict:
    timeout = CODEX_TIMEOUTS.get(event, TIMEOUT) if agent == "codex" else TIMEOUT
    g: dict = {}
    if event == "PostToolUse":
        g["matcher"] = "*"
    g["hooks"] = [{"type": "command", "command": hook_command(agent, event), "timeout": timeout}]
    return g


def merge(data: dict, agent: str) -> dict:
    new = copy.deepcopy(data)
    hooks = new.setdefault("hooks", {})
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        want = _group(agent, ev)
        idx = [i for i, g in enumerate(groups) if _is_scorer_group(g)]
        if idx:
            groups[idx[0]] = want  # replace in place (e.g. new interpreter path)
            for i in reversed(idx[1:]):
                del groups[i]
        else:
            groups.append(want)
    return new


def unmerge(data: dict) -> dict:
    new = copy.deepcopy(data)
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        return new
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not _is_scorer_group(g)]
        if len(kept) == len(groups):
            continue
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        del new["hooks"]
    return new


def dumps(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _load(path: Path) -> tuple[dict | None, str]:
    """-> (data, original text). Missing/empty file -> ({}, "")."""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}, text
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON is not an object")
    return data, text


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o7777
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _codex_notes(codex_home: Path) -> list[str]:
    notes = []
    cfg = codex_home / "config.toml"
    enabled = False
    try:
        import tomllib
        with open(cfg, "rb") as f:
            enabled = bool(tomllib.load(f).get("features", {}).get("hooks"))
    except Exception:
        enabled = False
    if not enabled:
        notes.append(f"codex: hooks are behind a feature flag. Add to {cfg}:\n"
                     "        [features]\n        hooks = true")
    notes.append("codex: new hooks only run once trusted (config.toml [hooks.state] trusted_hash). "
                 "Start `codex` interactively once and approve the scorer hooks; until then "
                 "they are skipped silently (incl. `codex exec`).")
    return notes


def _one(label: str, path: Path, agent: str | None, *, yes: bool, dry_run: bool,
         uninstall: bool) -> int:
    try:
        data, old_text = _load(path)
    except (ValueError, OSError) as e:
        print(f"{label}: cannot parse {path}: {e}; not touching it", file=sys.stderr)
        return 1
    new = unmerge(data) if uninstall else merge(data, agent)
    if new == data:
        print(f"{label}: {path} already up to date")
        return 0
    old_canon = old_text if old_text else ""
    new_text = dumps(new) if new or path.exists() else ""
    diff = "\n".join(difflib.unified_diff(
        old_canon.splitlines(), new_text.splitlines(), lineterm="",
        fromfile=f"{path} (current)", tofile=f"{path} (scorer {'uninstall' if uninstall else 'install'})"))
    print(diff)
    if dry_run:
        print(f"{label}: dry run, {path} not written")
        return 0
    if not yes and not _confirm(f"Apply to {path}? [y/N] "):
        print(f"{label}: skipped")
        return 1
    _atomic_write(path, new_text)
    print(f"{label}: wrote {path} (backup: {path.name}.bak)" if old_text
          else f"{label}: wrote {path}")
    return 0


def run_install(*, agents, claude_settings, codex_home, yes, dry_run, uninstall) -> int:
    rc = 0
    for agent in agents:
        if agent == "claude":
            rc |= _one("claude", Path(os.path.expanduser(claude_settings)), "claude",
                       yes=yes, dry_run=dry_run, uninstall=uninstall)
        elif agent == "codex":
            home = Path(os.path.expanduser(codex_home))
            rc |= _one("codex", home / "hooks.json", "codex",
                       yes=yes, dry_run=dry_run, uninstall=uninstall)
            if not uninstall:
                for n in _codex_notes(home):
                    print(n)
    return rc
