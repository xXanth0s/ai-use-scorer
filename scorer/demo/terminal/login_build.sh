#!/usr/bin/env bash
# Stitch the local login beat: demo/out/login-local.mp4 (1920x1080, 30 fps) + demo/out/login.marks.json
# Inputs: out/login-web.webm + out/login-web.marks.json (node demo/record.login.mjs),
#         out/login-term.mp4 (bash demo/terminal/login_record.sh), out/login-badge.png (node demo/record.login.mjs --badge)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT/demo/out"
read -r SS SE WS WE < <(jq -r '"\(.setup_start) \(.setup_end) \(.sessions_start) \(.sessions_end)"' login-web.marks.json)
# terminal cut points (seconds in login-term.mp4; see LOGIN-SHOTLIST.md "Local fallback")
I0=${I0:-2.4}; I1=${I1:-6.3}; HOLD=${HOLD:-0.5}        # install: mid-typing -> diff + "claude: wrote ..." (+ still hold)
C0=${C0:-11.6}; C1=${C1:-12.6}; C2=${C2:-37.0}; C3=${C3:-38.4}; SPEED=${SPEED:-12}  # claude run, middle sped up
A0=$(python3 -c "print(round($SS+0.3,2))"); A1=$(python3 -c "print(round(min($SE-0.6,$SS+3.9),2))")
TMP=$(mktemp -d)
enc() { ffmpeg -v error -y "$@" -r 30 -c:v libx264 -crf 16 -preset medium -pix_fmt yuv420p -an; }
N='scale=1920:1080:flags=lanczos,fps=30,setsar=1'
enc -ss "$A0" -to "$A1" -i login-web.webm -vf "$N" "$TMP/1.mp4"
enc -ss "$I0" -to "$I1" -i login-term.mp4 -vf "$N,tpad=stop_mode=clone:stop_duration=$HOLD" "$TMP/2.mp4"
enc -ss "$C0" -to "$C1" -i login-term.mp4 -vf "$N" "$TMP/3a.mp4"
enc -ss "$C1" -to "$C2" -i login-term.mp4 -i login-badge.png -filter_complex \
  "[0:v]setpts=(PTS-STARTPTS)/$SPEED,$N[v];[v][1:v]overlay=W-w-60:560" "$TMP/3b.mp4"
enc -ss "$C2" -to "$C3" -i login-term.mp4 -vf "$N" "$TMP/3c.mp4"
enc -ss "$WS" -to "$WE" -i login-web.webm -vf "$N" "$TMP/4.mp4"
: > "$TMP/list.txt"; for f in 1 2 3a 3b 3c 4; do echo "file '$TMP/$f.mp4'" >> "$TMP/list.txt"; done
ffmpeg -v error -y -f concat -safe 0 -i "$TMP/list.txt" -c:v libx264 -crf 16 -pix_fmt yuv420p -r 30 -movflags +faststart login-local.mp4
python3 - "$TMP" <<'PY'
import json, subprocess, sys
tmp = sys.argv[1]
d = lambda f: float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",f"{tmp}/{f}.mp4"]))
names = [("setup","1","local /setup: Codex + Claude Code detected, capture-hook consent line"),
         ("install","2","real `scorer install` into /tmp/scorer-login/.claude/settings.json (diff + wrote)"),
         ("agent_run","3a","sandboxed `claude -p` typed + Enter (1x)"),
         ("agent_run_fast","3b","same run, sped up with on-screen badge; hooks stream into bottom pane"),
         ("events","3c","reply + all 6 hook events incl. SessionEnd (1x)"),
         ("sessions","4","Sessions page filtered to demo sessions (acme-api)")]
t, segs = 0.0, []
for key, f, what in names:
    dur = d(f); segs.append({"id": key, "start": round(t,3), "end": round(t+dur,3), "what": what}); t += dur
json.dump({"file": "demo/out/login-local.mp4", "duration": round(t,3), "segments": segs}, open("login.marks.json","w"), indent=2)
print(json.dumps(segs))
PY
rm -rf "$TMP"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -show_entries format=duration -of compact login-local.mp4
