#!/usr/bin/env python3
"""Redact a Claude Code transcript into a committable test fixture (spec §6a).

  scripts/redact.py in.jsonl out.jsonl [--keep N] [--from-line K] [--meta in.meta.json out.meta.json]

Keeps structure, timestamps, ids, usage, models, tool names, is_error, userModified, queue ops.
Replaces human/model text, commands and paths with sha1[:8] while preserving what the parser keys on:
  - a leading `<tag>` (e.g. <command-name>, <task-notification>) and the `[Request interrupted by user`
    prefix and the peer-message prefix are kept verbatim, the rest is hashed;
  - multi-line edit strings (old_string/new_string/content) are hashed per line (line counts survive);
  - structuredPatch lines keep their +/-/space prefix;
  - Bash commands keep their first token if it is a plain program name (pytest, git, ls …).
Drops attachment, api-request*, last-prompt, titles, pr-link, file-history-*, cost-state and UI state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re

DROP_TYPES = {
    "attachment", "api-request", "api-request-blob", "api-request-shape", "last-prompt", "ai-title",
    "custom-title", "agent-name", "pr-link", "file-history-snapshot", "file-history-delta", "cost-state",
    "frame-link", "bridge-session", "artifact-autoreact-ledger", "artifact-comment-monitor",
    "history-suppression", "mode", "permission-mode", "atis-latch", "fork-context-ref",
}
# top-level keys kept verbatim (ids, flags, timings); everything else not listed here is dropped
KEEP_TOP = {
    "type", "subtype", "uuid", "parentUuid", "logicalParentUuid", "timestamp", "sessionId", "session_id",
    "requestId", "isMeta", "isSidechain", "isCompactSummary", "promptSource", "turnOrigin",
    "interruptedMessageId", "operation", "reason", "effort", "perTurnEffort", "durationMs", "messageCount",
    "pendingBackgroundAgentCount", "agentId", "userType", "entrypoint", "version", "toolUseID", "promptId",
    "continuedInSessionId", "hookCount", "level", "isApiErrorMessage", "commandUuid", "deliveryId",
    "preventedContinuation",
}
HASH_TOP = {"cwd", "gitBranch", "content", "slug"}
PREFIXES = ("[Request interrupted by user", "Another Claude session sent a message")
LINE_KEYS = {"old_string", "new_string", "content", "new_source"}
PROG = re.compile(r"^[a-z][a-z0-9_.-]{0,15}$")
TAG = re.compile(r"^\s*(<[A-Za-z][\w-]*>)")


def h(s) -> str:
    return hashlib.sha1(str(s).encode()).hexdigest()[:8]


def red_text(s):
    if not isinstance(s, str):
        return s
    for p in PREFIXES:
        if s.lstrip().startswith(p):
            rest = s.lstrip()[len(p):]
            return p + (h(rest) if rest.strip("] ") else rest)
    m = TAG.match(s)
    if m:
        return m.group(1) + h(s[m.end():])
    return h(s)


def red_lines(s):
    if not isinstance(s, str):
        return s
    return "\n".join(h(x) if x else "" for x in s.split("\n"))


def red_cmd(s):
    if not isinstance(s, str):
        return s
    first = s.strip().split(" ", 1)[0] if s.strip() else ""
    return (first + " " + h(s)) if PROG.match(first) else h(s)


def red_input(name: str, inp):
    if not isinstance(inp, dict):
        return {}
    out = {}
    for k, v in inp.items():
        if k == "command":
            out[k] = red_cmd(v)
        elif k in LINE_KEYS:
            out[k] = red_lines(v)
        elif k == "edits" and isinstance(v, list):
            out[k] = [{kk: red_lines(vv) for kk, vv in e.items() if kk in ("old_string", "new_string")}
                      for e in v if isinstance(e, dict)]
        elif isinstance(v, (bool, int, float)) or v is None:
            out[k] = v
        elif k in ("subagent_type", "model", "run_in_background"):
            out[k] = v
        else:
            out[k] = h(json.dumps(v)) if not isinstance(v, str) else h(v)
    return out


def red_block(b):
    if not isinstance(b, dict):
        return None
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": red_text(b.get("text"))}
    if t == "thinking":
        return {"type": "thinking", "thinking": h(b.get("thinking"))}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                "input": red_input(b.get("name"), b.get("input"))}
    if t == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            c = [{"type": "text", "text": h(x.get("text"))} for x in c if isinstance(x, dict) and x.get("type") == "text"]
        else:
            c = h(c)
        out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "content": c}
        if "is_error" in b:
            out["is_error"] = b["is_error"]
        return out
    return {"type": t}


def red_tur(tur):
    if not isinstance(tur, dict):
        return None
    out = {}
    for k in ("userModified", "agentId", "status", "isAsync", "agent_id"):
        if k in tur:
            out[k] = tur[k]
    if "filePath" in tur:
        out["filePath"] = h(tur["filePath"])
    if isinstance(tur.get("structuredPatch"), list):
        out["structuredPatch"] = [
            {**{k: v for k, v in hk.items() if k != "lines"},
             "lines": [(ln[:1] + h(ln[1:])) if isinstance(ln, str) else "" for ln in hk.get("lines") or []]}
            for hk in tur["structuredPatch"] if isinstance(hk, dict)]
    return out


def redact_entry(e: dict) -> dict | None:
    if e.get("type") in DROP_TYPES:
        return None
    out = {}
    for k, v in e.items():
        if k in KEEP_TOP:
            out[k] = v
        elif k in HASH_TOP:
            out[k] = red_text(v) if k == "content" else h(v)
        elif k == "origin" and isinstance(v, dict):
            out[k] = {"kind": v.get("kind")}
        elif k == "forkedFrom":
            out[k] = v
        elif k == "compactMetadata" and isinstance(v, dict):
            out[k] = {kk: v.get(kk) for kk in ("trigger", "preTokens", "postTokens", "durationMs")}
        elif k == "toolUseResult":
            out[k] = red_tur(v)
        elif k == "message" and isinstance(v, dict):
            m = {kk: v[kk] for kk in ("role", "model", "id", "usage", "stop_reason", "type") if kk in v}
            c = v.get("content")
            m["content"] = red_text(c) if isinstance(c, str) else [x for x in map(red_block, c or []) if x]
            out[k] = m
    return out


def redact_file(src: str, dst: str, keep: int | None = None, from_line: int = 0) -> int:
    n = 0
    with open(src, errors="replace") as f, open(dst, "w") as g:
        for i, line in enumerate(f):
            if i < from_line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            r = redact_entry(e) if isinstance(e, dict) else None
            if r is None:
                continue
            g.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
            if keep and n >= keep:
                break
    return n


def redact_meta(src: str, dst: str) -> None:
    with open(src) as f:
        m = json.load(f)
    keep = {k: m[k] for k in ("agentType", "spawnDepth", "model", "toolUseId", "isFork") if k in m}
    with open(dst, "w") as g:
        json.dump(keep, g)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--keep", type=int, default=None)
    ap.add_argument("--from-line", type=int, default=0)
    ap.add_argument("--meta", nargs=2, metavar=("IN", "OUT"))
    a = ap.parse_args()
    n = redact_file(a.src, a.dst, a.keep, a.from_line)
    if a.meta:
        redact_meta(*a.meta)
    print(f"{n} entries -> {a.dst}")


if __name__ == "__main__":
    main()
