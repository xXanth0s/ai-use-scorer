"""`scorer report` (spec §2): markdown daily report in the fixed 8-line structure.

  render(conn, *, day="today"|"YYYY-MM-DD"|None, days=N|None) -> str

Data layers
  * Numbers (lines 1, 7, 8 and the --days table) come from `metric_value` rows of the latest
    ingest run (db.latest_run_id), scope_type "day". Evidence turn ids come from `metric_evidence`.
  * Things that are not stored as metrics (window times, unverified state, best session) come from
    the read-only helpers in `metrics` (verification_batches, blocked + gaps, hand_edits).
  * No run / no metric rows for the day -> a short "no data for <day>" report.

Deterministic rules (documented here, tested in tests/test_report.py)
  1 Verdict: four candidate costs, all in minutes; the largest wins, ties broken in this order:
      blocked      = B_min
      interrupted  = thrown_s / 60                     (agent time thrown away by interruptions)
      unverified   = (V.n - verified) * UNVERIFIED_FIX_MIN   (V_tier:none + V_tier:inspected batches)
      hand_repair  = (H_repair if present else H) * HAND_REPAIR_MIN
    All zero -> "nothing stood out".
  2 Tomorrow: one fixed change per verdict category; evidence = turn ids of that metric's evidence.
  3 Corrections: judge.py rows C:cause:<cause> (fallback "C" / "C:<cause>"); top two evidence rows
    of the largest causes, with a fixed per-cause "prompt that would have prevented it" (_PREVENT).
    No C rows at all -> "judge pending"; C rows but no causes -> "none".
  4 Unverified now: per session, the last edit batch of the day whose tier is not verified_*;
    files as basenames; the command comes from the nearest manifest dir that exists on disk
    (pyproject -> `uv run pytest -q` / `pytest -q`, package.json -> pnpm|yarn|npm test, go.mod,
    Cargo.toml). Plus hand-edit evidence (H).
  5 Worst attention window: blocked gaps (metrics.blocked, joined to metrics.gaps for times) are
    clustered when less than WINDOW_JOIN_S apart; the cluster with the most blocked seconds wins.
  6 Pattern to repeat: the session with the best verified share (>= 1 verified batch; ties: more
    batches); else the most-used human-initiated tooling; else none.
  7 V, H, B_min, C (+ C by cause) today vs the median of the 7 report days before the focus day
    (only days that have the metric). Direction: V up = better; H, B_min, C down = better.
  8 Diagnostics from stored metrics; judge doubt from C_abstain, C:event:uncertain, C_source:regex.

Narrative: if scorer.agent imports, has_key() and SCORER_NO_LLM != "1", agent.narrative(conn, day,
facts) goes above the 8 lines under "## Narrative"; any failure -> deterministic text only.

Privacy: every human string (prompt, command, path) goes through model.clip (<= 80 chars).
"""
from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import metrics
from .db import latest_run_id
from .model import clip, local_day, local_tz

UNVERIFIED_FIX_MIN = 5.0     # typical cost of finding/fixing a defect shipped from an unverified batch
HAND_REPAIR_MIN = 3.0        # typical cost of one silent hand repair
WINDOW_JOIN_S = 10 * 60      # blocked gaps closer than this form one attention window
BASELINE_DAYS = 7
MAX_EVIDENCE = 5

SCORED = (("V", "V verified share", True), ("H", "H hand-edits", False),
          ("B_min", "B blocked min", False), ("C", "C corrections", False))


# --- small helpers -----------------------------------------------------------------------------

def _today() -> str:
    return local_day(datetime.now(timezone.utc))


def _resolve_day(day: str | None) -> str:
    if not day or day == "today":
        return _today()
    if day == "yesterday":
        return _shift(_today(), -1)
    datetime.strptime(day, "%Y-%m-%d")  # validate
    return day


def _shift(day: str, n: int) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def _turn(tid) -> str:
    return f"turn:{tid}"


def _turns_ref(ids, n=MAX_EVIDENCE) -> str:
    seen: list = []
    for i in ids:
        if i is not None and i not in seen:
            seen.append(i)
    if not seen:
        return "no turn evidence"
    more = f" (+{len(seen) - n} more)" if len(seen) > n else ""
    return ", ".join(_turn(i) for i in seen[:n]) + more


def _hm(d: datetime | None) -> str:
    return d.astimezone(local_tz()).strftime("%H:%M") if d else "?"


def _fmt_num(v, unit: str | None = None) -> str:
    if v is None:
        return "n/a"
    if unit == "share":
        return f"{100 * v:.0f}%"
    if unit == "min":
        return f"{v:.1f}"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _tok(n: float | None) -> str:
    n = n or 0
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def _model_name(m: str) -> str:
    return clip(m[7:] if m.startswith("claude-") else m, 28)


# --- metric loading ----------------------------------------------------------------------------

class DayMetrics:
    """metric_value rows of one run + one day scope, with lazy evidence lookup."""

    def __init__(self, conn: sqlite3.Connection, run_id: int | None, day: str):
        self.conn, self.run_id, self.day = conn, run_id, day
        self.rows: dict[str, dict] = {}
        if run_id is None:
            return
        for r in conn.execute("SELECT id, metric, value, unit, n FROM metric_value"
                              " WHERE run_id=? AND scope_type='day' AND scope_id=?", (run_id, day)):
            self.rows[r[1]] = {"id": r[0], "value": r[2], "unit": r[3], "n": r[4]}

    def __bool__(self) -> bool:
        return bool(self.rows)

    def v(self, metric: str, default=None):
        r = self.rows.get(metric)
        return r["value"] if r and r["value"] is not None else default

    def unit(self, metric: str) -> str | None:
        r = self.rows.get(metric)
        return r["unit"] if r else None

    def n(self, metric: str) -> int | None:
        r = self.rows.get(metric)
        return r["n"] if r else None

    def prefixed(self, prefix: str) -> dict[str, float]:
        return {k[len(prefix):]: r["value"] for k, r in self.rows.items() if k.startswith(prefix)}

    def evidence(self, metric: str) -> list[dict]:
        r = self.rows.get(metric)
        if not r:
            return []
        cur = self.conn.execute("SELECT turn_id, tool_call_id, note FROM metric_evidence"
                                " WHERE metric_value_id=? ORDER BY rowid", (r["id"],))
        return [{"turn_id": a, "tool_call_id": b, "note": clip(c, 80)} for a, b, c in cur.fetchall()]

    def judged(self) -> bool:
        """True once the judge wrote any C row for this day (then 0 corrections is a real 0)."""
        return any(k == "C" or k.startswith(("C:", "C_")) for k in self.rows)

    def c_cause_metrics(self) -> dict[str, str]:
        """cause -> metric id. judge.py writes C:cause:<cause> (corrections only); older/other
        writers may use C:<cause>. C:event:*, C:detection:*, C:defect_caught, C:resolved are not causes."""
        out = {k[len("C:cause:"):]: k for k in self.rows if k.startswith("C:cause:")}
        if out:
            return out
        skip = ("C:event:", "C:detection:", "C:defect_caught", "C:resolved")
        return {k[2:]: k for k in self.rows if k.startswith("C:") and not k.startswith(skip)}

    def c_causes(self) -> dict[str, float]:
        return {c: (self.v(k) or 0) for c, k in self.c_cause_metrics().items()}

    def c_total(self) -> float | None:
        if "C" in self.rows:
            return self.v("C")
        cs = self.c_causes()
        if cs:
            return sum(cs.values())
        return 0.0 if self.judged() else None

    def scored(self, key: str):
        return self.c_total() if key == "C" else self.v(key)


# --- the facts ---------------------------------------------------------------------------------

def _verdict(m: DayMetrics) -> dict:
    vn = m.n("V") or 0
    verified = m.v("V_tier:verified_pass", 0) + m.v("V_tier:verified_fail", 0)
    unverified = max(0, vn - verified) if "V" in m.rows else 0
    repairs = m.v("H_repair") if "H_repair" in m.rows else m.v("H", 0)
    cands = [
        ("blocked", m.v("B_min", 0.0), "B_min",
         f"{m.v('B_min', 0.0):.0f} min blocked: agents waited on you while you were elsewhere"),
        ("interrupted", m.v("thrown_s", 0.0) / 60.0, "interrupts",
         f"{m.v('thrown_s', 0.0) / 60:.0f} min of agent work thrown away by "
         f"{m.v('interrupts', 0):.0f} interruption(s)"),
        ("unverified", unverified * UNVERIFIED_FIX_MIN, "V",
         f"{unverified:.0f} of {vn} edit batches never verified "
         f"(≈ {unverified * UNVERIFIED_FIX_MIN:.0f} min at {UNVERIFIED_FIX_MIN:.0f} min/fix)"),
        ("hand_repair", (repairs or 0) * HAND_REPAIR_MIN, "H",
         f"{repairs or 0:.0f} hand edit(s) of agent output "
         f"(≈ {(repairs or 0) * HAND_REPAIR_MIN:.0f} min at {HAND_REPAIR_MIN:.0f} min each)"),
    ]
    best = max(cands, key=lambda c: c[1])  # max keeps the first on ties -> documented order
    if best[1] <= 0:
        return {"category": None, "minutes": 0.0, "text": "nothing stood out — no blocked time, interruptions,"
                " unverified batches or hand repairs", "metric": None,
                "candidates": {c[0]: round(c[1], 1) for c in cands}}
    return {"category": best[0], "minutes": round(best[1], 1), "text": best[3], "metric": best[2],
            "candidates": {c[0]: round(c[1], 1) for c in cands}}


_CHANGE = {
    "blocked": "Before switching to another session, leave the current agent a prompt that runs to a "
               "checkpoint (tests green, or a written plan) — don't let it idle waiting on you.",
    "interrupted": "Write the stop condition into the prompt (scope + \"done when …\") so you steer up front "
                   "instead of interrupting mid-run.",
    "unverified": "End every edit prompt with the check to run (e.g. `{cmd}`) and don't accept a batch "
                  "until it ran.",
    "hand_repair": "When you fix agent output by hand, tell the agent what was wrong in the next prompt "
                   "(or in CLAUDE.md) instead of repairing silently.",
}


def _evidence_turns(m: DayMetrics, metric: str | None, batches=None) -> list:
    if not metric:
        return []
    if metric == "V" and batches is not None:
        return [b.turn_id for b in batches if not b.tier.startswith("verified")]
    ev = m.evidence(metric)
    if metric == "V":
        ev = [e for e in ev if not (e["note"] or "").startswith("verified")]
    return [e["turn_id"] for e in ev]


def _check_command(d: str | None) -> str | None:
    if not d or not os.path.isdir(d):
        return None
    has = lambda f: os.path.exists(os.path.join(d, f))  # noqa: E731
    if has("pyproject.toml"):
        return "uv run pytest -q" if has("uv.lock") or _up_has(d, "uv.lock") else "pytest -q"
    if has("package.json"):
        if has("pnpm-lock.yaml") or _up_has(d, "pnpm-lock.yaml"):
            return "pnpm test"
        if has("yarn.lock") or _up_has(d, "yarn.lock"):
            return "yarn test"
        return "npm test"
    if has("go.mod"):
        return "go test ./..."
    if has("Cargo.toml"):
        return "cargo test"
    return None


def _up_has(d: str, f: str, levels: int = 3) -> bool:
    for _ in range(levels):
        d = os.path.dirname(d.rstrip("/"))
        if not d or d == "/":
            return False
        if os.path.exists(os.path.join(d, f)):
            return True
    return False


def _manifest_for(conn, batch, cwd: str | None) -> str | None:
    if batch.edit_call_ids:
        q = ",".join("?" * len(batch.edit_call_ids))
        for (md,) in conn.execute(f"SELECT DISTINCT manifest_dir FROM file_edit WHERE tool_call_id IN ({q})"
                                  " AND manifest_dir IS NOT NULL", batch.edit_call_ids):
            if os.path.isdir(md):
                return md
    try:
        from . import classify
        for p in batch.paths:
            md = classify.manifest_dir(p if os.path.isabs(p) or not cwd else os.path.join(cwd, p))
            if md:
                return md
        if cwd:
            md = classify.manifest_dir(os.path.join(cwd, "x"))
            if md:
                return md
    except Exception:
        pass
    return None


def _unverified_now(conn, day: str, batches) -> list[dict]:
    last: dict[str, object] = {}
    for b in batches:
        if b.session_id not in last or b.order > last[b.session_id].order:
            last[b.session_id] = b
    cwd = {r[0]: r[1] for r in conn.execute("SELECT id, cwd FROM session")}
    out = []
    for sid, b in sorted(last.items(), key=lambda kv: kv[1].order):
        if b.tier.startswith("verified"):
            continue
        md = _manifest_for(conn, b, cwd.get(sid))
        out.append({"session_id": sid, "turn_id": b.turn_id, "tier": b.tier,
                    "files": sorted({clip(os.path.basename(p.rstrip("/")) or p, 40) for p in b.paths}),
                    "manifest_dir": clip(md, 70) if md else None, "command": _check_command(md)})
    return out


def _worst_window(conn, day: str) -> dict | None:
    bl = metrics.blocked(conn, day)
    if not bl:
        return None
    gmap = {g.turn_id: g for g in metrics.gaps(conn, day)}
    items = []
    for b in bl:
        g = gmap.get(b["turn_id"])
        if g:
            items.append((g.prev_end, g.next_start, b))
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    windows: list[dict] = []
    for s, e, b in items:
        w = windows[-1] if windows else None
        if w and (s - w["end"]).total_seconds() <= WINDOW_JOIN_S:
            w["end"] = max(w["end"], e)
            w["blocked_s"] += b["blocked_s"]
            w["turns"].append(b["turn_id"])
            w["sessions"].add(b["session_id"])
            w["attended"] += int(b["attended"])
        else:
            windows.append({"start": s, "end": e, "blocked_s": b["blocked_s"], "turns": [b["turn_id"]],
                            "sessions": {b["session_id"]}, "attended": int(b["attended"])})
    w = max(windows, key=lambda x: x["blocked_s"])
    return {"start": _hm(w["start"]), "end": _hm(w["end"]), "minutes": round(w["blocked_s"] / 60, 1),
            "turns": w["turns"], "sessions": len(w["sessions"]), "attended": w["attended"],
            "windows": len(windows)}


def _pattern(conn, day: str, batches) -> dict | None:
    per: dict[str, list] = {}
    for b in batches:
        per.setdefault(b.session_id, []).append(b)
    best = None
    for sid, bs in per.items():
        ver = [b for b in bs if b.tier.startswith("verified")]
        if not ver:
            continue
        key = (len(ver) / len(bs), len(bs))
        if best is None or key > best[0]:
            best = (key, sid, bs, ver)
    if best:
        (share, n), sid, bs, ver = best
        cmd = None
        vid = ver[0].verifier_call_id
        if vid is not None:
            r = conn.execute("SELECT args_summary FROM tool_call WHERE id=?", (vid,)).fetchone()
            cmd = clip(r[0], 60) if r and r[0] else None
        human = sum(1 for b in ver if b.verifier_initiator == "human")
        return {"kind": "verified_session", "session": (sid or "?")[:8], "verified": len(ver), "batches": n,
                "share": share, "command": cmd, "human_initiated": human,
                "turns": [b.turn_id for b in ver]}
    tl = [x for x in metrics.tooling(conn, day) if x["initiator"] == "human"]
    if tl:
        cnt: dict[tuple, list] = {}
        for x in tl:
            cnt.setdefault((x["category"], x["name"]), []).append(x["turn_id"])
        (cat, name), turns = max(cnt.items(), key=lambda kv: len(kv[1]))
        return {"kind": "tooling", "category": cat, "name": clip(name, 40), "uses": len(turns), "turns": turns}
    return None


def _baseline(conn, run_id, days: list[str]) -> dict[str, float | None]:
    vals: dict[str, list] = {k: [] for k, _, _ in SCORED}
    causes: dict[str, list] = {}
    vals["V_fail_share"], vals["V_tier:verified_fail"] = [], []
    for d in days:
        m = DayMetrics(conn, run_id, d)
        if not m:
            continue
        for k in vals:
            if k == "H" and _h_unmeasured(m):
                continue
            x = m.scored(k)
            if x is not None:
                vals[k].append(x)
        for c, x in m.c_causes().items():
            causes.setdefault(c, []).append(x)
    out = {k: (statistics.median(v) if v else None) for k, v in vals.items()}
    out.update({f"C:{c}": statistics.median(v) for c, v in causes.items()})
    return out


def _h_unmeasured(m: "DayMetrics") -> bool:
    """H stays 0 with unit edits(partial) until hooks provide git snapshots: not measured, not 0."""
    return (m.unit("H") or "").endswith("(partial)") and not m.v("H", 0)


def _arrow(today, base, up_good: bool | None) -> str:
    if today is None or base is None:
        return "n/a"
    d = today - base
    if abs(d) < 1e-9:
        return "→ same"
    if up_good is None:
        return "▲" if d > 0 else "▼"
    better = (d > 0) == up_good
    return ("▲" if d > 0 else "▼") + (" better" if better else " worse")


_PREVENT = {
    "info_missing": "state the missing fact (path, flag, data shape) in the first prompt",
    "instruction_violated": "restate the constraint as a checklist line in the prompt or CLAUDE.md/AGENTS.md",
    "requirement_changed": "write the whole requirement, incl. edge cases, before the agent starts",
    "env_failure": "give the agent the exact setup/test command for this repo up front",
    "unknown": "add a \"done when …\" line so the agent can check itself",
}


def _corrections(m: DayMetrics) -> list[dict]:
    cm = m.c_cause_metrics()
    causes = sorted(cm.items(), key=lambda kv: (-(m.v(kv[1]) or 0), kv[0]))
    out = []
    for cause, k in causes:
        for e in m.evidence(k):
            note = e["note"] or ""
            quote = note.split(": ", 1)[1] if ": " in note else note
            out.append({"cause": clip(cause, 40), "turn_id": e["turn_id"], "quote": clip(quote, 60),
                        "prevent": _PREVENT.get(cause, _PREVENT["unknown"])})
            if len(out) >= 2:
                return out
    if not out and "C" in m.rows:
        for e in m.evidence("C")[:2]:
            out.append({"cause": "unlabelled", "turn_id": e["turn_id"], "quote": clip(e["note"], 60),
                        "prevent": _PREVENT["unknown"]})
    return out


def _judge_doubt(m: DayMetrics) -> str:
    parts = []
    if "C_abstain" in m.rows:
        parts.append(f"abstained on {m.v('C_abstain', 0):.0f} turn(s)")
        ev = m.evidence("C_abstain")
        if ev:
            parts[-1] += " (" + _turns_ref([e["turn_id"] for e in ev], 3) + ")"
    unc = m.v("C:event:uncertain", 0)
    if unc:
        parts.append(f"{unc:.0f} turn(s) labelled uncertain")
    rx = m.v("C_source:regex", 0)
    if rx:
        parts.append(f"{rx:.0f} label(s) from the regex stand-in (low confidence)")
    for k in sorted(m.rows):
        if k.startswith("C_low"):
            parts.append(f"{m.v(k, 0):.0f} low-confidence label(s)")
    if not parts:
        return "nothing flagged" if m.judged() else "judge pending"
    return "; ".join(parts)


def _diagnostics(m: DayMetrics) -> dict:
    act = m.v("active_s", 0.0)
    ov = m.v("overlap_s", 0.0)
    gb = {b: int(m.v(f"gap_bucket:{b}", 0)) for b in metrics.GAP_BUCKETS}
    models = []
    for model, tok in sorted(m.prefixed("tokens:").items(), key=lambda kv: -(kv[1] or 0)):
        models.append({"model": _model_name(model), "tokens": tok, "cost_usd": m.v(f"cost_usd:{model}")})
    if not models:
        for model, calls in sorted(m.prefixed("model_mix:").items(), key=lambda kv: -(kv[1] or 0)):
            models.append({"model": _model_name(model), "calls": calls, "cost_usd": m.v(f"cost_usd:{model}")})
    tooling = {k: v for k, v in m.prefixed("tooling:").items() if k.count(":") == 0}
    return {"active_h": round(act / 3600, 2), "overlap_pct": round(100 * ov / act) if act else 0,
            "turns": m.v("turns"), "sessions": m.v("sessions"), "gap_buckets": gb,
            "gap_median_s": m.v("gap_median_s"), "interrupts": m.v("interrupts", 0),
            "thrown_min": round(m.v("thrown_s", 0.0) / 60, 1),
            "context_p50": m.v("context_p50"), "context_p90": m.v("context_p90"),
            "tokens": (m.v("tokens_in", 0) + m.v("tokens_cached", 0) + m.v("tokens_out", 0)),
            "cost_usd": m.v("cost_usd"), "unpriced_calls": int(m.v("unpriced_calls", 0)), "models": models,
            "subagents": m.v("subagents", 0), "compactions": m.v("compactions", 0),
            "tooling": tooling, "tooling_human_share": m.v("tooling_human_share")}


def facts(conn, day: str, run_id: int | None = None, baseline_days: list[str] | None = None) -> dict | None:
    """Deterministic facts for one report day (also the narrative's input). None = no data."""
    run_id = latest_run_id(conn) if run_id is None else run_id
    m = DayMetrics(conn, run_id, day)
    if not m:
        return None
    if baseline_days is None:
        baseline_days = [_shift(day, -i) for i in range(BASELINE_DAYS, 0, -1)]
    batches = metrics.verification_batches(conn, day)
    v = _verdict(m)
    unv = _unverified_now(conn, day, batches)
    cmd = next((u["command"] for u in unv if u["command"]), None) or "the project's test command"
    change = _CHANGE.get(v["category"] or "", "Keep today's setup; nothing dominated the cost.").format(cmd=cmd)
    base = _baseline(conn, run_id, baseline_days)
    scored = []
    h_unmeasured = _h_unmeasured(m)
    for k, label, up in SCORED:
        t = m.scored(k)
        row = {"metric": k, "label": label, "today": t, "baseline": base.get(k),
               "unit": m.unit(k) if k != "C" else "count", "trend": _arrow(t, base.get(k), up)}
        if k == "H" and h_unmeasured:
            row.update(today=None, baseline=None, trend="n/a")
        scored.append(row)
        if k == "V" and "V" in m.rows:
            # counter-metric: on real data almost all of it is "verified, then edited again in the same
            # turn", not failing checks -> show the two separately, without a better/worse judgement
            fs = m.v("V_fail_share")
            scored.append({"metric": "V_fail_share", "label": "  V verified, then edited again",
                           "today": fs, "baseline": base.get("V_fail_share"), "unit": "share",
                           "trend": _arrow(fs, base.get("V_fail_share"), None)})
            vf = m.v("V_tier:verified_fail", 0)
            scored.append({"metric": "V_tier:verified_fail", "label": "  V checks that failed",
                           "today": vf, "baseline": base.get("V_tier:verified_fail"), "unit": "count",
                           "trend": _arrow(vf, base.get("V_tier:verified_fail"), None)})
    for c, x in sorted(m.c_causes().items(), key=lambda kv: -kv[1]):
        scored.append({"metric": f"C:{c}", "label": f"  C cause: {clip(c, 30)}", "today": x,
                       "baseline": base.get(f"C:{c}"), "unit": "count",
                       "trend": _arrow(x, base.get(f"C:{c}"), False)})
    hev = [e for e in m.evidence("H")]
    return {
        "day": day, "run_id": run_id, "baseline_days": [d for d in baseline_days],
        "verdict": v,
        "tomorrow": {"change": change, "evidence_turns": _evidence_turns(m, v["metric"], batches)},
        "corrections": _corrections(m),
        "judged": m.judged(),
        "unverified_now": unv,
        "hand_edits": {"count": m.v("H", 0), "partial": (m.unit("H") or "").endswith("(partial)"),
                       "unmeasured": h_unmeasured,
                       "repair": m.v("H_repair"), "own": m.v("H_own"),
                       "evidence": [{"turn_id": e["turn_id"], "note": e["note"]} for e in hev[:MAX_EVIDENCE]]},
        "worst_window": _worst_window(conn, day),
        "pattern": _pattern(conn, day, batches),
        "scored": scored,
        "diagnostics": _diagnostics(m),
        "judge_doubt": _judge_doubt(m),
        "action_items": _judge_call("action_items", conn, day, run_id=run_id, default=[]),
        "strengths": _judge_call("strengths", conn, day, run_id=run_id, default=[]),
        "attribution": _judge_call("attribution", conn, day, default=None),
    }


def _judge_call(name: str, conn, day: str, default=None, **kw):
    """judge.action_items / strengths / attribution; any failure -> default."""
    try:
        from . import judge
        return getattr(judge, name)(conn, day, **kw)
    except Exception:
        return default


# --- rendering ---------------------------------------------------------------------------------

def _line_unverified(f: dict) -> list[str]:
    out = []
    unv = f["unverified_now"]
    if unv:
        out.append(f"   - {len(unv)} session(s) end on an unverified edit batch:")
        for u in unv[:MAX_EVIDENCE]:
            files = ", ".join(u["files"][:4]) + (f" (+{len(u['files']) - 4})" if len(u["files"]) > 4 else "")
            run = f"run `{u['command']}` in `{u['manifest_dir']}`" if u["command"] else \
                "run the project's tests (no manifest found)"
            out.append(f"     - {u['session_id'][:8]} {_turn(u['turn_id'])} ({u['tier']}): "
                       f"{files or '?'} → {run}")
        if len(unv) > MAX_EVIDENCE:
            out.append(f"     - … {len(unv) - MAX_EVIDENCE} more")
    h = f["hand_edits"]
    if h["unmeasured"]:
        out.append("   - hand-edits: not measured yet (no git snapshots)")
    elif h["count"]:
        split = f" ({h['repair']:.0f} repair / {h['own']:.0f} own)" if h["repair"] is not None else ""
        part = " — partial: no git snapshots" if h["partial"] else ""
        out.append(f"   - {h['count']:.0f} hand edit(s){split}{part}: "
                   + "; ".join(f"{e['note']} {_turn(e['turn_id'])}" for e in h["evidence"][:3]))
    if not unv:
        out.insert(0, "   - clean: every session's last edit batch was verified.")
    return out


def _fmt_scored(x, unit):
    if x is None:
        return "n/a"
    return _fmt_num(x, unit)


def _diag_line(d: dict) -> str:
    gb = d["gap_buckets"]
    gaps = (f"gaps <30s {gb['lt30s']} · 30s–2m {gb['30s-2m']} · 2–5m {gb['2-5m']} · 5–15m {gb['5-15m']}"
            f" · breaks {gb['break']}")
    ctx = (f"context p50 {d['context_p50']:.0f}% / p90 {d['context_p90']:.0f}%"
           if d["context_p50"] is not None and d["context_p90"] is not None else "context n/a")
    cost = f"${d['cost_usd']:.2f}" if d["cost_usd"] is not None else "$ n/a"
    if d["unpriced_calls"]:
        cost += f" ({d['unpriced_calls']} unpriced)"
    models = []
    for x in d["models"][:4]:
        c = f" ${x['cost_usd']:.2f}" if x.get("cost_usd") is not None else ""
        amount = f"{_tok(x['tokens'])} tok" if "tokens" in x else f"{x['calls']:.0f} calls"
        models.append(f"{x['model']} {amount}{c}")
    tl = d["tooling"]
    if tl:
        tool = "tooling " + ", ".join(f"{k} {v:.0f}" for k, v in sorted(tl.items()))
        if d["tooling_human_share"] is not None:
            tool += f" ({100 * d['tooling_human_share']:.0f}% human)"
    else:
        tool = "tooling 0"
    return (f"active {d['active_h']:.1f} h, overlap {d['overlap_pct']}% · {gaps} · "
            f"interrupts {d['interrupts']:.0f} ({d['thrown_min']:.0f} min thrown) · {ctx} · "
            f"{_tok(d['tokens'])} tok {cost}" + (f" [{'; '.join(models)}]" if models else "")
            + f" · subagents {d['subagents']:.0f} · {tool}")


def _render_eight(f: dict) -> list[str]:
    L: list[str] = []
    v = f["verdict"]
    L.append(f"1. **Verdict:** {v['text']}." + (f" ({_turns_ref(f['tomorrow']['evidence_turns'], 3)})"
                                                  if v["category"] else ""))
    t = f["tomorrow"]
    L.append(f"2. **Tomorrow's one change:** {t['change']}")
    L.append(f"   - Evidence: {_turns_ref(t['evidence_turns'])}")
    cs = f["corrections"]
    if not cs and f["judged"]:
        L.append("3. **Top correction episodes:** none — the judge found no corrections today.")
    elif cs:
        L.append("3. **Top correction episodes:**")
        for c in cs:
            L.append(f"   - {c['cause']}: \"{c['quote'] or '…'}\" ({_turn(c['turn_id'])}) → "
                     f"would have prevented it: {c['prevent']}")
    else:
        L.append("3. **Top correction episodes:** judge pending — run `scorer judge` to label corrections "
                 "(cause + the prompt that would have prevented each).")
    L.append("4. **Unverified or hand-repaired right now:**")
    L.extend(_line_unverified(f))
    w = f["worst_window"]
    if w:
        L.append(f"5. **Worst attention window:** {w['start']}–{w['end']}, {w['minutes']:.0f} min lost across "
                 f"{w['sessions']} session(s) ({w['attended']} attended gap(s)) — {_turns_ref(w['turns'])}")
    else:
        L.append("5. **Worst attention window:** none — no blocked or babysat gaps today.")
    p = f["pattern"]
    if p and p["kind"] == "verified_session":
        how = f" with `{p['command']}`" if p["command"] else ""
        L.append(f"6. **Repeat:** session {p['session']} verified {p['verified']}/{p['batches']} edit batches"
                 f"{how} ({p['human_initiated']} asked for by you) — {_turns_ref(p['turns'])}")
    elif p:
        L.append(f"6. **Repeat:** you reached for {p['category']} `{p['name']}` {p['uses']}× yourself — "
                 f"{_turns_ref(p['turns'])}")
    else:
        L.append("6. **Repeat:** no verified edit batch or human-initiated tooling today to point at.")
    L.append(f"7. **Scored metrics vs {BASELINE_DAYS}-day baseline:**")
    L.append("")
    L.append("   | metric | today | 7d median | trend |")
    L.append("   |---|---|---|---|")
    for s in f["scored"]:
        unit = s["unit"]
        today = _fmt_scored(s["today"], unit)
        if s["metric"] == "H" and f["hand_edits"]["unmeasured"]:
            today = "not measured yet (no git snapshots)"
        elif s["metric"] == "H" and f["hand_edits"]["partial"] and s["today"] is not None:
            today += " (partial: no git snapshots)"
        if s["metric"] == "C" and s["today"] is None:
            today = "judge pending"
        L.append(f"   | {s['label']} | {today} | {_fmt_scored(s['baseline'], unit)} | {s['trend']} |")
    L.append("")
    L.extend(_attribution_lines(f.get("attribution")))
    L.append(f"8. **Diagnostics:** {_diag_line(f['diagnostics'])}. Judge unsure: {f['judge_doubt']}.")
    return L


def _narrative(conn, day: str, f: dict) -> str | None:
    if os.environ.get("SCORER_NO_LLM") == "1":
        return None
    try:
        from . import agent
    except Exception:
        return None
    try:
        if not agent.has_key():
            return None
    except Exception:
        return None
    try:
        txt = agent.narrative(conn, day, f)
    except Exception:
        return None
    return txt.strip() if isinstance(txt, str) and txt.strip() else None


def _no_data(day: str, what: str | None = None) -> str:
    return (f"# scorer report — {what or day}\n\nno data for {what or day}. "
            "Run `scorer ingest` (and use the agents a bit) first.\n")


def _days_table(conn, run_id, window: list[str]) -> list[str]:
    L = ["| day | active h | turns | V | H | B min | C | interrupts | cost $ |", "|---|---|---|---|---|---|---|---|---|"]
    for d in window:
        m = DayMetrics(conn, run_id, d)
        if not m:
            L.append(f"| {d} | – | – | – | – | – | – | – | – |")
            continue
        c = m.c_total()
        cost = m.v("cost_usd")  # H "n/m" = not measured (no git snapshots)
        L.append(f"| {d} | {m.v('active_s', 0) / 3600:.1f} | {m.v('turns', 0):.0f} | "
                 f"{_fmt_num(m.v('V'), 'share')} | {'n/m' if _h_unmeasured(m) else _fmt_num(m.v('H'))} | {m.v('B_min', 0):.0f} | "
                 f"{_fmt_num(c) if c is not None else '–'} | {m.v('interrupts', 0):.0f} | "
                 f"{f'{cost:.2f}' if cost is not None else 'n/a'} |")
    return L


def render(conn: sqlite3.Connection, *, day: str | None = "today", days: int | None = None) -> str:
    end = _resolve_day(day)
    run_id = latest_run_id(conn)
    multi = bool(days and days > 1)
    if multi:
        window = [_shift(end, -i) for i in range(days - 1, -1, -1)]
        have = [d for d in window if DayMetrics(conn, run_id, d)]
        if not have:
            return _no_data(end, f"{window[0]} … {window[-1]}")
        focus = have[-1]
        baseline_days = [d for d in window if d < focus] or None
        title = f"# scorer report — {window[0]} … {window[-1]} ({days} days)"
    else:
        focus, baseline_days = end, None
        title = f"# scorer report — {focus}"
    f = facts(conn, focus, run_id, baseline_days)
    if f is None:
        return _no_data(focus)
    n = f["diagnostics"]
    out = [title, "",
           f"{n['turns'] or 0:.0f} turns in {n['sessions'] or 0:.0f} session(s) on {focus} · ingest run {run_id}"
           " · evidence as `turn:<id>` (dashboard /#turn/<id>)", ""]
    if multi:
        out += _days_table(conn, run_id, window) + [""]
    nar = _narrative(conn, focus, f)
    if nar:
        out += ["## Narrative", "", nar, ""]
    if nar or multi:
        out += [f"## {focus}" + (" (latest day with data)" if multi and focus != end else ""), ""]
    out += _render_eight(f)
    out += _render_items(f.get("strengths") or [], "What you did well",
                         "none recorded today — the judge notes good moves once `scorer judge` has run.")
    out += _render_actions(f.get("action_items") or [])
    return "\n".join(out).rstrip() + "\n"


def _attribution_lines(a: dict | None) -> list[str]:
    """Section 7 add-on: each scored metric's you/agent split on one line (judge.attribution)."""
    if not a:
        return []
    c, v, t = a["corrections"], a["verification"], a["tooling"]
    via = f" (incl. {v['asked_by_you']['via_config']} via CLAUDE.md/hooks)" if v["asked_by_you"].get("via_config") else ""
    return [f"   - you vs agent: V asked by you {v['asked_by_you']['count']}{via} / agent initiative "
            f"{v['agent_initiative']['count']} / none {v['none']['count']} · C you-preventable "
            f"{c['user_preventable']['count']} / agent error {c['agent_error']['count']} / environment "
            f"{c['environment']['count']} / unclear {c['unclear']['count']} · H, B_min: yours by definition · "
            f"tooling you {t['you']['count']} / agent {t['agent']['count']} / default {t['default']['count']}", ""]


def _render_items(items: list[dict], heading: str, empty: str) -> list[str]:
    """A numbered "## <heading>" list. Plain numbered lines (no bold) so they never read as one of
    the 8 report sections."""
    L = ["", f"## {heading}", ""]
    if not items:
        return L + [empty]
    for i, it in enumerate(items, 1):
        src = "judge" if it.get("source") == "judge" else "metrics"
        L.append(f"{i}. {clip(it.get('text'), 120)} — {it.get('category')}, {it.get('count', 0)}× ({src}) — "
                 f"{_turns_ref(it.get('turn_ids') or [])}")
        if it.get("example_quote"):
            L.append(f"   - e.g. \"{clip(it['example_quote'], 80)}\"")
    if any(it.get("uncalibrated") for it in items):
        L += ["", "_Judge items are uncalibrated (no κ check yet)._"]
    return L


def _render_actions(items: list[dict]) -> list[str]:
    """## Action items after the 8 lines. Plain numbered lines (no bold) so they never read as one
    of the 8 report sections."""
    L = ["", "## Action items", ""]
    if not items:
        return L + ["none today — run `scorer judge` to derive lessons from your prompts."]
    for i, it in enumerate(items, 1):
        src = "judge" if it.get("source") == "judge" else "metrics"
        L.append(f"{i}. {clip(it.get('text'), 120)} — {it.get('category')}, {it.get('count', 0)}× ({src}) — "
                 f"{_turns_ref(it.get('turn_ids') or [])}")
        if it.get("example_quote"):
            L.append(f"   - e.g. \"{clip(it['example_quote'], 80)}\"")
        if it.get("claude_md_line"):
            md = clip(it["claude_md_line"], 160).replace("`", "'")
            L.append(f"   - CLAUDE.md/AGENTS.md: `{md}`")
    if any(it.get("uncalibrated") for it in items):
        L += ["", "_Judge items are uncalibrated (no κ check yet): treat them as prompts for reflection, "
                  "not scores._"]
    return L
