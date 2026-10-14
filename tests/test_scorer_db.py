"""SCORER_DB / --db: one DB file for serve, report, judge and the analyst; demo-data flag."""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path

from click.testing import CliRunner

from scorer import cli, model, serve
from scorer import db as dbm

from test_serve import DAY, T, build_fixture


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SCORER_HOME", str(home))
    monkeypatch.delenv("SCORER_DB", raising=False)
    monkeypatch.delenv("SCORER_MASTRA_URL", raising=False)
    return home


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def _serve(db):
    srv = serve.make_server(db, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_db_path_env(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    assert model.db_path() == home / "scorer.db"
    alt = tmp_path / "demo" / "demo.db"
    alt.parent.mkdir()
    monkeypatch.setenv("SCORER_DB", str(alt))
    assert model.db_path() == alt
    dbm.connect().close()
    assert alt.exists() and not (home / "scorer.db").exists()


def test_serve_env_never_touches_home_db(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    alt = tmp_path / "demo.db"
    build_fixture(str(alt))
    monkeypatch.setenv("SCORER_DB", str(alt))
    srv, base = _serve(None)
    try:
        days = _get(base, "/api/days")
        assert any(d["day"] == DAY and d["turns"] > 0 for d in days["days"])
        assert _get(base, f"/api/day/{DAY}")["sessions"]
        _get(base, f"/api/report/{DAY}")
    finally:
        srv.shutdown()
        srv.server_close()
    assert not (home / "scorer.db").exists()


def test_cli_db_flag_pins_env(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    alt = tmp_path / "x" / "demo.db"
    alt.parent.mkdir()
    build_fixture(str(alt))
    rel = os.path.relpath(alt)
    res = CliRunner().invoke(cli.cli, ["report", "--db", rel, "--day", DAY, "--no-judge"])
    assert res.exit_code == 0, res.output
    assert os.environ["SCORER_DB"] == str(alt.resolve())     # the analyst and children see the same file
    assert model.db_path() == alt.resolve()
    assert not (home / "scorer.db").exists()


def test_pin_db_without_flag_keeps_env(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert cli._pin_db(None) is None
    monkeypatch.setenv("SCORER_DB", str(tmp_path / "a.db"))
    assert cli._pin_db(None) == str(tmp_path / "a.db")


def test_demo_flag(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    path = tmp_path / "scorer.db"
    build_fixture(str(path))
    srv, base = _serve(str(path))
    try:
        assert _get(base, "/api/days")["demo"] == {"present": False, "session_ids": []}
        sessions = _get(base, f"/api/day/{DAY}")["sessions"]
        assert sessions and not any(s["demo"] for s in sessions)

        conn = dbm.connect(str(path))
        conn.execute("INSERT INTO session(id, agent, cwd) VALUES ('demo-x', 'claude', '/Users/x/Projects/demo')")
        conn.execute("INSERT INTO turn(session_id, idx, prompt_at, prompt_text, agent_finished_at)"
                     " VALUES ('demo-x', 0, ?, 'seeded', ?)", (T(15, 0), T(15, 5)))
        other = sessions[0]["id"]
        conn.execute("INSERT INTO event(session_id, at, kind, data) VALUES (?, ?, 'demo_marker', '{}')",
                     (other, T(15, 0)))
        conn.commit()
        conn.close()

        demo = _get(base, "/api/days")["demo"]
        assert demo["present"] is True
        assert set(demo["session_ids"]) == {"demo-x", other}
        flags = {s["id"]: s["demo"] for s in _get(base, f"/api/day/{DAY}")["sessions"]}
        assert flags["demo-x"] is True and flags[other] is True
        assert sum(flags.values()) == 2
    finally:
        srv.shutdown()
        srv.server_close()
