import json, os, re, glob, statistics as st
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta

ROOT = os.path.expanduser("~/.claude/projects")
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=7)

def pts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def pct(xs, p):
    if not xs: return 0
    xs = sorted(xs); return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]

def load(path):
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try: out.append(json.loads(line))
                except Exception: pass
    except Exception: pass
    return out

def in_window(es):
    for e in es:
        t = pts(e.get("timestamp") or "")
        if t and t >= CUTOFF: return True
    return False

def text_of(e):
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str): return c
    if isinstance(c, list):
        ts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if ts: return "\n".join(ts)
    return None

def is_prompt(e):
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"): return None
    t = text_of(e)
    if t is None or not t.strip() or t.lstrip().startswith("<"): return None
    return t

INT = "[Request interrupted by user"
TESTRE = re.compile(r"pytest|vitest|jest|npm test|pnpm test|\btsc\b|eslint|ruff|cargo test|go test|mypy")
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CORR = re.compile(r"^\s*(no\b|nope|wrong|not\b|don't|dont|actually|instead|stop|why did|that's not|thats not|nein|nicht|falsch|stattdessen)", re.I)
BUCKETS = [("<30s", 30), ("30s-2m", 120), ("2-5m", 300), ("5-15m", 900), ("15-60m", 3600), (">60m", 1e18)]
def bucket(s):
    for n, hi in BUCKETS:
        if s < hi: return n

files = [p for p in glob.glob(ROOT + "/*/*.jsonl")]
sessions = {}
for p in files:
    es = load(p)
    if es and in_window(es): sessions[p] = es

D = lambda: defaultdict(int)
day = lambda t: t.strftime("%Y-%m-%d")
sess_days = defaultdict(set); prompts_d = D(); turns_d = D()
seen_req = set(); reqs = []  # (day, ctx, model, usage tuple)
compact = Counter(); compact_d = defaultdict(Counter)
gaps = []; gap_d = defaultdict(list); agent_times = []; agent_d = defaultdict(float)
turn_dur_ms = 0
interrupts = 0; thrown = 0.0; int_d = D()
tool_counts = Counter(); tool_err = Counter(); tool_res = Counter(); loops = Counter()
test_total = 0; test_after_edit = 0; test_examples = Counter()
edit_calls = Counter(); edit_files = set(); churn = []; user_mod = 0; edit_result_n = 0
models = Counter(); efforts = Counter(); eff_keys = Counter()
cost_files = {}; cost_keys = set()
corr_n = corr_den = 0; corr_examples = []
tok_d = defaultdict(lambda: [0, 0, 0, 0]); usage_total = [0, 0, 0, 0]
first_ts_by_sess = {}

for p, es in sessions.items():
    sid = es[0].get("sessionId") or os.path.basename(p)
    id2name = {}; seq = []  # ordered tool results (name, is_err)
    prompts = []  # (idx, ts, text, is_int)
    tool_idx = 0; last_edit_idx = -99; turn_no = 0
    file_turns = defaultdict(set)
    ents = []  # (ts, type, e)
    for i, e in enumerate(es):
        t = pts(e.get("timestamp") or "")
        ty = e.get("type")
        if ty == "cost-state":
            cost_files[p] = e; cost_keys |= set(e.keys()); continue
        if not t: continue
        if t < CUTOFF: continue
        d = day(t)
        sess_days[sid].add(d)
        ents.append((t, ty, e))
        if ty == "system":
            st_ = e.get("subtype")
            if st_ == "turn_duration":
                turn_dur_ms += (e.get("durationMs") or 0)
            elif st_ == "compact_boundary":
                trig = (e.get("compactMetadata") or {}).get("trigger", "?")
                compact[trig] += 1; compact_d[d][trig] += 1
        if ty == "assistant":
            m = e.get("message") or {}
            rid = e.get("requestId") or e.get("uuid")
            if rid not in seen_req:
                seen_req.add(rid)
                u = m.get("usage") or {}
                a, b, c, o = (u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                              u.get("cache_creation_input_tokens") or 0, u.get("output_tokens") or 0)
                if m.get("model") != "<synthetic>":
                    reqs.append((d, a + b + c))
                    models[m.get("model")] += 1
                    ef = e.get("effort", m.get("effort")); efforts[str(ef)] += 1
                    for k in ("effort", "effortLevel", "thinkingLevel"):
                        if k in e or k in m: eff_keys[k] += 1
                    for j, v in enumerate((a, b, c, o)):
                        tok_d[d][j] += v; usage_total[j] += v
            cont = m.get("content")
            if isinstance(cont, list):
                for b in cont:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in id2name:
                        nm = b.get("name"); id2name[b["id"]] = nm
                        tool_counts[nm] += 1; tool_idx += 1
                        inp = b.get("input") or {}
                        if nm in EDITS:
                            edit_calls[nm] += 1; last_edit_idx = tool_idx
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp:
                                edit_files.add(fp); file_turns[fp].add(turn_no)
                        if nm == "Bash":
                            cmd = inp.get("command") or ""
                            mm = TESTRE.search(cmd)
                            if mm:
                                test_total += 1; test_examples[mm.group(0)] += 1
                                if 0 < tool_idx - last_edit_idx <= 3: test_after_edit += 1
        elif ty == "user":
            c = (e.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        nm = id2name.get(b.get("tool_use_id"), "?")
                        err = bool(b.get("is_error"))
                        tool_res[nm] += 1; tool_err[nm] += err
                        seq.append((nm, err))
            tur = e.get("toolUseResult")
            if isinstance(tur, dict) and "userModified" in tur:
                edit_result_n += 1; user_mod += bool(tur.get("userModified"))
            pt = is_prompt(e)
            if pt is not None:
                isint = pt.startswith(INT)
                prompts.append((len(ents) - 1, t, pt, isint))
                if not isint: turn_no += 1
    # error loops
    run = 0; prev = None
    for nm, err in seq + [(None, False)]:
        if err and nm == prev: run += 1
        elif err: run = 1
        else:
            run = 0
        prev = nm if err else None
        if err and run == 3: loops[nm] += 1  # count each run once at length 3
    for fp, ts_ in file_turns.items():
        if len(ts_) >= 3: churn.append((fp, len(ts_)))
    # prompts / turns
    real = [x for x in prompts if not x[3]]
    for idx, t, txt, isint in prompts:
        prompts_d[day(t)] += 1
    for idx, t, txt, isint in real: turns_d[day(t)] += 1
    first = True
    for k, (idx, t, txt, isint) in enumerate(prompts):
        if not first:
            corr_den += 1
            if isint or CORR.search(txt):
                corr_n += 1
                if len(corr_examples) < 3 and not isint: corr_examples.append(txt.strip()[:60].replace("\n", " "))
        first = False
    interrupts += sum(1 for x in prompts if x[3]);
    for x in prompts:
        if x[3]: int_d[day(x[1])] += 1
    # turns: from each real prompt to next real prompt; interrupt markers belong to current turn
    for k, (idx, t, txt, isint) in enumerate(real):
        end = real[k + 1][0] if k + 1 < len(real) else len(ents)
        seg = ents[idx + 1:end]
        agent = [(tt, ty) for tt, ty, ee in seg if ty == "assistant" or (ty == "user" and isinstance((ee.get("message") or {}).get("content"), list)
                 and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in ee["message"]["content"]))]
        asst = [tt for tt, ty in agent if ty == "assistant"]
        if not asst: continue
        dur = (agent[-1][0] - asst[0]).total_seconds()
        agent_times.append(dur); agent_d[day(t)] += dur
        interrupted = any(ee_ty == "user" and (is_prompt(ee) or "").startswith(INT) for _, ee_ty, ee in seg)
        if interrupted: thrown += dur
        if k + 1 < len(real):
            g = (real[k + 1][1] - agent[-1][0]).total_seconds()
            if g >= 0: gaps.append(g); gap_d[day(real[k + 1][1])].append(g)

# subagents
sub_n = 0; sub_models = Counter(); sub_meta_n = 0
for p in sessions:
    sdir = os.path.join(os.path.dirname(p), os.path.splitext(os.path.basename(p))[0], "subagents")
    for sp in glob.glob(sdir + "/*.jsonl"):
        sub_n += 1
        mp = sp[:-6] + ".meta.json"
        try:
            m = json.load(open(mp)); sub_meta_n += 1; sub_models[str(m.get("model"))] += 1
        except Exception: pass

# cost
cost_d = defaultdict(lambda: defaultdict(float)); cost_tot = defaultdict(float)
for p, e in cost_files.items():
    t = pts(e.get("timestamp") or "")
    dd = day(t) if t else day(datetime.fromtimestamp(os.path.getmtime(p), timezone.utc))
    for k in ("totalCostUSD", "totalAPIDuration", "totalToolDuration", "totalLinesAdded", "totalLinesRemoved"):
        v = e.get(k) or 0
        cost_d[dd][k] += v; cost_tot[k] += v

days = sorted({d for s in sess_days.values() for d in s})
P = print
P(f"== Claude Code usage exploration | window {CUTOFF:%Y-%m-%d %H:%M}Z..{NOW:%Y-%m-%d %H:%M}Z | all day bucketing UTC ==")
P(f"main sessions in window: {len(sessions)} | subagent files: {sub_n} (meta present {sub_meta_n}) models: {dict(sub_models)}")
P("\n[1] per day: sessions / human prompts(incl interrupt markers) / turns(real prompts)")
for d in days:
    P(f"  {d}: sess={sum(1 for s in sess_days.values() if d in s)} prompts={prompts_d[d]} turns={turns_d[d]}")
P(f"  TOTAL: sess={len(sess_days)} prompts={sum(prompts_d.values())} turns={sum(turns_d.values())}")

P("\n[2] context fill per API request (deduped by requestId)")
cs = [c for _, c in reqs]
def ctxline(xs):
    return f"n={len(xs)} p50={pct(xs,50)} p90={pct(xs,90)} max={max(xs) if xs else 0} >100k={sum(x>100000 for x in xs)/max(1,len(xs)):.1%} >150k={sum(x>150000 for x in xs)/max(1,len(xs)):.1%}"
for d in days:
    P(f"  {d}: " + ctxline([c for dd, c in reqs if dd == d]))
P("  TOTAL: " + ctxline(cs))
P(f"  compactions: {dict(compact)} | per day: " + ", ".join(f"{d}:{dict(compact_d[d])}" for d in days if compact_d[d]))

P("\n[3] idle gaps (last assistant/tool entry of turn -> next real prompt)")
P("  TOTAL: " + ", ".join(f"{n}={sum(1 for g in gaps if bucket(g)==n)}" for n, _ in BUCKETS) + f" (n={len(gaps)}, median={pct(gaps,50):.0f}s, max={max(gaps, default=0)/3600:.1f}h)")
for d in days:
    g = gap_d[d]
    if g: P(f"  {d}: " + ", ".join(f"{n}={sum(1 for x in g if bucket(x)==n)}" for n, _ in BUCKETS))
P(f"  agent-running time/turn: n={len(agent_times)} p50={pct(agent_times,50):.0f}s p90={pct(agent_times,90):.0f}s max={max(agent_times, default=0):.0f}s sum={sum(agent_times)/3600:.2f}h | sum of system turn_duration={turn_dur_ms/3.6e6:.2f}h")
P("  agent hours/day: " + ", ".join(f"{d}:{agent_d[d]/3600:.2f}" for d in days))

P("\n[4] interruptions")
P(f"  interrupt markers={interrupts} ({interrupts/max(1,sum(prompts_d.values())):.1%} of prompts) agent time in interrupted turns={thrown/60:.1f}min per day: {dict(int_d)}")

P("\n[5] tools (count, results, is_error rate)")
P("  " + "; ".join(f"{n}:{c} ({tool_err[n]}/{tool_res[n]}={tool_err[n]/max(1,tool_res[n]):.0%})" for n, c in tool_counts.most_common(14)))
P(f"  total calls={sum(tool_counts.values())} errors={sum(tool_err.values())} ({sum(tool_err.values())/max(1,sum(tool_res.values())):.1%})")
P(f"  error loops (>=3 consecutive same-tool errors): {sum(loops.values())} {dict(loops)}")
P(f"  test/lint/typecheck Bash cmds={test_total} {dict(test_examples)}; within 3 tool calls after Edit/Write={test_after_edit}")

P("\n[6] edits")
P(f"  calls={dict(edit_calls)} distinct files={len(edit_files)} churn files(>=3 turns in a session)={len(churn)} top={sorted(churn, key=lambda x:-x[1])[:3]}")
P(f"  userModified true={user_mod} of {edit_result_n} results carrying the field")

P("\n[7] subagents: see header line (files under <session>/subagents/)")

P("\n[8] tokens/cost (deduped usage: input / cache_read / cache_create / output)")
def hit(t): return t[1] / max(1, t[0] + t[1] + t[2])
for d in days:
    t = tok_d[d]; P(f"  {d}: in={t[0]} cr={t[1]} cc={t[2]} out={t[3]} cache_hit={hit(t):.1%}")
P(f"  TOTAL: in={usage_total[0]} cr={usage_total[1]} cc={usage_total[2]} out={usage_total[3]} cache_hit={hit(usage_total):.1%}")
P(f"  cost-state keys seen: {sorted(cost_keys)}; files with cost-state: {len(cost_files)}/{len(sessions)}")
P("  cost-state (last per file, whole-session running totals, attributed to day of cost-state ts/mtime; sessions spanning window start are over-counted):")
for d in sorted(cost_d): P(f"    {d}: " + ", ".join(f"{k}={v:.1f}" for k, v in cost_d[d].items()))
P("    TOTAL: " + ", ".join(f"{k}={v:.1f}" for k, v in cost_tot.items()))

P("\n[9] models (requests) / effort field")
P(f"  {dict(models)}\n  effort values: {dict(efforts)} keys present: {dict(eff_keys)}")

P("\n[10] correction heuristic")
P(f"  {corr_n}/{corr_den} = {corr_n/max(1,corr_den):.1%} of non-first prompts; examples (60 chars): {corr_examples}")
