"""Map wall-clock stamps (/tmp/scorer-harness/stamps.txt) to harness.mp4 time -> harness.marks.json."""
import json, subprocess
S = "/tmp/scorer-harness/stamps.txt"
rows = [l.split(" ", 1) for l in open(S).read().splitlines() if l.strip()]
rows = [(float(t), what) for t, what in rows]
show = next(t for t, w in rows if w == "show")
end_wall = next(t for t, w in rows if w == "end")
def first(pred):
    return next(t for t, w in rows if t >= show and pred(w))
def probe(f):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", f]).decode())
raw = probe("/tmp/scorer-harness-raw.mp4")  # terminal part (card appended after it)
dur = probe("demo/out/harness.mp4")
marks = {
    "hooks_start": first(lambda w: "demo_e2e" in w),
    "hooks_end": first(lambda w: w == "sessionend"),
    "ingest": first(lambda w: "scorer ingest" in w),
    "judge": first(lambda w: "scorer judge" in w),
    "export": first(lambda w: "scorer export" in w),
}
marks["sync"] = first(lambda w: "scorer sync" in w)
after = lambda t0: next(t for t, w in rows if t > t0 and w == "prompt")
marks["export_done"] = after(marks["export"])
marks["sync_done"] = after(marks["sync"])
marks["done"] = marks["sync_done"]
# vhs captures slightly slower than wall clock; map linearly: show -> 0, end stamp -> video end
scale = raw / (end_wall - show)
marks = {k: round((v - show) * scale, 2) for k, v in marks.items()}
marks["vercel_card"] = round(raw, 2)
marks["end"] = round(dur, 2)
marks["scale"] = round(scale, 4)  # video s per wall s during capture
json.dump(marks, open("demo/out/harness.marks.json", "w"), indent=2)
print(json.dumps(marks), f"scale={scale:.4f}")
