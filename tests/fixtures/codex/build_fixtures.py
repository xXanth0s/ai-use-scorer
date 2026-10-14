"""Rebuild the redacted Codex fixtures from real rollouts (run manually, never in CI).

    uv run python tests/fixtures/codex/build_fixtures.py

Whitelist redaction: every string is replaced by sha1[:8] unless its key is
structural (type/role/status/model/...) or the value looks like an id or a
timestamp. Kept: structure, ids, timestamps, ordinals, usage numbers, exit codes,
durations. Message text keeps a leading "<" or "/" so prompt filters still work;
shell commands keep their program name (first word) so intent tests still work;
`exec` scripts keep their `tools.X(` calls; file contents/diffs keep line count
and +/- prefixes; paths keep their extension and a skills/<name>/SKILL.md tail.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys

ROOT = os.path.expanduser("~/.codex/sessions/2026/10")
OUT = os.path.dirname(os.path.abspath(__file__))

# fixture name -> rollout id fragment
SOURCES = {
    "codex_interrupt_multiturn.jsonl": "01a124a2-a524-73f0-8856-14ebeef49100",  # 6 turns, 1 turn_aborted
    "codex_failing_exec_review.jsonl": "01a11840-bbfc-7d71-b06a-169773676fa6",  # review subagent, exit!=0 execs
    "codex_apply_patch_subagent.jsonl": "01a13295-ac0b-7a00-b829-553e95b99c29",  # forked subagent, FileChange add+update
    "codex_steer_mcp.jsonl": "01a13292-4f4d-7783-8196-0d433ba6f14f",  # steer (queued) prompt, MCP, spawn_agent
}

KEEP_KEYS = {
    "type", "role", "status", "phase", "model", "effort", "kind", "thread_source", "originator", "cli_version",
    "model_provider", "reason", "name", "namespace", "collaboration_mode_kind", "mode", "history_mode",
    "timestamp", "reasoning_effort", "model_provider_id", "service_tier", "multi_agent_version", "server", "tool",
}
ALWAYS_HASH_KEYS = {"creator_user_id", "creator_account_id", "repository_url", "branch", "commit_hash"}
ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:resp|call|msg|exec|ws|rs|ct|fc|ctc|item)[_-][\w-]+"
    r"|\d{4}-\d{2}-\d{2}T[\d:.]+Z?(?:[+-]\d{2}:\d{2})?)$")


def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:8]


def red_text(s: str) -> str:
    prefix = s.lstrip()[:1] if s.lstrip()[:1] in ("<", "/", "$") else ""
    return prefix + h(s)


def red_path(p: str) -> str:
    m = re.search(r"skills/(?:[\w.-]+/)*?([\w.-]+)/SKILL\.md", p)
    if m:
        return f"/r/{h(p)}/skills/{m.group(1)}/SKILL.md"
    ext = os.path.splitext(p)[1]
    return f"/r/{h(p)}{ext if re.fullmatch(r'\.\w{1,6}', ext or '') else ''}"


def red_cmd(cmd: str) -> str:
    first = cmd.strip().split(None, 1)[0] if cmd.strip() else ""
    first = first if re.fullmatch(r"[A-Za-z0-9_.+-]{1,24}", first) else ""
    skills = " ".join(red_path(m.group(0)) for m in re.finditer(r"\S*skills/\S*?/SKILL\.md", cmd))
    return " ".join(x for x in (first, h(cmd), skills) if x)


def red_lines(s: str, keep_prefix: bool) -> str:
    out = []
    for ln in s.splitlines():
        p = ln[:1] if keep_prefix and ln[:1] in "+- @" else ""
        if keep_prefix and (ln.startswith("+++") or ln.startswith("---") or ln.startswith("@@")):
            p = ln[:3] if not ln.startswith("@@") else "@@"
        out.append(p + h(ln))
    return "\n".join(out) + ("\n" if s.endswith("\n") else "")


def red_exec_input(s: str) -> str:
    calls = [f"tools.{n}({{x:'{h(s)}'}})" for n in re.findall(r"\btools\.(\w+)\s*\(", s)]
    skills = [f"/*{red_path(m.group(0))}*/" for m in re.finditer(r"[\w./~-]*skills/\S*?/SKILL\.md", s)]
    return "text(" + ";".join(calls + skills) + ")" if calls or skills else h(s)


def red(v, key: str | None = None, ctx: dict | None = None):
    if isinstance(v, dict):
        ty = v.get("type")
        out = {}
        for k, x in v.items():
            if ty == "FileChange" and k == "changes" and isinstance(x, dict):
                out[k] = {red_path(p): {kk: (red_lines(vv, kk == "unified_diff") if kk in ("content", "unified_diff")
                                             and isinstance(vv, str) else red(vv, kk))
                                        for kk, vv in (c.items() if isinstance(c, dict) else [])}
                          for p, c in x.items()}
            elif ty == "CommandExecution" and k == "command" and isinstance(x, list):
                out[k] = [*x[:-1], red_cmd(x[-1])] if x else x
            elif ty == "custom_tool_call" and k == "input" and isinstance(x, str):
                out[k] = red_exec_input(x)
            elif k in ("cwd", "path") and isinstance(x, str):
                out[k] = red_path(x)
            else:
                out[k] = red(x, k, v)
        return out
    if isinstance(v, list):
        return [red(x, key, ctx) for x in v]
    if isinstance(v, str):
        if key in ALWAYS_HASH_KEYS:
            return h(v)
        if key in KEEP_KEYS and len(v) <= 64 and not (key == "name" and "." in v):  # file names -> hash
            return v
        if ID_RE.match(v):
            return v
        return red_text(v)
    return v


def main() -> int:
    files = glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True)
    for name, frag in SOURCES.items():
        src = next((f for f in files if frag in f), None)
        if not src:
            print(f"missing {frag}", file=sys.stderr)
            continue
        with open(src) as f, open(os.path.join(OUT, name), "w") as o:
            n = 0
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                o.write(json.dumps(red(r), ensure_ascii=False) + "\n")
                n += 1
        print(f"{name}: {n} lines, {os.path.getsize(os.path.join(OUT, name))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
