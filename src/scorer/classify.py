"""Bash intent classifier + tool kinds + test relevance (spec §3).

  bash_intent(cmd) -> str                 # one of model.BASH_INTENTS, aggregated per spec
  bash_git_subkind(cmd) -> str | None     # "read" for diff|log|status|show|blame else "write"; None if no git
  bash_edit_paths(cmd) -> list[str]       # files written by redirects/sed -i/tee/patch/mv/cp/rm/touch/mkdir
  tool_kind(name) -> str                  # one of model.TOOL_KINDS (Claude + Codex tool names)
  manifest_dir(path, stop=None) -> str | None   # nearest dir with package.json|pyproject.toml|go.mod|Cargo.toml
  is_relevant(test_cmd, test_cwd, edited_paths) -> str | None  # "relevant" | "relevant-broad" | None
  segments(cmd) -> list[Segment]          # parsed segments (argv after wrapper stripping, redirects, cd)

Splitting: commands are split on `&&`, `||`, `;`, `|`, `&` and newlines with
shlex (quotes respected); unbalanced quotes fall back to a regex split.
Heredoc bodies are dropped before tokenizing. Per segment, leading env
assignments and wrappers (env, sudo, time, timeout N, nohup, npx, bunx,
pnpm exec, uv run, poetry run, ...) are stripped; `cd x` segments only move the
effective cwd. Aggregate priority: test > build > edit > git > run > search > read > other.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache

PRIORITY = ("test", "build", "edit", "git", "run", "search", "read", "other")
_RANK = {k: i for i, k in enumerate(PRIORITY)}

_OPS = {"&&", "||", ";", "|", "&", "|&", ";;", "\n"}
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIR = re.compile(r"^(\d*|&)(>>?|>\|)(.*)$")   # >, >>, 2>, &>, >|, with optional attached target


@dataclass
class Segment:
    argv: list[str]                          # after wrapper stripping, redirects removed
    redirects: list[str] = field(default_factory=list)   # write targets (excl. /dev/null, fd dups)
    cwd: str | None = None                   # effective `cd` dir (relative or absolute) or None


# --- tokenizing ------------------------------------------------------------------------------------

def _strip_heredocs(cmd: str) -> str:
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delims = [m.group(2) for m in _HEREDOC.finditer(line)]
        i += 1
        for d in delims:
            while i < len(lines) and lines[i].strip() != d:
                i += 1
            i += 1  # skip delimiter line
    return "\n".join(out)


def _tokens(cmd: str) -> list[str]:
    s = _strip_heredocs(cmd or "")
    s = s.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=";&|<>")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        toks = []
        for t in lex:
            toks.append(t)
        return toks
    except ValueError:
        parts = re.split(r"(\&\&|\|\||;|\||\n)", s)
        toks = []
        for p in parts:
            if p in _OPS:
                toks.append(p)
            else:
                toks.extend(p.split())
        return toks


def _split(toks: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for t in toks:
        if t in _OPS or (t and set(t) <= set(";&|\n")):
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def _take_redirects(argv: list[str]) -> tuple[list[str], list[str]]:
    out, targets = [], []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t.isdigit() and i + 1 < len(argv) and argv[i + 1][:1] in "<>":
            i += 1  # fd number of `2>` / `2>&1`
            continue
        if t in ("<<", "<<-", "<<<", "<"):
            i += 2
            continue
        if t.startswith("<<") or (t.startswith("<") and len(t) > 1 and not t.startswith("<(")):
            i += 1
            continue
        m = _REDIR.match(t)
        if m and not t.startswith(">(") and (m.group(1) or t[0] == ">"):
            tgt = m.group(3)
            if not tgt:
                tgt = argv[i + 1] if i + 1 < len(argv) else ""
                i += 1
            i += 1
            if tgt.startswith("&") or tgt in ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty", ""):
                continue
            targets.append(tgt)
            continue
        out.append(t)
        i += 1
    return out, targets


_UV_ARGFLAGS = {"--with", "--project", "--directory", "--python", "-p", "--package", "--env-file",
                "--with-requirements", "--extra", "--group", "--index", "--from"}


def _strip_wrappers(argv: list[str]) -> list[str]:
    a = list(argv)
    changed = True
    while a and changed:
        changed = False
        while a and (a[0] in ("(", ")", "{", "}", "!") ):
            a = a[1:]
            changed = True
        if a and a[0].startswith("(") and len(a[0]) > 1:
            a[0] = a[0][1:]
        while a and _ENV.match(a[0]):
            a = a[1:]
            changed = True
        if not a:
            break
        h = os.path.basename(a[0])
        if h in ("env", "sudo", "nohup", "command", "exec", "nice", "caffeinate", "dotenv", "xargs"):
            a = a[1:]
            while a and (a[0].startswith("-") or _ENV.match(a[0])):
                a = a[1:]
            changed = True
        elif h == "time":
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[1:]
            changed = True
        elif h in ("timeout", "gtimeout"):
            a = a[1:]
            while a and a[0].startswith("-"):
                flag = a[0]
                a = a[1:]
                if flag in ("-s", "-k", "--signal", "--kill-after") and a:
                    a = a[1:]
            if a and re.match(r"^\d+(\.\d+)?[smhd]?$", a[0]):
                a = a[1:]
            changed = True
        elif h in ("npx", "bunx", "pnpx"):
            a = a[1:]
            while a and a[0].startswith("-"):
                flag = a[0]
                a = a[1:]
                if flag in ("-p", "--package") and a:
                    a = a[1:]
            changed = True
        elif h in ("pnpm", "yarn", "npm", "bun") and len(a) > 1 and a[1] in ("exec", "dlx", "x"):
            a = a[2:]
            changed = True
        elif h in ("uv", "poetry", "pipenv", "hatch", "pdm", "rye") and len(a) > 1 and a[1] == "run":
            a = a[2:]
            while a and a[0].startswith("-"):
                flag = a[0]
                a = a[1:]
                if flag in _UV_ARGFLAGS and "=" not in flag and a:
                    a = a[1:]
            changed = True
        elif h in ("uvx", "pipx") and len(a) > 1:
            a = a[2:] if h == "pipx" and a[1] == "run" else a[1:]
            changed = True
    if a:
        a[0] = a[0].rstrip(")")
    return [x for x in a if x]


def segments(cmd: str | None) -> list[Segment]:
    out: list[Segment] = []
    cwd: str | None = None
    for raw in _split(_tokens(cmd or "")):
        argv, redirs = _take_redirects(raw)
        argv = _strip_wrappers(argv)
        if argv and argv[0] in ("cd", "pushd"):
            if len(argv) > 1 and argv[1] != "-":
                d = os.path.expanduser(argv[1])
                cwd = d if (cwd is None or os.path.isabs(d)) else os.path.join(cwd, d)
            continue
        if argv or redirs:
            out.append(Segment(argv, redirs, cwd))
    return out


# --- per-segment classification ----------------------------------------------------------------

_TEST_BIN = {"pytest", "py.test", "vitest", "jest", "mocha", "ava", "tap", "karma", "rspec", "tox", "nox",
             "phpunit", "ctest"}
_BUILD_BIN = {"tsc", "mypy", "pyright", "basedpyright", "eslint", "ruff", "biome", "oxlint", "flake8",
              "pylint", "vue-tsc", "svelte-check", "golangci-lint", "tsgo"}
_EDIT_BIN = {"tee", "patch", "mv", "cp", "rm", "mkdir", "touch", "rmdir", "ln", "truncate", "install", "unzip",
             "rsync"}
_SEARCH_BIN = {"grep", "egrep", "fgrep", "rg", "ag", "fd", "fdfind", "find", "ack", "locate"}
_READ_BIN = {"cat", "head", "tail", "awk", "gawk", "wc", "ls", "tree", "jq", "yq", "stat", "less", "more", "file",
             "du", "bat", "nl", "sort", "uniq", "cut", "diff", "realpath", "readlink", "which", "pwd", "column",
             "md5", "shasum", "sha256sum", "xxd", "od", "strings", "basename", "dirname", "tr"}
_RUN_BIN = {"curl", "wget", "bash", "sh", "zsh", "open", "http", "deno"}
_PKG = {"npm", "pnpm", "yarn", "bun"}
_PY = re.compile(r"^python(\d+(\.\d+)?)?$")
_TEST_SCRIPT = re.compile(r"^(test|tests|e2e|spec)([:\-_].*)?$")
_BUILD_SCRIPT = re.compile(r"^(build|lint|typecheck|type-check|tsc|check|types|compile)([:\-_].*)?$")


def _args(argv: list[str]) -> list[str]:
    return [x for x in argv[1:] if not x.startswith("-")]


def classify_segment(seg: Segment) -> str:
    argv = seg.argv
    if not argv:
        return "edit" if seg.redirects else "other"
    h = os.path.basename(argv[0])
    rest = argv[1:]
    sub = rest[0] if rest else ""
    if argv[0].startswith(("./", "../", "~/")) and h not in _TEST_BIN | _BUILD_BIN:
        k = "run"
    else:
        k = _classify_argv(h, rest, sub)
    if seg.redirects and _RANK["edit"] < _RANK[k]:
        return "edit"
    return k


def _classify_argv(h: str, rest: list[str], sub: str) -> str:
    if h in _TEST_BIN:
        return "test"
    if h == "playwright":
        return "test" if sub == "test" else "run"
    if h == "cargo":
        if sub in ("test", "nextest"):
            return "test"
        if sub in ("build", "check", "clippy"):
            return "build"
        return "run"
    if h == "go":
        if sub == "test":
            return "test"
        if sub in ("build", "vet"):
            return "build"
        return "run"
    if h in ("dotnet", "swift", "mvn", "gradle", "./gradlew", "gradlew"):
        if sub == "test":
            return "test"
        if sub in ("build", "compile"):
            return "build"
        return "run"
    if h in _PKG:
        if sub in ("run", "run-script"):
            script = rest[1] if len(rest) > 1 else ""
        elif h == "npm" and sub not in ("test", "t"):
            script = ""
            if sub in ("install", "i", "ci", "add", "uninstall", "remove", "publish", "init"):
                return "other"
        else:
            script = sub
        if script in ("t",) or _TEST_SCRIPT.match(script or ""):
            return "test"
        if _BUILD_SCRIPT.match(script or ""):
            return "build"
        if sub in ("install", "i", "ci", "add", "remove", "uninstall", "init", "publish", "create"):
            return "other"
        return "run" if script else "other"
    if h in ("make", "gmake", "just", "task"):
        targets = [x for x in rest if not x.startswith("-") and "=" not in x]
        if any(_TEST_SCRIPT.match(t) for t in targets):
            return "test"
        return "run"
    if _PY.match(h) or h in ("pypy3", "pypy"):
        if "-m" in rest:
            mod = rest[rest.index("-m") + 1] if rest.index("-m") + 1 < len(rest) else ""
            if mod in ("pytest", "unittest", "nose2", "tox"):
                return "test"
            if mod in ("mypy", "pyright", "ruff", "flake8", "pylint", "compileall", "py_compile", "build"):
                return "build"
            return "run"
        return "run"
    if h in ("node", "tsx", "ts-node"):
        return "run"
    if h == "docker" or h == "podman":
        if sub == "build" or (sub == "buildx" and "build" in rest):
            return "build"
        if sub == "compose" and "build" in rest:
            return "build"
        return "run"
    if h in _BUILD_BIN:
        return "build"
    if h == "prettier":
        return "build" if ("--check" in rest or "-c" in rest) else "edit"
    if h == "git":
        return "git"
    if h == "gh":
        return "git"
    if h == "sed" or h == "gsed":
        if any(x == "-i" or x.startswith("-i") or x == "--in-place" or x.startswith("--in-place=") for x in rest):
            return "edit"
        return "read"
    if h == "perl" and any(x.startswith("-pi") or x.startswith("-i") for x in rest):
        return "edit"
    if h in _EDIT_BIN:
        return "edit"
    if h in _SEARCH_BIN:
        return "search"
    if h in _READ_BIN:
        return "read"
    if h in _RUN_BIN or h.startswith("./") or h.startswith("../") or (h.startswith("/") and "/bin/" not in h):
        return "run"
    return "other"


def bash_intent(cmd: str) -> str:
    return _bash_intent(cmd or "")


@lru_cache(maxsize=65536)
def _bash_intent(cmd: str) -> str:
    best = "other"
    for seg in segments(cmd):
        k = classify_segment(seg)
        if _RANK[k] < _RANK[best]:
            best = k
    return best


def bash_git_subkind(cmd: str) -> str | None:
    for seg in segments(cmd):
        if not seg.argv or os.path.basename(seg.argv[0]) != "git":
            continue
        rest = seg.argv[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            if rest[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(rest):
                i += 1
            i += 1
        sub = rest[i] if i < len(rest) else ""
        return "read" if sub in ("diff", "log", "status", "show", "blame") else "write"
    return None


def _join(cwd: str | None, p: str) -> str:
    p = os.path.expanduser(p)
    return p if (cwd is None or os.path.isabs(p)) else os.path.join(cwd, p)


def bash_edit_paths(cmd: str) -> list[str]:
    return list(_bash_edit_paths(cmd or ""))


@lru_cache(maxsize=65536)
def _bash_edit_paths(cmd: str) -> tuple:
    out: list[str] = []
    for seg in segments(cmd):
        out += [_join(seg.cwd, t) for t in seg.redirects]
        if not seg.argv:
            continue
        h = os.path.basename(seg.argv[0])
        rest = seg.argv[1:]
        pos = [x for x in rest if not x.startswith("-")]
        paths: list[str] = []
        if h == "tee":
            paths = pos
        elif h in ("sed", "gsed") and classify_segment(seg) == "edit":
            # script is the first positional unless -e/-f given
            has_e = any(x in ("-e", "-f", "--expression", "--file") for x in rest)
            args, skip = [], False
            for j, x in enumerate(rest):
                if skip:
                    skip = False
                    continue
                if x in ("-e", "-f", "--expression", "--file"):
                    skip = True
                    continue
                if x == "-i" and j + 1 < len(rest) and rest[j + 1] in ("", "''") :
                    skip = True  # BSD: sed -i '' ...
                    continue
                if x.startswith("-"):
                    continue
                args.append(x)
            paths = args if has_e else args[1:]
        elif h == "perl" and classify_segment(seg) == "edit":
            paths = pos[1:] if not any(x == "-e" for x in rest) else pos[1:]
        elif h == "patch":
            if "-o" in rest and rest.index("-o") + 1 < len(rest):
                paths = [rest[rest.index("-o") + 1]]
            else:
                paths = pos[:1]
        elif h in ("mv", "cp", "ln", "install", "rsync"):
            paths = pos[-1:] if len(pos) >= 2 else []
            if h == "mv" and len(pos) >= 2:
                paths = pos[:-1] + pos[-1:]  # source disappears too
        elif h in ("rm", "mkdir", "touch", "rmdir", "truncate"):
            paths = pos
        out += [_join(seg.cwd, p) for p in paths if p]
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return tuple(res)


# --- tool kinds -----------------------------------------------------------------------------------

_TOOL_KINDS = {
    # Claude Code
    "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "bash", "BashOutput": "bash",
    "Read": "read", "NotebookRead": "read",
    "Grep": "search", "Glob": "search", "LS": "search",
    "Task": "subagent", "Agent": "subagent",
    "WebFetch": "web", "WebSearch": "web",
    # Codex (current rollouts: custom_tool_call `exec` (JS code mode), function_call `js`, `spawn_agent`)
    "apply_patch": "edit",
    "shell": "bash", "exec_command": "bash", "local_shell": "bash", "unified_exec": "bash",
    "write_stdin": "bash", "exec": "bash", "js": "bash", "container.exec": "bash",
    "spawn_agent": "subagent",
    "web_search": "web", "web_search_call": "web", "web__run": "web",
    "view_image": "read",
}


def tool_kind(name: str) -> str:
    return _TOOL_KINDS.get(name or "", "other")


# --- manifests + relevance --------------------------------------------------------------------

MANIFESTS = ("package.json", "pyproject.toml", "go.mod", "Cargo.toml")


@lru_cache(maxsize=65536)
def _has_manifest(d: str) -> bool:
    return any(os.path.isfile(os.path.join(d, m)) for m in MANIFESTS)


@lru_cache(maxsize=65536)
def manifest_dir(path: str, stop: str | None = None) -> str | None:
    """Nearest ancestor dir of `path` (file or dir; need not exist) containing a manifest.
    Stops after checking `stop` (inclusive) or at the filesystem root."""
    if not path:
        return None
    p = os.path.normpath(os.path.expanduser(path))
    return _walk(p if os.path.isdir(p) else os.path.dirname(p), stop)


@lru_cache(maxsize=65536)
def _walk(d: str, stop: str | None = None) -> str | None:
    stop_n = os.path.normpath(stop) if stop else None
    while d:
        if _has_manifest(d):
            return d
        if stop_n and d == stop_n:
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _within(child: str, parent: str) -> bool:
    """child == parent or child below parent (normalized path strings)."""
    c, p = os.path.normpath(child), os.path.normpath(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


_TEST_STEM = re.compile(r"^(test_)?(.+?)(_test|\.test|\.spec|_spec|Test)?$")


def _stem(path: str) -> str:
    b = os.path.basename(path)
    for ext in (".d.ts",):
        if b.endswith(ext):
            b = b[: -len(ext)]
    b = os.path.splitext(b)[0]
    m = _TEST_STEM.match(b)
    return m.group(2) if m else b


def _looks_like_path(tok: str) -> bool:
    if tok.startswith("-") or "=" in tok or tok.startswith("$"):
        return False
    return "/" in tok or bool(re.search(r"\.(py|ts|tsx|js|jsx|mjs|cjs|go|rs|rb|java|kt|vue|svelte)$", tok)) \
        or tok in (".", "..") or "::" in tok


def _test_scope(test_cmd: str, test_cwd: str | None) -> tuple[str | None, list[str]]:
    """Effective cwd + path args of the test/build segments."""
    eff, args = test_cwd, []
    for seg in segments(test_cmd):
        if classify_segment(seg) not in ("test", "build"):
            continue
        c = seg.cwd
        if c is not None:
            eff = _join(test_cwd, c)
        for t in seg.argv[1:]:
            if _looks_like_path(t):
                t = t.split("::")[0]
                args.append(os.path.normpath(_join(eff, t)))
    return eff, args


def is_relevant(test_cmd: str, test_cwd: str | None, edited_paths: list[str]) -> str | None:
    return _is_relevant(test_cmd or "", test_cwd, tuple(edited_paths or ()))


@lru_cache(maxsize=65536)
def _is_relevant(test_cmd: str, test_cwd: str | None, edited_paths: tuple) -> str | None:
    """Spec §3 relevance of a test/build command to edited files.

    - path args: an arg that contains/is contained in the edited file's manifest dir (or the
      file itself), or that pairs by name (foo.ts<->foo.test.ts, test_foo.py<->foo.py) -> "relevant"
    - no path args: effective cwd == manifest dir (or inside it) -> "relevant";
      cwd is an ancestor (repo root) -> "relevant-broad"
    - nothing to compare against (no edited paths, no cwd, no manifest found) -> "relevant-broad"
    """
    eff, args = _test_scope(test_cmd or "", test_cwd)
    paths = [p for p in (edited_paths or []) if p]
    if not paths:
        return "relevant-broad"
    best: str | None = None
    for raw in paths:
        p = os.path.normpath(_join(test_cwd, raw))
        m = manifest_dir(p) if os.path.isabs(p) else None
        if args:
            for a in args:
                if _stem(a) == _stem(p) and os.path.basename(a) != "" and "." in os.path.basename(a):
                    return "relevant"
                if _within(p, a):          # arg is the file or an ancestor dir of it
                    return "relevant"
                if m and (_within(a, m) and os.path.isdir(a) or _within(m, a)):
                    return "relevant"
                if m and _within(a, m) and not os.path.isdir(a) and _stem(a) == _stem(p):
                    return "relevant"
            continue
        if eff is None:
            best = best or "relevant-broad"
            continue
        if m is None:
            # no manifest known: compare with the file's directory
            if _within(p, eff):
                best = best or "relevant-broad"
                if _within(os.path.dirname(p), eff) and os.path.normpath(eff) == os.path.dirname(p):
                    return "relevant"
            continue
        if _within(eff, m) and _walk(os.path.normpath(eff)) in (m, None):
            return "relevant"
        if _within(m, eff):
            best = "relevant-broad"
    return best
