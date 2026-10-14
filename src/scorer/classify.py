"""Bash intent classifier (spec §3). STUB — owned by agent C; keep these signatures.

  bash_intent(cmd) -> str                 # one of model.BASH_INTENTS, aggregated per spec
  bash_git_subkind(cmd) -> str | None     # "read" for diff|log|status|show|blame else "write"
  bash_edit_paths(cmd) -> list[str]       # files written by redirects/sed -i/tee/mv/cp/rm/touch/mkdir
  tool_kind(name) -> str                  # one of model.TOOL_KINDS (Claude + Codex tool names)
  manifest_dir(path, stop=None) -> str | None   # nearest dir with package.json|pyproject.toml|go.mod|Cargo.toml
  is_relevant(test_cmd, test_cwd, edited_paths) -> str | None  # "relevant" | "relevant-broad" | None
"""
from __future__ import annotations

import re

_TEST = re.compile(r"\b(pytest|vitest|jest|cargo test|go test|npm test|pnpm test)\b")
_BUILD = re.compile(r"\b(tsc|mypy|pyright|eslint|ruff|cargo build|go build)\b")


def bash_intent(cmd: str) -> str:
    if _TEST.search(cmd or ""):
        return "test"
    if _BUILD.search(cmd or ""):
        return "build"
    return "other"


def bash_git_subkind(cmd: str) -> str | None:
    return None


def bash_edit_paths(cmd: str) -> list[str]:
    return []


def tool_kind(name: str) -> str:
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"):
        return "edit"
    if name in ("Bash", "shell", "exec_command", "local_shell"):
        return "bash"
    if name in ("Task", "Agent"):
        return "subagent"
    return "other"


def manifest_dir(path: str, stop: str | None = None) -> str | None:
    return None


def is_relevant(test_cmd: str, test_cwd: str | None, edited_paths: list[str]) -> str | None:
    return "relevant-broad"
