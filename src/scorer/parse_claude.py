"""Claude Code transcript parser: one main transcript (+ its subagents/) -> Bundle.

Format notes: docs/notes/claude-format.md. Pure: never touches the DB.

Segmentation follows FILE ORDER (entries are not ts-sorted); durations use ts.
Turn end precedence: interrupt marker > Stop hook > max(turn_duration ts) > last
assistant/tool_result ts ("last_entry", or "crash" for a dangling tool_use at the
very end of the session). Subagent activity extends a turn only up to the next
prompt of the session, so subagents can never create negative gaps.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timedelta

from . import classify, prices
from .model import (INTERRUPT_PREFIX, ApiCall, Bundle, Event, FileEdit, Session, ToolCall, Turn, clip, iso,
                    parse_ts)

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_ARGS = 500
BREAK = timedelta(minutes=15)   # spec: gap > 15 min = break
ABANDON = timedelta(minutes=10)  # prompt w/o agent output resubmitted within this = Esc before first output
CAP = timedelta(seconds=60)     # max turn_duration overshoot past the last agent entry / next prompt
CMD_NAME = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
SKILL_BASE = "Base directory for this skill"
PEER_PREFIX = "Another Claude session sent a message"  # peer relay, sometimes without isMeta
# Claude Code's own CLI commands: session plumbing, not tooling. Recorded as event "command_builtin".
# (/review is left out on purpose: it is commonly a user/plugin skill of the same name.)
BUILTIN_COMMANDS = frozenset({
    "exit", "quit", "clear", "reset", "new", "model", "compact", "resume", "continue", "help", "config",
    "settings", "cost", "usage", "status", "login", "logout", "init", "memory", "permissions",
    "allowed-tools", "agents", "mcp", "hooks", "ide", "vim", "theme", "doctor", "bug", "feedback",
    "release-notes", "add-dir", "terminal-setup", "upgrade", "context", "export", "rewind", "fast",
    "effort", "plugin", "plugins", "statusline", "output-style", "privacy-settings", "todos", "tasks",
    "bashes", "sandbox", "rename", "copy", "files", "keybindings", "extra-usage", "stickers", "migrate-installer",
    "install-github-app", "install-slack-app", "remote-env", "output-style:new",
})


# Failure markers in test/build output. Claude Code only sets tool_result.is_error when the shell exits
# non-zero; `pytest ... | tail`, `...; echo "exit: $?"` etc. mask the exit status, so the runner's own
# summary line is the only signal. Each pattern needs a non-zero count or a runner-specific verdict, so a
# passing test that merely mentions "error"/"fail" (e.g. test_error PASSED) does not match.
FAIL_MARKERS = re.compile("|".join([
    r"^Exit code [1-9]\d*",                                         # Claude Code's own non-zero prefix
    r"^\s*(?i:exit(?:[ _]?code|[ _]status)?|rc)\s*[:=]\s*[1-9]\d*\s*$",  # echo "exit code: $?"
    r"\b[1-9]\d* (?:failed|errors?)\b[^\n]* in [\d.]+m?s\b",          # pytest: "1 failed, 2 passed in 0.1s"
    r"\b(?:Test Files|Tests)\s+[1-9]\d* failed\b",                    # vitest
    r"\b(?:Tests|Test Suites):\s+[1-9]\d* failed\b",                  # jest
    r"^(?:\S+:\s+)?\s*[1-9]\d* (?:fail|failed|failing)\s*$",          # bun / playwright / mocha
    r"^(?:\S+:\s+)?\s*(?:--- )?FAIL\b",                              # go test, jest, vitest file lines
    r"\btest result: FAILED\b",                                       # cargo test
    r"^error: could not compile\b",                                   # cargo build
    r"\berror TS\d+:",                                                # tsc
    r"\b[1-9]\d* problems? \([1-9]\d* errors?",                        # eslint
    r"\bexited with code [1-9]\d*",                                    # bun/npm/yarn scripts
    r"\bcommand\s+(?:\([^)\n]*\)\s+)?exited \([1-9]\d*\)",              # turbo
    r"^Failed:\s+\S+#\S+",                                             # turbo task summary
    r"^npm (?:ERR!|error) code\b",                                     # npm
    r"^Found [1-9]\d* errors? in [1-9]\d* files?",                     # mypy
]), re.M)


def output_failed(text: str | None) -> bool:
    """True when test/build output shows a failure that the exit status may have hidden."""
    return bool(text) and FAIL_MARKERS.search(text) is not None


def _result_text(blk: dict) -> str:
    c = blk.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


# --- small helpers ----------------------------------------------------------
def _load(path: str) -> list[dict]:
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def _content(e: dict):
    m = e.get("message")
    return m.get("content") if isinstance(m, dict) else None


def _text(e: dict) -> str | None:
    c = _content(e)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        ts = [b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(ts) if ts else None
    return None


def _blocks(e: dict, kind: str) -> list[dict]:
    c = _content(e)
    if not isinstance(c, list):
        return []
    return [b for b in c if isinstance(b, dict) and b.get("type") == kind]


def prompt_kind(e: dict) -> str | None:
    """'prompt' | 'command' | 'interrupt' (marker) | None (not a human prompt)."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"):
        return None
    if _blocks(e, "tool_result"):
        return None
    t = _text(e)
    if not t or not t.strip():
        return None
    s = t.lstrip()
    if s.startswith(INTERRUPT_PREFIX):
        return "interrupt"
    origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
    if origin.get("kind") == "task-notification" or e.get("promptSource") == "system":
        return None
    if s.startswith(PEER_PREFIX):
        return None
    if s.startswith("<command-name>") or s.startswith("<command-message>"):
        return "command"
    if s.startswith("<"):
        return None
    return "prompt"


def _count_lines(s) -> int:
    if not isinstance(s, str) or not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _edit_counts(name: str, inp: dict, tur) -> tuple[int, int]:
    if isinstance(tur, dict) and isinstance(tur.get("structuredPatch"), list) and tur["structuredPatch"]:
        a = r = 0
        for h in tur["structuredPatch"]:
            for ln in (h.get("lines") or []) if isinstance(h, dict) else []:
                if isinstance(ln, str) and ln.startswith("+"):
                    a += 1
                elif isinstance(ln, str) and ln.startswith("-"):
                    r += 1
        return a, r
    if name == "Write":
        return _count_lines(inp.get("content")), 0
    if name == "Edit":
        return _count_lines(inp.get("new_string")), _count_lines(inp.get("old_string"))
    if name == "MultiEdit":
        a = r = 0
        for ed in inp.get("edits") or []:
            if isinstance(ed, dict):
                a += _count_lines(ed.get("new_string"))
                r += _count_lines(ed.get("old_string"))
        return a, r
    if name == "NotebookEdit":
        return _count_lines(inp.get("new_source")), 0
    return 0, 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


# --- fork / continuation dedupe ---------------------------------------------
_CONT_CACHE: dict[str, dict[str, str]] = {}
_UUID_CACHE: dict[str, set[str]] = {}


def _continued_index(d: str) -> dict[str, str]:
    """{continued_session_id: source transcript path} for one project dir (memoized)."""
    if d in _CONT_CACHE:
        return _CONT_CACHE[d]
    idx: dict[str, str] = {}
    for p in glob.glob(os.path.join(d, "*.jsonl")):
        try:
            with open(p, errors="replace") as f:
                for line in f:
                    if '"continued-in"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("type") == "continued-in" and e.get("continuedInSessionId"):
                        idx[e["continuedInSessionId"]] = p
        except OSError:
            continue
    _CONT_CACHE[d] = idx
    return idx


def _uuids(path: str) -> set[str]:
    if path not in _UUID_CACHE:
        _UUID_CACHE[path] = {e["uuid"] for e in _load(path) if e.get("uuid")}
    return _UUID_CACHE[path]


# --- per-turn accumulator ---------------------------------------------------
class _T:
    def __init__(self, turn: Turn, at: datetime):
        self.turn = turn
        self.at = at
        self.first_asst: datetime | None = None
        self.last_act: datetime | None = None      # assistant / tool_result
        self.td: list[datetime] = []
        self.interrupt_at: datetime | None = None
        self.prompt_id: str | None = None          # user entry promptId == hook payload prompt_id
        self.last_uuid: str | None = None
        self.dangling_last = False                 # last activity = tool_use w/o result
        self.side_ts: list[datetime] = []          # subagent activity inside the turn's window
        self.run_closed_at: datetime | None = None  # ts of the last turn_duration
        self.detached = False                      # later runs (after a >15 min idle) don't extend the end

    def attach(self, t: datetime, idle_resume: bool = False) -> bool:
        """Does activity at t still belong to the turn's agent interval?

        After a run ended (turn_duration) or after a non-tool assistant reply, a new run that starts
        more than BREAK later was triggered by something else (task notification, peer message,
        resume) -- its tool/api rows stay in this turn but it does not stretch the turn's end."""
        if self.detached:
            return False
        ref = self.run_closed_at or (self.last_act if idle_resume else None)
        if ref is not None and t - ref > BREAK:
            self.detached = True
            return False
        self.run_closed_at = None
        return True


def _initiator(turn: Turn | None, kind: str, intent: str | None) -> tuple[str, str]:
    if turn is not None and turn.prompt_kind == "command":
        return "default", "2:command"
    if turn is not None and kind == "bash" and intent in ("test", "build"):
        span = classify.human_check_request(turn.prompt_text)
        if span:
            return "human", "3:prompt:" + clip(span, 60)
    return "agent", "5"


class _Parser:
    def __init__(self, path: str, hook_events: list[dict] | None):
        self.path = path
        self.hooks = hook_events or []
        self.b: Bundle | None = None
        self.ts_list: list[_T] = []
        self.tools: dict[str, ToolCall] = {}
        self.tool_inp: dict[str, dict] = {}
        self.tool_at: dict[str, datetime] = {}
        self.edits: dict[str, list[FileEdit]] = {}
        self.reqs: dict[str, dict] = {}
        self.tooling_seen: set[tuple] = set()   # (turn_idx, category, name)
        self.cmd_event: dict[int, Event] = {}   # turn_idx -> its command event
        self.sess_cwd: str | None = None

    # ---- tooling events (skills / commands / mcp / plugins) ----
    def _tooling(self, t, tidx, category: str, name: str, initiator: str, rule: str,
                 tool_use_id: str | None = None, once: bool = False) -> Event | None:
        if not name or t is None:
            return None
        key = (tidx, category, name)
        if once and key in self.tooling_seen:
            return None
        self.tooling_seen.add(key)
        ev = Event("tooling", iso(t), tidx, {"category": category, "name": name, "initiator": initiator,
                                              "rule": rule, "tool_use_id": tool_use_id})
        self.b.events.append(ev)
        if category == "skill" and ":" in name:
            self._tooling(t, tidx, "plugin", name.split(":", 1)[0], initiator, rule, tool_use_id, once=True)
        return ev

    def _promote_command(self, tidx: int, t) -> None:
        """The turn's slash command turned out to be a skill: one human skill use, not command + skill."""
        ev = self.cmd_event.pop(tidx)
        ev.data["category"] = "skill"
        self.tooling_seen.add((tidx, "skill", ev.data["name"]))
        if ":" in ev.data["name"]:
            self._tooling(t or parse_ts(ev.at), tidx, "plugin", ev.data["name"].split(":", 1)[0],
                          "human", "claude:command-name", once=True)

    def _assistant_tooling(self, e: dict, t, tidx) -> None:
        sk = e.get("attributionSkill")
        if isinstance(sk, str) and sk:
            ev = self.cmd_event.get(tidx) if tidx is not None else None
            if ev is not None and ev.data["name"].split(":")[-1] == sk.split(":")[-1]:
                self._promote_command(tidx, t)   # attribution of the command this turn started
            self._tooling(t, tidx, "skill", sk, "agent", "claude:attributionSkill", once=True)
        pl = e.get("attributionPlugin")
        if isinstance(pl, str) and pl:
            self._tooling(t, tidx, "plugin", pl, "agent", "claude:attributionPlugin", once=True)

    # ---- turn lookup by time ----
    def _turn_at(self, t: datetime | None) -> int | None:
        if t is None:
            return None
        k = None
        for i, tt in enumerate(self.ts_list):
            if tt.at <= t:
                k = i
            else:
                break
        return k

    # ---- shared assistant / tool handling (main + sidechain) ----
    def _assistant(self, e: dict, t: datetime | None, tidx: int | None, side: str | None) -> None:
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        rid = e.get("requestId")
        if rid and m.get("model") != "<synthetic>":
            u = m.get("usage") or {}
            r = self.reqs.get(rid)
            if r is None:
                self.reqs[rid] = {"first": e, "best": e, "t": t, "tidx": tidx, "side": side}
            else:
                bo = ((r["best"].get("message") or {}).get("usage") or {}).get("output_tokens") or 0
                if (u.get("output_tokens") or 0) > bo:
                    r["best"] = e
        self._assistant_tooling(e, t, tidx)
        for blk in _blocks(e, "tool_use"):
            tid = blk.get("id")
            if not tid or tid in self.tools:
                continue
            name = blk.get("name") or "?"
            inp = blk.get("input") if isinstance(blk.get("input"), dict) else {}
            kind = _safe(classify.tool_kind, name) or "other"
            intent = None
            args = None
            if kind == "bash" or name == "Bash":
                cmd = inp.get("command") or ""
                intent = _safe(classify.bash_intent, cmd)
                args = (_safe(classify.summarize_command, cmd, MAX_ARGS) or cmd)[:MAX_ARGS]
            elif inp.get("file_path") or inp.get("notebook_path"):
                args = str(inp.get("file_path") or inp.get("notebook_path"))[:MAX_ARGS]
            elif inp.get("pattern") or inp.get("path"):
                args = str(inp.get("pattern") or inp.get("path"))[:MAX_ARGS]
            turn = self.ts_list[tidx].turn if tidx is not None else None
            ini, rule = _initiator(turn, kind, intent)
            tc = ToolCall(turn_idx=tidx, tool_use_id=tid, name=name, kind=kind, bash_intent=intent,
                          initiator=ini, initiator_rule=rule, args_summary=args, started_at=iso(t),
                          sidechain_id=side, cwd=e.get("cwd"))
            self.tools[tid] = tc
            if name == "Skill":
                self._tooling(t, tidx, "skill", str(inp.get("skill") or ""), "agent", "claude:Skill", tid)
            elif name.startswith("mcp__"):
                server = name.split("__")[1] if name.count("__") >= 2 else name[5:]
                self._tooling(t, tidx, "mcp", server, "agent", "claude:mcp", tid)
                if server.startswith("plugin_"):
                    self._tooling(t, tidx, "plugin", server[7:].split("_")[0], "agent", "claude:mcp", tid,
                                  once=True)
            self.tool_inp[tid] = inp
            if t:
                self.tool_at[tid] = t
            if name in EDIT_TOOLS:
                p = inp.get("file_path") or inp.get("notebook_path")
                if p:
                    a, r = _edit_counts(name, inp, None)
                    self.edits[tid] = [FileEdit(tid, p, _safe(classify.manifest_dir, p), a, r)]
            elif kind == "bash" or name == "Bash":
                # every written path, whatever the aggregated intent (`cat > f <<EOF … && pytest` is "test")
                paths = _safe(classify.bash_edit_paths, inp.get("command") or "") or []
                cwd = e.get("cwd")
                paths = [os.path.join(cwd, p) if cwd and not os.path.isabs(p) else p for p in paths]
                # `cp x .` targets and agent scratch space (/tmp, */scratchpad/*) are not code edits
                paths = [p for p in paths if not classify.noise_edit_path(p, self.sess_cwd)]
                if paths:
                    self.edits[tid] = [FileEdit(tid, p, _safe(classify.manifest_dir, p)) for p in paths]

    def _tool_results(self, e: dict, t: datetime | None) -> set:
        """Mark results on their tool calls; returns the turn idxs of the matched calls."""
        res = _blocks(e, "tool_result")
        tur = e.get("toolUseResult")
        turns = set()
        for blk in res:
            tc = self.tools.get(blk.get("tool_use_id"))
            if tc is None:
                continue
            turns.add(tc.turn_idx)
            tc.has_result = True
            tc.is_error = bool(blk.get("is_error"))
            if tc.name == "Bash" and not tc.is_error:
                if isinstance(tur, dict) and len(res) == 1 and tur.get("interrupted") is True:
                    tc.is_error = True
                elif tc.bash_intent in ("test", "build") and output_failed(_result_text(blk)):
                    tc.is_error = True
            st = self.tool_at.get(tc.tool_use_id)
            if st and t:
                tc.duration_ms = max(0, int((t - st).total_seconds() * 1000))
            if tc.name in EDIT_TOOLS and tc.tool_use_id in self.edits:
                if tc.is_error:
                    del self.edits[tc.tool_use_id]
                    continue
                fe = self.edits[tc.tool_use_id][0]
                if isinstance(tur, dict) and len(res) == 1:
                    fe.user_modified = bool(tur.get("userModified"))
                    fe.lines_added, fe.lines_removed = _edit_counts(tc.name, self.tool_inp[tc.tool_use_id], tur)
        return turns

    # ---- main transcript ----
    def run(self) -> Bundle | None:
        path = self.path
        ents = _load(path)
        if not ents:
            return None
        stem = os.path.splitext(os.path.basename(path))[0]
        sid = next((e["sessionId"] for e in ents if e.get("sessionId")), None) or stem
        d = os.path.dirname(path)

        skip: set[str] = set()
        forked_from = None
        src = _continued_index(d).get(sid)
        if src and os.path.abspath(src) != os.path.abspath(path):
            skip = _uuids(src)
            forked_from = os.path.splitext(os.path.basename(src))[0]

        sess = Session(id=sid, agent="claude", transcript_path=path)
        self.b = b = Bundle(session=sess)
        seen: set[str] = set()
        fifo: list[dict] = []
        pending: dict | None = None
        all_ts: list[datetime] = []
        cur: _T | None = None
        last_act_kind = None  # "tool_use" | "other"
        builtin_event: dict[int, Event] = {}

        for e in ents:
            u = e.get("uuid")
            if u:
                if u in seen or u in skip:
                    continue
                seen.add(u)
            ff = e.get("forkedFrom")
            if isinstance(ff, dict) and ff.get("sessionId") and ff["sessionId"] != sid:
                forked_from = forked_from or ff["sessionId"]
                if os.path.exists(os.path.join(d, ff["sessionId"] + ".jsonl")):
                    continue
            ty = e.get("type")
            t = parse_ts(e.get("timestamp"))
            if ty in ("user", "assistant", "system") and t:
                all_ts.append(t)
            if ty in ("user", "assistant", "system", "attachment"):
                sess.cwd = sess.cwd or e.get("cwd")
                self.sess_cwd = sess.cwd
                sess.branch = sess.branch or e.get("gitBranch")
            if e.get("isSidechain"):
                continue

            if ty == "queue-operation":
                op = e.get("operation")
                if op == "enqueue":
                    fifo.append({"at": t, "content": e.get("content")})
                elif op in ("dequeue", "popAll"):
                    item = fifo.pop(0) if fifo else {"at": None, "content": e.get("content")}
                    if op == "popAll":
                        fifo.clear()
                    item["deq"] = t
                    pending = item
                elif op == "remove":
                    c = e.get("content")
                    for i, it in enumerate(fifo):
                        if it["content"] == c:
                            fifo.pop(i)
                            break
                    if t and isinstance(c, str) and not c.lstrip().startswith("<"):
                        b.events.append(Event("queue_enqueue", iso(t), self._turn_idx(cur),
                                              {"reason": e.get("reason") or "removed"}))
                continue

            if ty == "user":
                pk = prompt_kind(e)
                if pk == "interrupt":
                    if cur is not None and t:
                        cur.interrupt_at = t
                        cur.turn.interrupted = True
                        cur.last_uuid = u or cur.last_uuid
                    continue
                if pk in ("prompt", "command") and t:
                    kind = pk
                    prompt_at = t
                    enq = None
                    ps = e.get("promptSource")
                    if ps == "queued" or (ps is None and pending is not None):
                        if pk == "prompt":
                            kind = "queued"
                            dq = pending.get("deq") if pending is not None else None
                            # a stale dequeue (not right before this prompt) is not this prompt's
                            if dq is not None and abs((t - dq).total_seconds()) <= 5:
                                prompt_at = min(dq, t)
                                enq = pending.get("at")
                                if enq is not None and enq > prompt_at:
                                    enq = None
                    pending = None
                    turn = Turn(idx=len(self.ts_list), prompt_at=iso(prompt_at), prompt_text=_text(e),
                                prompt_kind=kind, first_uuid=u, last_uuid=u, enqueued_at=iso(enq))
                    cur = _T(turn, prompt_at)
                    cur.prompt_id = e.get("promptId")
                    self.ts_list.append(cur)
                    b.turns.append(turn)
                    last_act_kind = None
                    m = CMD_NAME.search(turn.prompt_text or "") if kind == "command" else None
                    if m and m.group(1).lower() in BUILTIN_COMMANDS:
                        ev = Event("command_builtin", iso(t), turn.idx, {"name": m.group(1)})
                        b.events.append(ev)
                        builtin_event[turn.idx] = ev
                    elif m:
                        ev = self._tooling(t, turn.idx, "command", m.group(1), "human", "claude:command-name")
                        if ev is not None:
                            self.cmd_event[turn.idx] = ev
                    continue
                if e.get("isMeta") and cur is not None and (_text(e) or "").lstrip().startswith(SKILL_BASE):
                    if cur.turn.idx in builtin_event:   # a skill shadowing a built-in name: it is tooling
                        bev = builtin_event.pop(cur.turn.idx)
                        b.events.remove(bev)
                        ev = self._tooling(parse_ts(bev.at), cur.turn.idx, "command", bev.data["name"], "human",
                                           "claude:command-name")
                        if ev is not None:
                            self.cmd_event[cur.turn.idx] = ev
                    if cur.turn.idx in self.cmd_event:
                        self._promote_command(cur.turn.idx, t)   # the slash command expanded a skill
                if not e.get("isMeta") and not _blocks(e, "tool_result") and _text(e):
                    pending = None  # dequeued item was a task-notification / agent message
                owners = self._tool_results(e, t)
                if cur is not None and t and cur.turn.idx in owners and cur.attach(t):
                    cur.last_act = max(cur.last_act or t, t)
                    cur.last_uuid = u or cur.last_uuid
                    last_act_kind = "other"
                continue

            if ty == "assistant":
                if (e.get("message") or {}).get("model") == "<synthetic>":
                    continue    # local/API-error placeholder, not model work
                tidx = self._turn_idx(cur)
                self._assistant(e, t, tidx, None)
                if cur is not None and t and cur.attach(t, idle_resume=last_act_kind == "other"):
                    cur.first_asst = cur.first_asst or t
                    cur.last_act = max(cur.last_act or t, t)
                    cur.last_uuid = u or cur.last_uuid
                    last_act_kind = "tool_use" if _blocks(e, "tool_use") else "other"
                continue

            if ty == "system":
                st = e.get("subtype")
                if st == "turn_duration" and cur is not None and t and cur.attach(t):
                    cur.td.append(t)
                    cur.run_closed_at = t
                elif st == "compact_boundary" and t:
                    cm = e.get("compactMetadata") or {}
                    b.events.append(Event("compaction", iso(t), self._turn_idx(cur),
                                          {"trigger": cm.get("trigger"), "preTokens": cm.get("preTokens")}))
                continue

        if not b.turns:
            return None
        self.ts_list[-1].dangling_last = (
            last_act_kind == "tool_use"
            and any(not tc.has_result and tc.turn_idx == len(self.ts_list) - 1 and tc.sidechain_id is None
                    for tc in self.tools.values()))
        self._subagents(path)
        self._match_hooks()
        self._finish_turns()
        self._hooks()
        self._emit()
        sess.forked_from = forked_from
        # worktrees group under their main repo; branch from git only when the transcript has none
        sess.repo, git_branch = classify.repo_root(sess.cwd)
        sess.branch = sess.branch or git_branch
        if all_ts:
            sess.started_at, sess.ended_at = iso(min(all_ts)), iso(max(all_ts))
        sess.ended_by = b.turns[-1].end_source
        return b

    def _turn_idx(self, cur: _T | None) -> int | None:
        return cur.turn.idx if cur is not None else None

    # ---- subagents ----
    def _subagents(self, path: str) -> None:
        sdir = os.path.join(os.path.splitext(path)[0], "subagents")
        for sp in sorted(glob.glob(os.path.join(sdir, "*.jsonl"))):
            ents = _load(sp)
            if not ents:
                continue
            meta = {}
            mp = sp[:-6] + ".meta.json"
            try:
                with open(mp) as f:
                    meta = json.load(f) or {}
            except (OSError, ValueError):
                pass
            aid = next((e["agentId"] for e in ents if e.get("agentId")), None) \
                or os.path.basename(sp)[:-6].removeprefix("agent-")
            n_before = len(self.tools)
            first = last = None
            model = meta.get("model")
            seen: set[str] = set()
            for e in ents:
                u = e.get("uuid")
                if u:
                    if u in seen:
                        continue
                    seen.add(u)
                t = parse_ts(e.get("timestamp"))
                if t is None or e.get("type") not in ("user", "assistant"):
                    continue
                first = first or t
                last = max(last or t, t)
                tidx = self._turn_at(t)
                if e.get("type") == "assistant":
                    if not model:
                        mm = (e.get("message") or {}).get("model")
                        model = mm if mm != "<synthetic>" else None
                    self._assistant(e, t, tidx, aid)
                else:
                    self._tool_results(e, t)
                if tidx is not None:
                    tt = self.ts_list[tidx]
                    tt.side_ts.append(t)
            if first is None:
                continue
            link = self.tools.get(meta.get("toolUseId") or "")
            tidx = link.turn_idx if link is not None and link.sidechain_id is None else self._turn_at(first)
            data = {"agent_id": aid, "model": model, "n_tools": len(self.tools) - n_before}
            if meta.get("spawnDepth") is not None:
                data["depth"] = meta.get("spawnDepth")
            if meta.get("agentType"):
                data["agent_type"] = meta.get("agentType")
            self.b.events.append(Event("subagent", iso(first), tidx, data))

    # ---- turn ends ----
    def _finish_turns(self) -> None:
        n = len(self.ts_list)
        for i, tt in enumerate(self.ts_list):
            turn = tt.turn
            nxt = self.ts_list[i + 1].at if i + 1 < n else None
            turn.agent_started_at = iso(tt.first_asst)
            turn.last_uuid = tt.last_uuid or turn.last_uuid
            if tt.interrupt_at is not None:
                turn.agent_finished_at, turn.end_source = iso(tt.interrupt_at), "interrupt"
                continue
            if self._abandoned(i):
                # Esc before the first agent output writes no marker: Claude puts the prompt back in the
                # input box and the human resubmits (often edited). Zero agent time, interrupted.
                turn.interrupted = True
                turn.agent_finished_at, turn.end_source = iso(tt.at), "interrupt"
                continue
            # Stop hooks matched by prompt_id (exact) else by time window; a Stop long after the
            # turn's last agent entry belongs to a detached rerun and is ignored
            stops = [at for h, at, k in self.hook_turns if k == i and h.get("event") == "Stop"
                     and at >= tt.at and (nxt is None or at < nxt)
                     and (tt.last_act is None or at - tt.last_act <= BREAK)]
            if stops:
                end, src = max(stops), "stop_hook"
            elif tt.td:
                end, src = max(tt.td), "turn_duration"
                # sanity cap: a turn_duration far past the turn's own last agent entry or the next
                # prompt was not this turn's run end -> fall back to the last agent entry
                if tt.last_act is not None and (end - tt.last_act > CAP or (nxt is not None and end - nxt > CAP)):
                    end, src = tt.last_act, "last_entry"
            elif tt.last_act is not None:
                end = tt.last_act
                src = "crash" if (i == n - 1 and tt.dangling_last) else "last_entry"
            else:
                end, src = tt.at, "last_entry"
            ext = end
            for st in sorted(tt.side_ts):   # subagent activity extends the end while contiguous
                if st - ext > BREAK:
                    break
                ext = max(ext, st)
            if ext > end:                   # ... but never past the next prompt
                end = ext if nxt is None else max(end, min(ext, nxt))
            turn.agent_finished_at, turn.end_source = iso(max(end, tt.at)), src

    def _abandoned(self, i: int) -> bool:
        """Prompt with no agent output at all, followed by another prompt within ABANDON."""
        tt = self.ts_list[i]
        if i + 1 >= len(self.ts_list) or tt.turn.prompt_kind not in ("prompt", "queued"):
            return False
        nx = self.ts_list[i + 1]
        if nx.turn.prompt_kind not in ("prompt", "queued") or nx.at - tt.at > ABANDON:
            return False
        if tt.first_asst is not None or tt.last_act is not None or tt.td or tt.side_ts:
            return False
        return not any(tc.turn_idx == i for tc in self.tools.values())

    # ---- hook events ----
    def _match_hooks(self) -> None:
        """hook line {at, agent, event, payload{session_id, prompt_id, …}, git?} -> (line, at, turn_idx)."""
        by_pid = {tt.prompt_id: i for i, tt in enumerate(self.ts_list) if tt.prompt_id}
        self.hook_turns = []
        for h in self.hooks:
            at = parse_ts(h.get("at"))
            if at is None:
                continue
            p = h.get("payload") if isinstance(h.get("payload"), dict) else {}
            k = by_pid.get(p.get("prompt_id"))
            self.hook_turns.append((h, at, k if k is not None else self._turn_at(at)))

    def _hooks(self) -> None:
        # git lives only on git_snapshot (taken at Stop); hook rows carry no paths, so metrics
        # never count the same working-tree state twice
        for h, at, tidx in self.hook_turns:
            git = h.get("git") if isinstance(h.get("git"), dict) else None
            p = h.get("payload") if isinstance(h.get("payload"), dict) else {}
            data = {"event": h.get("event"), "has_git": git is not None}
            if p.get("prompt_id"):
                data["prompt_id"] = p.get("prompt_id")
            if p.get("tool_use_id"):
                data["tool_use_id"] = p.get("tool_use_id")
            self.b.events.append(Event("hook", iso(at), tidx, data))
            if h.get("event") == "Stop" and git is not None:
                self.b.events.append(Event("git_snapshot", iso(at), tidx, {"git": git}))
            elif h.get("event") == "SessionStart" and git is not None:
                # working-tree state before the agent did anything: hand_edits diffs against it
                self.b.events.append(Event("git_snapshot", iso(at), tidx, {"git": git, "baseline": True}))

    # ---- api calls / tool calls / edits into the bundle ----
    def _emit(self) -> None:
        b = self.b
        for rid, r in self.reqs.items():
            e = r["best"]
            m = e.get("message") or {}
            u = m.get("usage") or {}
            model = m.get("model") or (r["first"].get("message") or {}).get("model")
            inp, cr, cw, out = (int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
                                int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0))
            b.api_calls.append(ApiCall(
                turn_idx=r["tidx"], request_id=rid, model=model, effort=e.get("effort") or r["first"].get("effort"),
                input_tok=inp, cached_tok=cr, cache_write_tok=cw, output_tok=out,
                context_window=_safe(prices.context_window, model),
                cost_usd=_safe(prices.cost_usd, model, inp, cr, cw, out),
                at=iso(r["t"]), sidechain_id=r["side"]))
        b.tool_calls = list(self.tools.values())
        for fes in self.edits.values():
            b.file_edits.extend(fes)


def parse(path: str, hook_events: list[dict] | None = None) -> Bundle | None:
    return _Parser(path, hook_events).run()
