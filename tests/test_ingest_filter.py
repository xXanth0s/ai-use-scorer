"""Default exclusion of lab / scratch / temp sessions from ingest (+ purge of stored ones)."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

HOME = "/fakehome/u"
REAL = {"real": "/srv/work/app", "real2": "/srv/work/lab-notes-not/sub", "none": None}
LAB = {
    "tmp": "/tmp/scratch",
    "ptmp": "/private/tmp/x",
    "folders": "/private/var/folders/ab/T/pytest-1/repo",
    "tmpdir": "/scratchtmp/T/thing",
    "lab": f"{HOME}/scorer-lab/lab-todo-cli",
    "labhome": f"{HOME}/.scorer-lab/judge",
    "scorerx": f"{HOME}/.scorer-e2e/work",
    "capture": "/srv/scorer-capture-repo.AbC123",
    "e2e": "/srv/scorer-e2e.XyZ",
    "judge": "/srv/x/scorer-judge",
    "labbase": "/srv/lab-kata",
}


@pytest.fixture
def env(monkeypatch, scorer_home):
    from scorer import config
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setenv("TMPDIR", "/scratchtmp/T/")
    monkeypatch.delenv("SCORER_CWD_PREFIX", raising=False)
    scorer_home.mkdir(parents=True, exist_ok=True)
    config.load.cache_clear()
    yield scorer_home
    config.load.cache_clear()


def _fake(monkeypatch, cwds):
    from scorer import ingest, parse_claude
    from scorer.model import Bundle, Session, Turn
    monkeypatch.setattr(ingest, "discover_claude", lambda root, days: sorted(cwds))
    monkeypatch.setattr(ingest, "discover_codex", lambda root, days: [])
    monkeypatch.setattr(parse_claude, "parse", lambda path, hooks=None: Bundle(
        Session(id=path, agent="claude", cwd=cwds[path]),
        turns=[Turn(idx=0, prompt_at="2026-10-14T10:00:00Z", interrupted=True)]))
    return ingest


def _ids(conn):
    return sorted(r[0] for r in conn.execute("SELECT id FROM session"))


def _run(ingest, conn, **kw):
    ingest.run_ingest(conn, since_days=30, claude_root="x", codex_root="x", events_file=None, **kw)


def test_default_exclusion(env, monkeypatch, tmp_path):
    from scorer.db import connect
    ingest = _fake(monkeypatch, {**REAL, **LAB, "judgewd": str(env / "judge" / "sub")})
    conn = connect(tmp_path / "t.db")
    _run(ingest, conn, exclude_cwd=ingest.default_exclude_patterns())
    assert _ids(conn) == sorted(REAL)


def test_include_lab_via_cli(env, monkeypatch, tmp_path):
    from scorer.cli import cli
    from scorer.db import connect
    _fake(monkeypatch, {**REAL, **LAB})
    db = tmp_path / "c.db"
    r = CliRunner().invoke(cli, ["ingest", "--db", str(db), "--no-metrics"])
    assert r.exit_code == 0, r.output
    assert _ids(connect(db)) == sorted(REAL)
    r = CliRunner().invoke(cli, ["ingest", "--db", str(db), "--no-metrics", "--include-lab"])
    assert r.exit_code == 0, r.output
    assert _ids(connect(db)) == sorted({**REAL, **LAB})


def test_explicit_prefix_overrides_exclusion(env, monkeypatch, tmp_path):
    from scorer.db import connect
    ingest = _fake(monkeypatch, {**REAL, **LAB})
    conn = connect(tmp_path / "p.db")
    _run(ingest, conn, cwd_prefixes=[f"{HOME}/scorer-lab"], exclude_cwd=ingest.default_exclude_patterns())
    assert _ids(conn) == ["lab"]
    # env var counts as explicit too (lab orchestrator / SCORER_CWD_PREFIX)
    monkeypatch.setenv("SCORER_CWD_PREFIX", "/tmp")
    conn = connect(tmp_path / "e.db")
    _run(ingest, conn, exclude_cwd=ingest.default_exclude_patterns())
    assert _ids(conn) == ["ptmp", "tmp"]  # kept (/tmp == /private/tmp) although excluded by default


def test_purge_preexisting_excluded(env, monkeypatch, tmp_path):
    from scorer.db import connect
    ingest = _fake(monkeypatch, {**REAL, **LAB})
    conn = connect(tmp_path / "g.db")
    _run(ingest, conn)  # library default: no exclusion -> everything stored
    assert _ids(conn) == sorted({**REAL, **LAB})
    # next run discovers only the real sessions; stored lab sessions must still be purged
    ingest = _fake(monkeypatch, REAL)
    _run(ingest, conn, exclude_cwd=ingest.default_exclude_patterns())
    assert _ids(conn) == sorted(REAL)
    n_turn_sessions = {r[0] for r in conn.execute("SELECT session_id FROM turn")}
    n_event_sessions = {r[0] for r in conn.execute("SELECT session_id FROM event")}
    assert n_turn_sessions == n_event_sessions == set(REAL)


def test_config_override(env, monkeypatch, tmp_path):
    from scorer import config
    from scorer.db import connect
    (env / "config.toml").write_text('exclude_cwd = ["/srv/work/**"]\n')
    config.load.cache_clear()
    ingest = _fake(monkeypatch, {**REAL, **LAB})
    pats = ingest.default_exclude_patterns()
    assert "/srv/work/**" in pats and "/tmp/**" not in pats
    conn = connect(tmp_path / "o.db")
    _run(ingest, conn, exclude_cwd=pats)
    # /srv/work/* excluded; the built-in lab patterns no longer apply
    assert _ids(conn) == sorted(set(LAB) | {"none"})


def test_prefix_run_never_purges_sessions_outside_prefix(env, monkeypatch, tmp_path):
    """Lab run4 ingest with --cwd-prefix .../run4 wiped run2's sessions from the lab DB."""
    from scorer.db import connect
    run2 = {"r2a": f"{HOME}/scorer-lab/run2/lab-a", "r2b": f"{HOME}/scorer-lab/run2/lab-b"}
    run4 = {"r4a": f"{HOME}/scorer-lab/run4/lab-a"}
    conn = connect(tmp_path / "lab.db")
    ingest = _fake(monkeypatch, run2)
    _run(ingest, conn, cwd_prefixes=[f"{HOME}/scorer-lab/run2"], exclude_cwd=ingest.default_exclude_patterns())
    ingest = _fake(monkeypatch, {**run2, **run4})
    _run(ingest, conn, cwd_prefixes=[f"{HOME}/scorer-lab/run4"], exclude_cwd=ingest.default_exclude_patterns())
    assert _ids(conn) == ["r2a", "r2b", "r4a"]
