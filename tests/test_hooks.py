"""Hook contract tests for `scorer event` (spec §6c)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scorer import hookio

FIXTURES = Path(__file__).parent / "fixtures" / "hook_payloads"


def run_event(home: Path, stdin: bytes, agent="claude", event="UserPromptSubmit", extra=()):
    env = dict(os.environ, SCORER_HOME=str(home))
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "scorer", "event", "--agent", agent, "--event", event, *extra],
                       input=stdin, capture_output=True, env=env, timeout=10)
    return r, time.monotonic() - t0


def lines(home: Path) -> list[dict]:
    p = home / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    g = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(g + ["add", "a.py"], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    return path


# --- contract (subprocess) -------------------------------------------------

def test_one_mb_stdin_silent_fast(scorer_home):
    payload = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "x" * (1 << 20)}
    r, dt = run_event(scorer_home, json.dumps(payload).encode())
    assert r.returncode == 0
    assert r.stdout == b"" and r.stderr == b""
    assert dt < 1.0, dt
    [ln] = lines(scorer_home)
    assert ln["agent"] == "claude" and ln["event"] == "UserPromptSubmit"
    assert ln["hook_event_name"] == "UserPromptSubmit"
    assert ln["payload"]["session_id"] == "s1"
    assert len(ln["payload"]["prompt"]) <= hookio.MSG_CLIP + 1


@pytest.mark.parametrize("data", [b"{not json", b"\xff\xfe\x00garbage", b"", b"[1,2]", b"x" * 10_000])
def test_bad_stdin_still_writes_one_line(scorer_home, data):
    r, _ = run_event(scorer_home, data)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    [ln] = lines(scorer_home)
    assert isinstance(ln["payload"], dict)
    if data.strip():
        assert len(ln["payload"]["raw"]) <= hookio.RAW_CLIP


def test_no_args_and_unwritable_home_exit_zero(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    env = dict(os.environ, SCORER_HOME=str(blocker / "sub"))  # parent is a file -> mkdir fails
    r = subprocess.run([sys.executable, "-m", "scorer", "event"], input=b"{}", capture_output=True,
                       env=env, timeout=10)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


def test_parallel_eight(scorer_home):
    env = dict(os.environ, SCORER_HOME=str(scorer_home))
    procs = []
    for i in range(8):
        body = json.dumps({"session_id": f"s{i}", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_input": {"command": str(i) * (50_000 * (i + 1))}}).encode()
        p = subprocess.Popen([sys.executable, "-m", "scorer", "event", "--agent", "codex", "--event",
                              "PostToolUse"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        procs.append((p, body))
    for p, body in procs:
        out, err = p.communicate(body, timeout=15)
        assert p.returncode == 0 and out == b"" and err == b""
    got = lines(scorer_home)
    assert len(got) == 8
    assert sorted(l["payload"]["session_id"] for l in got) == [f"s{i}" for i in range(8)]


def test_stop_in_git_repo_has_snapshot(scorer_home, tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 2\ny = 3\n")
    (repo / "new.txt").write_text("n\n")
    payload = {"session_id": "s", "hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False}
    r, dt = run_event(scorer_home, json.dumps(payload).encode(), event="Stop")
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b"" and dt < 1.0
    [ln] = lines(scorer_home)
    g = ln["git"]
    assert len(g["head"]) == 40 and g["branch"] == "main"
    assert g["files_changed"] == 1 and g["insertions"] == 2 and g["deletions"] == 1
    assert g["untracked_count"] == 1 and g["changed_paths"] == ["a.py"]
    assert len(g["diff_stat_sha1"]) == 40 and len(g["untracked_sha1"]) == 40


def test_stop_non_git_cwd_no_snapshot(scorer_home, tmp_path):
    for cwd in (str(tmp_path), "/does/not/exist", None):
        payload = {"session_id": "s", "hook_event_name": "Stop", "cwd": cwd}
        r, _ = run_event(scorer_home, json.dumps(payload).encode(), event="Stop")
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    got = lines(scorer_home)
    assert len(got) == 3 and all("git" not in l for l in got)


def test_real_claude_payloads_roundtrip(scorer_home):
    for raw in (FIXTURES / "claude.jsonl").read_text().splitlines():
        p = json.loads(raw)
        r, _ = run_event(scorer_home, raw.encode(), event=p["hook_event_name"])
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    got = lines(scorer_home)
    assert [l["hook_event_name"] for l in got] == [
        "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]
    assert all(l["payload"]["session_id"] for l in got)
    post = got[2]["payload"]
    assert post["tool_name"] == "Bash" and post["tool_use_id"].startswith("toolu_")


# --- helpers (in-process) --------------------------------------------------

def test_parse_argv():
    assert hookio._parse_argv(["--agent", "codex", "--event", "Stop"]) == ("codex", "Stop", [])
    assert hookio._parse_argv(["--agent=claude", "--event=X", "{}"]) == ("claude", "X", ["{}"])
    assert hookio._parse_argv(["--agent"]) == (None, None, ["--agent"])


def test_shape_truncates_tool_fields_keeps_ids():
    big = "y" * 50_000
    p = {"session_id": "s", "prompt_id": "p", "tool_use_id": "t", "tool_name": "Write", "cwd": "/x",
         "tool_input": {"file_path": "/a", "content": big},
         "tool_response": [{"type": "text", "text": big}], "duration_ms": 5}
    s = hookio._shape_payload(p)
    assert s["tool_input"]["file_path"] == "/a"
    assert len(s["tool_input"]["content"]) <= hookio.FIELD_CLIP + 1
    assert len(s["tool_response"]) <= hookio.FIELD_CLIP + 1
    for k in ("session_id", "prompt_id", "tool_use_id", "tool_name", "cwd", "duration_ms"):
        assert s[k] == p[k]
    assert len(json.dumps(s)) < 10_000


def test_argv_json_fallback_when_stdin_empty():
    ln = hookio.build_line("codex", "Stop", b"", ['{"type":"agent-turn-complete","thread-id":"t"}'])
    assert ln["payload"]["thread-id"] == "t"


def test_branch_parsing():
    assert hookio._parse_branch("## main...origin/main [ahead 1]") == "main"
    assert hookio._parse_branch("## No commits yet on dev") == "dev"
    assert hookio._parse_branch("## HEAD (no branch)") is None


def test_snapshot_repo_without_commits(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "u.txt").write_text("u")
    g = hookio._git_snapshot(str(tmp_path))
    assert g is not None and g["head"] is None and g["untracked_count"] == 1
