"""Regenerates the hand-written Claude golden transcripts g1..g9 (python tests/golden/make_claude_goldens.py)."""
import json, itertools
from datetime import datetime, timedelta, timezone
import os
OUT=os.path.dirname(os.path.abspath(__file__))+"/"
T0=datetime(2026,10,1,8,0,0,tzinfo=timezone.utc)
def ts(s): return (T0+timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z"
class W:
  def __init__(s, sid, cwd="/w/repo", t0=0):
    s.sid=sid; s.cwd=cwd; s.rows=[]; s.n=itertools.count(1); s.req=itertools.count(1); s.t0=t0
  def base(s, typ, sec, **kw):
    e={"type":typ,"uuid":f"{s.sid[:4]}-{next(s.n):04d}","timestamp":ts(s.t0+sec),"sessionId":s.sid,"cwd":s.cwd,"gitBranch":"main","isSidechain":False}
    e.update(kw); s.rows.append(e); return e
  def prompt(s, sec, text, **kw): return s.base("user", sec, message={"role":"user","content":text}, promptSource=kw.pop("promptSource","typed"), **kw)
  def asst(s, sec, blocks, rid=None, usage=None, model="claude-opus-5-5"):
    rid=rid or f"req_{s.sid[:4]}_{next(s.req)}"
    u=usage or {"input_tokens":10,"cache_read_input_tokens":1000,"cache_creation_input_tokens":50,"output_tokens":20}
    return s.base("assistant", sec, requestId=rid, effort="high", message={"role":"assistant","model":model,"usage":u,"content":blocks})
  def text(s, sec, t="ok", **kw): return s.asst(sec, [{"type":"text","text":t}], **kw)
  def tool(s, sec, tid, name, inp, **kw): return s.asst(sec, [{"type":"tool_use","id":tid,"name":name,"input":inp}], **kw)
  def result(s, sec, tid, err=False, tur=None):
    e=s.base("user", sec, message={"role":"user","content":[{"type":"tool_result","tool_use_id":tid,"content":"out","is_error":err}]})
    if tur is not None: e["toolUseResult"]=tur
    return e
  def td(s, sec, dur_s): return s.base("system", sec, subtype="turn_duration", durationMs=int(dur_s*1000), messageCount=3)
  def interrupt(s, sec): return s.base("user", sec, message={"role":"user","content":[{"type":"text","text":"[Request interrupted by user]"}]})
  def q(s, sec, op, content=None):
    e={"type":"queue-operation","operation":op,"timestamp":ts(s.t0+sec),"sessionId":s.sid}
    if content is not None: e["content"]=content
    s.rows.append(e)
  def write(s, name):
    with open(OUT+name,"w") as f:
      for r in s.rows: f.write(json.dumps(r)+"\n")

# 1: gaps 10/90/200 s. Each turn: prompt at p, assistant p+1, td ending p+20.
w=W("11111111-0000-0000-0000-000000000001"); p=0
for gap in (10,90,200,None):
  w.prompt(p, f"do step at {p}"); w.text(p+1); w.td(p+20, 20)
  if gap is not None: p=p+20+gap
w.write("g1_gaps.jsonl")

# 2: one requestId over 3 assistant lines
w=W("22222222-0000-0000-0000-000000000002")
w.prompt(0,"explain"); u={"input_tokens":5,"cache_read_input_tokens":2000,"cache_creation_input_tokens":100,"output_tokens":300}
w.asst(1,[{"type":"thinking","thinking":"hm"}],rid="req_same",usage=u)
w.asst(2,[{"type":"text","text":"a"}],rid="req_same",usage=u)
w.asst(3,[{"type":"tool_use","id":"toolu_g2","name":"Read","input":{"file_path":"/w/repo/a.py"}}],rid="req_same",usage=u)
w.result(4,"toolu_g2"); w.text(5,rid="req_two"); w.td(6,6)
w.write("g2_request_dedupe.jsonl")

# 4: queued prompt: enqueued at 5 s during turn 1, turn 1 ends at 30 s, dequeued at 30.1 s
w=W("44444444-0000-0000-0000-000000000004")
w.prompt(0,"build the thing"); w.text(1); w.q(5,"enqueue","also add docs"); w.text(25); w.td(30,30)
w.q(30.1,"dequeue"); w.prompt(30.2,"also add docs",promptSource="queued"); w.text(31); w.td(40,9.8)
w.write("g4_queued.jsonl")

# 5: interrupt: first assistant at 2 s, marker at 47 s -> 45 s thrown; next prompt at 60 s
w=W("55555555-0000-0000-0000-000000000005")
w.prompt(0,"refactor everything"); w.tool(2,"toolu_g5a","Bash",{"command":"ls -la"}); w.result(3,"toolu_g5a")
w.tool(30,"toolu_g5b","Bash",{"command":"sleep 100"}); w.result(47,"toolu_g5b",err=True); w.interrupt(47)
w.prompt(60,"no, only the parser"); w.text(61); w.td(70,10)
w.write("g5_interrupt.jsonl")

# 7: userModified edit
w=W("77777777-0000-0000-0000-000000000007")
w.prompt(0,"fix the bug")
w.tool(1,"toolu_g7","Edit",{"file_path":"/w/repo/src/a.py","old_string":"x = 1\n","new_string":"x = 2\ny = 3\n"})
w.result(2,"toolu_g7",tur={"filePath":"/w/repo/src/a.py","userModified":True,"structuredPatch":[{"oldStart":1,"oldLines":1,"newStart":1,"newLines":2,"lines":["-x = 1","+x = 2","+y = 3"]}]})
w.tool(3,"toolu_g7b","Write",{"file_path":"/w/repo/src/b.py","content":"a\nb\nc\n"})
w.result(4,"toolu_g7b",tur={"filePath":"/w/repo/src/b.py","userModified":False})
w.text(5); w.td(6,6)
w.write("g7_user_modified.jsonl")

# 8: two sessions overlapping 120 s, different cwd. A: 0..300, B: 180..480
a=W("88888888-0000-0000-0000-00000000000a", cwd="/w/repo-a"); a.prompt(0,"task a"); a.text(1); a.text(299.5); a.td(300,300); a.write("g8_overlap_a.jsonl")
b=W("88888888-0000-0000-0000-00000000000b", cwd="/w/repo-b"); b.prompt(180,"task b"); b.text(181); b.text(479.5); b.td(480,300); b.write("g8_overlap_b.jsonl")

# tooling: slash command expanding a skill, Skill tool, mcp tool, attributionSkill, test run requested by prompt
w=W("99999999-0000-0000-0000-000000000009")
w.prompt(0,"<command-message>superpowers:brainstorming</command-message>\n<command-name>/superpowers:brainstorming</command-name>")
w.base("user",0.1,isMeta=True,message={"role":"user","content":[{"type":"text","text":"Base directory for this skill: /x"}]})
w.text(1); w.td(5,5)
w.prompt(10,"run the tests please")
w.tool(11,"toolu_g9a","Skill",{"skill":"mattpocock-skills:tdd"})
w.result(12,"toolu_g9a")
e=w.tool(13,"toolu_g9b","mcp__linear-server__get_issue",{"id":"X-1"}); e["attributionSkill"]="mattpocock-skills:tdd"; e["attributionMcpServer"]="linear-server"
w.result(14,"toolu_g9b")
e=w.tool(15,"toolu_g9c","Bash",{"command":"uv run pytest -q"}); e["attributionSkill"]="review-loop"
w.result(20,"toolu_g9c")
w.text(21); w.td(22,12)
w.prompt(30,"<command-name>/clear</command-name>\n<command-message>clear</command-message>")
w.write("g9_tooling.jsonl")
print("ok")
