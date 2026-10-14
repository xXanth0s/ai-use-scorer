# Login beat: shot lists

Beat goal: sign in, connect your agent once, and from then on hooks capture every session. The beat has no voice-over. Text overlays sit in the bottom-left ~500x160 px of the frame, so keep key content out of that corner.

- **Hosted (A).** You record this yourself on https://how-do-you-ai.vercel.app. It needs your own account, so no agent logs in for you.
- **Local fallback (B).** Already rendered as `demo/out/login-local.mp4`, about 15.7 s. Re-recording steps are at the end of this file.

---

## A. Hosted variant, about 30 s raw (cut to about 15 s)

### Before you record
- Use a fresh Chrome profile or a guest window with no extensions, bookmarks bar or other tabs. Turn on light mode (System Settings > Appearance > Light).
- Set the browser window to 1920x1080 CSS px. Either use full screen on a 1920x1080 display, or run
  `osascript -e 'tell application "Google Chrome" to set bounds of front window to {0, 0, 1920, 1080}'`.
  On a Retina laptop, use a scaled resolution where the window still measures 1920x1080 points.
- Zoom: 110 % makes the setup cards easier to read on video.
- Hide the dock (`⌥⌘D`) and notifications (Focus > Do Not Disturb).
- Open Claude Code in a terminal next to the browser, or in a second space. Use the same theme as the other terminal shots if you can (light, JetBrains Mono).
- Have a fresh invitation link ready (invitations expire after 15 minutes). The setup message also contains a one-time pairing code that expires after 15 minutes. Record the whole take inside that window.

### Capture
```bash
cd ~/Projects/Private/ai-use-scorer
screencapture -v -V 40 demo/out/login-raw.mov   # 40 s, whole screen; press Ctrl-C to stop early
```
QuickTime alternative: File > New Screen Recording (`⇧⌘5`) > Record Selected Portion. Drag the selection to the browser window, then under Options set "Save to" to `demo/out/`. Rename the result to `demo/out/login-raw.mov`.

### Shots (seconds into the raw take)

| # | t (s) | Action | Must be visible | Hide (blur/crop) |
|---|---|---|---|---|
| 1 | 0–4 | Open the invitation link. The Clerk "Sign in" card appears. Sign in with your existing session or Google button. Don't type a password on camera. | how do you ai logo, Clerk sign-in card | Your email in the Clerk field and the account chooser (blur) |
| 2 | 4–8 | You land on **/setup**: "HOW DO YOU AI · SETUP", "Connect your AI workspace.", and the step rail *Sign up → Choose agent → Copy setup message → Approve installation → Connected*. Wait until the heading reads **"Choose your agent."** | Step rail and the two buttons **Connect Codex** / **Connect Claude Code** | Right column "YOUR ACCOUNT" only if it shows usage you don't want shown |
| 3 | 8–10 | Click **Connect Claude Code**. The message is copied automatically, and the heading becomes **"Your setup message is copied."** | Heading, the **Copy setup message / Copy again** button, the line "Private pairing · expires in 15 minutes" | — |
| 4 | 10–13 | Optional: open **View setup message** for about 2 s so viewers can see that it is a message to paste. | First lines of the textarea ("Set up the how do you ai plugin for Claude Code on this computer. Download …") | **Must blur:** the whole sentence containing the one-time invitation code ("…pairing helper, never in command arguments or logs: `<code>`"). It is a live credential. Blur it in the recording (see below), not only in the edit. If unsure, skip this shot. |
| 5 | 13–14 | Click **I've opened my agent**. The heading becomes **"Approve setup in your agent."** with "Waiting for your plugin to check in". | Heading + waiting status | — |
| 6 | 14–22 | Switch to Claude Code (new chat) and paste with `⌘V`. Show the first line of the pasted message and press Enter. The agent downloads the plugin, verifies the SHA-256 and installs `how-do-you-ai@how-do-you-ai`. **Approve the plugin install and the hook review** when Claude Code asks; this is the "hook review". | The paste, the install/permission prompts, your approval click or keypress | The pairing code inside the pasted prompt (blur it or scroll it out of view), your home path if you care, and any other chats in the sidebar |
| 7 | 22–26 | Back in the browser: within about 15 s the page polls and shows **"Your plugin is connected."** with "Connection verified by plugin check-in". Cut here if the wait is longer, and don't speed it up silently. | Heading + green status | The "Last contact …" timestamp is fine. Blur "N sessions synced" only if the number gives away something private |
| 8 | 26–30 | Click **Open daily analysis**, or go to **Sessions**, and hold on the first sessions. | Sessions list | **Every session title that is not demo work.** Titles are your real prompts. Use a project filter or search so only demo rows are visible, or blur the rows |

### Blurring in the take
- **Browser:** before shot 4, open DevTools (Console) and run
  `document.querySelectorAll('textarea[aria-label$="setup message"]').forEach(t=>t.style.filter='blur(6px)')`,
  then close DevTools. This blurs the whole message. Run the same with `'.session-row'` for rows you can't filter out.
- **Terminal:** paste, then press Enter right away and scroll so the prompt block is above the visible area. Or blur that region in post: `ffmpeg … -filter_complex "[0]crop=W:H:X:Y,boxblur=20[b];[0][b]overlay=X:Y"` (box coordinates in pixels).

### Trim markers
Write the two cut points of the usable take, in seconds of `login-raw.mov`, to `demo/login-raw.trim`:
```bash
echo "3.2 29.5" > demo/login-raw.trim   # in out
```
The assembler cuts `[in, out]`, scales to 1920x1080 at 30 fps, and shortens it to about 15 s. To control the cuts precisely, add more `in out` pairs, one per line, in playback order. Each line is one kept range.

---

## B. Local fallback (recorded): `demo/out/login-local.mp4`

Ari's app on :3300 runs in **local mode** (no `HDYAI_HOSTED`). Its /setup page has no "Connect Claude Code" / "Connect Codex" buttons and no setup message. Those exist only in the hosted build: `POST /api/plugin-setup` answers "Plugin pairing is available from the hosted dashboard." So the local clip shows what local setup really looks like and does not imitate the hosted flow:

| Segment | Out time (s) | Source |
|---|---|---|
| setup | 0.0–3.6 | :3300/setup: "Connect once. Start working.", Codex + Claude Code "Found on this computer" (highlighted), the consent line "…adds capture hooks for Codex and Claude Code". Nothing is clicked. |
| install | 3.6–8.0 | Real `scorer install --agent claude --settings /tmp/scorer-login/.claude/settings.json --no-schedule --yes`: the diff scrolls, then "claude: wrote /tmp/scorer-login/.claude/settings.json" (last frame held 0.5 s) |
| agent_run | 8.0–12.4 | Real sandboxed `claude -p "add a docstring to hello.py, then run it" --settings /tmp/scorer-login/.claude/settings.json …`. The middle 24.4 s play 12× faster with a "real run · sped up 12×" badge. Hooks stream into the bottom pane: SessionStart, UserPromptSubmit, PostToolUse×2, Stop, SessionEnd. |
| sessions | 12.4–15.7 | :3300/sessions filtered with the app's search box to `acme-api` (the 6 demo sessions). Any non-demo row is blurred by injected CSS (safety net). |

The sandbox: `SCORER_HOME=/tmp/scorer-login/home`, `CLAUDE_CONFIG_DIR=/tmp/scorer-login/claude-config`, cwd `/tmp/scorer-login/repo` (a fresh git repo), and `scorer` on PATH from this repo's `.venv`. `login_record.sh` checks that `~/.claude/settings.json`, `~/.codex/config.toml` and `~/.codex/hooks.json` have the same sha256 before and after. It exits non-zero if any of them changed.

Re-record (from the repo root, with :3300 running):
```bash
bash demo/terminal/login_record.sh            # terminal: out/login-term.mp4 (+ stamps, events, checksum check)
(cd demo && node record.login.mjs)            # browser:  out/login-web.webm + out/login-web.marks.json
(cd demo && BADGE_SPEED='12×' node record.login.mjs --badge)
bash demo/terminal/login_build.sh             # -> out/login-local.mp4 + out/login.marks.json
```
If a re-recorded terminal run takes a different amount of time, adjust the cut points with env vars: `I0 I1 HOLD C0 C1 C2 C3 SPEED` (seconds in `login-term.mp4`).
