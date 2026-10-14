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


def hook_command(agent: str, event: str, home: str | None = None) -> str:
    """`home` pins SCORER_HOME (project/lab installs) regardless of the agent's env."""
    # `2>/dev/null || true`: a broken venv must never print or exit non-zero.
    prefix = f'SCORER_HOME="{home}" ' if home else ""
    return (prefix + f'"{os.path.abspath(sys.executable)}" -m scorer event --agent {agent} --event {event}'
            " 2>/dev/null || true")


def _is_scorer_group(group) -> bool:
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks") or []:
        if isinstance(h, dict) and MARKER in str(h.get("command", "")):
            return True
    return False


def _group(agent: str, event: str, home: str | None = None) -> dict:
    timeout = CODEX_TIMEOUTS.get(event, TIMEOUT) if agent == "codex" else TIMEOUT
    g: dict = {}
    if event == "PostToolUse":
        g["matcher"] = "*"
    g["hooks"] = [{"type": "command", "command": hook_command(agent, event, home), "timeout": timeout}]
    return g


def merge(data: dict, agent: str, home: str | None = None) -> dict:
    new = copy.deepcopy(data)
    hooks = new.setdefault("hooks", {})
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        want = _group(agent, ev, home)
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
         uninstall: bool, home: str | None = None, delete_if_empty: bool = False) -> int:
    try:
        data, old_text = _load(path)
    except (ValueError, OSError) as e:
        print(f"{label}: cannot parse {path}: {e}; not touching it", file=sys.stderr)
        return 1
    new = unmerge(data) if uninstall else merge(data, agent, home)
    if new == data:
        print(f"{label}: {path} already up to date")
        return 0
    old_canon = old_text if old_text else ""
    remove = uninstall and delete_if_empty and not new
    new_text = "" if remove else (dumps(new) if new or path.exists() else "")
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
    if remove:
        path.unlink()
        try:
            path.parent.rmdir()  # only if now empty
        except OSError:
            pass
        print(f"{label}: removed {path} (only scorer hooks were left)")
        return 0
    _atomic_write(path, new_text)
    print(f"{label}: wrote {path} (backup: {path.name}.bak)" if old_text
          else f"{label}: wrote {path}")
    return 0


# --- daily ingest + report job ---------------------------------------------
LABEL = "com.scorer.daily"
CRON_TAG = "# scorer-daily"

DEFAULT_CONFIG = """\
# scorer config (all keys optional; uncomment to override the default)
# timezone = "Europe/Berlin"      # default: system tz
# day_cutoff = "04:00"            # turns before it belong to the previous day
# report_time = "18:00"           # daily launchd/cron ingest + report
# judge_model = "claude-haiku-5-5"
# analyst_model = "claude-opus-5-5"
# [prices."claude-opus-5-5"]      # USD per 1M tokens; overrides prices.py
# input = 5.0
# cached = 0.5
# cache_write = 6.25
# output = 25.0
"""


def _scheduler() -> str:
    """launchd on macOS, cron elsewhere. SCORER_SCHEDULER=launchd|cron overrides (tests)."""
    s = os.environ.get("SCORER_SCHEDULER")
    if s in ("launchd", "cron"):
        return s
    return "launchd" if sys.platform == "darwin" else "cron"


def _launchagents_dir() -> Path:
    return Path(os.environ.get("SCORER_LAUNCHAGENTS_DIR") or os.path.expanduser("~/Library/LaunchAgents"))


def _job_shell(home: Path) -> str:
    """Shell command for the daily job; expects $SCORER_HOME in its environment."""
    py = f'"{os.path.abspath(sys.executable)}"'
    return (f'mkdir -p "$SCORER_HOME/reports" && {py} -m scorer ingest --since 2d && '
            f'{py} -m scorer report --day today > "$SCORER_HOME/reports/$(date +%F).md"')


def _job_path_env() -> str:
    dirs = [os.path.dirname(os.path.abspath(sys.executable)),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def _report_time():
    from . import config
    config.load.cache_clear()
    return config.load().report_time


def plist_text(home: Path, hour: int, minute: int) -> str:
    import plistlib
    d = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", _job_shell(home)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"SCORER_HOME": str(home), "PATH": _job_path_env()},
        "StandardOutPath": str(home / "logs" / "daily.out.log"),
        "StandardErrorPath": str(home / "logs" / "daily.err.log"),
        "RunAtLoad": False,
    }
    return plistlib.dumps(d, sort_keys=False).decode("utf-8")


def cron_line(home: Path, hour: int, minute: int) -> str:
    # cron treats a bare % as newline -> escape it.
    cmd = _job_shell(home).replace("%", r"\%")
    log = home / "logs" / "daily.log"
    return (f'{minute} {hour} * * * SCORER_HOME="{home}" PATH="{_job_path_env()}" '
            f'/bin/sh -c \'{cmd}\' >> "{log}" 2>&1 {CRON_TAG}')


def _run(cmd: list[str], stdin: str | None = None):
    import subprocess
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=20)


def _diff(old: str, new: str, name: str, what: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="",
                                          fromfile=f"{name} (current)", tofile=f"{name} ({what})"))


def _apply_gate(label: str, diff: str, target: str, *, yes: bool, dry_run: bool) -> int | None:
    """Print diff; return an rc to stop with, or None to proceed."""
    print(diff)
    if dry_run:
        print(f"{label}: dry run, {target} not changed")
        return 0
    if not yes and not _confirm(f"Apply to {target}? [y/N] "):
        print(f"{label}: skipped")
        return 1
    return None


def _schedule_launchd(home: Path, hm, *, yes, dry_run, uninstall) -> int:
    plist = _launchagents_dir() / f"{LABEL}.plist"
    launchctl = os.environ.get("SCORER_LAUNCHCTL") or "launchctl"
    domain = f"gui/{os.getuid()}"
    old = plist.read_text(encoding="utf-8") if plist.exists() else ""
    new = "" if uninstall else plist_text(home, hm.hour, hm.minute)
    if old == new:
        print(f"schedule: {plist} already up to date")
        return 0
    rc = _apply_gate("schedule", _diff(old, new, str(plist), "uninstall" if uninstall else "install"),
                     str(plist), yes=yes, dry_run=dry_run)
    if rc is not None:
        return rc
    if old:
        _run([launchctl, "bootout", domain, str(plist)])  # ignore "not loaded"
    if uninstall:
        plist.unlink()
        print(f"schedule: removed {plist}")
        return 0
    (home / "logs").mkdir(parents=True, exist_ok=True)
    _atomic_write(plist, new)
    r = _run([launchctl, "bootstrap", domain, str(plist)])
    if r.returncode != 0:
        print(f"schedule: launchctl bootstrap failed ({r.returncode}): {(r.stderr or '').strip()[:200]}",
              file=sys.stderr)
        return 1
    print(f"schedule: loaded {LABEL} daily at {hm.hour:02d}:{hm.minute:02d}")
    return 0


def _schedule_cron(home: Path, hm, *, yes, dry_run, uninstall) -> int:
    crontab = os.environ.get("SCORER_CRONTAB") or "crontab"
    r = _run([crontab, "-l"])
    old = r.stdout if r.returncode == 0 else ""  # "no crontab for user" -> empty
    kept = [l for l in old.splitlines() if not l.rstrip().endswith(CRON_TAG)]
    if not uninstall:
        kept.append(cron_line(home, hm.hour, hm.minute))
    new = "\n".join(kept) + "\n" if kept else ""
    if new == old or (not old.strip() and not new.strip()):
        print("schedule: crontab already up to date")
        return 0
    rc = _apply_gate("schedule", _diff(old, new, "crontab", "uninstall" if uninstall else "install"),
                     "crontab", yes=yes, dry_run=dry_run)
    if rc is not None:
        return rc
    (home / "logs").mkdir(parents=True, exist_ok=True)
    r = _run([crontab, "-"], stdin=new)
    if r.returncode != 0:
        print(f"schedule: crontab write failed: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
        return 1
    print("schedule: crontab updated")
    return 0


def run_schedule(*, yes: bool, dry_run: bool, uninstall: bool) -> int:
    from .model import scorer_home
    home = scorer_home().expanduser().resolve()
    hm = _report_time()
    fn = _schedule_launchd if _scheduler() == "launchd" else _schedule_cron
    return fn(home, hm, yes=yes, dry_run=dry_run, uninstall=uninstall)


def ensure_default_config(*, dry_run: bool) -> None:
    from .model import scorer_home
    p = scorer_home() / "config.toml"
    if p.exists():
        return
    if dry_run:
        print(f"config: would write default {p}")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_CONFIG, encoding="utf-8")
    print(f"config: wrote default {p}")


CODEX_PROJECT_NOTE = ("codex: project hooks go to <project>/.codex/hooks.json (codex 0.160 loads "
                      "project-local hooks for trusted projects; not verified at runtime). They only run "
                      "once the project and the hooks are trusted; otherwise codex is measured from "
                      "rollouts only (filter by --cwd-prefix).")


def run_project_install(project: str, *, agents, codex_home, yes, dry_run, uninstall) -> int:
    """Lab mode: hooks only in <project>/.claude/settings.local.json (+ <project>/.codex/hooks.json),
    commands pinned to the current SCORER_HOME. No schedule, no global files."""
    from .model import scorer_home
    root = Path(os.path.expanduser(project)).resolve()
    if not root.is_dir():
        print(f"project: {root} is not a directory", file=sys.stderr)
        return 1
    home = str(scorer_home().expanduser().resolve())
    rc = 0
    for agent in agents:
        if agent == "claude":
            rc |= _one("claude", root / ".claude" / "settings.local.json", "claude",
                       yes=yes, dry_run=dry_run, uninstall=uninstall, home=home,
                       delete_if_empty=True)
        elif agent == "codex":
            rc |= _one("codex", root / ".codex" / "hooks.json", "codex",
                       yes=yes, dry_run=dry_run, uninstall=uninstall, home=home,
                       delete_if_empty=True)
            if not uninstall:
                print(CODEX_PROJECT_NOTE)
                for n in _codex_notes(Path(os.path.expanduser(codex_home))):
                    print(n)
    if not uninstall:
        print(f"project: events from {root} go to SCORER_HOME={home}")
    return rc


def run_install(*, agents, claude_settings, codex_home, yes, dry_run, uninstall,
                schedule: bool | None = None, project: str | None = None) -> int:
    """`schedule=None` -> on unless env SCORER_NO_SCHEDULE=1. `project` -> lab mode (see above)."""
    if project:
        return run_project_install(project, agents=agents, codex_home=codex_home, yes=yes,
                                   dry_run=dry_run, uninstall=uninstall)
    if schedule is None:
        schedule = os.environ.get("SCORER_NO_SCHEDULE") not in ("1", "true", "yes")
    rc = 0
    if not uninstall:
        ensure_default_config(dry_run=dry_run)
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
    if schedule:
        rc |= run_schedule(yes=yes, dry_run=dry_run, uninstall=uninstall)
    return rc
