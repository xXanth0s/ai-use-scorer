"""Correction-episode judge (spec §1 metric C, §6d). Runs inside scorer at report time.

Unit: a turn whose prompt follows a previous agent turn in the same session. The judge sees
  - a clipped summary of the previous agent turn (tool calls with bash_intent / is_error,
    end_source, interruption) built ONLY from DB columns (final assistant text is not stored),
  - the current prompt text (<= PROMPT_CHARS),
and returns {event, cause, detection, evidence, confidence, defect_caught, resolved,
user_action, preventable_by_user, action_category} (same call). user_action is a <= 120-char
imperative for the human; validate() nulls it on abstain, when not preventable, and when it
quotes > 80 chars of the prompt. `action_items(conn, day)` aggregates them per day (see below).

Backends, in order: Anthropic API (ANTHROPIC_API_KEY) -> `claude -p` (CLI on PATH) -> regex
stand-in (source="regex", confidence 0.3, clearly flagged). Every backend's output goes through
`validate()`: unknown enum -> abstain; evidence must be an exact substring of the prompt the model
saw, else abstain. Empty prompt -> abstain without any call.

Cache: table judge_label(turn_hash PK, model, label JSON, at). turn_hash =
sha1(model + prompt + prev summary + PROMPT_VERSION). Cached hashes are never re-sent.

Metrics (scope day, per run_id; previous C rows for run+day deleted first):
  C:event:<event>, C:cause:<cause>, C:detection:<detection> (cause/detection over corrections only),
  C_abstain, C_source:<api|claude-p|regex|rule>, C:defect_caught, C:resolved,
  C_labelled (value = labelled turns, n = candidate turns; < n when the report path's cap hit),
  C+:<episode> (gated positive episodes; evidence note "<episode>: " + clip(reason, 60)).
Spec: "Scored by cause, never by count."

Positive episodes (judge-v3, spec §1 "C+"): the same call also returns positive_episode (one of
EPISODES or null) + episode_reason (<= 120). validate() nulls unknown labels, labels without
evidence, and reasons quoting > 80 prompt chars. The label alone never counts: `episodes(conn, day)`
applies a deterministic gate per label (see _episode_gates) and writes C+:<episode> rows.
First prompts of a session are labelled too (candidates(first=True)) so good_spec can be judged;
they are excluded from every C row and from the n/labelled stats.

Privacy: prompt text goes to the model only. Logs/stdout carry counts, never prompt text.
Stored evidence <= 200 chars (judge_label.label); metric_evidence.note uses clip(evidence, 60).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .model import DEFINITION_VERSION, clip, local_day, scorer_home

PROMPT_VERSION = "judge-v3"  # v3: positive_episode / episode_reason (v2: user_action, user_strength)
PROMPT_CHARS = 2000
SUMMARY_CHARS = 1500
EVIDENCE_CHARS = 200
ACTION_CHARS = 120
QUOTE_MAX = 80             # an action may never contain more than this many consecutive prompt chars
MAX_TOOL_LINES = 40
WORKERS = 4
CLAUDE_P_TIMEOUT = 60

EVENTS = ("correction", "new_requirement", "clarification", "uncertain", "abstain")
CAUSES = ("instruction_violated", "info_missing", "requirement_changed", "env_failure", "unknown")
DETECTIONS = ("human_review", "automated_check", "agent_self_review", "unknown")
SOURCES = ("api", "claude-p", "regex", "rule")
ACTION_CATEGORIES = ("prompt_specificity", "verification", "context", "task_split", "tooling", "review", "other")
# positive episodes, in report priority order (line 6 "Repeat" picks the first present)
EPISODES = ("verification_catch", "good_spec", "scoped_turn", "early_redirect", "knew_when_not_to_delegate")
REASON_CHARS = 120
FIRST_SUMMARY = "previous turn: none (session start: this is the human's FIRST prompt of the session)"

DDL = """CREATE TABLE IF NOT EXISTS judge_label(turn_hash TEXT PRIMARY KEY, model TEXT,
  label JSON, at TEXT);
CREATE TABLE IF NOT EXISTS judge_action(day TEXT, run_hash TEXT, items JSON, at TEXT,
  PRIMARY KEY(day, run_hash));"""

SCHEMA = {
    "type": "object",
    "properties": {
        "event": {"type": "string", "enum": list(EVENTS)},
        "cause": {"type": "string", "enum": list(CAUSES)},
        "detection": {"type": "string", "enum": list(DETECTIONS)},
        "evidence": {"type": "string",
                     "description": "Exact, verbatim substring of the USER PROMPT (max 200 chars) that "
                                    "justifies the label. Empty string only when event is abstain."},
        "confidence": {"type": "number", "description": "0..1"},
        "defect_caught": {"type": "boolean"},
        "resolved": {"type": ["boolean", "null"]},
        "user_action": {"type": ["string", "null"],
                        "description": "Short imperative for the HUMAN (max 120 chars), grounded in the evidence. "
                                       "Null when the human could not reasonably have prevented this turn."},
        "preventable_by_user": {"type": "boolean"},
        "action_category": {"type": "string", "enum": list(ACTION_CATEGORIES)},
        "user_strength": {"type": ["string", "null"],
                          "description": "Short description (max 120 chars) of a good HUMAN move visible in "
                                         "this prompt, grounded in the evidence. Null if none."},
        "strength_category": {"type": "string", "enum": list(ACTION_CATEGORIES)},
        "positive_episode": {"anyOf": [{"type": "string", "enum": list(EPISODES)}, {"type": "null"}],
                             "description": "A positive episode the HUMAN shows in this prompt, or null."},
        "episode_reason": {"type": ["string", "null"],
                           "description": "Why (max 120 chars), grounded in the evidence; null when "
                                          "positive_episode is null."},
    },
    "required": ["event", "cause", "detection", "evidence", "confidence", "defect_caught", "resolved",
                 "user_action", "preventable_by_user", "action_category", "user_strength",
                 "strength_category", "positive_episode", "episode_reason"],
    "additionalProperties": False,
}

TOOL_NAME = "record_label"


SYSTEM = """You label one moment in a human's session with a coding agent.
You see what the agent did in its previous turn (a summary of tool calls; the agent's final text is not available) and the human's next prompt. Decide what the human's prompt is:

event:
- correction: the human says the agent's previous work is wrong, broken, not what was asked, or tells it to undo/redo something.
- new_requirement: the previous work is accepted (or not addressed) and the human asks for something new or additional.
- clarification: the human answers a question, adds missing detail, or disambiguates the existing request without saying the work was wrong.
- uncertain: it might be a correction but the prompt alone does not make it clear.
- abstain: not enough signal (empty, a bare slash command, "continue", pasted output without comment).

cause (why the correction was needed; for non-corrections use the closest fit or unknown):
- instruction_violated: the agent ignored or contradicted an explicit instruction.
- info_missing: the agent lacked information the human had not given (or a doc/README misled it).
- requirement_changed: the human changed their mind.
- env_failure: tooling/environment/flaky test/network broke, not the agent's reasoning.
- unknown.

detection (who noticed the problem):
- human_review: the human read code/output and noticed.
- automated_check: a test/build/linter/CI failure surfaced it (e.g. the human pastes an error).
- agent_self_review: the agent itself reported the problem and the human reacts.
- unknown.

defect_caught: true if the prompt points out a concrete defect in the agent's output.
resolved: true/false if the prompt says whether an earlier problem is now fixed, else null.
evidence: copy an exact, verbatim substring of the USER PROMPT (not of the summary), at most 200 characters, that supports your label. Do not paraphrase, translate or fix typos. The prompt may be German or English.
confidence: 0..1.

Lesson for the human (score the human's choices, not the agent):
preventable_by_user: true only if a different choice by the HUMAN (a more specific prompt, asking for a check, giving context, splitting the task, using a tool, reviewing earlier) would likely have avoided this turn.
user_action: one short imperative telling the human what to do differently next time, at most 120 characters, e.g. "Name the CLI library in the first prompt", "Ask for a test run before accepting edits", "Split the task: one worktree per feature". It must follow from the evidence quote. Do not copy the prompt: never repeat more than a few words of it. Use null when preventable_by_user is false, when event is abstain, and for a new_requirement or clarification that carries no lesson.
action_category: prompt_specificity | verification | context | task_split | tooling | review | other (closest fit; "other" when user_action is null).
user_strength: a short description, at most 120 characters, of a good move by the HUMAN visible in this prompt, e.g. "Gave acceptance criteria up front", "Pointed the agent at the failing test", "Caught the defect in review", "Split the task into a fresh session", "Asked for a test run before accepting". It must follow from the evidence quote; do not copy the prompt. Null if there is none (most prompts have none) and when event is abstain.
strength_category: same categories as action_category ("other" when user_strength is null).

Positive episode (what the HUMAN did well; most prompts have none -> null; null when event is abstain):
- good_spec: only on the session's FIRST prompt (no previous turn): it states the goal, constraints AND acceptance criteria ("done when ...", a test that must pass).
- verification_catch: the human asks for a check (run the tests/build/typecheck, review the diff) to catch defects before accepting the work.
- early_redirect: the human interrupts or corrects the agent early, before it goes down a wrong path, and states a specific reason (not just "stop" or "no").
- scoped_turn: a small, bounded request with explicit done criteria (e.g. one function or file, "done when X passes").
- knew_when_not_to_delegate: the human says they fixed or will fix something by hand, or takes over a part the agent kept getting wrong.
episode_reason: at most 120 characters, why this is a good move, following from the evidence quote; do not copy the prompt. Null when positive_episode is null.
The evidence quote must also support the positive_episode when you give one.
When there is no previous agent turn (session start), event is new_requirement or abstain, never correction.
Call the record_label tool exactly once."""


_PROMPT_DIGEST = hashlib.sha1((SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:12]


# --- input -------------------------------------------------------------------------------------

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_summary(prev: dict, tools: list[dict], interrupted: bool = False) -> str:
    """Clipped, DB-only summary of the previous agent turn."""
    lines = [f"previous turn: end={prev.get('end_source') or 'unknown'}"
             + (", interrupted by user" if interrupted else "")]
    errs = sum(1 for t in tools if t.get("is_error"))
    lines.append(f"tool calls: {len(tools)}, errors: {errs}")
    for t in tools[-MAX_TOOL_LINES:]:
        bits = [t.get("name") or "?"]
        if t.get("bash_intent"):
            bits.append(f"intent={t['bash_intent']}")
        if t.get("is_error"):
            bits.append("ERROR")
        if t.get("has_result") == 0:
            bits.append("no-result")
        arg = clip(t.get("args_summary"), 80)
        lines.append("- " + " ".join(bits) + (f" :: {arg}" if arg else ""))
    s = "\n".join(lines)
    return s if len(s) <= SUMMARY_CHARS else s[: SUMMARY_CHARS - 1] + "…"


def build_prompt_text(text: str | None) -> str:
    s = (text or "").strip()
    return s[:PROMPT_CHARS]


def turn_hash(model: str, prompt: str, summary: str) -> str:
    # PROMPT_VERSION plus a digest of SYSTEM/SCHEMA, so any prompt edit invalidates the cache.
    return hashlib.sha1("\x1f".join((model, prompt, summary, PROMPT_VERSION, _PROMPT_DIGEST)).encode()).hexdigest()


def candidates(conn: sqlite3.Connection, *, day: str | None = None, limit: int | None = None,
               first: bool = False) -> list[dict]:
    """Turns with a previous turn in the same session, optionally on one local day.
    first=True also returns each session's first prompt (first=True, prev_id None, FIRST_SUMMARY).
    Returns dicts: turn_id, prev_id, session_id, day, prompt_kind, prompt (clipped input), summary,
    pos (0-based position in the whole session), first, prev_interrupted."""
    turns = _rows(conn, "SELECT id, session_id, idx, prompt_at, prompt_text, prompt_kind, end_source"
                        " FROM turn ORDER BY session_id, prompt_at, idx")
    interrupted = {r["turn_id"] for r in _rows(conn, "SELECT turn_id FROM event WHERE kind='interrupt'")}
    by_sess: dict[str, list[dict]] = {}
    for t in turns:
        if t["prompt_kind"] == "interrupt":
            continue
        by_sess.setdefault(t["session_id"], []).append(t)
    out = []
    for lst in by_sess.values():
        for pos, cur in enumerate(lst):
            if pos == 0 and not first:
                continue
            prev = lst[pos - 1] if pos else None
            d = local_day(cur["prompt_at"])
            if day and d != day:
                continue
            out.append({"turn_id": cur["id"], "prev_id": prev["id"] if prev else None,
                        "session_id": cur["session_id"], "day": d, "prompt_kind": cur["prompt_kind"],
                        "prev": prev, "pos": pos, "first": pos == 0,
                        "prev_interrupted": bool(prev and prev["id"] in interrupted),
                        "prompt": build_prompt_text(cur["prompt_text"]), "at": cur["prompt_at"] or ""})
    out.sort(key=lambda c: c["at"], reverse=True)  # most recent first, so --limit keeps the newest
    if limit is not None:
        out = out[:limit]
    for c in out:
        prev = c.pop("prev")
        if prev is None:
            c["summary"] = FIRST_SUMMARY
            continue
        tools = _rows(conn, "SELECT name, bash_intent, is_error, has_result, args_summary FROM tool_call"
                            " WHERE turn_id = ? ORDER BY started_at, id", (c["prev_id"],))
        c["summary"] = build_summary(prev, tools, c["prev_interrupted"])
    return out


# --- validation --------------------------------------------------------------------------------

def abstain(source: str, reason: str = "", confidence: float = 0.0) -> dict:
    return {"event": "abstain", "cause": "unknown", "detection": "unknown", "evidence": "",
            "confidence": confidence, "defect_caught": False, "resolved": None,
            "user_action": None, "preventable_by_user": False, "action_category": "other",
            "user_strength": None, "strength_category": "other",
            "positive_episode": None, "episode_reason": None,
            "source": source, "reason": reason}


def quotes_prompt(text: str | None, prompts, n: int = QUOTE_MAX) -> bool:
    """True if `text` contains more than `n` consecutive characters of any of `prompts`."""
    if not text or len(text) <= n:
        return False
    ps = [prompts] if isinstance(prompts, str) else list(prompts)
    for i in range(len(text) - n):
        w = text[i:i + n + 1]
        if any(w in p for p in ps if p):
            return True
    return False


def _action_fields(raw: dict, prompt: str, event: str, evidence: str) -> dict:
    """Coerce user_action / preventable_by_user / action_category / user_strength / strength_category.
    The action is null on abstain, when not preventable, without evidence, or when it quotes
    > QUOTE_MAX chars of the prompt; the strength likewise (except preventability)."""
    cat = raw.get("action_category")
    cat = cat if cat in ACTION_CATEGORIES else "other"
    prev = raw.get("preventable_by_user") is True
    act = raw.get("user_action")
    act = " ".join(act.split())[:ACTION_CHARS] if isinstance(act, str) else None
    if (not act or event == "abstain" or not prev or not evidence.strip()
            or quotes_prompt(act, prompt)):
        act = None
    scat = raw.get("strength_category")
    scat = scat if scat in ACTION_CATEGORIES else "other"
    st = raw.get("user_strength")
    st = " ".join(st.split())[:ACTION_CHARS] if isinstance(st, str) else None
    if not st or event == "abstain" or not evidence.strip() or quotes_prompt(st, prompt):
        st = None
    ep = raw.get("positive_episode")
    ep = ep if ep in EPISODES else None
    why = raw.get("episode_reason")
    why = " ".join(why.split())[:REASON_CHARS] if isinstance(why, str) else None
    if (not ep or not why or event == "abstain" or not evidence.strip() or evidence not in prompt
            or quotes_prompt(why, prompt)):
        ep = why = None
    return {"user_action": act, "preventable_by_user": prev and event != "abstain",
            "action_category": cat if act else "other",
            "user_strength": st, "strength_category": scat if st else "other",
            "positive_episode": ep, "episode_reason": why}


def validate(raw, prompt: str, source: str) -> dict:
    """Coerce any backend output to the schema; evidence must be a substring of `prompt`."""
    if not isinstance(raw, dict):
        return abstain(source, "not-an-object")
    ev, cause, det = raw.get("event"), raw.get("cause"), raw.get("detection")
    if ev not in EVENTS:
        return abstain(source, "bad-event")
    cause = cause if cause in CAUSES else "unknown"
    det = det if det in DETECTIONS else "unknown"
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    evidence = raw.get("evidence") or ""
    if not isinstance(evidence, str):
        evidence = ""
    evidence = evidence[:EVIDENCE_CHARS]
    resolved = raw.get("resolved")
    resolved = resolved if isinstance(resolved, bool) else None
    label = {"event": ev, "cause": cause, "detection": det, "evidence": evidence, "confidence": conf,
             "defect_caught": bool(raw.get("defect_caught")), "resolved": resolved, "source": source}
    label.update(_action_fields(raw, prompt, ev, evidence))
    if ev != "abstain" and (not evidence.strip() or evidence not in prompt):
        out = abstain(source, "evidence-not-substring")
        out["claimed"] = ev
        return out
    if ev == "abstain":
        label["evidence"] = evidence if evidence in prompt else ""
        label.update(user_action=None, preventable_by_user=False, action_category="other",
                     user_strength=None, strength_category="other", positive_episode=None, episode_reason=None)
    return label


# --- backends ----------------------------------------------------------------------------------

def user_message(summary: str, prompt: str) -> str:
    return (f"<previous_agent_turn>\n{summary}\n</previous_agent_turn>\n\n"
            f"<user_prompt>\n{prompt}\n</user_prompt>")


_NO_SAMPLING = re.compile(r"claude-(haiku-5|sonnet-5|opus-5|opus-4-[78]|fable|mythos)")
_NO_FORCED_TOOL = re.compile(r"claude-(opus-5-5|sonnet-5-5|fable-5-1|mythos-5-1)")


def _api_client():
    import anthropic
    return anthropic.Anthropic(max_retries=1, timeout=30.0)


def call_api(summary: str, prompt: str, model: str, client=None, *, system: str = SYSTEM,
             schema: dict = SCHEMA, tool_name: str = TOOL_NAME, content: str | None = None) -> dict:
    """One Messages API call with a strict tool. Returns a raw (unvalidated) dict.
    system/schema/tool_name/content override the per-turn label call (used by action clustering)."""
    client = client or _api_client()
    tool = {"name": tool_name, "description": "Record the result.",
            "strict": True, "input_schema": schema}
    kw = dict(model=model, max_tokens=1024, system=system, tools=[tool],
              messages=[{"role": "user", "content": content if content is not None
                         else user_message(summary, prompt)}])
    if _NO_FORCED_TOOL.search(model):  # these 400 on forced tool_choice; thinking stays on
        kw["tool_choice"] = {"type": "auto"}
        kw["max_tokens"] = 4096
    else:  # forced tool call skips thinking on Haiku 5.5: cheap and fast
        kw["tool_choice"] = {"type": "tool", "name": tool_name}
    if not _NO_SAMPLING.search(model):
        kw["temperature"] = 0  # current-gen models reject non-default sampling (400)
    resp = client.messages.create(**kw)
    if getattr(resp, "stop_reason", None) in ("max_tokens", "refusal"):
        return {"event": "abstain", "_reason": f"stop:{resp.stop_reason}"}
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == tool_name:
            return dict(b.input) if not isinstance(b.input, str) else json.loads(b.input)
    return {"event": "abstain", "_reason": "no-tool-call"}


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def judge_cwd() -> Path:
    """cwd for `claude -p`: SCORER_HOME/judge, whose Claude project slug must contain
    ingest.JUDGE_SLUG_MARKER so the judge's own sessions are never ingested. If SCORER_HOME
    does not end in 'scorer' (e.g. a temp dir), use SCORER_HOME/scorer-judge instead."""
    from .ingest import JUDGE_SLUG_MARKER
    d = scorer_home() / "judge"
    if JUDGE_SLUG_MARKER not in _slug(str(d.resolve() if d.exists() else d)):
        d = scorer_home() / "scorer-judge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def call_claude_p(summary: str, prompt: str, model: str) -> dict:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found")
    cmd = [exe, "-p", "--output-format", "json", "--model", model,
           "--json-schema", json.dumps(SCHEMA), "--no-session-persistence",
           "--setting-sources", "project", "--tools", "",
           "--append-system-prompt", SYSTEM.replace("Call the record_label tool exactly once.",
                                                    "Answer with the JSON object only.")]
    env = {k: v for k, v in os.environ.items() if k != "SUPERSET_HOME_DIR"}
    r = subprocess.run(cmd, input=user_message(summary, prompt), capture_output=True, text=True,
                       timeout=CLAUDE_P_TIMEOUT, cwd=str(judge_cwd()), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}")
    out = json.loads(r.stdout)
    if isinstance(out, dict) and isinstance(out.get("structured_output"), dict):
        return out["structured_output"]
    text = out.get("result", "") if isinstance(out, dict) else ""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in claude -p result")
    return json.loads(m.group(0))


# Regex stand-in: German + English correction cues. Low confidence, flagged source="regex".
_CORRECTION = re.compile(
    r"(?i)(?:^\s*(?:no|nope|nein|ne)\b|\bactually\b|\binstead\b|that'?s (?:wrong|not)|\bwrong\b"
    r"|why did you|why are you|don'?t do|\bundo\b|\brevert\b|you (?:broke|forgot|missed|ignored)"
    r"|\bstill (?:broken|failing|fails|not)\b|doesn'?t work|not what i|"
    r"\bfalsch\b|stattdessen|eigentlich|warum hast du|wieso hast du|das stimmt nicht|nicht so\b"
    r"|geht (?:immer )?noch nicht|funktioniert (?:immer )?noch nicht|rückgängig|du hast .{0,30}vergessen)")
_CLARIFY = re.compile(r"(?i)(?:^\s*(?:yes|ja|yep|genau|i mean|ich meine|meinte)\b|\bto clarify\b)")


def call_regex(summary: str, prompt: str) -> dict:
    m = _CORRECTION.search(prompt)
    if m:
        return {"event": "correction", "cause": "unknown", "detection": "human_review",
                "evidence": m.group(0), "confidence": 0.3, "defect_caught": True, "resolved": None}
    m = _CLARIFY.search(prompt)
    if m:
        return {"event": "clarification", "cause": "unknown", "detection": "unknown",
                "evidence": m.group(0), "confidence": 0.3, "defect_caught": False, "resolved": None}
    return {"event": "new_requirement", "cause": "unknown", "detection": "unknown",
            "evidence": prompt[: min(len(prompt), 40)], "confidence": 0.3,
            "defect_caught": False, "resolved": None}


def backend_chain() -> list[str]:
    chain = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        chain.append("api")
    if shutil.which("claude"):
        chain.append("claude-p")
    chain.append("regex")
    return chain


def label_one(c: dict, model: str, chain: list[str], client=None) -> tuple[dict, str]:
    """Label one candidate. Returns (label, cache_model). Never raises."""
    prompt, summary = c["prompt"], c["summary"]
    if not prompt.strip():
        return abstain("rule", "empty-prompt"), "rule"
    if c.get("prompt_kind") == "command" and prompt.lstrip().startswith("<"):
        return abstain("rule", "slash-command"), "rule"
    for b in chain:
        try:
            if b == "api":
                raw = call_api(summary, prompt, model, client)
            elif b == "claude-p":
                raw = call_claude_p(summary, prompt, model)
            else:
                lab = validate(call_regex(summary, prompt), prompt, "regex")
                lab["confidence"] = min(lab["confidence"], 0.3) if lab["event"] != "abstain" else 0.0
                return lab, "regex"
            lab = validate(raw, prompt, b)
            if c.get("first") and lab["event"] in ("correction", "uncertain"):
                lab["event"] = "new_requirement"   # no previous agent turn to correct
            return lab, model
        except Exception as e:  # noqa: BLE001 - fall through to the next backend
            c.setdefault("errors", []).append(f"{b}:{type(e).__name__}")
    return abstain("regex", "all-backends-failed"), "regex"


def resolve_day(day: str | None) -> str | None:
    """'today' | YYYY-MM-DD | None -> local report day (model.local_day) or None."""
    if not day:
        return None
    if day == "today":
        return local_day(datetime.now(timezone.utc))
    return day


# --- run ---------------------------------------------------------------------------------------

def _cache_get(conn, h: str) -> dict | None:
    r = conn.execute("SELECT label FROM judge_label WHERE turn_hash = ?", (h,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return None


def _cache_put(conn, h: str, model: str, label: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO judge_label(turn_hash, model, label, at) VALUES (?,?,?,?)",
                 (h, model, json.dumps(label), datetime.now(timezone.utc).isoformat()))


def _pool(items: list, fn, workers: int, timeout: float | None):
    """Run fn over items on daemon threads; return ([(item, result)], n_unfinished).
    Daemon threads (not ThreadPoolExecutor, whose workers are joined at interpreter exit) so a
    timed-out straggler never keeps the CLI alive. fn must not touch the DB."""
    import queue
    import threading
    import time
    q: queue.Queue = queue.Queue()
    for it in items:
        q.put(it)
    out: list = []
    lock = threading.Lock()

    def work():
        while True:
            try:
                it = q.get_nowait()
            except queue.Empty:
                return
            r = fn(it)
            with lock:
                out.append((it, r))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.start()
    end = None if timeout is None else time.monotonic() + timeout
    for t in threads:
        t.join(None if end is None else max(0.0, end - time.monotonic()))
    with lock:
        done = list(out)
    return done, len(items) - len(done)


def label_candidates(conn, cands: list[dict], *, model: str, chain: list[str] | None = None,
                     client=None, max_calls: int | None = None, workers: int = WORKERS,
                     timeout: float | None = None) -> dict:
    """Label candidates: cache first, then a thread pool for the uncached ones.

    max_calls: label at most this many uncached turns (in `cands` order = most recent first);
    timeout: overall seconds for the pool; unfinished turns stay unlabelled and uncached.
    Sets c['label'] on each labelled candidate. All DB access stays on this thread."""
    ensure_table(conn)
    chain = chain if chain is not None else backend_chain()
    primary = chain[0] if chain else "regex"
    cache_model = model if primary in ("api", "claude-p") else "regex"
    todo = []
    hits = 0
    for c in cands:
        c["hash"] = turn_hash(cache_model, c["prompt"], c["summary"])
        lab = _cache_get(conn, c["hash"])
        if lab is not None:
            c["label"] = lab
            hits += 1
        else:
            todo.append(c)
    skipped = 0
    if max_calls is not None and len(todo) > max_calls:
        skipped = len(todo) - max_calls
        todo = todo[:max_calls]
    done: list[tuple[dict, tuple[dict, str]]] = []
    timed_out = 0
    if todo:
        done, timed_out = _pool(todo, lambda c: label_one(c, model, chain, client), workers, timeout)
        with conn:
            for c, (lab, used_model) in done:
                c["label"] = lab
                # Cache only what the primary backend (or a deterministic rule) produced; a regex
                # fallback after an API failure is not cached, so the next run retries the API.
                if used_model in (cache_model, "rule"):
                    _cache_put(conn, c["hash"], used_model, lab)
    errs = sum(1 for c, _ in done if c.get("errors"))
    return {"n": len(cands), "cached": hits, "called": len(done), "errors": errs, "chain": chain,
            "unlabelled": skipped + timed_out, "timed_out": timed_out,
            "labelled": hits + len(done)}


def _delete_c_rows(conn, run_id: int, day: str) -> None:
    where = ("run_id = ? AND scope_type = 'day' AND scope_id = ? AND (metric LIKE 'C:%' OR metric LIKE 'C+:%'"
             " OR metric IN ('C_abstain', 'C_labelled') OR metric LIKE 'C^_source:%' ESCAPE '^')")
    conn.execute(f"DELETE FROM metric_evidence WHERE metric_value_id IN (SELECT id FROM metric_value WHERE {where})",
                 (run_id, day))
    conn.execute(f"DELETE FROM metric_value WHERE {where}", (run_id, day))


def _put(conn, run_id, metric, day, value, unit, n, evidence=()):
    cur = conn.execute("INSERT INTO metric_value(run_id, metric, definition_version, scope_type, scope_id,"
                       " value, unit, n) VALUES (?,?,?,?,?,?,?,?)",
                       (run_id, metric, DEFINITION_VERSION, "day", day, float(value), unit, n))
    for tid, note in evidence:
        conn.execute("INSERT INTO metric_evidence(metric_value_id, turn_id, tool_call_id, git_change_id, note)"
                     " VALUES (?,?,?,?,?)", (cur.lastrowid, tid, None, None, note))


def _cands_episodes(conn, cands: list[dict]) -> dict[str, list[dict]]:
    """Gated positive episodes per day over the labels carried by `cands` (best effort)."""
    by_day: dict[str, list[dict]] = {}
    for c in sorted(cands, key=lambda c: c["at"]):
        if c.get("day"):
            by_day.setdefault(c["day"], []).append(c)
    out = {}
    for day, lst in by_day.items():
        try:
            out[day] = _episodes(conn, day, [(c, c.get("label")) for c in lst])
        except Exception:  # noqa: BLE001 - episodes never break the C rows
            out[day] = []
    return out


def write_metrics(conn, run_id: int, cands: list[dict], eps_by_day: dict | None = None) -> int:
    """Per day: C rows over labelled candidates, plus C_labelled (value = labelled, n = all).
    First prompts (c["first"]) never count in C rows; they only feed C+:<episode> (gated episodes,
    evidence note = "<episode>: " + clip(reason, 60))."""
    by_day: dict[str, list[dict]] = {}
    for c in cands:
        if c.get("day"):
            by_day.setdefault(c["day"], []).append(c)
    if eps_by_day is None:
        eps_by_day = _cands_episodes(conn, cands)
    rows = 0
    with conn:
        for day, all_c in by_day.items():
            _delete_c_rows(conn, run_id, day)
            all_c = [c for c in all_c if not c.get("first")]
            eg: dict[str, list] = {}
            for e in eps_by_day.get(day, []):
                eg.setdefault(f"C+:{e['episode']}", []).append(
                    (e["turn_id"], f"{e['episode']}: " + clip(e["reason"], 60)))
            for metric, evs in sorted(eg.items()):
                _put(conn, run_id, metric, day, len(evs), "episodes", None, evs)
                rows += 1
            if not all_c:
                continue
            lst = [c for c in all_c if c.get("label")]
            _put(conn, run_id, "C_labelled", day, len(lst), "turns", len(all_c))
            rows += 1
            n = len(lst)
            groups: dict[str, list] = {}
            for c in lst:
                lab = c["label"]
                note = f"{lab['event']}/{lab['cause']}/{lab['detection']}: " + clip(lab.get("evidence"), 60)
                ev = (c["turn_id"], note)
                groups.setdefault(f"C:event:{lab['event']}", []).append(ev)
                groups.setdefault(f"C_source:{lab.get('source', 'regex')}", []).append(None)
                if lab["event"] == "abstain":
                    groups.setdefault("C_abstain", []).append(ev)
                if lab["event"] == "correction":
                    groups.setdefault(f"C:cause:{lab['cause']}", []).append(ev)
                    groups.setdefault(f"C:detection:{lab['detection']}", []).append(ev)
                if lab.get("defect_caught"):
                    groups.setdefault("C:defect_caught", []).append(ev)
                if lab.get("resolved") is True:
                    groups.setdefault("C:resolved", []).append(ev)
            for metric, evs in sorted(groups.items()):
                _put(conn, run_id, metric, day, len(evs), "turns", n, [e for e in evs if e])
                rows += 1
    return rows


REPORT_MAX_CALLS = 40
REPORT_WORKERS = 8
REPORT_TIMEOUT_S = 45.0


def run_judge(conn: sqlite3.Connection, run_id: int | None, *, limit: int | None = None,
              day: str | None = None, client=None, chain: list[str] | None = None,
              max_calls: int | None = None, workers: int = WORKERS,
              timeout: float | None = None) -> dict:
    """Judge candidate turns (of `day`, or all days) and write C metrics for `run_id`.
    `limit` caps candidates (most recent first); `max_calls` caps uncached labels this run;
    `timeout` bounds the pool (report path: REPORT_* constants).
    Returns stats: n, labelled, unlabelled, cached, called, errors, chain, dist, sources, model, metric_rows."""
    from .config import load
    model = load().judge_model
    ensure_table(conn)
    cands = candidates(conn, day=day, limit=limit, first=True)
    stats = label_candidates(conn, cands, model=model, chain=chain, client=client,
                             max_calls=max_calls, workers=workers, timeout=timeout)
    # coverage stats count follow-up turns only (first prompts feed positive episodes, not C)
    follow = [c for c in cands if not c.get("first")]
    firsts = [c for c in cands if c.get("first")]
    lab_n = sum(1 for c in follow if c.get("label"))
    stats.update(n=len(follow), labelled=lab_n, unlabelled=len(follow) - lab_n,
                 first_turns=len(firsts), first_labelled=sum(1 for c in firsts if c.get("label")))
    dist: dict[str, int] = {}
    src: dict[str, int] = {}
    for c in follow:
        if not c.get("label"):
            continue
        dist[c["label"]["event"]] = dist.get(c["label"]["event"], 0) + 1
        s = c["label"].get("source", "?")
        src[s] = src.get(s, 0) + 1
    eps_by_day = _cands_episodes(conn, cands)
    eps: dict[str, int] = {}
    for lst in eps_by_day.values():
        for e in lst:
            eps[e["episode"]] = eps.get(e["episode"], 0) + 1
    stats.update(dist=dist, sources=src, model=model, episodes=eps,
                 metric_rows=write_metrics(conn, run_id, cands, eps_by_day) if run_id is not None else 0)
    return stats


# --- action items / strengths / attribution (daily aggregation) --------------------------------

ACTION_VERSION = "actions-v2"
MAX_ACTION_ITEMS = 5
CLAUDE_MD_CHARS = 160
CLUSTER_TOOL = "record_items"
# severity weight per category: count × severity ranks the items; ties: PRIORITY order
SEVERITY = {"verification": 3, "prompt_specificity": 3, "context": 2, "task_split": 2, "review": 2,
            "tooling": 1, "other": 1}
PRIORITY = ("verification", "prompt_specificity", "context", "task_split", "review", "tooling", "other")
INTERRUPT_MIN = 2           # repeated interruptions: at least this many in a day
BLOCKED_MIN = 1.0           # blocked minutes below this are noise
LOW_BLOCKED_MIN = 5.0       # parallel work with fewer blocked minutes than this is a strength

KINDS = {  # kind -> (label field, category field)
    "action": ("user_action", "action_category"),
    "strength": ("user_strength", "strength_category"),
}

CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Max 120 chars."},
                    "category": {"type": "string", "enum": list(ACTION_CATEGORIES)},
                    "turn_ids": {"type": "array", "items": {"type": "integer"}},
                    "claude_md_line": {"type": ["string", "null"],
                                       "description": "Optional one-line rule for CLAUDE.md/AGENTS.md; else null."},
                },
                "required": ["text", "category", "turn_ids", "claude_md_line"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

_CLUSTER_RULES = f"""Merge entries that mean the same thing into one item; keep distinct ones separate. Return at most {MAX_ACTION_ITEMS} items.
Each item: category = the best-fitting category; turn_ids = every input turn id the item covers (each id in exactly one item).
Call the {CLUSTER_TOOL} tool exactly once."""

CLUSTER_SYSTEM = {
    "action": "You merge per-turn lessons for one human who works with coding agents into a short to-do list.\n"
              "Input: one line per turn: turn id, category, and the lesson (an imperative for the human).\n"
              "text = one short imperative for the human (max 120 chars, concrete, no blame on the agent); "
              "claude_md_line = an optional one-line rule for CLAUDE.md/AGENTS.md that would make the agent "
              "prompt or check for this, else null.\n" + _CLUSTER_RULES,
    "strength": "You merge per-turn strengths of one human who works with coding agents into a short list of "
                "what they did well.\nInput: one line per turn: turn id, category, and the good move.\n"
                "text = one short description of the good move (max 120 chars, concrete); claude_md_line = null.\n"
                + _CLUSTER_RULES,
}


def _day_all(conn, day: str, first: bool = False) -> list[tuple[dict, dict | None]]:
    """(candidate, cached label or None) for `day`, oldest first. Never calls a model.
    first=True includes each session's first prompt (positive episodes, strengths)."""
    from .config import load
    ensure_table(conn)
    model = load().judge_model
    out = []
    for c in sorted(candidates(conn, day=day, first=first), key=lambda c: c["at"]):
        lab = None
        for m in (model, "regex"):
            lab = _cache_get(conn, turn_hash(m, c["prompt"], c["summary"]))
            if lab is not None:
                break
        out.append((c, lab))
    return out


def _day_labels(conn, day: str, kind: str = "action", rows=None) -> list[dict]:
    """Cached labels of `day` carrying a `kind` text: [{turn_id, action, category, evidence, prompt}]."""
    field, cat_field = KINDS[kind]
    out = []
    for c, lab in (rows if rows is not None else _day_all(conn, day)):
        if not lab or not lab.get(field):
            continue
        text = lab[field]
        if quotes_prompt(text, c["prompt"]):
            continue
        cat = lab.get(cat_field)
        out.append({"turn_id": c["turn_id"], "action": clip(text, ACTION_CHARS),
                    "category": cat if cat in ACTION_CATEGORIES else "other",
                    "evidence": clip(lab.get("evidence") or "", QUOTE_MAX), "prompt": c["prompt"]})
    return out


def _run_hash(labels: list[dict], model: str, kind: str = "action") -> str:
    key = json.dumps(sorted((l["turn_id"], l["action"], l["category"], l["evidence"]) for l in labels))
    return hashlib.sha1("\x1f".join((kind, key, model, ACTION_VERSION, CLUSTER_SYSTEM[kind],
                                     json.dumps(CLUSTER_SCHEMA, sort_keys=True))).encode()).hexdigest()


def _item(text, category, turn_ids, by_id, claude_md_line=None) -> dict:
    ids = [t for t in dict.fromkeys(turn_ids) if t in by_id]
    quote = next((by_id[t]["evidence"] for t in ids if by_id[t]["evidence"]), "")
    return {"text": clip(text, ACTION_CHARS), "category": category, "count": len(ids), "turn_ids": ids,
            "example_quote": clip(quote, QUOTE_MAX), "claude_md_line": claude_md_line,
            "uncalibrated": True, "source": "judge"}


def _cluster_fallback(labels: list[dict]) -> list[dict]:
    """No API: one item per category, text = the most frequent entry in that category."""
    by_id = {l["turn_id"]: l for l in labels}
    groups: dict[str, list[dict]] = {}
    for l in labels:
        groups.setdefault(l["category"], []).append(l)
    out = []
    for cat, ls in groups.items():
        texts = [l["action"] for l in ls]
        text = max(dict.fromkeys(texts), key=lambda a: (texts.count(a), -texts.index(a)))
        out.append(_item(text, cat, [l["turn_id"] for l in ls], by_id))
    return out


def _cluster_api(labels: list[dict], model: str, kind: str = "action") -> list[dict]:
    by_id = {l["turn_id"]: l for l in labels}
    lines = "\n".join(f"turn {l['turn_id']} [{l['category']}]: {l['action']}" for l in labels)
    raw = call_api("", "", model, _api_client(), system=CLUSTER_SYSTEM[kind], schema=CLUSTER_SCHEMA,
                   tool_name=CLUSTER_TOOL, content=f"<entries>\n{lines}\n</entries>")
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("no items")
    prompts = [l["prompt"] for l in labels]
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            continue
        ids = [t for t in (it.get("turn_ids") or []) if isinstance(t, int) and t in by_id and t not in seen]
        if not ids or quotes_prompt(it["text"], prompts):
            continue
        seen.update(ids)
        cat = it.get("category") if it.get("category") in ACTION_CATEGORIES else by_id[ids[0]]["category"]
        md = it.get("claude_md_line") if kind == "action" else None
        md = clip(" ".join(md.split()), CLAUDE_MD_CHARS) if isinstance(md, str) and md.strip() else None
        if md and quotes_prompt(md, prompts):
            md = None
        out.append(_item(" ".join(it["text"].split()), cat, ids, by_id, md))
    rest = [l for l in labels if l["turn_id"] not in seen]  # entries the model dropped still count
    if rest:
        out += _cluster_fallback(rest)
    if not out:
        raise ValueError("no valid items")
    return out


def _judge_items(conn, day: str, kind: str = "action", rows=None) -> list[dict]:
    from .config import load
    labels = _day_labels(conn, day, kind, rows)
    if not labels:
        return []
    model = load().judge_model
    use_api = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("SCORER_NO_LLM") != "1" \
        and len(labels) > 1
    if not use_api:
        return _cluster_fallback(labels)
    h = _run_hash(labels, model, kind)
    r = conn.execute("SELECT items FROM judge_action WHERE day = ? AND run_hash = ?", (day, h)).fetchone()
    if r:
        try:
            return json.loads(r[0])
        except (TypeError, ValueError):
            pass
    try:
        items = _cluster_api(labels, model, kind)
    except Exception:  # noqa: BLE001 - never fail the report over clustering
        return _cluster_fallback(labels)
    with conn:
        conn.execute("INSERT OR REPLACE INTO judge_action(day, run_hash, items, at) VALUES (?,?,?,?)",
                     (day, h, json.dumps(items), datetime.now(timezone.utc).isoformat()))
    return items


def _metric_item(text, cat, turns, count, md=None, **extra) -> dict:
    d = {"text": clip(text, ACTION_CHARS), "category": cat, "count": int(count),
         "turn_ids": [t for t in dict.fromkeys(turns) if t is not None], "example_quote": "",
         "claude_md_line": md, "uncalibrated": False, "source": "metrics"}
    d.update(extra)
    return d


def _day_metrics(conn, day: str, run_id: int | None):
    from . import report
    from .db import latest_run_id
    return report.DayMetrics(conn, latest_run_id(conn) if run_id is None else run_id, day)


def _metric_items(conn, day: str, run_id: int | None) -> list[dict]:
    """Deterministic action items from stored metrics (no judge): unverified batches, blocked minutes,
    repeated interruptions. source="metrics", uncalibrated=False (facts, not judge labels)."""
    from . import metrics, report
    m = _day_metrics(conn, day, run_id)
    if not m:
        return []
    out = []
    if "V" in m.rows:
        vn = m.n("V") or 0
        unverified = max(0, vn - int(m.v("V_tier:verified_pass", 0) + m.v("V_tier:verified_fail", 0)))
        if unverified:
            batches = metrics.verification_batches(conn, day)
            unv = report._unverified_now(conn, day, batches)
            cmd = next((u["command"] for u in unv if u["command"]), None)
            check = f"`{cmd}`" if cmd else "a test/build run"
            out.append(_metric_item(
                f"Ask for {check} before accepting edits ({unverified} of {vn} edit batches unverified)",
                "verification", report._evidence_turns(m, "V", batches), unverified,
                f"After editing code, run {check} and show the result before you finish.", command=cmd))
    bmin = m.v("B_min", 0.0) or 0.0
    if bmin >= BLOCKED_MIN:
        ev = m.evidence("B_min")
        out.append(_metric_item(
            f"Check back on parallel sessions sooner or turn on notifications ({bmin:.0f} min blocked)",
            "tooling", [e["turn_id"] for e in ev], len(ev) or (m.n("B_min") or 1), minutes=round(bmin, 1)))
    ints = int(m.v("interrupts", 0) or 0)
    if ints >= INTERRUPT_MIN:
        thrown = (m.v("thrown_s", 0.0) or 0.0) / 60
        out.append(_metric_item(
            f"Put scope and a \"done when …\" line in the prompt instead of interrupting "
            f"({ints}×, {thrown:.0f} min thrown away)", "prompt_specificity",
            [e["turn_id"] for e in m.evidence("interrupts")], ints, minutes=round(thrown, 1)))
    return out


def _rank_key(it: dict):
    sev = SEVERITY.get(it["category"], 1)
    pri = PRIORITY.index(it["category"]) if it["category"] in PRIORITY else 99
    return (-(it["count"] * sev), -sev, pri, it["source"] != "judge")


def action_items(conn: sqlite3.Connection, day: str, *, run_id: int | None = None) -> list[dict]:
    """Action items for the human on `day` ('today' | YYYY-MM-DD), ranked by count × severity.

    Judge items (source="judge", uncalibrated=True): that day's cached per-turn user_action labels,
    merged into <= MAX_ACTION_ITEMS by one Haiku call (cached in judge_action by a hash of the input
    labels), or grouped by action_category without an API key / with SCORER_NO_LLM=1.
    Metric items (source="metrics", uncalibrated=False): unverified batches, blocked minutes,
    repeated interruptions, from the stored metrics of `run_id` (default: latest run).
    Item: {text, category, count, turn_ids, example_quote (<= 80), claude_md_line, uncalibrated, source}.
    Never calls the per-turn judge; run `scorer judge` first."""
    d = resolve_day(day) or day
    items = sorted(_judge_items(conn, d, "action"), key=_rank_key)[:MAX_ACTION_ITEMS]
    try:
        items += _metric_items(conn, d, run_id)
    except Exception:  # noqa: BLE001 - metric items are best effort
        pass
    return sorted(items, key=_rank_key)


# --- strengths ---------------------------------------------------------------------------------

def _quick_corrections(rows) -> list[int]:
    """Correction turns whose next judged turn in the same session is not another correction:
    the defect was fixed within one turn."""
    by_sess: dict[str, list] = {}
    for c, lab in rows:
        by_sess.setdefault(c["session_id"], []).append((c, lab))
    out = []
    for lst in by_sess.values():
        for i, (c, lab) in enumerate(lst):
            if not lab or lab.get("event") != "correction":
                continue
            nxt = lst[i + 1][1] if i + 1 < len(lst) else None
            if nxt and nxt.get("event") not in ("correction", "uncertain", "abstain"):
                out.append(c["turn_id"])
    return out


def _metric_strengths(conn, day: str, run_id: int | None, rows) -> list[dict]:
    from . import metrics
    m = _day_metrics(conn, day, run_id)
    out = []
    batches = metrics.verification_batches(conn, day)
    asked = [b for b in batches if b.tier.startswith("verified") and b.verifier_initiator == "human"]
    if asked:
        out.append(_metric_item(f"Asked for verification yourself ({len(asked)} edit batch(es))", "verification",
                                [b.turn_id for b in asked], len(asked)))
    per: dict[str, list] = {}
    for b in batches:
        per.setdefault(b.session_id, []).append(b)
    full = [sid for sid, bs in per.items() if bs and all(b.tier.startswith("verified") for b in bs)]
    if full:
        out.append(_metric_item(f"Every edit batch verified in {len(full)} session(s)", "verification",
                                [per[s][-1].turn_id for s in full], len(full)))
    caught = [c["turn_id"] for c, lab in rows if lab and lab.get("event") == "correction"
              and lab.get("detection") == "human_review"]
    if not caught and m:
        caught = [e["turn_id"] for e in m.evidence("C:detection:human_review")]
    if caught:
        out.append(_metric_item(f"Caught {len(caught)} defect(s) in your own review", "review", caught, len(caught)))
    quick = _quick_corrections(rows)
    if quick:
        out.append(_metric_item(f"Corrected the agent within one turn ({len(quick)}×)", "review", quick, len(quick)))
    if m:
        sessions, ov, bmin = int(m.v("sessions", 0) or 0), m.v("overlap_s", 0.0) or 0.0, m.v("B_min", 0.0) or 0.0
        if sessions >= 2 and ov >= 60 and bmin < LOW_BLOCKED_MIN:
            out.append(_metric_item(f"Ran {sessions} sessions in parallel ({ov / 60:.0f} min overlap) with only "
                                    f"{bmin:.0f} min blocked", "task_split", [], sessions,
                                    minutes=round(ov / 60, 1)))
    human = [x for x in metrics.tooling(conn, day) if x["initiator"] == "human"]
    if human:
        names = list(dict.fromkeys(x["name"] for x in human))
        out.append(_metric_item(f"Invoked skills/commands deliberately ({len(human)}×: "
                                + ", ".join(clip(n, 40) for n in names[:3]) + ")", "tooling",
                                [x["turn_id"] for x in human], len(human)))
    return out


def strengths(conn: sqlite3.Connection, day: str, *, run_id: int | None = None) -> list[dict]:
    """What the human did well on `day`; same shape and ranking as action_items.
    Judge items from cached per-turn user_strength labels (<= 5, clustered like action items);
    metric items: verifications you asked for, sessions with every batch verified, defects caught in
    human review, corrections fixed within one turn, low-blocked parallel sessions, human tooling."""
    d = resolve_day(day) or day
    rows = _day_all(conn, d, first=True)
    items = sorted(_judge_items(conn, d, "strength", rows), key=_rank_key)[:MAX_ACTION_ITEMS]
    for it in items:
        it.pop("claude_md_line", None)
    try:
        ms = _metric_strengths(conn, d, run_id, rows)
        for it in ms:
            it.pop("claude_md_line", None)
        items += ms
    except Exception:  # noqa: BLE001
        pass
    for it in items:
        it["episode"] = None
    try:
        eps = _episode_items(_episodes(conn, d, rows))
    except Exception:  # noqa: BLE001 - episodes are best effort
        eps = []
    return eps + sorted(items, key=_rank_key)


# --- positive episodes (C+) --------------------------------------------------------------------

EPISODE_TITLE = {"verification_catch": "Verification catch", "good_spec": "Good spec",
                 "scoped_turn": "Scoped turn", "early_redirect": "Early redirect",
                 "knew_when_not_to_delegate": "Knew when not to delegate"}
EPISODE_CATEGORY = {"verification_catch": "verification", "good_spec": "prompt_specificity",
                    "scoped_turn": "task_split", "early_redirect": "review",
                    "knew_when_not_to_delegate": "other"}
SCOPED_MAX_FILES = 3        # scoped_turn: at most this many files ...
SCOPED_MAX_LINES = 80       # ... and this many lines (added + removed) edited in the turn
EARLY_MAX_POS = 2           # early_redirect: prompt at session position 1 or 2 (after agent turn 1 or 2)
STRUGGLE_MIN = 2            # knew_when_not_to_delegate: failed checks or corrections on the file
CHECK_INTENTS = ("test", "build")


def _same_file(a: str | None, b: str | None) -> bool:
    """Path match across absolute / repo-relative / clip()-truncated ("…") spellings."""
    if not a or not b:
        return False
    trunc = a.endswith("…")
    a = a.rstrip("…").rstrip("/")
    b = b.rstrip("/")
    if not a:
        return False
    if trunc:
        return b.startswith(a) or ("/" + a) in ("/" + b)
    return a == b or b.endswith("/" + a) or a.endswith("/" + b)


def _turn_checks(conn, turn_ids) -> dict[int, list[dict]]:
    """turn_id -> test/build tool calls [{initiator, is_error}] (bash_intent, else classified)."""
    from . import classify
    ids = [t for t in dict.fromkeys(turn_ids) if t is not None]
    out: dict[int, list[dict]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in _rows(conn, "SELECT turn_id, name, kind, bash_intent, initiator, is_error, args_summary"
                             f" FROM tool_call WHERE turn_id IN ({','.join('?' * len(part))})"
                             " ORDER BY started_at, id", part):
            intent = r["bash_intent"]
            if not intent and (r["kind"] == "bash" or r["name"] in ("Bash", "shell", "exec_command")):
                intent = classify.bash_intent(r["args_summary"] or "")
            if intent in CHECK_INTENTS:
                out.setdefault(r["turn_id"], []).append({"initiator": r["initiator"],
                                                         "is_error": bool(r["is_error"])})
    return out


def _turn_edits(conn, turn_ids) -> dict[int, list[dict]]:
    """turn_id -> file_edit rows [{path, lines}] (lines None when unknown: NULL, or 0/0 as stored for
    Bash-synthesized edits, whose line counts are never measured)."""
    ids = [t for t in dict.fromkeys(turn_ids) if t is not None]
    out: dict[int, list[dict]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in _rows(conn, "SELECT tc.turn_id, fe.path, fe.lines_added, fe.lines_removed FROM file_edit fe"
                             " JOIN tool_call tc ON tc.id = fe.tool_call_id"
                             f" WHERE tc.turn_id IN ({','.join('?' * len(part))})", part):
            la, lr = r["lines_added"], r["lines_removed"]
            out.setdefault(r["turn_id"], []).append(
                {"path": r["path"], "lines": (la or 0) + (lr or 0) or None})
    return out


def _ep(episode: str, c: dict, lab: dict | None, reason: str) -> dict:
    """One episode. Judge-backed (label names this episode, has a reason and a grounded quote) ->
    source "judge", uncalibrated; else the deterministic reason, evidence None, source "metrics"."""
    judged = bool(lab and lab.get("positive_episode") == episode and lab.get("episode_reason")
                  and (lab.get("evidence") or "").strip() and lab["evidence"] in c.get("prompt", ""))
    return {"episode": episode, "turn_id": c["turn_id"], "session_id": c["session_id"],
            "reason": clip(lab["episode_reason"] if judged else reason, REASON_CHARS),
            "evidence_quote": clip(lab["evidence"], QUOTE_MAX) if judged else None,
            "source": "judge" if judged else "metrics", "uncalibrated": judged}


def _judged(e: dict) -> list[dict]:
    """Mixed-rule episodes need the judge's why + grounded quote; without them they do not count."""
    return [e] if e["source"] == "judge" else []


def _episodes(conn, day: str, rows) -> list[dict]:
    """Gated positive episodes for `day` from (candidate, label) rows (oldest first).

    The judge's positive_episode is a proposal; the deterministic gate decides:
      good_spec          first prompt of the session (judge) AND no later correction-labelled turn
                         in that session.
      verification_catch judge label on turn T AND T ran a test/build that failed (is_error) and was
                         human-initiated, or failed and the next turn of the session has a passing check.
      early_redirect     judge label AND session position 1..EARLY_MAX_POS AND (event correction OR the
                         previous agent turn was interrupted).
      scoped_turn        gate alone: <= SCOPED_MAX_FILES files, <= SCOPED_MAX_LINES lines edited in T,
                         and every edit batch of T verified_pass.
      knew_when_not_to_delegate  gate alone: a git-snapshot hand edit (H repair/own) of a file that had
                         >= STRUGGLE_MIN failed verifications or >= STRUGGLE_MIN correction turns
                         touching it earlier in the session.
    Gate-alone episodes carry the judge's reason + quote when the label agrees, else evidence None and
    source "metrics". Sorted by EPISODES priority, then time."""
    from . import metrics
    rows = [(c, lab) for c, lab in rows if c.get("turn_id") is not None]
    if not rows:
        return []
    by_tid = {c["turn_id"]: (c, lab) for c, lab in rows}
    sess: dict[str, list] = {}
    for c, lab in rows:
        sess.setdefault(c["session_id"], []).append((c, lab))
    nxt: dict[int, int] = {}
    for lst in sess.values():
        for (a, _), (b, _) in zip(lst, lst[1:]):
            nxt[a["turn_id"]] = b["turn_id"]
    tids = list(by_tid) + [c.get("prev_id") for c, _ in rows]
    checks = _turn_checks(conn, tids)
    edits = _turn_edits(conn, tids)
    batches = metrics.verification_batches(conn, day)
    bt: dict[int, list] = {}
    for b in batches:
        bt.setdefault(b.turn_id, []).append(b)
    out: list[dict] = []

    for c, lab in rows:
        tid, ep = c["turn_id"], (lab or {}).get("positive_episode")
        if ep == "good_spec" and c.get("first"):
            later = [lb for cc, lb in sess[c["session_id"]] if cc["at"] > c["at"]]
            if not any(lb and lb.get("event") == "correction" for lb in later):
                out += _judged(_ep("good_spec", c, lab, ""))
        elif ep == "verification_catch":
            ch = checks.get(tid, [])
            failed = [x for x in ch if x["is_error"]]
            human_fail = any(x["initiator"] == "human" for x in failed)
            n = nxt.get(tid)
            fixed = bool(failed) and n is not None and (
                any(b.tier == "verified_pass" for b in bt.get(n, []))
                or (checks.get(n) and not checks[n][-1]["is_error"]))
            if human_fail or fixed:
                out += _judged(_ep("verification_catch", c, lab, ""))
        elif ep == "early_redirect":
            pos = c.get("pos")
            if pos is not None and 1 <= pos <= EARLY_MAX_POS and (
                    lab.get("event") == "correction" or c.get("prev_interrupted")):
                out += _judged(_ep("early_redirect", c, lab, ""))
        # scoped_turn: deterministic gate, judge only adds the why
        ed = edits.get(tid, [])
        bs = bt.get(tid, [])
        if ed and bs and all(b.tier == "verified_pass" for b in bs):
            files = {e["path"] for e in ed if e["path"]}
            lines = [e["lines"] for e in ed]
            if files and len(files) <= SCOPED_MAX_FILES and None not in lines and sum(lines) <= SCOPED_MAX_LINES:
                out.append(_ep("scoped_turn", c, lab, f"Small, verified change: {len(files)} file(s), "
                                                      f"{sum(lines)} line(s), checks passed"))

    # knew_when_not_to_delegate: hand edit on a file the agent was struggling with
    try:
        hev = metrics.hand_edits(conn, day)["evidence"]
    except Exception:  # noqa: BLE001
        hev = []
    seen: set = set()
    for e in hev:
        note = e.get("note") or ""
        kind, _, path = note.partition(": ")
        if kind not in ("repair", "own") or not path or e.get("turn_id") is None:
            continue
        h = conn.execute("SELECT session_id, prompt_at FROM turn WHERE id = ?", (e["turn_id"],)).fetchone()
        if not h or e["turn_id"] in seen:
            continue
        sid, h_at = h[0], h[1] or ""
        fails = [b for b in batches if b.session_id == sid and b.tier == "verified_fail"
                 and any(_same_file(path, p) for p in b.paths)
                 and (by_tid.get(b.turn_id, ({"at": ""},))[0]["at"] or "") <= h_at]
        corr = [c for c, lab in sess.get(sid, []) if lab and lab.get("event") == "correction"
                and c["at"] <= h_at
                and any(_same_file(path, x["path"]) for t in (c["turn_id"], c.get("prev_id"))
                        for x in edits.get(t, []))]
        n_f, n_c = len({b.turn_id for b in fails}), len(corr)
        if max(n_f, n_c) < STRUGGLE_MIN:
            continue
        seen.add(e["turn_id"])
        hc, hlab = by_tid.get(e["turn_id"], (None, None))
        n = nxt.get(e["turn_id"])
        if n is not None and not (hlab and hlab.get("positive_episode") == "knew_when_not_to_delegate"):
            hc2, hlab2 = by_tid[n]
            if hlab2 and hlab2.get("positive_episode") == "knew_when_not_to_delegate":
                hc, hlab = hc2, hlab2
        if hc is None:
            hc = {"turn_id": e["turn_id"], "session_id": sid, "prompt": ""}
        what = (f"{n_f} failed check(s)" if n_f >= STRUGGLE_MIN else f"{n_c} correction(s)")
        name = clip(path.rstrip("…").rstrip("/").rsplit("/", 1)[-1] or path, 40)
        out.append(_ep("knew_when_not_to_delegate", hc, hlab,
                       f"Fixed {name} by hand after {what} on it"))

    order = {c["turn_id"]: i for i, (c, _) in enumerate(rows)}
    out.sort(key=lambda x: (EPISODES.index(x["episode"]), order.get(x["turn_id"], 1 << 30)))
    return out


def episodes(conn: sqlite3.Connection, day: str) -> list[dict]:
    """Positive episodes of `day` ('today' | YYYY-MM-DD) from cached judge labels + deterministic gates.
    Item: {episode, turn_id, session_id, reason (<= 120), evidence_quote (<= 80 or None), source
    ("judge" | "metrics"), uncalibrated}. Never calls a model; run `scorer judge` first."""
    d = resolve_day(day) or day
    return _episodes(conn, d, _day_all(conn, d, first=True))


def pick_episode(eps: list[dict]) -> dict | None:
    """The strongest episode by EPISODES priority (judge-backed first, then earliest), plus every
    turn id with the same label: {episode, title, reason, evidence_quote, source, uncalibrated,
    turn_id, turn_ids, count}. None when `eps` is empty."""
    if not eps:
        return None
    best = min(eps, key=lambda e: (EPISODES.index(e["episode"]) if e["episode"] in EPISODES else 99,
                                   e.get("source") != "judge"))
    same = [e["turn_id"] for e in eps if e["episode"] == best["episode"]]
    return dict(best, title=EPISODE_TITLE.get(best["episode"], best["episode"]),
                turn_ids=list(dict.fromkeys([best["turn_id"]] + same)), count=len(same))


def _episode_items(eps: list[dict]) -> list[dict]:
    """Strength items for episodes, one per label in priority order (episode = the label)."""
    out = []
    for ep in EPISODES:
        lst = [e for e in eps if e["episode"] == ep]
        if not lst:
            continue
        best = pick_episode(lst)
        quote = next((e["evidence_quote"] for e in lst if e.get("evidence_quote")), "")
        judged = any(e["source"] == "judge" for e in lst)
        out.append({"text": clip(f"{EPISODE_TITLE[ep]}: {best['reason']}", ACTION_CHARS),
                    "category": EPISODE_CATEGORY[ep], "count": len(lst), "turn_ids": best["turn_ids"],
                    "example_quote": clip(quote, QUOTE_MAX), "uncalibrated": judged,
                    "source": "judge" if judged else "metrics", "episode": ep})
    return out


# --- attribution: you vs agent -----------------------------------------------------------------

def _bucket(names) -> dict:
    return {n: {"count": 0, "turn_ids": []} for n in names}


def _add(b: dict, key: str, tid) -> None:
    b[key]["count"] += 1
    if tid is not None and tid not in b[key]["turn_ids"]:
        b[key]["turn_ids"].append(tid)


def attribution(conn: sqlite3.Connection, day: str) -> dict:
    """Who owns today's outcomes. corrections (cached judge labels): user_preventable (cause
    info_missing / requirement_changed, or preventable_by_user) | agent_error (instruction_violated,
    not preventable) | environment (env_failure) | unclear. verification (edit batches):
    asked_by_you (verifier initiator human: your prompt asked for the check; same numerator as metrics
    V_human, so asked_by_you / (asked_by_you + agent_initiative) == V_human) | agent_initiative (every
    other verified batch, incl. initiator default = a slash command / hooks / CLAUDE.md, as in V_human)
    | none (inspected or unverified). tooling: you | agent | default (events by initiator)."""
    from . import metrics
    d = resolve_day(day) or day
    corr = _bucket(("user_preventable", "agent_error", "environment", "unclear"))
    for c, lab in _day_all(conn, d):
        if not lab or lab.get("event") != "correction":
            continue
        cause, prev = lab.get("cause"), lab.get("preventable_by_user") is True
        if prev or cause in ("info_missing", "requirement_changed"):
            k = "user_preventable"
        elif cause == "instruction_violated":
            k = "agent_error"
        elif cause == "env_failure":
            k = "environment"
        else:
            k = "unclear"
        _add(corr, k, c["turn_id"])
    ver = _bucket(("asked_by_you", "agent_initiative", "none"))
    for b in metrics.verification_batches(conn, d):
        if b.tier not in ("verified_pass", "verified_fail"):
            _add(ver, "none", b.turn_id)
        elif b.verifier_initiator == "human":
            _add(ver, "asked_by_you", b.turn_id)
        else:
            _add(ver, "agent_initiative", b.turn_id)
    tool = _bucket(("you", "agent", "default"))
    for x in metrics.tooling(conn, d):
        k = {"human": "you", "agent": "agent", "default": "default"}.get(x["initiator"])
        if k:
            _add(tool, k, x["turn_id"])
    return {"day": d, "corrections": corr, "verification": ver, "tooling": tool}
