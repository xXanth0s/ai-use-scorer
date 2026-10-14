"""`scorer install/uninstall` merge tests. Only ever reads the real config files."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from scorer import install
from scorer.install import MARKER, run_install

SAMPLE_CLAUDE = {
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "model": "opus",
    "hooks": {
        "Notification": [{"matcher": "", "hooks": [{"type": "command", "command": "notify 'input'"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-hook ups"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "other-hook stop"}]},
                 {"hooks": [{"type": "command", "command": "/x/timeline-stop-hook", "timeout": "5"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "other-hook ptu"}]}],
        "SessionStart": [{"matcher": "^(startup|resume)$",
                          "hooks": [{"type": "command", "command": "bash s.sh", "timeout": 10}]}],
    },
    "statusLine": {"type": "command", "command": "sl"},
    "enabledPlugins": {"a@b": True},
}
SAMPLE_CODEX = {"hooks": {
    "SessionStart": [{"hooks": [{"command": "SUPERSET_AGENT_ID=codex notify.sh", "type": "command"}]}],
    "Stop": [{"hooks": [{"command": "SUPERSET_AGENT_ID=codex notify.sh", "type": "command"}]}],
}}


def _realistic(src: str, sample: dict, dst: Path) -> Path:
    """Copy the real file (read-only) if present and canonical, else write the sample."""
    real = Path(os.path.expanduser(src))
    if real.exists():
        text = real.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and install.dumps(data) == text:
            shutil.copyfile(real, dst)
            return dst
    dst.write_text(install.dumps(sample), encoding="utf-8")
    return dst


@pytest.fixture(autouse=True)
def fake_schedulers(tmp_path, monkeypatch):
    """Never touch ~/Library/LaunchAgents, real launchctl or the real crontab."""
    d = tmp_path / "fakebin"
    d.mkdir()
    log = d / "calls.log"
    cron = d / "crontab.txt"
    (d / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    (d / "crontab").write_text(
        f'#!/bin/sh\nif [ "$1" = "-l" ]; then [ -f "{cron}" ] || {{ echo "no crontab" >&2; exit 1; }}; '
        f'cat "{cron}"; else echo "crontab $@" >> "{log}"; cat > "{cron}"; fi\n')
    for f in ("launchctl", "crontab"):
        (d / f).chmod(0o755)
    agents_dir = tmp_path / "LaunchAgents"
    monkeypatch.setenv("SCORER_LAUNCHAGENTS_DIR", str(agents_dir))
    monkeypatch.setenv("SCORER_LAUNCHCTL", str(d / "launchctl"))
    monkeypatch.setenv("SCORER_CRONTAB", str(d / "crontab"))
    return {"agents_dir": agents_dir, "log": log, "cron": cron}


@pytest.fixture
def claude_file(tmp_path):
    return _realistic("~/.claude/settings.json", SAMPLE_CLAUDE, tmp_path / "settings.json")


@pytest.fixture
def codex_home(tmp_path):
    h = tmp_path / "codex"
    h.mkdir()
    _realistic("~/.codex/hooks.json", SAMPLE_CODEX, h / "hooks.json")
    return h


def scorer_groups(data: dict) -> dict:
    return {ev: [g for g in gs if any(MARKER in h.get("command", "") for h in g.get("hooks", []))]
            for ev, gs in data.get("hooks", {}).items()}


def inst(claude_file, codex_home, agents=("claude",), **kw):
    opts = dict(yes=True, dry_run=False, uninstall=False, schedule=False)
    opts.update(kw)
    return run_install(agents=agents, claude_settings=str(claude_file), codex_home=str(codex_home), **opts)


@pytest.mark.parametrize("sample", [False, True])
def test_merge_idempotent_uninstall_byte_equal(tmp_path, claude_file, sample):
    if sample:
        claude_file.write_text(install.dumps(SAMPLE_CLAUDE))
    orig = claude_file.read_bytes()
    before = json.loads(orig)
    assert inst(claude_file, tmp_path) == 0
    after = json.loads(claude_file.read_text())
    assert claude_file.read_text().endswith("}\n")
    assert list(after) == list(before)  # top-level key order kept
    assert (claude_file.with_name("settings.json.bak")).read_bytes() == orig
    sg = scorer_groups(after)
    for ev in install.EVENTS:
        assert len(sg[ev]) == 1
        g = sg[ev][0]
        assert g["hooks"][0]["timeout"] == 5 and g["hooks"][0]["type"] == "command"
        assert f"--agent claude --event {ev}" in g["hooks"][0]["command"]
        assert ("matcher" in g) == (ev == "PostToolUse")
        # non-scorer groups untouched and still first
        others = [x for x in after["hooks"][ev] if x not in sg[ev]]
        assert others == before.get("hooks", {}).get(ev, [])
        assert after["hooks"][ev][-1] == g
    for k in before:
        if k != "hooks":
            assert after[k] == before[k]
    for ev, gs in before.get("hooks", {}).items():
        if ev not in install.EVENTS:
            assert after["hooks"][ev] == gs

    once = claude_file.read_bytes()
    assert inst(claude_file, tmp_path) == 0
    assert claude_file.read_bytes() == once  # idempotent

    assert inst(claude_file, tmp_path, uninstall=True) == 0
    assert claude_file.read_bytes() == orig


def test_dry_run_writes_nothing(tmp_path, claude_file, capsys):
    orig = claude_file.read_bytes()
    st = claude_file.stat()
    assert inst(claude_file, tmp_path, yes=False, dry_run=True) == 0
    assert claude_file.read_bytes() == orig and claude_file.stat().st_mtime_ns == st.st_mtime_ns
    assert not claude_file.with_name("settings.json.bak").exists()
    out = capsys.readouterr().out
    assert "+" in out and "-m scorer event --agent claude --event Stop" in out


def test_default_asks_and_no_means_no(tmp_path, claude_file, monkeypatch):
    orig = claude_file.read_bytes()
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert inst(claude_file, tmp_path, yes=False) == 1
    assert claude_file.read_bytes() == orig
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert inst(claude_file, tmp_path, yes=False) == 0
    assert claude_file.read_bytes() != orig

    def eof(*_):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert inst(claude_file, tmp_path, yes=False, uninstall=True) == 1


def test_missing_file_created_and_uninstall_empties(tmp_path):
    f = tmp_path / "nested" / "settings.json"
    assert inst(f, tmp_path) == 0
    data = json.loads(f.read_text())
    assert set(data["hooks"]) == set(install.EVENTS)
    assert inst(f, tmp_path, uninstall=True) == 0
    assert json.loads(f.read_text()) == {}


def test_invalid_json_not_clobbered(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("{oops")
    assert inst(f, tmp_path) == 1
    assert f.read_text() == "{oops"


def test_stale_scorer_group_replaced_in_place(tmp_path, claude_file, monkeypatch):
    inst(claude_file, tmp_path)
    monkeypatch.setattr(install.sys, "executable", "/new/python")
    inst(claude_file, tmp_path)
    sg = scorer_groups(json.loads(claude_file.read_text()))
    assert all(len(v) == 1 and v[0]["hooks"][0]["command"].startswith('"/new/python"')
               for ev, v in sg.items() if ev in install.EVENTS)


def test_codex_uses_codex_home(tmp_path, claude_file, codex_home, capsys):
    claude_orig = claude_file.read_bytes()
    orig = (codex_home / "hooks.json").read_bytes()
    assert inst(claude_file, codex_home, agents=("codex",)) == 0
    assert claude_file.read_bytes() == claude_orig
    data = json.loads((codex_home / "hooks.json").read_text())
    sg = scorer_groups(data)
    assert all(len(sg[ev]) == 1 for ev in install.EVENTS)
    assert "--agent codex --event SessionEnd" in sg["SessionEnd"][0]["hooks"][0]["command"]
    assert sg["SessionEnd"][0]["hooks"][0]["timeout"] == 3
    out = capsys.readouterr().out
    assert "[features]" in out and "trusted" in out  # no config.toml in tmp home -> flag hint
    assert not (codex_home / "config.toml").exists()
    assert inst(claude_file, codex_home, agents=("codex",), uninstall=True) == 0
    assert (codex_home / "hooks.json").read_bytes() == orig


def test_codex_flag_hint_suppressed_when_enabled(tmp_path, capsys):
    (tmp_path / "config.toml").write_text("[features]\nhooks = true\n")
    inst(tmp_path / "x.json", tmp_path, agents=("codex",), dry_run=True)
    out = capsys.readouterr().out
    assert "[features]" not in out and "trusted" in out
    assert not (tmp_path / "hooks.json").exists()


# --- daily schedule + default config ------------------------------------------

def sched(**kw):
    opts = dict(agents=(), claude_settings="unused", codex_home="unused", yes=True, dry_run=False,
                uninstall=False, schedule=True)
    opts.update(kw)
    return run_install(**opts)


def calls(fs) -> list[str]:
    return fs["log"].read_text().splitlines() if fs["log"].exists() else []


def test_launchd_install_idempotent_uninstall(fake_schedulers, scorer_home, monkeypatch):
    import plistlib
    monkeypatch.setenv("SCORER_SCHEDULER", "launchd")
    scorer_home.mkdir(parents=True)
    (scorer_home / "config.toml").write_text('report_time = "07:45"\n')
    plist = fake_schedulers["agents_dir"] / "com.scorer.daily.plist"
    assert sched() == 0
    d = plistlib.loads(plist.read_bytes())
    assert d["Label"] == "com.scorer.daily"
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 45}
    home = str(scorer_home.resolve())
    assert d["EnvironmentVariables"]["SCORER_HOME"] == home and d["EnvironmentVariables"]["PATH"]
    assert d["StandardOutPath"].startswith(home + "/logs") and d["StandardErrorPath"].startswith(home)
    cmd = d["ProgramArguments"][-1]
    assert "-m scorer ingest --since 2d &&" in cmd and "-m scorer report --day today >" in cmd
    assert '$SCORER_HOME/reports/$(date +%F).md' in cmd
    assert (scorer_home / "logs").is_dir()
    c = calls(fake_schedulers)
    assert len(c) == 1 and c[0].startswith("bootstrap gui/") and c[0].endswith(str(plist))

    assert sched() == 0  # unchanged -> no launchctl calls
    assert len(calls(fake_schedulers)) == 1

    (scorer_home / "config.toml").write_text('report_time = "18:00"\n')
    assert sched() == 0  # changed -> bootout then bootstrap
    assert [x.split()[0] for x in calls(fake_schedulers)] == ["bootstrap", "bootout", "bootstrap"]
    assert plistlib.loads(plist.read_bytes())["StartCalendarInterval"] == {"Hour": 18, "Minute": 0}

    assert sched(uninstall=True) == 0
    assert not plist.exists()
    assert calls(fake_schedulers)[-1].startswith("bootout")


def test_launchd_dry_run_and_decline(fake_schedulers, monkeypatch, capsys):
    monkeypatch.setenv("SCORER_SCHEDULER", "launchd")
    plist = fake_schedulers["agents_dir"] / "com.scorer.daily.plist"
    assert sched(yes=False, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "+<plist" in out or "+<?xml" in out
    assert not plist.exists() and calls(fake_schedulers) == []
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert sched(yes=False) == 1
    assert not plist.exists() and calls(fake_schedulers) == []


def test_cron_install_keeps_other_lines_and_uninstall(fake_schedulers, monkeypatch):
    monkeypatch.setenv("SCORER_SCHEDULER", "cron")
    fake_schedulers["cron"].write_text("MAILTO=x\n0 1 * * * backup.sh\n")
    assert sched() == 0
    lines = fake_schedulers["cron"].read_text().splitlines()
    assert lines[:2] == ["MAILTO=x", "0 1 * * * backup.sh"] and len(lines) == 3
    assert lines[2].startswith("0 18 * * * ") and lines[2].endswith("# scorer-daily")
    assert r"date +\%F" in lines[2] and "date +%F" not in lines[2]  # cron % escaping
    assert sched() == 0
    assert len(fake_schedulers["cron"].read_text().splitlines()) == 3
    assert sched(uninstall=True) == 0
    assert fake_schedulers["cron"].read_text() == "MAILTO=x\n0 1 * * * backup.sh\n"


def test_cron_no_existing_crontab(fake_schedulers, monkeypatch):
    monkeypatch.setenv("SCORER_SCHEDULER", "cron")
    assert sched(dry_run=True) == 0
    assert not fake_schedulers["cron"].exists()
    assert sched() == 0
    assert fake_schedulers["cron"].read_text().count("# scorer-daily") == 1


def test_no_schedule_env_and_flag(fake_schedulers, monkeypatch):
    monkeypatch.setenv("SCORER_SCHEDULER", "launchd")
    monkeypatch.setenv("SCORER_NO_SCHEDULE", "1")
    assert sched(schedule=None) == 0
    assert sched(schedule=False) == 0
    assert not fake_schedulers["agents_dir"].exists() and calls(fake_schedulers) == []


def test_default_config_written_once(fake_schedulers, scorer_home, tmp_path):
    import tomllib
    assert inst(tmp_path / "s.json", tmp_path, dry_run=True) == 0
    assert not (scorer_home / "config.toml").exists()
    assert inst(tmp_path / "s.json", tmp_path) == 0
    cfg = scorer_home / "config.toml"
    text = cfg.read_text()
    assert 'report_time = "18:00"' in text and tomllib.loads(text) == {}  # all commented
    cfg.write_text('report_time = "09:00"\n')
    inst(tmp_path / "s.json", tmp_path)
    assert cfg.read_text() == 'report_time = "09:00"\n'


# --- lab mode: project-scoped hooks + ingest cwd filter -----------------------

def test_project_install_only_touches_project(tmp_path, claude_file, codex_home, scorer_home,
                                              fake_schedulers, capsys):
    import hashlib
    proj = tmp_path / "lab" / "proj"
    proj.mkdir(parents=True)
    glob_sha = hashlib.sha256(claude_file.read_bytes()).hexdigest()
    codex_sha = hashlib.sha256((codex_home / "hooks.json").read_bytes()).hexdigest()
    kw = dict(agents=("claude", "codex"), claude_settings=str(claude_file), codex_home=str(codex_home),
              uninstall=False, project=str(proj))

    assert run_install(yes=False, dry_run=True, **kw) == 0
    assert not (proj / ".claude").exists() and not (proj / ".codex").exists()

    assert run_install(yes=True, dry_run=False, **kw) == 0
    local = proj / ".claude" / "settings.local.json"
    data = json.loads(local.read_text())
    sg = scorer_groups(data)
    home = str(scorer_home.resolve())
    for ev in install.EVENTS:
        cmd = sg[ev][0]["hooks"][0]["command"]
        assert cmd.startswith(f'SCORER_HOME="{home}" "') and f"--agent claude --event {ev}" in cmd
    assert scorer_groups(json.loads((proj / ".codex" / "hooks.json").read_text()))["Stop"]
    out = capsys.readouterr().out
    assert "rollouts only (filter by --cwd-prefix)" in out

    once = local.read_bytes()
    assert run_install(yes=True, dry_run=False, **kw) == 0
    assert local.read_bytes() == once  # idempotent

    # nothing global touched: no schedule, no config.toml, global files unchanged
    assert hashlib.sha256(claude_file.read_bytes()).hexdigest() == glob_sha
    assert hashlib.sha256((codex_home / "hooks.json").read_bytes()).hexdigest() == codex_sha
    assert calls(fake_schedulers) == [] and not fake_schedulers["agents_dir"].exists()
    assert not (scorer_home / "config.toml").exists()

    assert run_install(yes=True, dry_run=False, **dict(kw, uninstall=True)) == 0
    assert not (proj / ".claude").exists() and not (proj / ".codex").exists()
    assert sorted(p.name for p in proj.iterdir()) == []


def test_project_install_merges_existing_local_settings(tmp_path):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    local = proj / ".claude" / "settings.local.json"
    local.write_text(install.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    orig = local.read_bytes()
    kw = dict(agents=("claude",), claude_settings="unused", codex_home="unused", yes=True, dry_run=False,
              project=str(proj))
    assert run_install(uninstall=False, **kw) == 0
    assert json.loads(local.read_text())["permissions"] == {"allow": ["Bash(ls)"]}
    assert run_install(uninstall=True, **kw) == 0
    assert local.read_bytes() == orig


def test_project_must_exist(tmp_path):
    assert run_install(agents=("claude",), claude_settings="x", codex_home="x", yes=True, dry_run=False,
                       uninstall=False, project=str(tmp_path / "nope")) == 1


def _fake_ingest(monkeypatch, tmp_path, cwds: dict[str, str]):
    from scorer import ingest, parse_claude
    from scorer.model import Bundle, Session
    monkeypatch.setattr(ingest, "discover_claude", lambda root, days: sorted(cwds))
    monkeypatch.setattr(ingest, "discover_codex", lambda root, days: [])
    monkeypatch.setattr(parse_claude, "parse",
                        lambda path, hooks=None: Bundle(Session(id=path, agent="claude", cwd=cwds[path])))
    return ingest


def _sessions(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute("SELECT id FROM session"))


def test_ingest_cwd_prefix_filter(tmp_path, monkeypatch):
    from scorer.db import connect
    lab = tmp_path / "lab"
    (lab / "proj").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    link = tmp_path / "lablink"
    link.symlink_to(lab)  # like /tmp -> /private/tmp
    ingest = _fake_ingest(monkeypatch, tmp_path, {
        "a": str(link / "proj"), "b": str(other), "c": str(tmp_path / "lab2"), "d": None})
    monkeypatch.delenv("SCORER_CWD_PREFIX", raising=False)

    conn = connect(tmp_path / "f.db")
    ingest.run_ingest(conn, since_days=30, claude_root="x", codex_root="x", events_file=None,
                      cwd_prefixes=[str(lab)])
    assert _sessions(conn) == ["a"]  # symlink resolved; lab2 is not under lab; None excluded

    conn = connect(tmp_path / "all.db")
    ingest.run_ingest(conn, since_days=30, claude_root="x", codex_root="x", events_file=None)
    assert _sessions(conn) == ["a", "b", "c", "d"]

    monkeypatch.setenv("SCORER_CWD_PREFIX", os.pathsep.join([str(other), str(link)]))
    conn = connect(tmp_path / "env.db")
    ingest.run_ingest(conn, since_days=30, claude_root="x", codex_root="x", events_file=None)
    assert _sessions(conn) == ["a", "b"]
