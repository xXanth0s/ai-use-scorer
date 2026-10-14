# Codex rollout format (as seen 2026-10, this machine)

Survey of `~/.codex/sessions/2026/10/**/*.jsonl`: 19 files, 4074 records, 45 MB.
`cli_version` in `session_meta`: 0.159.0 (21), 0.160.0 (3), 0.155.0-alpha.9.2 (1).
Shapes are the same across these versions; the parser keys on record shapes, not versions,
and skips unknown `type` / `payload.type` silently. No prompt text is reproduced here.

## Envelope

Every line: `{timestamp (UTC, Z), type, ordinal, payload}`; 801 lines also carry `metadata`
(`retained_source.turn_id`, `client_authored`, ...). `ordinal` is strictly increasing per file.

| type / payload.type | count | notes |
|---|---|---|
| event_msg / item_completed | 973 | `{thread_id, turn_id, item, started_at_ms, completed_at_ms}`; item.type below |
| event_msg / token_count | 500 | `info.total_token_usage` CUMULATIVE (resets after compaction), `info.last_token_usage`, `info.model_context_window`, `rate_limits` |
| token_usage_record | 478 | `{thread_id, turn_id, root_turn_id, session_id, response_id, usage, turn_token_usage, thread_token_usage}` |
| response_item / custom_tool_call (+ _output) | 306 | always `name="exec"` ("code mode"), `input` = JavaScript |
| response_item / reasoning | 276 | encrypted |
| response_item / message | 449 | role user 164 / assistant 155 / developer 130 |
| turn_context | 117 | `turn_id, cwd, model, effort, collaboration_mode.settings.reasoning_effort, sandbox/approval policy` |
| event_msg / task_started | 115 | `turn_id, root_turn_id, started_at (epoch s), model_context_window (258400)` |
| event_msg / thread_settings_applied | 101 | model/service tier |
| event_msg / task_complete | 94 | `turn_id, started_at, completed_at, duration_ms, time_to_first_token_ms, last_agent_message` |
| response_item / function_call (+ _output) | 83 | `collaboration.send_message` 38, `mcp__cua_repl.js` 33, `request_user_input_async` 6, `collaboration.spawn_agent` 6 |
| world_state, inter_agent_communication_metadata, response_item/agent_message | 50 each | multi-agent plumbing |
| session_meta | 25 | first one per file is the file's own thread (see subagents) |
| event_msg / turn_aborted | 15 | `reason: "interrupted"` (all), `duration_ms` |
| compacted | 3 | `window_number, compaction_response_id, latest_token_usage_record, replacement_history` |

There is no `event_msg/user_message` and no `response_item/function_call name=shell` in this format.

item_completed item types: Reasoning 283, Extension (web) 247, AgentMessage 154, UserMessage 92,
CommandExecution 85, SubAgentActivity 50, McpToolCall 33, ImageView 16, FileChange 8,
ContextCompaction 3, EnteredReviewMode/ExitedReviewMode 1.

## Session

* `session_meta.payload.id` = this thread. `payload.session_id` = ROOT thread (differs in subagent files).
* `cwd`, `timestamp`, `originator` (`codex_work_desktop` | `codex_exec`), `source` (`vscode` | `exec` | `{subagent: ...}`),
  `thread_source` (`user` | `subagent` | `avatar_quick_chat`).
* `git {branch, commit_hash, repository_url}` only on `codex exec` sessions (2/25). No git toplevel -> `repo = cwd`.
* Subagents: `forked_from_id`, `parent_thread_id`, `agent_nickname`, `agent_path`, `subagent_history_start_ordinal`.
  A forked subagent file COPIES the parent's history: a second `session_meta` (the parent's) at ordinal 1 and all
  parent records up to `subagent_history_start_ordinal`. The parser skips every record with
  `ordinal < subagent_history_start_ordinal` (this also drops the duplicated parent `token_usage_record`s).
* Review subagents (`source.subagent = "review"`) have `parent_thread_id` but no `forked_from_id`. The review
  parent (`codex exec review`) mirrors the child's items (CommandExecution etc.) without any model tool call or
  usage records.

## Turns

* A Codex task = `task_started` ... `task_complete` | `turn_aborted`. Usually one human prompt per task.
* Human prompt = `event_msg/item_completed` with `item.type == "UserMessage"` (`content[].text`, may include
  `local_image`). This is the only reliable prompt marker: present in every version here, also in 0.155 where the
  `response_item` user message has no `retained_source` metadata. Text may start with `<` and still be human
  (`<in-app-browser-context>` + question, `<send_user_message_question_reply>`), so `UserMessage` items are NOT
  filtered by `<`. Injections (environment_context, AGENTS.md, skills list, permissions) arrive only as
  `response_item/message` role `user`/`developer` and are ignored.
* The UserMessage is logged 0.1–3 s after `task_started` (context assembly) -> `prompt_at = task_started ts`.
* Steer: a second UserMessage inside a running task (3 cases) -> new turn `prompt_kind="queued"`,
  `enqueued_at = prompt_at = message ts`; previous turn ends at its last agent record (`end_source="last_entry"`).
* Tasks without a human prompt: subagent threads (prompt = `agent_message` from the parent, or a generated review
  prompt) -> one turn per task, `initiator="agent"`. In user threads (1 case: an empty aborted task) -> folded into
  the previous turn; its end is extended only when the task had agent activity, never flips `interrupted`.
* End: Stop hook ts (hook_events, first Stop in [agent_started_at, next prompt)) -> `task_complete` ts
  (`end_source="task_complete"`) -> `turn_aborted` (`interrupted`, `end_source="interrupt"`) -> last record
  (`last_entry`). In review-mode files `task_complete.turn_id` is the ROOT turn id, so end markers close the
  currently open task regardless of id.
* Slash commands: no Codex slash command was recorded as a prompt in this data; text starting with `/` would be
  `prompt_kind="command"`.
* Counts (after the subagent skip): 96 `task_started` -> 98 turns (+3 steers, -1 folded empty task).

## Usage

* `token_usage_record.usage` is per request, keyed by `response_id` (unique in all files; golden 3 dedupes
  repeats). Sum over records == final `total_token_usage` in every non-compacted file -> this is the source.
* `token_count` is emitted after every request AND sometimes repeated; `last_token_usage` summed over
  `token_count` lines over-counts in 8/19 files; `total_token_usage` is cumulative and resets at compaction.
  Only used as fallback (files with no `token_usage_record`): consecutive differences of `total_token_usage`,
  skipping unchanged totals, `last_token_usage` after a reset; `request_id = f"{session_id}:{n}"`.
* `cached_input_tokens` ⊂ `input_tokens`; `reasoning_output_tokens` ⊂ `output_tokens`;
  `cache_write_input_tokens` always 0. Stored `input_tok = input − cached − cache_write`.
* The compaction request (`compaction_response_id`) is itself a `token_usage_record`.
* model/effort from the latest `turn_context`; context window from `task_started.model_context_window`.

## Tools

* "Code mode": the model has one freeform tool `exec` whose input is JS (`text(await tools.exec_command({cmd:...}))`,
  `tools.apply_patch(...)`, `tools.web__run(...)`, `tools.view_image(...)`, `tools.write_stdin(...)`,
  `Promise.allSettled([...])`). Output text: `Script completed\nWall time ...` or `aborted by user ...`.
* Real work shows up as `item_completed` items between the call and its output (only one call is ever open):
  * `CommandExecution {id: "exec-<uuid>", command: ["/bin/zsh","-lc",cmd], cwd, exit_code, status
    (completed|failed), duration {secs,nanos}, parsed_cmd}` -> one ToolCall each (`name="exec_command"`,
    `is_error = exit_code != 0 or status == "failed"`). 3 complete up to 2 s after the call output
    (background) -> attributed to the last call. In the review-parent mirror they have no call at all -> skipped
    (the child file has them, same ids).
  * `FileChange {id, changes: {abs_path: {type: add, content} | {type: update, unified_diff, move_path}}, status}`
    -> ToolCall `apply_patch` + FileEdit per path (add: content lines; update: +/- lines of the diff).
  * `McpToolCall {id == function_call.call_id, server, tool, pluginId, status, result, duration}` -> enriches the
    function_call.
  * `Extension` (web search/open), `ImageView`: no own ToolCall; the wrapper covers them.
* An `exec` wrapper that produced CommandExecution/FileChange children is dropped (children represent it);
  otherwise it is one ToolCall named after its main inner tool (apply_patch > exec_command > write_stdin >
  view_image > web__run). `*** Add/Update/Delete File` patch headers are parsed only as a fallback when no
  FileChange exists (JS-escaped `\n` handled).
* `function_call`s are one ToolCall per call_id; MCP ones are named `mcp__<server>__<tool>`.
* Real data: 395 ToolCalls (web 223, bash 78, subagent 44, other 39 = 33 MCP + 6 request_user_input,
  edit 8, read 3), 13 errors, 0 without result. 3 bash calls have no command (write_stdin polls, one
  tool-less `exec` script).

## Events

* `compacted` record + `ContextCompaction` item 5–10 ms apart = one compaction event.
* `SubAgentActivity {kind: started|interacted|completed, agent_thread_id, agent_path}` -> `subagent` events
  (started/completed only).
* `request_user_input_async` exists (6) but returns immediately; no permission-wait span derived.

### Tooling (skills / slash commands / MCP)

* No dedicated skill record exists. Skills are listed in a developer message (`<skills_instructions>`, every
  session) and USED by the agent reading `.../skills/<name>/SKILL.md` via exec_command (13 CommandExecutions,
  also visible in the `exec` input). -> `tooling` event `{category: skill, name, initiator: agent,
  rule: "codex:skill-item"}`, once per (turn, skill).
* Human `$skill` mention anywhere / `/name` at the start of a UserMessage -> `{category: skill | command
  (Codex built-in slash command), initiator: human, rule: "codex:user-prefix"}`. None in this data (the only `/`
  tokens are paths).
* MCP: `function_call` with `namespace = "mcp__<server>"` (+ McpToolCall item with `server`, `pluginId`) ->
  `{category: mcp, name: server, initiator: agent, rule: "codex:mcp-call"}`. Real data: 33 × `cua_repl`.
