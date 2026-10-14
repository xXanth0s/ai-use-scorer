#!/usr/bin/env python3
"""Tool correctness: POST /api/tools/<name> on `scorer serve` vs direct read-only SQL on the same DB.

    uv run python agent-mastra/qa/tool_checks.py --url http://127.0.0.1:8790 --db $QA_DIR/scorer.db [--json out.json]

Privacy: never prints DB free text. Only ids, counts, lengths and pass/fail.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from scorer.model import clip, local_day  # noqa: E402  (same day bucketing as the tools)

CLIP = 80
RESULTS: list[dict] = []


def check(group: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"group": group, "check": name, "ok": bool(ok), "detail": detail[:160]})
    print(f"{'PASS' if ok else 'FAIL'}  {group:13} {name:48} {detail[:110]}")
    return ok


class Tools:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def __call__(self, name: str, inp: dict, n0: int = 1, day: str | None = None) -> dict:
        req = urllib.request.Request(f"{self.url}/api/tools/{name}", method="POST",
                                     data=json.dumps({"input": inp, "n0": n0, "day": day}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200, r.status
            return json.loads(r.read())

    def status(self, name: str, body: bytes) -> int:
        req = urllib.request.Request(f"{self.url}/api/tools/{name}", method="POST", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def rows(c, sql, args=()):
    cur = c.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


class LeakProbe:
    """Content past the 80-char clip point of any sensitive value must never appear in a tool result."""

    def __init__(self, c):
        vals = [r[0] for r in c.execute(
            "SELECT prompt_text FROM turn UNION ALL SELECT args_summary FROM tool_call "
            "UNION ALL SELECT initiator_rule FROM tool_call UNION ALL SELECT path FROM file_edit "
            "UNION ALL SELECT data FROM event") if r[0]]
        self.probes = set()
        for v in vals:
            t = re.sub(r"\s+", " ", str(v)).strip()
            for start in range(85, len(t) - 24, 120):       # several 24-char windows past the clip point
                p = t[start:start + 24]
                if len(set(p)) > 6:                           # skip low-entropy runs ("------")
                    self.probes.add(p)
        # A window that also occurs inside some value's allowed 80-char excerpt (repeated prompts,
        # shared path prefixes) is legitimately visible: drop it, or every hit is a false positive.
        allowed = "\n".join(clip(v, 80) for v in vals)
        # ids (session uuids, tool_use ids) show up inside commands; they are not free text
        allowed += "\n".join(str(r[0]) for r in c.execute(
            "SELECT id FROM session UNION ALL SELECT first_uuid FROM turn UNION ALL SELECT last_uuid FROM turn "
            "UNION ALL SELECT tool_use_id FROM tool_call UNION ALL SELECT request_id FROM api_call") if r[0])
        self.probes = {p for p in self.probes if p not in allowed}

    def leaks(self, result) -> int:
        blob = "\n".join(strings(result))
        return sum(1 for p in self.probes if p in blob)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8790")
    ap.add_argument("--db", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    T = Tools(a.url)
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rid = c.execute("SELECT max(id) FROM ingest_run").fetchone()[0]
    runs_before = c.execute("SELECT count(*) FROM ingest_run").fetchone()[0]
    probe = LeakProbe(c)
    print(f"# DB run_id={rid}, leak probes={len(probe.probes)}")
    today = local_day(datetime.now().astimezone())

    # ------------------------------------------------------------------ list_sessions
    turns = rows(c, "SELECT t.id, t.session_id, t.prompt_at, t.agent_finished_at, s.agent, s.repo, s.cwd "
                    "FROM turn t LEFT JOIN session s ON s.id = t.session_id WHERE t.prompt_at IS NOT NULL")
    by_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in turns:
        by_day[local_day(t["prompt_at"])][t["session_id"]].append(t)
    days_api = {d["day"]: d for d in json.loads(urllib.request.urlopen(f"{a.url}/api/days").read())["days"]}
    busiest = max(by_day, key=lambda d: sum(len(v) for v in by_day[d]))
    for day in sorted({today, busiest, "2026-10-10", "2026-10-13", "2026-09-01"}):
        r = T("list_sessions", {"day": day})
        exp = by_day.get(day, {})
        got = {s["id"]: s for s in r["data"]["sessions"]} if r["ok"] else {}
        ok = r["ok"] and set(got) == set(exp) and all(got[k]["turns"] == len(v) for k, v in exp.items())
        check("list_sessions", f"day={day} sessions+turns", ok,
              f"{len(got)} sessions/{sum(s['turns'] for s in got.values())} turns vs SQL {len(exp)}/{sum(map(len, exp.values()))}")
        if exp:
            bad_active = []
            for sid, ts in exp.items():
                act = 0.0
                for t in ts:
                    if t["agent_finished_at"]:
                        dsec = (datetime.fromisoformat(t["agent_finished_at"].replace("Z", "+00:00"))
                                - datetime.fromisoformat(t["prompt_at"].replace("Z", "+00:00"))).total_seconds()
                        if dsec > 0:
                            act += round(dsec, 1)
                rep = Path(str(ts[0]["repo"] or ts[0]["cwd"] or "").rstrip("/")).name or (ts[0]["repo"] or ts[0]["cwd"] or "")
                g = got.get(sid, {})
                if abs(g.get("active_s", -1) - act) > 1 or (g.get("repo") or "") != rep or g.get("agent") != ts[0]["agent"]:
                    bad_active.append(sid[:8])
            check("list_sessions", f"day={day} active_s/repo/agent", not bad_active, f"mismatch {bad_active[:4]}")
            if day in days_api:
                check("list_sessions", f"day={day} agrees with /api/days", days_api[day]["turns"] == sum(map(len, exp.values()))
                      and days_api[day]["sessions"] == len(got), f"/api/days {days_api[day]['sessions']}/{days_api[day]['turns']}")
    r1 = T("list_sessions", {}, day=None)
    check("list_sessions", "no day -> today", r1["ok"] and r1["data"]["day"] == today, r1["data"].get("day", "") if r1["ok"] else r1.get("error", ""))
    r2 = T("list_sessions", {}, day="2026-10-10")
    check("list_sessions", "no day -> body day (day on screen)", r2["ok"] and r2["data"]["day"] == "2026-10-10", "")
    r3 = T("list_sessions", {"day": "today"})
    check("list_sessions", "day='today' literal", r3["ok"] and r3["data"]["day"] == today, "")
    r4 = T("list_sessions", {"day": "not-a-day"})
    check("list_sessions", "garbage day -> empty or error (not crash)", isinstance(r4, dict) and "ok" in r4,
          f"ok={r4.get('ok')} sessions={len(r4.get('data', {}).get('sessions', [])) if r4.get('ok') else '-'}")
    check("list_sessions", "no citations (sessions are not turns)", r1["citations"] == [], "")

    # ------------------------------------------------------------------ get_metric
    def mv(metric_sql, args):
        return rows(c, "SELECT id, metric, scope_type, scope_id, value, unit, n FROM metric_value WHERE run_id = ? AND " + metric_sql,
                    [rid, *args])

    v_days = [r["scope_id"] for r in mv("metric = 'V' AND scope_type='day' ORDER BY scope_id DESC", [])]
    for day in v_days[:4]:
        for n0 in (1, 7):
            r = T("get_metric", {"metric": "V", "day": day}, n0=n0)
            exp = mv("metric='V' AND scope_type='day' AND scope_id=?", [day])
            ev = rows(c, "SELECT turn_id FROM metric_evidence WHERE metric_value_id = ? LIMIT 20", [exp[0]["id"]])
            exp_turns = [e["turn_id"] for e in ev if e["turn_id"] is not None]
            ok = r["ok"] and len(r["data"]["values"]) == 1 and abs(r["data"]["values"][0]["value"] - exp[0]["value"]) < 1e-9 \
                and r["data"]["values"][0]["n"] == exp[0]["n"] and r["data"]["values"][0]["unit"] == exp[0]["unit"]
            got_turns = [e["turn_id"] for e in r["data"]["evidence"]] if r["ok"] else []
            ns = [x["n"] for x in r["citations"]]
            uniq = list(dict.fromkeys(exp_turns))
            if n0 == 1:
                check("get_metric", f"V day={day} value/unit/n", ok, f"{r.get('summary', '')}")
                check("get_metric", f"V day={day} evidence turns == SQL", sorted(got_turns) == sorted(exp_turns),
                      f"{len(got_turns)} vs {len(exp_turns)}")
            check("get_metric", f"V day={day} n0={n0} citations numbered n0..", ns == list(range(n0, n0 + len(uniq)))
                  and [x["turn_id"] for x in r["citations"]] == uniq
                  and all(e.get("n") in ns for e in r["data"]["evidence"] if e["turn_id"] is not None), f"ns={ns[:6]}")
    # prefix, scopes, defaults
    d0 = v_days[0]
    r = T("get_metric", {"metric": "V_tier", "day": d0})
    exp = mv("metric LIKE 'V_tier:%' AND scope_type='day' AND scope_id=?", [d0])
    check("get_metric", "prefix V_tier -> all tier rows", r["ok"] and {v["metric"]: v["value"] for v in r["data"]["values"]}
          == {e["metric"]: e["value"] for e in exp}, f"{len(r['data']['values']) if r['ok'] else r.get('error')} vs {len(exp)}")
    r = T("get_metric", {"metric": "cost_usd", "scope_type": "all"})
    exp = mv("metric='cost_usd' AND scope_type='all'", [])
    check("get_metric", "cost_usd scope=all", r["ok"] and len(r["data"]["values"]) == len(exp)
          and all(abs(x["value"] - y["value"]) < 1e-9 for x, y in zip(r["data"]["values"], exp)),
          f"{r['data']['values'][:1] if r['ok'] else r.get('error')}")
    sess = mv("metric='V' AND scope_type='session' ORDER BY scope_id DESC, metric LIMIT 60", [])
    r = T("get_metric", {"metric": "V", "scope_type": "session"})
    check("get_metric", "V scope=session (<=60 rows)", r["ok"] and len(r["data"]["values"]) == len(sess),
          f"{len(r['data']['values']) if r['ok'] else r.get('error')} vs {len(sess)}")
    r = T("get_metric", {"metric": "V"}, day=None)
    check("get_metric", "no day, no screen day -> all day rows", r["ok"] and len(r["data"]["values"]) == len(v_days),
          f"{len(r['data']['values']) if r['ok'] else r.get('error')} values (SQL {len(v_days)}); evidence_scope={r['data'].get('evidence_scope') if r['ok'] else '-'}")
    r = T("get_metric", {"metric": "V"}, day=v_days[1])
    check("get_metric", "no day -> body day", r["ok"] and r["data"]["values"][0]["scope_id"] == v_days[1], "")
    r = T("get_metric", {"metric": "NOPE_metric"})
    check("get_metric", "unknown metric -> error as data", r["ok"] is False and "not found" in r["error"] and r.get("available"),
          f"error={r.get('error', '')[:60]} available={len(r.get('available') or [])}")
    no_v = next((d for d in ("2026-10-11", "2026-10-09", "2026-09-01") if d not in v_days), None)
    r = T("get_metric", {"metric": "V", "day": no_v})
    check("get_metric", f"day without value ({no_v}) -> days_with_values", r["ok"] is False and r.get("days_with_values"),
          f"{r.get('error', '')[:60]}")
    r = T("get_metric", {"metric": ""})
    check("get_metric", "empty metric -> error", r["ok"] is False, r.get("error", "")[:60])
    r = T("get_metric", {"metric": "V", "scope_type": "bogus", "day": d0})
    check("get_metric", "bad scope_type -> falls back to day", r["ok"] and r["data"]["scope_type"] == "day", "")

    # ------------------------------------------------------------------ get_turn
    ids = [r[0] for r in c.execute("SELECT id FROM turn ORDER BY random() LIMIT 5")]
    ids.append(c.execute("SELECT turn_id FROM tool_call GROUP BY turn_id ORDER BY count(*) DESC LIMIT 1").fetchone()[0])
    ids.append(c.execute("SELECT turn_id FROM event WHERE kind='interrupt' AND turn_id IS NOT NULL LIMIT 1").fetchone()[0])
    ids.append(c.execute("SELECT c.turn_id FROM file_edit f JOIN tool_call c ON c.id=f.tool_call_id GROUP BY c.turn_id ORDER BY count(*) DESC LIMIT 1").fetchone()[0])
    ids.append(c.execute("SELECT id FROM turn ORDER BY length(prompt_text) DESC LIMIT 1").fetchone()[0])
    for tid in ids:
        r = T("get_turn", {"turn_id": tid}, n0=3)
        t = rows(c, "SELECT * FROM turn WHERE id=?", [tid])[0]
        calls = rows(c, "SELECT id, name, is_error FROM tool_call WHERE turn_id=? ORDER BY started_at, id", [tid])
        edits = c.execute("SELECT count(*) FROM file_edit f JOIN tool_call c ON c.id=f.tool_call_id WHERE c.turn_id=?", [tid]).fetchone()[0]
        api = rows(c, "SELECT model, cost_usd FROM api_call WHERE turn_id=?", [tid])
        intr = bool(c.execute("SELECT 1 FROM event WHERE turn_id=? AND kind='interrupt'", [tid]).fetchone())
        d = r.get("data", {})
        cost = round(sum(x["cost_usd"] for x in api if x["cost_usd"] is not None), 4) if any(x["cost_usd"] is not None for x in api) else None
        errs_exp = sum(1 for x in calls[:40] if x["is_error"])
        ok = (r["ok"] and d["turn_id"] == tid and d["session"]["id"] == t["session_id"] and d["idx"] == t["idx"]
              and d["tool_calls_total"] == len(calls) and len(d["tool_calls"]) == min(40, len(calls))
              and [x["id"] for x in d["tool_calls"]] == [x["id"] for x in calls[:40]]
              and sum(x["is_error"] for x in d["tool_calls"]) == errs_exp
              and len(d["file_edits"]) == min(40, edits) and d["api_calls"]["count"] == len(api)
              and d["api_calls"]["cost_usd"] == cost and d["interrupted"] == intr
              and d["prompt"] == clip(t["prompt_text"], 80))
        cites_ok = r["ok"] and r["citations"] == [{"n": 3, "turn_id": tid, "session_id": t["session_id"], "label": r["citations"][0]["label"]}] \
            and d.get("n") is None  # get_turn's _slot is data itself -> n written into data
        check("get_turn", f"turn {tid} vs rows", ok,
              f"calls {d.get('tool_calls_total')}/{len(calls)} shown {len(d.get('tool_calls', []))} edits {len(d.get('file_edits', []))}/{edits} api {d.get('api_calls', {}).get('count')}/{len(api)} intr {d.get('interrupted')}")
        check("get_turn", f"turn {tid} citation n0=3", r["ok"] and len(r["citations"]) == 1 and r["citations"][0]["n"] == 3
              and r["citations"][0]["turn_id"] == tid and d.get("n") == 3, f"data.n={d.get('n')}")
        if len(calls) > 40:
            check("get_turn", f"turn {tid} truncation is signalled", d["tool_calls_total"] > len(d["tool_calls"]),
                  f"total {d['tool_calls_total']} > shown {len(d['tool_calls'])} (no explicit truncated flag)")
        if edits > 40:
            check("get_turn", f"turn {tid} file_edits truncation is signalled", False,
                  f"{edits} edits, 40 shown, no total/truncated field")
    r = T("get_turn", {"turn_id": 99999999})
    check("get_turn", "unknown turn -> error as data", r["ok"] is False and "not found" in r["error"] and r["citations"] == [], r.get("error", ""))
    r = T("get_turn", {"turn_id": "abc"})
    check("get_turn", "non-int turn_id -> error as data", r["ok"] is False, r.get("error", ""))
    r = T("get_turn", {})
    check("get_turn", "missing turn_id -> error as data", r["ok"] is False, r.get("error", ""))

    # ------------------------------------------------------------------ query_db: results
    def q(sql, limit=None, n0=1):
        inp = {"sql": sql} if limit is None else {"sql": sql, "limit": limit}
        return T("query_db", inp, n0=n0)

    r = q("SELECT count(*) AS n FROM turn")
    check("query_db", "count(*) turn", r["ok"] and r["data"]["rows"][0][0] == c.execute("SELECT count(*) FROM turn").fetchone()[0],
          str(r["data"]["rows"] if r["ok"] else r.get("error")))
    sql = "SELECT agent, count(*) AS n FROM session GROUP BY agent ORDER BY agent"
    r = q(sql)
    check("query_db", "group by agent", r["ok"] and r["data"]["rows"] == [list(x) for x in c.execute(sql)], str(r["data"]["rows"] if r["ok"] else r.get("error")))
    sql = ("SELECT model, count(*) AS calls, round(sum(cost_usd), 2) AS usd FROM api_call GROUP BY model ORDER BY usd DESC")
    r = q(sql)
    check("query_db", "cost per model", r["ok"] and r["data"]["rows"] == [list(x) for x in c.execute(sql)], f"{len(r['data']['rows']) if r['ok'] else r.get('error')} rows")
    sql = "SELECT t.id AS turn_id, count(c.id) AS calls FROM turn t JOIN tool_call c ON c.turn_id=t.id GROUP BY t.id ORDER BY calls DESC LIMIT 25"
    r = q(sql, n0=4)
    exp_ids = [x[0] for x in c.execute(sql)]
    cmap = r["data"].get("citations", {}) if r["ok"] else {}
    check("query_db", "turn_id rows -> citations (max 20, from n0)", r["ok"] and [x["turn_id"] for x in r["citations"]] == exp_ids[:20]
          and [x["n"] for x in r["citations"]] == list(range(4, 24)) and cmap == {str(t): 4 + i for i, t in enumerate(exp_ids[:20])},
          f"{len(r['citations']) if r['ok'] else r.get('error')} citations")
    r = q("SELECT id FROM turn", limit=500)
    check("query_db", "limit 500 -> capped at 200, truncated", r["ok"] and len(r["data"]["rows"]) == 200 and r["data"]["truncated"], "")
    r = q("SELECT id FROM turn", limit=0)
    check("query_db", "limit 0 -> 1 row", r["ok"] and len(r["data"]["rows"]) == 1, "")
    r = q("SELECT id FROM turn", limit="abc")
    check("query_db", "limit 'abc' -> default 50", r["ok"] and len(r["data"]["rows"]) == 50, "")
    r = q("SELECT id FROM turn")
    check("query_db", "default limit 50", r["ok"] and len(r["data"]["rows"]) == 50 and r["data"]["truncated"], "")
    r = q("SELECT 1 AS x /* ; */ -- ;")
    check("query_db", "';' inside comments allowed", r["ok"], r.get("error", ""))
    r = q("SELECT ';' AS x")
    check("query_db", "';' inside string allowed", r["ok"], r.get("error", ""))
    r = q("WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM x) SELECT count(*) FROM x")
    check("query_db", "runaway recursive CTE -> timeout error", r["ok"] is False and "cancelled" in r.get("error", ""), r.get("error", "")[:60])

    # ------------------------------------------------------------------ query_db: rejections
    writes = {
        "INSERT": "INSERT INTO ingest_run(at) VALUES ('x')",
        "UPDATE": "UPDATE turn SET idx = idx",
        "DELETE": "DELETE FROM ingest_run",
        "DROP": "DROP TABLE ingest_run",
        "CREATE": "CREATE TABLE z(a)",
        "ATTACH": "ATTACH DATABASE '/tmp/qa-x.db' AS x",
        "DETACH": "DETACH DATABASE main",
        "PRAGMA": "PRAGMA journal_mode=DELETE",
        "PRAGMA read": "PRAGMA table_info(turn)",
        "VACUUM INTO": "VACUUM INTO '/tmp/qa-vac.db'",
        "REPLACE": "REPLACE INTO ingest_run(id, at) VALUES (1, 'x')",
        "multi SELECT;SELECT": "SELECT 1; SELECT 2",
        "multi SELECT;DROP": "SELECT 1; DROP TABLE ingest_run",
        "WITH ... INSERT": "WITH a AS (SELECT 1) INSERT INTO ingest_run(at) SELECT 'x' FROM a",
        "WITH ... UPDATE": "WITH a AS (SELECT 1) UPDATE turn SET idx = idx WHERE id IN (SELECT * FROM a)",
        "WITH ... DELETE": "WITH a AS (SELECT 1) DELETE FROM ingest_run WHERE id IN (SELECT * FROM a)",
        "load_extension": "SELECT load_extension('/tmp/x')",
        "main.turn raw prompt": "SELECT prompt_text FROM main.turn LIMIT 1",
        "main.tool_call raw args": "SELECT args_summary FROM main.tool_call LIMIT 1",
        "main.file_edit raw path": "SELECT path FROM main.file_edit LIMIT 1",
        "event.data": "SELECT data FROM main.event LIMIT 1",
        "BEGIN": "BEGIN IMMEDIATE",
        "empty": "   ",
    }
    for label, sql in writes.items():
        r = q(sql)
        check("query_db-reject", label, r["ok"] is False and r["citations"] == [], r.get("error", "")[:70] if not r["ok"] else "ACCEPTED")
    r = q("SELECT * FROM pragma_table_info('turn')")
    check("query_db-reject", "pragma_table_info() table-valued fn", r["ok"] is False,
          ("ACCEPTED: " + str(len(r["data"]["rows"])) + " rows (schema only, no data)") if r["ok"] else r.get("error", "")[:60])
    r = q("SELECT * FROM pragma_database_list")
    check("query_db-reject", "pragma_database_list (leaks DB path)", r["ok"] is False,
          ("ACCEPTED: file path len " + str(max((len(str(x)) for row in r["data"]["rows"] for x in row), default=0))) if r["ok"] else r.get("error", "")[:60])
    check("query_db-reject", "DB unchanged after write attempts",
          c.execute("SELECT count(*) FROM ingest_run").fetchone()[0] == runs_before
          and not c.execute("SELECT 1 FROM sqlite_master WHERE name='z'").fetchone(), "")

    # ------------------------------------------------------------------ query_db: privacy
    leak_sql = {
        "substr(prompt_text, 81)": "SELECT substr(prompt_text, 81) AS s FROM turn WHERE length(prompt_text) >= 80",
        "substr(args_summary, 81)": "SELECT substr(args_summary, 81) AS s FROM tool_call",
        "prompt_text aliased": "SELECT prompt_text AS p, length(prompt_text) AS L FROM turn ORDER BY L DESC",
        "temp.turn": "SELECT prompt_text AS x FROM temp.turn",
        "max length via view": "SELECT max(length(prompt_text)) AS m FROM turn",
        "group_concat(prompt_text)": "SELECT group_concat(prompt_text, ' ') AS g FROM turn",
        "group_concat(args)": "SELECT group_concat(args_summary, '|') AS g FROM tool_call",
        "path concat": "SELECT path || '/' || path AS p FROM file_edit",
        "replace(prompt)": "SELECT replace(prompt_text, '…', '') AS r FROM turn",
        "instr probe": "SELECT id, instr(prompt_text, 'e') AS i FROM turn",
        "json_object": "SELECT json_group_array(prompt_text) AS j FROM turn",
        "hex(prompt)": "SELECT hex(prompt_text) AS h FROM turn",
        "unicode/char rebuild": "SELECT id AS turn_id, unicode(substr(prompt_text, 79, 1)) AS u FROM turn",
        "session cwd/repo/branch": "SELECT cwd, repo, branch, transcript_path FROM session",
        "metric_evidence.note": "SELECT note FROM metric_evidence",
        "git_change.files": "SELECT files, author FROM git_change",
        "event via view": "SELECT data FROM event",
    }
    for label, sql in leak_sql.items():
        r = q(sql, limit=200)
        if not r["ok"]:
            check("query_db-privacy", label, True, f"rejected: {r.get('error', '')[:60]}")
            continue
        n_leaks = probe.leaks(r)
        longest = max((len(s) for s in strings(r["data"]["rows"])), default=0)
        ok = n_leaks == 0
        detail = f"leaks={n_leaks} longest_cell={longest}"
        if label.startswith("substr(") and ok:
            ok = all(row[0] in ("", None) for row in r["data"]["rows"])
            detail += " all-empty" if ok else " NON-EMPTY"
        if label in ("prompt_text aliased", "temp.turn", "max length via view", "replace(prompt)") and ok:
            ok = longest <= CLIP or label == "max length via view"
        if label == "max length via view":
            ok = ok and r["data"]["rows"][0][0] <= CLIP
            detail += f" max={r['data']['rows'][0][0]}"
        check("query_db-privacy", label, ok, detail)
    # group_concat: each fragment is clipped, but one cell > 80 chars of prompt material
    r = q("SELECT group_concat(prompt_text, ' ') AS g FROM turn")
    if r["ok"]:
        check("query_db-privacy", "no cell > 80 chars of prompt material", len(r["data"]["rows"][0][0] or "") <= CLIP,
              f"group_concat cell = {len(r['data']['rows'][0][0] or '')} chars (each fragment ≤80; cell clipped at 200 not 80)")
    r = q("SELECT cwd FROM session ORDER BY length(cwd) DESC")
    if r["ok"]:
        check("query_db-privacy", "session.cwd clipped to 80 (serve.py docstring)", max(len(x[0] or "") for x in r["data"]["rows"]) <= CLIP,
              f"longest cwd = {max(len(x[0] or '') for x in r['data']['rows'])}")
    r = q("SELECT note FROM metric_evidence ORDER BY length(note) DESC")
    if r["ok"]:
        check("query_db-privacy", "metric_evidence.note clipped to 80", max(len(x[0] or "") for x in r["data"]["rows"]) <= CLIP,
              f"longest note = {max(len(x[0] or '') for x in r['data']['rows'])}")

    # ------------------------------------------------------------------ all tools: string-length + leak sweep
    sweep = [("list_sessions", {"day": busiest}), ("get_metric", {"metric": "V", "day": v_days[0]}),
             ("get_metric", {"metric": "C", "day": v_days[0]}), *[("get_turn", {"turn_id": t}) for t in ids]]
    worst = 0
    leaks = 0
    for name, inp in sweep:
        r = T(name, inp)
        leaks += probe.leaks(r)
        for cit in r.get("citations", []):
            worst = max(worst, len(cit["label"]))
        if name == "get_turn" and r["ok"]:
            d = r["data"]
            worst = max(worst, len(d["prompt"] or ""), *[len(x["args"] or "") for x in d["tool_calls"]] or [0],
                        *[len(x["file"] or "") for x in d["file_edits"]] or [0])
        worst = max(worst, len(r.get("summary", "")))
    check("privacy", "get_turn/get_metric/list_sessions: no tail leaks", leaks == 0, f"leaks={leaks}")
    check("privacy", "prompt/args/file/labels/summary ≤ 80 chars", worst <= CLIP, f"longest={worst}")

    # ------------------------------------------------------------------ HTTP edge cases
    check("http", "unknown tool -> 404", T.status("nope", b'{"input":{}}') == 404, "")
    check("http", "bad body -> 400", T.status("query_db", b'not json') == 400, "")
    check("http", "n0=-1 -> 400", T.status("get_turn", b'{"input":{"turn_id":1},"n0":-1}') == 400, "")
    check("http", "input not object -> 400", T.status("get_turn", b'{"input":[1]}') == 400, "")

    npass = sum(r["ok"] for r in RESULTS)
    print(f"\n{npass}/{len(RESULTS)} passed")
    by_group = defaultdict(lambda: [0, 0])
    for r in RESULTS:
        by_group[r["group"]][0] += r["ok"]
        by_group[r["group"]][1] += 1
    for g, (p, n) in by_group.items():
        print(f"  {g:18} {p}/{n}")
    if a.json:
        Path(a.json).write_text(json.dumps(RESULTS, indent=1))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
