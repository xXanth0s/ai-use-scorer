"""Codex rollout parser: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl -> Bundle.

Keyed on record shapes, not on cli_version (seen: 0.155.x, 0.159.0, 0.160.0).
Format notes: docs/notes/codex-format.md. Summary:

* session: first `session_meta` (payload.id is THIS thread; payload.session_id is
  the root thread). Subagent rollouts copy the parent's history; every record
  with ordinal < subagent_history_start_ordinal is inherited and skipped.
* turn: one per human prompt = `event_msg/item_completed` item.type UserMessage
  (always human UI input, even when it starts with "<in-app-browser-context>";
  AGENTS.md / environment_context injections are response_item role=user and
  are ignored). A Codex task
  (`task_started` .. `task_complete` | `turn_aborted`) normally holds one prompt;
  a second prompt inside a running task (steer) becomes a `queued` turn.
  Tasks without a human prompt: subagent threads -> initiator=agent turn;
  otherwise folded into the previous turn.
* usage: `token_usage_record` (per request, `response_id`, deduped).
  `token_count.info.total_token_usage` is cumulative and only used as a
  fallback (consecutive differences) when a file has no token_usage_record.
* tools: Codex "code mode" wraps everything in a `custom_tool_call` named
  `exec` whose input is JavaScript calling `tools.exec_command(...)`,
  `tools.apply_patch(...)`, `tools.web__run(...)`. Real shell commands and edits
  surface as `item_completed` CommandExecution / FileChange items inside the
  call window; those become the ToolCalls and the wrapper is dropped. Wrappers
  without such children (web, view_image, ...) stay as one ToolCall named after
  the inner tool. Direct `function_call`s (spawn_agent, send_message, MCP, shell)
  are one ToolCall per call_id.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from . import classify, prices
from .parse_claude import output_failed
from .model import ApiCall, Bundle, Event, FileEdit, Session, ToolCall, Turn, clip, iso, parse_ts

ARGS_MAX = 500
HUMAN_ACTION_RE = re.compile(r"\b(test|tests|run|lint|verify|check|pytest)\b", re.I)
INNER_TOOL_RE = re.compile(r"\btools\.(\w+)\s*\(")
# priority when an `exec` script calls several tools
INNER_PRIORITY = ("apply_patch", "exec_command", "write_stdin", "view_image", "web__run")
SHELL_NAMES = ("shell", "exec_command", "local_shell", "container.exec", "shell_command")
_FALLBACK_KIND = {
    "exec_command": "bash", "write_stdin": "bash", "shell": "bash", "local_shell": "bash",
    "shell_command": "bash", "apply_patch": "edit", "view_image": "read", "web__run": "web",
    "web_search": "web", "spawn_agent": "subagent", "send_message": "subagent",
    "wait_agent": "subagent", "close_agent": "subagent",
}
# Codex built-in slash commands (TUI); anything else after a leading "/" is treated as a skill
CODEX_BUILTIN_COMMANDS = {
    "model", "approvals", "permissions", "review", "new", "init", "compact", "undo", "diff", "mention",
    "status", "mcp", "logout", "quit", "exit", "resume", "fork", "plan", "feedback", "agent", "agents",
    "prompts", "settings", "theme", "clear", "rename", "statusline", "experimental", "skills", "apps",
}
SLASH_RE = re.compile(r"^\s*/([A-Za-z][\w:-]*)(?=\s|$)")
DOLLAR_RE = re.compile(r"(?:^|\s)\$([a-z][\w:-]*)(?=[\s.,;:!?)]|$)")  # $skill (lower-case: not $HOME)
SKILL_READ_RE = re.compile(r"skills/(?:[\w.-]+/)*?([\w.-]+)/SKILL\.md")
MCP_INNER_RE = re.compile(r"\btools\.mcp__([\w-]+?)__\w+\s*\(")
JS_CMD_RE = re.compile(r"\btools\.(?:exec_command|shell|shell_command)\(\s*\{[^}]*?\b(?:cmd|command)\s*:\s*"
                       r"(['\"`])((?:\\.|(?!\1).)*)\1", re.S)
INTENT_PRIORITY = ("test", "build", "edit", "git", "run", "search", "read")


def js_commands(src: str) -> list[str]:
    """Shell command literals passed to tools.exec_command/shell inside an `exec` script."""
    out = []
    for m in JS_CMD_RE.finditer(src or ""):
        out.append(re.sub(r"\\(['\"`\\])", r"\1", m.group(2)).replace("\\n", "\n"))
    return out


def aggregate_intent(cmds: list[str]) -> str | None:
    intents = [classify.bash_intent(c) for c in cmds if c]
    if not intents:
        return None
    return next((i for i in INTENT_PRIORITY if i in intents), intents[0])


_AGENT_ITEM_TYPES = ("reasoning", "message", "function_call", "custom_tool_call", "agent_message",
                     "local_shell_call", "web_search_call")


def _ts(s) -> str | None:
    return iso(parse_ts(s)) if isinstance(s, str) else None


def _ms_iso(ms) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def _ms_between(a: str | None, b: str | None) -> int | None:
    da, db = parse_ts(a), parse_ts(b)
    if not da or not db:
        return None
    return max(0, int((db - da).total_seconds() * 1000))


def _kind(name: str) -> str:
    k = classify.tool_kind(name)
    if k == "other":
        k = _FALLBACK_KIND.get(name, "other")
    return k


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def _shell_cmd(command) -> str:
    """CommandExecution.command is argv, usually ['/bin/zsh', '-lc', '<cmd>']."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command:
        parts = [str(c) for c in command]
        if len(parts) >= 3 and parts[-2] in ("-lc", "-c", "-l"):
            return parts[-1]
        return " ".join(parts)
    return ""


def _duration_ms(d) -> int | None:
    if isinstance(d, dict) and ("secs" in d or "nanos" in d):
        return int((d.get("secs") or 0) * 1000 + (d.get("nanos") or 0) / 1e6)
    if isinstance(d, (int, float)):
        return int(d)
    return None


def _count_lines(s: str | None) -> int:
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _diff_counts(diff: str | None) -> tuple[int, int]:
    add = rem = 0
    for ln in (diff or "").splitlines():
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            add += 1
        elif ln.startswith("-"):
            rem += 1
    return add, rem


_PATCH_HDR = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")


def parse_patch(text: str) -> list[tuple[str, str, int, int]]:
    """apply_patch text -> [(op, path, added, removed)]. Accepts JS-escaped newlines."""
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    out: list[list] = []
    for ln in text.splitlines():
        m = _PATCH_HDR.match(ln.strip())
        if m:
            out.append([m.group(1).lower(), m.group(2), 0, 0])
            continue
        if not out or ln.startswith("***"):
            continue
        if ln.startswith("+"):
            out[-1][2] += 1
        elif ln.startswith("-"):
            out[-1][3] += 1
    return [tuple(x) for x in out]


def _hook_name(h: dict) -> str | None:
    p = h.get("payload") or {}
    return h.get("event") or p.get("hook_event_name") or p.get("hookEventName")


def _hook_ts(h: dict) -> str | None:
    p = h.get("payload") or {}
    return _ts(h.get("at") or p.get("timestamp"))


def _usage_tokens(u: dict) -> tuple[int, int, int, int]:
    """OpenAI usage -> (uncached input, cached, cache_write, output). cached ⊂ input."""
    inp = int(u.get("input_tokens") or 0)
    cached = int(u.get("cached_input_tokens") or 0)
    cw = int(u.get("cache_write_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    return max(0, inp - cached - cw), cached, cw, out


class _Parser:
    def __init__(self, path: str, meta: dict, records: list[dict], hook_events: list[dict] | None):
        self.path = path
        self.meta = meta
        self.records = records
        self.hooks = hook_events or []
        self.sid = meta.get("id") or meta.get("session_id")
        src = meta.get("source")
        self.is_sub = meta.get("thread_source") == "subagent" or (isinstance(src, dict) and "subagent" in src)
        self.side = self.sid if self.is_sub else None

        self.turns: list[Turn] = []
        self.api_calls: list[ApiCall] = []
        self.tool_calls: list[ToolCall] = []
        self.file_edits: list[FileEdit] = []
        self.events: list[Event] = []

        self.model = None
        self.effort = None
        self.ctx_window = None
        self.cwd = meta.get("cwd")
        # task state
        self.task_open = False
        self.task_ids: set[str] = set()
        self.task_start: str | None = None
        self.task_turn: int | None = None      # turn idx the task's activity goes to
        self.task_prompted = False
        self.task_active = False
        self.task_tid: str | None = None
        self.cur: int | None = None            # current turn idx (for attribution)
        self.last_agent_ts: str | None = None
        # tools
        self.calls: dict[str, dict] = {}       # call_id -> info
        self.call_order: list[str] = []
        self.open_calls: list[str] = []
        self.last_call: str | None = None
        self.seen_resp: set[str] = set()
        self.compaction_ts: list[datetime] = []
        self.tooling_seen: set[tuple] = set()

    # --- turns ---------------------------------------------------------------
    def _new_turn(self, prompt_at: str, text: str | None, kind: str, initiator: str = "human",
                  enqueued: str | None = None) -> int:
        t = Turn(idx=len(self.turns), prompt_at=prompt_at, prompt_text=text, prompt_kind=kind,
                 initiator=initiator, first_uuid=self.task_tid, last_uuid=self.task_tid, enqueued_at=enqueued)
        self.turns.append(t)
        self.cur = t.idx
        self.last_agent_ts = None
        return t.idx

    def _close(self, idx: int | None, at: str | None, source: str, interrupted: bool = False) -> None:
        if idx is None or at is None:
            return
        t = self.turns[idx]
        t.agent_finished_at, t.end_source = at, source
        t.interrupted = t.interrupted or interrupted

    def _ensure_turn(self, ts: str) -> int:
        """Turn idx for agent activity at ts (creates/folds no-prompt tasks)."""
        if self.task_open and self.task_turn is None:
            if self.is_sub or self.cur is None:
                self.task_turn = self._new_turn(self.task_start or ts, None, "prompt", initiator="agent")
                self.task_prompted = True
            else:
                self.task_turn = self.cur  # fold into previous turn
        if self.cur is None:
            self.cur = self._new_turn(ts, None, "prompt", initiator="agent")
        return self.cur

    def _agent_activity(self, ts: str) -> int:
        idx = self._ensure_turn(ts)
        t = self.turns[idx]
        if t.agent_started_at is None:
            t.agent_started_at = ts
        self.last_agent_ts = ts
        if self.task_open:
            self.task_active = True
            t.last_uuid = self.task_tid or t.last_uuid
        return idx

    def _on_prompt(self, ts: str, text: str) -> None:
        self._on_prompt_inner(ts, text)
        if not self.is_sub and self.cur is not None:
            self._prompt_tooling(ts, self.cur, text)

    def _on_prompt_inner(self, ts: str, text: str) -> None:
        kind = "command" if SLASH_RE.match(text or "") else "prompt"  # not "/Users/..." paths
        if self.is_sub:
            # subagent threads: prompts come from the parent agent / review launcher
            if self.task_open and not self.task_prompted:
                self.task_turn = self._new_turn(self.task_start or ts, text, "prompt", initiator="agent")
                self.task_prompted = True
            return
        if self.task_open and not self.task_prompted:
            self.task_turn = self._new_turn(self.task_start or ts, text, kind)
            self.task_prompted = True
        elif self.task_open:
            # steer: a human message while the task is running
            prev = self.cur
            if prev is not None and self.turns[prev].agent_finished_at is None:
                self._close(prev, ts, "last_entry")  # agent kept running until the steer: gap 0
            self.task_turn = self._new_turn(ts, text, "queued", enqueued=ts)
        else:
            self._new_turn(ts, text, kind)

    def _on_task_start(self, p: dict, ts: str) -> None:
        if self.task_open:  # previous task never ended
            self._end_task(self.last_agent_ts or ts, "last_entry")
        self.task_open = True
        self.task_ids = {x for x in (p.get("turn_id"), p.get("root_turn_id")) if x}
        self.task_tid = p.get("turn_id")
        self.task_start = ts
        self.task_turn = None
        self.task_prompted = False
        self.task_active = False
        if p.get("model_context_window"):
            self.ctx_window = p["model_context_window"]

    def _end_task(self, ts: str, source: str, interrupted: bool = False) -> None:
        if not self.task_open:
            return
        idx = self.task_turn
        if idx is not None:
            if self.task_prompted:
                self._close(idx, ts, source, interrupted)
            elif self.task_active and not self.turns[idx].interrupted:
                # no-prompt task folded into the previous turn: extend its end
                self._close(idx, ts, source, interrupted)
        self.task_open = False
        self.task_turn = None

    # --- tooling (skills / slash commands / MCP) ------------------------------
    def _tooling(self, ts: str, idx: int | None, category: str, name: str, initiator: str, rule: str,
                 tool_use_id: str | None = None, dedupe: tuple | None = None) -> None:
        if dedupe is not None:
            if dedupe in self.tooling_seen:
                return
            self.tooling_seen.add(dedupe)
        self.events.append(Event("tooling", ts, idx, {"category": category, "name": name, "initiator": initiator,
                                                       "rule": rule, "tool_use_id": tool_use_id}))

    def _prompt_tooling(self, ts: str, idx: int, text: str) -> None:
        m = SLASH_RE.match(text or "")
        if m:
            name = m.group(1)
            cat = "command" if name.lower() in CODEX_BUILTIN_COMMANDS else "skill"
            self._tooling(ts, idx, cat, name, "human", "codex:user-prefix")
        for m in DOLLAR_RE.finditer(text or ""):
            self._tooling(ts, idx, "skill", m.group(1), "human", "codex:user-prefix", dedupe=(idx, "h", m.group(1)))

    def _skill_reads(self, ts: str, idx: int | None, text: str | None, tool_use_id: str | None) -> None:
        for m in SKILL_READ_RE.finditer(text or ""):
            self._tooling(ts, idx, "skill", m.group(1), "agent", "codex:skill-item", tool_use_id,
                          dedupe=(idx, "a", m.group(1)))

    # --- tools ---------------------------------------------------------------
    def _tool(self, tc: ToolCall) -> None:
        self.tool_calls.append(tc)

    def _on_call(self, p: dict, ts: str, idx: int) -> None:
        cid = p.get("call_id") or p.get("id")
        if not cid:
            return
        ptype = p.get("type")
        raw_name = p.get("name") or ptype
        info = {"cid": cid, "type": ptype, "raw": raw_name, "ts": ts, "turn": idx, "kids": 0,
                "has_result": False, "is_error": False, "end": None, "duration": None, "cmd": None,
                "patch": None, "name": raw_name, "direct_shell": False, "ns": p.get("namespace")}
        if ptype == "custom_tool_call":
            inp = p.get("input") if isinstance(p.get("input"), str) else ""
            if raw_name == "exec":
                inner = INNER_TOOL_RE.findall(inp)
                name = next((n for n in INNER_PRIORITY if n in inner), inner[0] if inner else "exec")
                info["name"] = name
                if name == "apply_patch":
                    info["patch"] = inp
                cmds = js_commands(inp)
                if cmds:
                    info["cmds"] = cmds
                    info["cmd"] = " ; ".join(cmds)
            elif raw_name == "apply_patch":
                info["patch"] = inp
            elif raw_name in SHELL_NAMES:
                info["cmd"], info["direct_shell"] = inp, True
        elif ptype in ("function_call", "local_shell_call"):
            args = p.get("arguments") or p.get("action") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            if raw_name in SHELL_NAMES or ptype == "local_shell_call":
                cmd = args.get("cmd") or args.get("command")
                info["cmd"] = _shell_cmd(cmd) if cmd is not None else None
                info["direct_shell"] = True
                info["name"] = raw_name if raw_name in SHELL_NAMES else "local_shell"
            elif raw_name == "apply_patch":
                info["patch"] = args.get("input") or args.get("patch") or args.get("_raw")
        if ptype == "custom_tool_call" and isinstance(p.get("input"), str):
            self._skill_reads(ts, idx, p["input"], cid)
            for server in dict.fromkeys(MCP_INNER_RE.findall(p["input"])):
                self._tooling(ts, idx, "mcp", server, "agent", "codex:mcp-call", cid)
        ns = p.get("namespace") or ""
        mcp_server = ns[5:] if ns.startswith("mcp__") else None
        if mcp_server is None and raw_name.startswith("mcp__") and raw_name.count("__") >= 2:
            mcp_server = raw_name.split("__")[1]
        if mcp_server:
            self._tooling(ts, idx, "mcp", mcp_server, "agent", "codex:mcp-call", cid)
            if ns:
                info["name"] = f"{ns}__{raw_name}"  # Claude-style MCP name, e.g. mcp__cua_repl__js
        self.calls[cid] = info
        self.call_order.append(cid)
        self.open_calls.append(cid)
        self.last_call = cid

    def _on_output(self, p: dict, ts: str) -> None:
        cid = p.get("call_id")
        info = self.calls.get(cid)
        if not info:
            return
        if cid in self.open_calls:
            self.open_calls.remove(cid)
        info["has_result"] = True
        info["end"] = ts
        out = p.get("output")
        text = _text(out) if not isinstance(out, dict) else json.dumps(out)
        if isinstance(out, str):
            try:
                j = json.loads(out)
                if isinstance(j, dict):
                    ec = (j.get("metadata") or {}).get("exit_code", j.get("exit_code"))
                    if isinstance(ec, int) and ec != 0:
                        info["is_error"] = True
                    dur = (j.get("metadata") or {}).get("duration_seconds")
                    if isinstance(dur, (int, float)):
                        info["duration"] = int(dur * 1000)
            except ValueError:
                pass
        info["out"] = text
        head = text[:200]
        if re.match(r"\s*(Script failed|Script error|Error:|error:)", head):
            info["is_error"] = True
        m = re.search(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)", head)
        if m and int(m.group(1)) != 0:
            info["is_error"] = True

    def _parent_call(self) -> dict | None:
        if self.open_calls:
            return self.calls[self.open_calls[-1]]
        return self.calls.get(self.last_call) if self.last_call else None

    def _initiator(self, idx: int | None, intent: str | None) -> tuple[str, str]:
        t = self.turns[idx] if idx is not None and idx < len(self.turns) else None
        if t is None or t.initiator == "agent":
            return "agent", "5"
        if t.prompt_kind == "command":
            return "default", "2:command"
        if intent in ("test", "build") and t.prompt_text:
            m = HUMAN_ACTION_RE.search(t.prompt_text)
            if m:
                return "human", "3:prompt:" + clip(m.group(0), 30)
        return "agent", "5"

    def _on_item(self, item: dict, p: dict, ts: str, idx: int) -> None:
        ty = item.get("type")
        started = _ms_iso(p.get("started_at_ms")) or ts
        if ty == "CommandExecution":
            parent = self._parent_call()
            if parent is None:
                return  # mirrored review child (no model calls in this file); child file carries it
            cmd = _shell_cmd(item.get("command"))
            ec = item.get("exit_code")
            err = (isinstance(ec, int) and ec != 0) or item.get("status") == "failed"
            out = item.get("aggregated_output") or item.get("formatted_output") or ""
            dur = _duration_ms(item.get("duration"))
            if dur is None and p.get("completed_at_ms") and p.get("started_at_ms"):
                dur = int(p["completed_at_ms"] - p["started_at_ms"])
            parent["kids"] += 1
            if parent["direct_shell"] and parent["kids"] == 1:
                parent["cmd"] = cmd or parent["cmd"]
                parent["is_error"] = parent["is_error"] or err
                parent["out"] = parent.get("out") or (out if isinstance(out, str) else "")
                parent["duration"] = dur
                parent["has_result"] = True
                parent["cwd"] = item.get("cwd")
                return
            self._skill_reads(ts, parent["turn"], cmd, item.get("id"))
            intent = classify.bash_intent(cmd)
            if not err and intent in ("test", "build") and isinstance(out, str) and output_failed(out):
                err = True  # exit status masked by a pipe / `; echo`
            ini, rule = self._initiator(parent["turn"], intent)
            self._tool(ToolCall(turn_idx=parent["turn"], tool_use_id=item.get("id") or f"{parent['cid']}:{parent['kids']}",
                                name="exec_command", kind=_kind("exec_command"), bash_intent=intent,
                                initiator=ini, initiator_rule=rule, args_summary=cmd[:ARGS_MAX], started_at=started,
                                duration_ms=dur, is_error=bool(err), has_result=True, sidechain_id=self.side,
                                cwd=item.get("cwd") or self.cwd))
        elif ty == "FileChange":
            parent = self._parent_call()
            if parent is None:
                return
            parent["kids"] += 1
            tuid = item.get("id") or f"{parent['cid']}:{parent['kids']}"
            changes = item.get("changes") or {}
            paths = list(changes)
            ini, rule = self._initiator(parent["turn"], None)
            dur = None
            if p.get("completed_at_ms") and p.get("started_at_ms"):
                dur = int(p["completed_at_ms"] - p["started_at_ms"])
            self._tool(ToolCall(turn_idx=parent["turn"], tool_use_id=tuid, name="apply_patch",
                                kind=_kind("apply_patch"), initiator=ini, initiator_rule=rule,
                                args_summary=" ".join(paths)[:ARGS_MAX], started_at=started, duration_ms=dur,
                                is_error=item.get("status") not in (None, "completed"), has_result=True,
                                sidechain_id=self.side, cwd=self.cwd))
            for path, ch in changes.items():
                ch = ch if isinstance(ch, dict) else {}
                op = ch.get("type")
                if op == "add":
                    a, r = _count_lines(ch.get("content")), 0
                elif op == "delete":
                    a, r = 0, _count_lines(ch.get("content"))
                else:
                    a, r = _diff_counts(ch.get("unified_diff"))
                self._edit(tuid, ch.get("move_path") or path, a, r)
        elif ty == "McpToolCall":
            info = self.calls.get(item.get("id"))
            if info:
                info["duration"] = _duration_ms(item.get("duration"))
                res = item.get("result") or {}
                if item.get("status") == "failed" or (isinstance(res, dict) and res.get("isError")):
                    info["is_error"] = True
        elif ty == "SubAgentActivity":
            if item.get("kind") in ("started", "completed"):
                parent = self.calls.get(self.open_calls[-1]) if self.open_calls else None
                self.events.append(Event("subagent", ts, idx, {
                    "kind": item.get("kind"), "agent_thread_id": item.get("agent_thread_id"),
                    "agent_path": item.get("agent_path"),
                    "tool_use_id": parent["cid"] if parent and parent["raw"] in ("spawn_agent", "wait_agent",
                                                                                 "close_agent") else None}))
        elif ty == "ContextCompaction":
            d = parse_ts(ts)
            if not any(abs((d - c).total_seconds()) < 5 for c in self.compaction_ts):
                self.compaction_ts.append(d)
                self.events.append(Event("compaction", ts, idx, {}))

    def _edit(self, tuid: str, path: str, added: int, removed: int) -> None:
        if path and not os.path.isabs(path) and self.cwd:
            path = os.path.join(self.cwd, path)
        self.file_edits.append(FileEdit(tool_use_id=tuid, path=path, manifest_dir=classify.manifest_dir(path),
                                        lines_added=added, lines_removed=removed))

    def _finish_calls(self) -> None:
        for cid in self.call_order:
            info = self.calls[cid]
            if info["kids"] and not info["direct_shell"]:
                continue  # represented by its CommandExecution / FileChange children
            name = info["name"]
            cmd = info["cmd"]
            intent = aggregate_intent(info.get("cmds") or ([cmd] if cmd else []))
            summary = cmd
            if info["patch"] is not None:
                files = parse_patch(info["patch"])
                summary = " ".join(f[1] for f in files)
                for op, path, a, r in files:
                    self._edit(cid, path, a, r)
            dur = info["duration"] if info["duration"] is not None else _ms_between(info["ts"], info["end"])
            if not info["is_error"] and intent in ("test", "build") and output_failed(info.get("out")):
                info["is_error"] = True  # exit status masked by a pipe / `; echo`
            ini, rule = self._initiator(info["turn"], intent)
            self._tool(ToolCall(turn_idx=info["turn"], tool_use_id=cid, name=name, kind=_kind(name),
                                bash_intent=intent, initiator=ini, initiator_rule=rule,
                                args_summary=(summary or "")[:ARGS_MAX] or None, started_at=info["ts"],
                                duration_ms=dur, is_error=info["is_error"], has_result=info["has_result"],
                                sidechain_id=self.side, cwd=info.get("cwd") or self.cwd))

    # --- usage ---------------------------------------------------------------
    def _api(self, rid: str, u: dict, ts: str, idx: int) -> None:
        if rid in self.seen_resp:
            return
        self.seen_resp.add(rid)
        inp, cached, cw, out = _usage_tokens(u)
        model = self.model
        cost = prices.cost_usd(model, inp, cached, cw, out) if model else None
        self.api_calls.append(ApiCall(turn_idx=idx, request_id=rid, model=model, effort=self.effort,
                                      input_tok=inp, cached_tok=cached, output_tok=out, cache_write_tok=cw,
                                      context_window=self.ctx_window or prices.context_window(model),
                                      cost_usd=cost, at=ts, sidechain_id=self.side))

    # --- main ----------------------------------------------------------------
    def run(self) -> Bundle:
        has_records = any(r.get("type") == "token_usage_record" for r in self.records)
        prev_total: dict | None = None
        synth = 0
        last_ts = None
        for r in self.records:
            ts = _ts(r.get("timestamp"))
            if not ts:
                continue
            last_ts = ts
            t = r.get("type")
            p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            pt = p.get("type")
            if t == "turn_context":
                self.model = p.get("model") or self.model
                self.effort = p.get("effort") or (p.get("collaboration_mode") or {}).get("settings", {}).get(
                    "reasoning_effort") or self.effort
                self.cwd = p.get("cwd") or self.cwd
            elif t == "event_msg":
                if pt == "task_started":
                    self._on_task_start(p, ts)
                elif pt == "task_complete":
                    self._end_task(ts, "task_complete")
                elif pt == "turn_aborted":
                    self._end_task(ts, "interrupt", interrupted=True)
                elif pt == "item_completed":
                    item = p.get("item") or {}
                    if item.get("type") == "UserMessage":
                        # UserMessage items are always human input from the UI (also when the
                        # text starts with "<in-app-browser-context>" or a question reply);
                        # injected context arrives only as response_item role=user.
                        self._on_prompt(ts, _text(item.get("content")))
                        continue
                    if item.get("type") in ("EnteredReviewMode", "ExitedReviewMode"):
                        continue
                    idx = self._agent_activity(ts)
                    self._on_item(item, p, ts, idx)
                elif pt == "token_count" and not has_records:
                    info = p.get("info") or {}
                    tot = info.get("total_token_usage")
                    if info.get("model_context_window"):
                        self.ctx_window = info["model_context_window"]
                    if tot and tot != prev_total:
                        if prev_total and (tot.get("total_tokens") or 0) >= (prev_total.get("total_tokens") or 0):
                            delta = {k: (tot.get(k) or 0) - (prev_total.get(k) or 0) for k in tot}
                        else:
                            delta = info.get("last_token_usage") or tot
                        prev_total = tot
                        synth += 1
                        self._api(f"{self.sid}:{synth}", delta, ts, self._agent_activity(ts))
            elif t == "token_usage_record":
                rid = p.get("response_id")
                if rid and isinstance(p.get("usage"), dict):
                    self._api(rid, p["usage"], ts, self._agent_activity(ts))
            elif t == "compacted":
                d = parse_ts(ts)
                if not any(abs((d - c).total_seconds()) < 5 for c in self.compaction_ts):
                    self.compaction_ts.append(d)
                    self.events.append(Event("compaction", ts, self._ensure_turn(ts) if self.turns or self.task_open else None,
                                             {"window_number": p.get("window_number")}))
            elif t == "response_item":
                if pt == "message" and p.get("role") != "assistant":
                    continue
                if pt in _AGENT_ITEM_TYPES:
                    idx = self._agent_activity(ts)
                    if pt in ("function_call", "custom_tool_call", "local_shell_call"):
                        self._on_call(p, ts, idx)
                elif pt and (pt.endswith("_call_output") or pt == "function_call_output"):
                    self._agent_activity(ts)
                    self._on_output(p, ts)
        # unterminated task / turn
        if self.task_open:
            self._end_task(self.last_agent_ts or last_ts, "last_entry")
        for tr in self.turns:
            if tr.agent_finished_at is None:
                tr.agent_finished_at, tr.end_source = (tr.agent_started_at and last_ts) or tr.prompt_at, "last_entry"
        self._finish_calls()
        self._apply_hooks()
        return self._bundle(last_ts)

    def _turn_at(self, ts: str) -> int | None:
        d = parse_ts(ts)
        found = None
        for t in self.turns:
            if parse_ts(t.prompt_at) <= d:
                found = t.idx
        return found

    def _apply_hooks(self) -> None:
        hooks = sorted(((h, _hook_ts(h)) for h in self.hooks if _hook_ts(h)), key=lambda x: x[1])
        for h, ts in hooks:
            name = _hook_name(h)
            idx = self._turn_at(ts)
            self.events.append(Event("hook", ts, idx, {"event": name}))
        stops = [parse_ts(ts) for h, ts in hooks if _hook_name(h) == "Stop"]
        for i, t in enumerate(self.turns):
            if t.interrupted or not stops:
                continue
            lo = parse_ts(t.agent_started_at or t.prompt_at)
            hi = parse_ts(self.turns[i + 1].prompt_at) if i + 1 < len(self.turns) else lo + timedelta(days=2)
            cand = [s for s in stops if lo <= s < hi]
            if cand:
                t.agent_finished_at, t.end_source = iso(cand[0]), "stop_hook"

    def _bundle(self, last_ts: str | None) -> Bundle:
        m = self.meta
        git = m.get("git") if isinstance(m.get("git"), dict) else {}
        first_ts = _ts(m.get("timestamp")) or (_ts(self.records[0].get("timestamp")) if self.records else None)
        s = Session(id=self.sid, agent="codex", cwd=m.get("cwd"), repo=m.get("cwd"), branch=git.get("branch"),
                    transcript_path=self.path,
                    forked_from=m.get("forked_from_id") or m.get("parent_thread_id"),
                    started_at=first_ts, ended_at=last_ts or first_ts,
                    ended_by="interrupt" if self.turns and self.turns[-1].interrupted else None)
        return Bundle(session=s, turns=self.turns, api_calls=self.api_calls, tool_calls=self.tool_calls,
                      file_edits=self.file_edits, events=self.events)


def parse(path: str, hook_events: list[dict] | None = None) -> Bundle | None:
    meta = None
    start_ord = 0
    records: list[dict] = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if meta is None and r.get("type") == "session_meta" and isinstance(r.get("payload"), dict):
                meta = r["payload"]
                start_ord = int(meta.get("subagent_history_start_ordinal") or 0)
                continue
            o = r.get("ordinal")
            if isinstance(o, int) and o < start_ord:
                continue  # history inherited from the parent thread
            if r.get("type") == "session_meta":
                continue
            records.append(r)
    if meta is None or not (meta.get("id") or meta.get("session_id")):
        return None
    return _Parser(path, meta, records, hook_events).run()
