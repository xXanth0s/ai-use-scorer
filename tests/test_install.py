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
    opts = dict(yes=True, dry_run=False, uninstall=False)
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
