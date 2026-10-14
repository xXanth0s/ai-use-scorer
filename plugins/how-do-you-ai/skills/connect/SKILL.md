---
name: connect
description: Pair, check, or disconnect the how do you ai session analytics plugin when the user asks to connect their agent sessions to their dashboard.
---

Use the installed plugin's `scripts/run.sh` helper. This plugin supports local Codex and Claude Code sessions on macOS/Linux and requires Node.js 24+. It does not need Entire, Git, a Gateway API key, or a local web server. If Node is missing, help the user install it from the official Node.js distribution using the host's normal permissions. Do not bypass hook trust review.

For pairing, use the one-time invitation supplied by the user and the analysis-service origin in their setup message. Pass the invitation to `bash scripts/run.sh pair <service-origin>` over stdin; never add it to command arguments, print it, or save it in a script. Pairing stores only a private, revocable device credential. Run `bash scripts/run.sh status` to verify, then `bash scripts/run.sh sync`. Avoid repeated pairing attempts; if the invitation expired, obtain a fresh one from the dashboard.

Explain the data flow before the first sync: the plugin reads supported local Codex/Claude transcripts, saves private snapshots, and sends session summaries, deterministic metrics, and bounded best-effort-redacted excerpts to how do you ai / Eve / AI Gateway for daily reports. Original full transcripts stay local. Use the user's installation/pairing request as authorization when it already includes this scope; otherwise ask before the first upload.

The native plugin hook definitions perform capture. Do not also install global user hooks or modify agent configuration/trust records. Start a new session and complete the agent's native hook approval if required. `status` reports pairing/sync state, not proof that every hook is approved.

Use `bash scripts/run.sh disconnect` when the user asks to stop capture and revoke the collector. It preserves existing local snapshots. Disabling/uninstalling this plugin stops its hooks; it does not delete stored platform reports. Do not send messages or modify work artifacts as part of setup.
