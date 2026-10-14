#!/usr/bin/env bash
# narration.txt -> out/vo/<id>.m4a + out/vo/durations.json. VOICE/RATE overridable.
set -euo pipefail
cd "$(dirname "$0")"
VOICE="${VOICE:-Samantha}"; RATE="${RATE:-150}"
mkdir -p out/vo
echo "{" > out/vo/durations.json.tmp; first=1
grep -v '^#' narration.txt | grep -v '^$' | while IFS='|' read -r id cap spoken; do
  text="${spoken:-$cap}"
  if [[ "$text" == *"[N]"* || "$text" == *"[\$N]"* ]]; then echo "skip $id (unfilled [N])" >&2; continue; fi
  say -v "$VOICE" -r "$RATE" -o "out/vo/$id.aiff" "$text" </dev/null
  ffmpeg -nostdin -v error -y -i "out/vo/$id.aiff" -c:a aac -b:a 160k -ar 48000 "out/vo/$id.m4a"
  d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "out/vo/$id.m4a")
  [ $first = 1 ] && first=0 || echo "," >> out/vo/durations.json.tmp
  printf '  "%s": %s' "$id" "$d" >> out/vo/durations.json.tmp
done
echo; echo "}" >> out/vo/durations.json.tmp
mv out/vo/durations.json.tmp out/vo/durations.json
cat out/vo/durations.json
