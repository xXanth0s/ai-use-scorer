# Claude Code transcript format — survey notes (2026-10-14)

Survey of this machine: 58 main transcripts (`~/.claude/projects/*/*.jsonl`, 200 MB total),
74 subagent transcripts (`*/<session>/subagents/agent-<id>.jsonl` + `agent-<id>.meta.json`).
CLI versions around 2.1.28x. No prompt text is reproduced here.

## Entry types (main files, counts)

assistant 8186 · attachment 7191 · user 5061 · last-prompt 1483 · atis-latch 1424 · mode 1420 ·
ai-title 1079 · permission-mode 985 · system/stop_hook_summary 620 · system/turn_duration 561 ·
queue-operation enqueue 469 / dequeue 262 / remove 206 / popAll 1 · file-history-snapshot 444 ·
pr-link 401 · bridge-session 340 · agent-name 256 · custom-title 251 · system/away_summary 153 ·
api-request-blob 113 · artifact-autoreact-ledger 90 · cost-state 84 · file-history-delta 84 ·
api-request 71 · system/local_command 46 · system/informational 43 · frame-link 14 ·
history-suppression 10 · api-request-shape 6 · artifact-comment-monitor 5 ·
system/compact_boundary 3 · system/agents_killed 3 · continued-in 2.
Subagent files: assistant/attachment/user plus api-request*, one `fork-context-ref`.

Only `user`, `assistant`, `system`, `attachment` carry `uuid`/`timestamp`/`parentUuid`.
`queue-operation`, `pr-link`, `continued-in`, `file-history-delta` carry `timestamp` but no uuid.

**File order is not timestamp order** (e.g. an `attachment/queued_command` is written after later
tool results with its original enqueue ts). Turn segmentation uses file order; durations use ts.

## Prompts

`user` entries carry `promptSource` (typed | queued | system | sdk), `origin.kind`
(human | task-notification), `turnOrigin` (human | peer | task_notification | sdk), `promptId`.
- Real human prompts: content is a string (or a list with text/image blocks), not starting with `<`.
- `<task-notification>…` (async subagent finished), `<local-command-caveat>` (isMeta),
  `<local-command-stdout>`, `<bash-input>`/`<bash-stdout>` (`!` shell mode), `<pasted_content …>`
  start with `<` and are not prompts. 3 task-notification entries do NOT start with `<` →
  also excluded via `origin.kind == "task-notification"` / `promptSource == "system"`.
- "Another Claude session sent a message: …" peer messages are `isMeta: true`.
- Slash commands: string content starting `<command-message>` (skills; the agent then works) or
  `<command-name>/clear|/model|/plugin…` (local commands; followed by `<local-command-stdout>`, no
  assistant work). Parser: both → `prompt_kind=command`; command turns with no assistant activity
  are dropped.

## Turn end markers

- `system/turn_duration` {durationMs, messageCount, pendingBackgroundAgentCount?} is written right
  after `system/stop_hook_summary` at the end of every agent run. `ts − durationMs` ≈ the ts of the
  most recent user input that triggered the run (human prompt **or** `<task-notification>` /
  `<command-message>`): 359/382 plain prompts within 1 s. A human turn can therefore contain several
  turn_duration entries (follow-up runs triggered by background-agent notifications).
  Parser: end = max over the turn's turn_duration entries of (trigger ts + durationMs).
- Interrupted runs have no turn_duration / stop_hook_summary.
- `system/stop_hook_summary` {hookCount, hookInfos[{command,durationMs}], hookErrors,
  preventedContinuation, stopReason, toolUseID} — always present when any Stop hook is configured.
- `~/.scorer/events.jsonl` does not exist yet on this machine → Stop-hook ends untested on real data.

## Interrupts

User entry, list content, one text block `[Request interrupted by user]` (20) or
`[Request interrupted by user for tool use]` (8); usually directly after a tool_result. Never inside
a tool_result. Some user entries carry `interruptedMessageId`. The next real prompt follows later.

## Queued prompts

`queue-operation` entries: `enqueue` {content, ts}, `dequeue` {ts, no content}, `remove`
{content, reason: "absorbed_mid_turn", commandUuid?, deliveryId?}, `popAll` {content}.
Enqueue content may also be a task-notification / agent-message (starts with `<`).
Normal queued flow: enqueue (during a run) → stop_hook_summary → turn_duration → dequeue → user
entry with `promptSource: "queued"` (dequeue ts ≈ prompt ts). `absorbed_mid_turn` = the queued text
was injected into the running turn as `attachment/queued_command`; no new turn.
Parser: FIFO of enqueues; dequeue pops; remove drops by content; a prompt matching a dequeued
enqueue (by content) or with promptSource=queued → `queued`, enqueued_at = enqueue ts.

## Compaction

`system/compact_boundary` {compactMetadata: trigger manual|auto, preTokens, postTokens,
durationMs, preservedSegment…}, followed by a `user` entry with `isCompactSummary: true`.

## Files edits

`toolUseResult` on the tool_result entry for Edit/Write: {filePath, oldString, newString,
structuredPatch[{oldStart,oldLines,newStart,newLines,lines[+/-…]}], userModified, originalFile}.
userModified: 152 seen, all false.

## API calls

Assistant entries: `requestId`, `message.model`, `message.usage`
{input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens}, `effort`
(high|medium), `perTurnEffort`. One API response is split over N lines (one per content block),
all with the same requestId and (in 2675/2676 cases) identical usage → dedupe per requestId
(take the max-output line). `<synthetic>` model on 20 lines (API error / local messages,
`isApiErrorMessage`) → skipped. Models seen: claude-fable-5-1, claude-opus-5-5, claude-opus-5,
claude-opus-4-6.

## Subagents

`<session>/subagents/agent-<agentId>.jsonl`, entries all `isSidechain: true`, carry `agentId`,
same `sessionId` as parent. `agent-<agentId>.meta.json` {agentType, description, spawnDepth,
model?, toolUseId? (65/76 — the parent Agent/Task tool_use id), name?, teamName?, isFork?,
stoppedByUser?}. Parent tool_result `toolUseResult` for Agent: {status, agentId, isAsync,
resolvedModel, outputFile…} or teammate form {agent_id, name, model…}. Async agents finish later and
announce themselves with a `<task-notification>` user entry in the parent, which triggers a new
agent run inside the same human turn.

## Forks / continuations / resumes

- Fork: copied entries keep their uuid and carry `forkedFrom {sessionId, messageUuid}` with the new
  sessionId (e4fe98ef: 539 entries from 4570f293, which exists). Parser skips them when the source
  transcript exists next to this one; Session.forked_from = source id.
- Continuation: the source session gets a `continued-in {continuedInSessionId}` entry and the new
  file contains a full copy of the history with **rewritten sessionId and no marker**
  (acb2cb61 ⊃ 257 uuids of 077c5fde). Parser builds a per-directory index of continued-in links
  and skips uuids that the source file already holds.
- Within one file no duplicate uuids were seen; dedupe by uuid anyway.

## Permission waits

Not visible as timed waits. `permissionDecision {decision: accept|reject, source: config|user_abort}`
on user entries, `toolDenialKind: automode-blocked|user-rejected`. No `permission_wait` events
emitted in v1.

## Ignored for scoring

attachment (except as noise), api-request*, last-prompt, ai-title/custom-title/agent-name, pr-link,
file-history-*, cost-state, mode/permission-mode/atis-latch, bridge-session, frame-link,
artifact-*, history-suppression, away_summary, informational, agents_killed.

## Tooling (skills / commands / MCP / plugins)

- Slash commands: every `<command-name>` prompt also carries `<command-message>`; a skill expansion is
  recognisable by the following isMeta user entry "Base directory for this skill…" → category skill,
  otherwise command (`/clear`, `/model`, `/plugin`, …). Plugin skills are `plugin:skill`.
- `Skill` tool_use: input {skill, args?} (68 calls).
- `attributionSkill` / `attributionPlugin` / `attributionMcpServer` / `attributionAgent` sit on
  **every** assistant line produced while a skill/plugin/MCP/subagent is active, so they are
  attribution, not invocations → emitted at most once per (turn, name) and only if no Skill/command
  event for that name exists in the turn.
- MCP tools: `mcp__<server>__<tool>`.
- No hook-triggered skill/command invocations were found (hook attachments are `hook_success`,
  `hook_additional_context` for SessionStart/UserPromptSubmit/Stop/PreToolUse only) → rule
  "claude:hook" is never emitted in v1.

## Detached runs

Turns can absorb agent runs triggered without a human prompt (task notifications, peer messages,
resumes) hours or days later (seen: a turn whose last turn_duration was 5 days after its prompt).
Parser rule: after a run closes (turn_duration) or after a non-tool assistant reply, activity that
resumes more than 15 min later (spec break threshold) is "detached": its tool/api rows stay in the
turn, but it no longer extends the turn's end. Subagent activity extends the end only while
contiguous (≤ 15 min steps) and never past the next prompt. Tool results only extend the turn that
issued the tool_use. `<synthetic>` assistant entries are not activity.
