# scorer demo recording

Scripted, recordable click-through of the scorer dashboard for a demo video. Everything lives in `demo/`;
nothing here touches `src/`.

## Run

```bash
cd demo
npm i                              # playwright 1.56.1, local to demo/
npx playwright install chromium    # once
# scorer must be serving at http://127.0.0.1:8811 (override with SCORER_URL / SCORER_DAY)
node record.mjs                    # -> out/walkthrough.webm, out/walkthrough.mp4, out/stills/*.png
node render_titles.mjs             # -> out/titles/*.png (title cards) and out/subs/*.png (subtitle strips)
bash assemble.sh                   # -> out/demo.mp4  (titles + walkthrough + narration.srt burned in)
```

`npm run all` runs the three steps in order.

Note: `record.mjs` is being reworked in parallel into a narration-driven cut (it reads `out/vo/durations.json`
and writes `out/marks.json`). The beat-based recorder this README describes, verified end-to-end on
2026-10-14, is kept as `record.beats.mjs`; run `node record.beats.mjs` for that version.

What the recorder does:

- chromium headless, 1920x1080, deviceScaleFactor 2, light theme (`?theme=light`), `slowMo` 60 ms
- injects a fake cursor (`#__cursor`, follows `mousemove` with a CSS transition) and a clay highlight ring on
  every `mousedown` target (`.__ring`), so clicks are visible in the video
- waits up to 60 s for the server before starting and retries `page.goto` for 60 s (the server may restart)
- each beat is isolated: a failing beat is logged and the script continues; a PNG of every beat goes to
  `out/stills/NN-<beat>.png` (usable as slides)
- the Ask AI answer is awaited for up to 60 s; if it never finishes, the script holds 10 s and continues
- converts the .webm to .mp4 with ffmpeg when ffmpeg is on PATH

## Beats and timings

All timings are in the `T` object at the top of `record.mjs` (milliseconds):

| key                 | default | meaning                                                            |
|---------------------|---------|--------------------------------------------------------------------|
| `slowMo`            | 60      | Playwright delay per action                                        |
| `typeDelay`         | 75      | per character while typing the question                            |
| `settle`            | 600     | after a tab switch                                                 |
| `hoverHold`         | 1800    | how long a hover (turn link, timeline popover) stays on screen     |
| `connectRetry`      | 60000   | server wait / goto retry budget                                    |
| `chatTimeout`       | 60000   | max wait for the streamed answer                                   |
| `chatFallback`      | 10000   | pause if the answer never arrives                                  |
| `beats.<name>`      | 1500-3000 | pause at the end of each beat (narration room)                   |

Beat order: `today` -> `timeline` -> `sessions` -> `trends` -> `leverage` -> `ask-open` -> `ask-typed` -> `ask-answer`.
To lengthen a beat for narration, raise `T.beats.<name>`. To change the question, edit `QUESTION`.
The last run produced a 71.9 s walkthrough.

## Selectors used (from the live DOM / app.js)

| beat        | selector                                                    | note |
|-------------|-------------------------------------------------------------|------|
| boot        | `#askbtn[aria-pressed="true"]`                              | `boot()` auto-opens the chat when `innerWidth >= 1440`; the script clicks `#askbtn` to close it first |
| today       | `#narrative h1, #narrative .narr`                           | deterministic verdict `<h1>` or the analyst narrative `.md.narr` (replaces the h1 once loaded) |
| today       | `.band .stat` (count 4)                                     | the four scored cards |
| today       | `.band .stat .evid button[data-turn]`                       | turn link that is hovered (click would open the turn detail) |
| timeline    | `.timeline-panel`, `#timeline svg rect.bar[data-turn]`      | scrolled inside `#maincol` (the window does not scroll); widest bar is hovered |
| timeline    | `#pop.show`                                                 | hover popover (arms 250 ms after mouseover) |
| tabs        | `.tabs a[data-tab="today\|sessions\|trends\|leverage"]`     | active tab has `aria-current="page"` |
| sessions    | `table.sess tr[data-session]`                               | first row is hovered |
| trends      | `.seg button[data-range="30"]`, `.multiples`                | pressed state `aria-pressed="true"` |
| leverage    | `#skillbars`, `.seg button[data-skillcat="skill"]`          | category filter clicked |
| ask         | `#askbtn`, `#ask.open`, `#log [data-suggest]`               | suggested questions in the empty chat |
| ask         | `#input` (placeholder "Ask about this day..."), `#send.ready` | typing fires `input`, which enables send |
| ask         | `#send.stop`, `.msg.assistant .md`                          | streaming indicator, rendered answer |

Chat endpoint: `POST /api/chat` (SSE); availability from `GET /api/chat/status`. Without `ANTHROPIC_API_KEY`
on the server the suggestions are disabled and the answer beat falls back to a 10 s pause.

## Title cards

`titles/*.html` are 1920x1080 cards using the dashboard tokens from `app.css` (`_title.css`: pine `#1c2d22`,
grass `#316c5a`, clay `#b36813`, salmon `#ffb299`, app background `#fbf6ee`, card `#fefcf8`, border `#ddd9d3`,
same font stacks). `render_titles.mjs` screenshots them at 1x into `out/titles/`, and also renders every cue of
`narration.srt` as a transparent strip into `out/subs/` (see below).

## Assembly

`assemble.sh` uses only ffmpeg: it loops the three opening cards for `TITLE_SECONDS` (default 3) each, appends
`out/walkthrough.mp4`, appends the closing card, and burns `narration.srt` in.

- Cue times in `narration.srt` are relative to the start of the walkthrough; the script shifts them by
  `3 * TITLE_SECONDS` automatically.
- If ffmpeg has the `subtitles` filter (libass) it is used directly. Homebrew ffmpeg ships without libass and
  libfreetype, so the script falls back to overlaying the pre-rendered strips from `out/subs/` with
  `overlay=enable='between(t,a,b)'`. Either way the SRT is also muxed as a soft `mov_text` track.
- Replace the six placeholder cues in `narration.srt` with the real narration, re-run `node render_titles.mjs`
  (to refresh the strips) and `bash assemble.sh`.
