import os

import pytest

from scorer import classify
from scorer.classify import bash_edit_paths, bash_git_subkind, bash_intent, is_relevant, manifest_dir, tool_kind

INTENT_CASES = [
    # test
    ("pytest -q", "test"),
    ("cd web && pnpm test", "test"),
    ("FOO=1 uv run pytest -q", "test"),
    ("python -m pytest tests/", "test"),
    ("python3 -m unittest discover", "test"),
    ("make test", "test"),
    ("npm test", "test"),
    ("npm run test -- --watch=false", "test"),
    ("yarn test:unit", "test"),
    ("npx vitest run src/a.test.ts", "test"),
    ("cargo test --all", "test"),
    ("go test ./...", "test"),
    ("timeout 60 pytest -x 2>&1 | tail -20", "test"),
    ("time npx jest", "test"),
    ("poetry run pytest", "test"),
    ("bunx playwright test", "test"),
    ("pytest > out.log", "test"),
    # build
    ("npx tsc --noEmit", "build"),
    ("uv run mypy src", "build"),
    ("ruff check . && ruff format --check .", "build"),
    ("pnpm run build", "build"),
    ("pnpm lint", "build"),
    ("npm run typecheck", "build"),
    ("cargo clippy", "build"),
    ("go vet ./...", "build"),
    ("docker build .", "build"),
    ("pnpm exec eslint src", "build"),
    # edit
    ("cat > a.py <<'EOF'\nprint(1)\nEOF", "edit"),
    ("sed -i '' s/a/b/ f", "edit"),
    ("sed -i 's/a/b/' f.txt", "edit"),
    ("echo hi > notes.txt", "edit"),
    ("echo hi >> notes.txt", "edit"),
    ("mkdir -p src/x && touch src/x/__init__.py", "edit"),
    ("mv a.py b.py", "edit"),
    ("rm -rf build", "edit"),
    ("echo x | tee out.txt", "edit"),
    ("patch -p1 < fix.diff", "edit"),
    # git
    ("git diff | head", "git"),
    ("git status", "git"),
    ("git -C repo log --oneline -5", "git"),
    ("git commit -m 'x && y'", "git"),
    # run
    ("make build", "run"),
    ("python -c 'print(1)'", "run"),
    ("node -e 'console.log(1)'", "run"),
    ("curl -s https://example.com", "run"),
    ("./run.sh", "run"),
    ("docker run --rm img", "run"),
    ("uv run python script.py", "run"),
    # search
    ("rg foo | wc -l", "search"),
    ("grep -rn foo src", "search"),
    ("find . -name '*.py'", "search"),
    # read
    ("cat a.py", "read"),
    ("ls -la", "read"),
    ("sed -n '1,20p' f.py", "read"),
    ("head -50 f.py | jq .", "read"),
    ("cat a.py 2>/dev/null", "read"),
    # other / not edit
    ("echo x > /dev/null", "other"),
    ("echo 'a > b'", "other"),
    ("pip install foo", "other"),
    ("", "other"),
    ("echo 'unbalanced", "other"),
]


@pytest.mark.parametrize("cmd,expected", INTENT_CASES, ids=[c[:60] or "<empty>" for c, _ in INTENT_CASES])
def test_bash_intent(cmd, expected):
    assert bash_intent(cmd) == expected


def test_case_count():
    assert len(INTENT_CASES) >= 40


@pytest.mark.parametrize("cmd,sub", [
    ("git diff | head", "read"),
    ("git --no-pager log -3", "read"),
    ("git -C x status", "read"),
    ("git blame f.py", "read"),
    ("git show HEAD", "read"),
    ("git commit -am x", "write"),
    ("cd x && git push", "write"),
    ("ls", None),
])
def test_git_subkind(cmd, sub):
    assert bash_git_subkind(cmd) == sub


@pytest.mark.parametrize("cmd,paths", [
    ("echo x > a.txt", ["a.txt"]),
    ("echo x > /dev/null", []),
    ("cmd 2>&1 >> log.txt", ["log.txt"]),
    ("cat > a.py <<'EOF'\nx > y\nEOF", ["a.py"]),
    ("sed -i '' s/a/b/ f", ["f"]),
    ("sed -i 's/a/b/' f1 f2", ["f1", "f2"]),
    ("sed -i -e s/a/b/ f", ["f"]),
    ("echo x | tee -a out.txt", ["out.txt"]),
    ("cp a.py b.py", ["b.py"]),
    ("mv a.py b.py", ["a.py", "b.py"]),
    ("rm -f a b", ["a", "b"]),
    ("mkdir -p x/y && touch x/y/z", ["x/y", "x/y/z"]),
    ("cd web && touch a.ts", ["web/a.ts"]),
    ("patch -o out.py in.py fix.diff", ["out.py"]),
    ("ls -la", []),
])
def test_bash_edit_paths(cmd, paths):
    assert bash_edit_paths(cmd) == paths


@pytest.mark.parametrize("name,kind", [
    ("Edit", "edit"), ("Write", "edit"), ("MultiEdit", "edit"), ("NotebookEdit", "edit"),
    ("Bash", "bash"), ("BashOutput", "bash"), ("Read", "read"), ("NotebookRead", "read"),
    ("Grep", "search"), ("Glob", "search"), ("LS", "search"), ("Task", "subagent"), ("Agent", "subagent"),
    ("WebFetch", "web"), ("WebSearch", "web"), ("apply_patch", "edit"), ("shell", "bash"),
    ("exec_command", "bash"), ("local_shell", "bash"), ("unified_exec", "bash"), ("write_stdin", "bash"),
    ("exec", "bash"), ("spawn_agent", "subagent"), ("send_message", "subagent"), ("js", "other"), ("TodoWrite", "other"),
    ("mcp__x__y", "other"), ("", "other"),
])
def test_tool_kind(name, kind):
    assert tool_kind(name) == kind


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{}")
    (tmp_path / "pkg").mkdir()
    classify.manifest_dir.cache_clear()
    classify._has_manifest.cache_clear()
    classify._is_relevant.cache_clear()
    classify._walk.cache_clear()
    return tmp_path


def test_manifest_dir(repo):
    assert manifest_dir(str(repo / "web" / "src" / "gone.ts")) == str(repo / "web")
    assert manifest_dir(str(repo / "pkg" / "a.py")) == str(repo)
    assert manifest_dir(str(repo / "pkg" / "a.py"), stop=str(repo / "pkg")) is None
    assert manifest_dir("") is None


def test_is_relevant(repo):
    r = str(repo)
    web_file = os.path.join(r, "web", "src", "foo.ts")
    py_file = os.path.join(r, "pkg", "foo.py")
    assert is_relevant("pnpm test", os.path.join(r, "web"), [web_file]) == "relevant"
    assert is_relevant("cd web && pnpm test", r, [web_file]) == "relevant"
    assert is_relevant("pytest", r, [py_file]) == "relevant"            # cwd == manifest dir
    assert is_relevant("pnpm -r test", "/", [web_file]) == "relevant-broad"  # ancestor, no args
    assert is_relevant("pytest tests/test_foo.py", r, [py_file]) == "relevant"
    assert is_relevant("vitest run src/foo.test.ts", os.path.join(r, "web"), [web_file]) == "relevant"
    assert is_relevant("pytest tests/test_bar.py", "/elsewhere", [py_file]) is None
    assert is_relevant("pnpm test", os.path.join(r, "web"), [py_file]) is None
    assert is_relevant("pytest", r, []) == "relevant-broad"
