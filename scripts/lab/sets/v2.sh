# project set v2: four new small Claude-only projects (no codex -> ~/.codex/config.toml is never touched).
#   lab-md-links    markdown link checker   vague start -> interrupt -> early redirect, queued prompt, verification ask
#   lab-rate-limit  token-bucket limiter    not asked to test, clarification, hand edit, correction, asked to test
#   lab-schema-diff JSON-schema diff tool   good spec w/ acceptance criteria, scoped turns, trivial edit done by hand
#   lab-retry-util  retry/backoff util      seeded off-by-one + flaky test -> "run the tests and check" catches it
# Sourced by orchestrate.sh. Defines AGENT_DIRS, set_setup, set_launch, set_drive.
AGENT_DIRS=(lab-mdl:lab-md-links lab-rate:lab-rate-limit lab-schema:lab-schema-diff lab-retry:lab-retry-util)

set_setup() {
  local d
  # 1) markdown link checker: seeded docs with 2 broken relative links + one inside a code fence
  d="$LAB/lab-md-links"; py_project "$d" lab-md-links; runtests_cmd "$d"; mkdir -p "$d/docs"
  printf '# Docs\n\n- [Guide](guide.md)\n- [API reference](api.md)\n- [Website](https://example.com)\n' > "$d/docs/index.md"
  cat > "$d/docs/guide.md" <<'MD'
# Guide

Back to [the index](index.md). See [install](#install) and [setup notes](../SETUP.md).

## Install

```markdown
[not a real link](nope.md)
```
MD
  commit_all "$d" "init"

  # 2) rate limiter: empty project
  d="$LAB/lab-rate-limit"; py_project "$d" lab-rate-limit; runtests_cmd "$d"; commit_all "$d" "init"

  # 3) json-schema diff: empty project + two fixture schemas
  d="$LAB/lab-schema-diff"; py_project "$d" lab-schema-diff; runtests_cmd "$d"; mkdir -p "$d/fixtures"
  printf '{"type":"object","required":["id"],"properties":{"id":{"type":"integer"},"name":{"type":"string"},"address":{"type":"object","properties":{"zip":{"type":"string"}}}}}\n' > "$d/fixtures/old.schema"
  printf '{"type":"object","required":["id","email"],"properties":{"id":{"type":"string"},"email":{"type":"string"},"address":{"type":"object","properties":{"zip":{"type":"integer"},"city":{"type":"string"}}}}}\n' > "$d/fixtures/new.schema"
  commit_all "$d" "init"

  # 4) retry util: seeded off-by-one (one call short) + an intentionally flaky test
  d="$LAB/lab-retry-util"; py_project "$d" lab-retry-util; runtests_cmd "$d"
  cat > "$d/retry.py" <<'PY'
import time


def retry(fn, max_attempts=3, base_delay=0.01, factor=2.0, exceptions=(Exception,), sleep=time.sleep):
    """Call fn() until it succeeds. max_attempts is the total number of calls.

    Sleeps base_delay * factor**i between attempts (no sleep after the last one).
    Raises RuntimeError when every attempt failed.
    """
    for attempt in range(max_attempts - 1):
        try:
            return fn()
        except exceptions:
            sleep(base_delay * (factor ** attempt))
    raise RuntimeError(f"gave up after {max_attempts} attempts")
PY
  cat > "$d/tests/test_retry.py" <<'PY'
import random

import pytest

from retry import retry


def failing_n_times(n, value="ok"):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= n:
            raise ValueError("boom")
        return value
    return fn, calls


def test_first_try():
    fn, calls = failing_n_times(0)
    assert retry(fn, sleep=lambda d: None) == "ok"
    assert len(calls) == 1


def test_succeeds_on_last_attempt():
    fn, calls = failing_n_times(2)
    assert retry(fn, max_attempts=3, sleep=lambda d: None) == "ok"
    assert len(calls) == 3


def test_delays_grow_exponentially():
    seen = []
    fn, _ = failing_n_times(99)
    with pytest.raises(RuntimeError):
        retry(fn, max_attempts=4, base_delay=1, sleep=seen.append)
    assert seen == [1, 2, 4]


def test_flaky_network():
    # flaky on purpose: the "network" fails ~40% of the time
    def fetch():
        if random.random() < 0.4:
            raise IOError("timeout")
        return 200
    assert retry(fetch, max_attempts=2, sleep=lambda d: None) == 200
PY
  commit_all "$d" "init"

  install_hooks lab-md-links lab-rate-limit lab-schema-diff lab-retry-util
  log "setup set=v2 projects=lab-md-links,lab-rate-limit,lab-schema-diff,lab-retry-util (claude only, no codex)"
}

set_launch() {
  start_agent lab-md-links    "$LAB/lab-md-links"    lab-mdl    "$(claude_cmd)"
  start_agent lab-rate-limit  "$LAB/lab-rate-limit"  lab-rate   "$(claude_cmd)"
  start_agent lab-schema-diff "$LAB/lab-schema-diff" lab-schema "$(claude_cmd)"
  start_agent lab-retry-util  "$LAB/lab-retry-util"  lab-retry  "$(claude_cmd)"
}

set_drive() {
  # t0: all four start in parallel
  P lab-schema good-spec "Create schema_diff.py with diff(old, new) returning a list of changes between two JSON Schemas (dicts). Scope: only 'properties' (recurse into nested objects), 'required' and 'type'. Each change is a dict {path, kind, old, new}; kind is one of added, removed, type_changed, now_required, no_longer_required; path is dotted, e.g. address.zip. CLI: python3 schema_diff.py OLD NEW prints one line per change and exits 1 if any change is breaking (removed, type_changed, now_required), else 0. Out of scope: \$ref, array items, oneOf. Acceptance: tests/test_schema_diff.py covers every kind plus one nested case; python3 -m pytest -q passes; running it on fixtures/old.schema fixtures/new.schema exits 1. Run pytest at the end. $NOQ"
  P lab-mdl task "Write a link checker for the markdown files in this repo. $NOQ"
  P lab-rate not-asked-to-test "Implement a token bucket rate limiter in ratelimit.py: class TokenBucket(capacity, refill_rate) with allow(cost=1) -> bool. Just write the code; don't run tests or any commands. $NOQ"
  P lab-retry not-asked-to-test "Add a jitter=False option to retry() in retry.py: when True, add a random 0..delay extra to each sleep. Change nothing else. Just write the code; don't run anything. $NOQ"

  # md-links: Esc once the agent starts producing output, then redirect within the first turns
  interrupt_when_output lab-mdl "⏺"
  P lab-mdl early-redirect "Actually, narrower: only check relative links to local files and #anchors, no HTTP requests. Print file:line: link for each broken one and exit 1 if any. Put it in mdlinks.py. $NOQ"
  working_after_prompt lab-mdl 20; sleep 10
  if [ "$(aget lab-mdl | cut -d' ' -f1)" = working ]; then P lab-mdl queued "When you're done, also print a final summary line like '2 broken links in 1 file'."; else log "skip queued (lab-mdl not working)"; fi

  # rate: clarification, hand edit of the agent's file, then a correction
  W lab-rate
  P lab-rate clarification "To clarify: capacity is the burst size, refill_rate is tokens per second, and allow() should take an optional now= timestamp so it can be tested without sleeping. Don't run anything."
  W lab-rate
  if [ -f "$LAB/lab-rate-limit/ratelimit.py" ]; then
    printf '\n# NOTE(human): capacity == max burst; keep refill math in floats.\n' >> "$LAB/lab-rate-limit/ratelimit.py"
    log "hand-edit lab-rate ratelimit.py (append 2 lines to agent-written file, outside agent)"
  else
    echo "Hand-edited by the lab orchestrator." >> "$LAB/lab-rate-limit/README.md"
    log "hand-edit lab-rate README.md (append 1 line, outside agent; ratelimit.py missing)"
  fi
  P lab-rate correction "No, don't use time.time() for the clock, use time.monotonic(). And make allow() thread-safe with a threading.Lock. Keep my NOTE comment."

  # retry: verification ask finds the seeded off-by-one (and maybe the flaky test)
  W lab-retry
  P lab-retry verification-catch "Now run the tests and check the attempt counts and delays are actually right."
  W lab-retry
  P lab-retry fix-after-catch "Fix retry() so max_attempts really is the total number of calls, and make test_flaky_network deterministic (seed or monkeypatch random). Then run pytest 3 times to confirm it's stable."

  # schema: after the good spec, a trivial README edit done by hand instead of prompting, then scoped turns
  W lab-schema 600
  printf '\n## Usage\n\n    python3 schema_diff.py OLD NEW   # exit 1 on breaking changes\n' >> "$LAB/lab-schema-diff/README.md"
  log "hand-edit-trivial lab-schema README.md usage section (done by hand instead of prompting)"
  P lab-schema scoped-turn "Sort the CLI output by path. Change nothing else."
  W lab-schema
  P lab-schema scoped-turn "Add a --quiet flag: print nothing, only set the exit code. Change nothing else."

  # md-links: human asks to verify the result against known broken links
  W lab-mdl
  P lab-mdl verification-catch "Run it on docs/ and check it reports exactly the two broken links (api.md and ../SETUP.md) and nothing from the code fence."

  # rate: asked to test at the end
  W lab-rate
  P lab-rate asked-to-test "Add tests in tests/test_ratelimit.py for burst, refill over time (using now=) and cost>1, and run pytest to verify they pass."

  # schema: human tooling event (project slash command)
  W lab-schema
  P lab-schema slash-command "/runtests"

  for a in lab-retry lab-mdl lab-rate lab-schema; do W "$a"; done
}
