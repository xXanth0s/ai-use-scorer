"""Price + context-window tables (per 1M tokens, USD).

  cost_usd(model, input_tok, cached_tok, cache_write_tok, output_tok) -> float | None  # None if unknown model
  context_window(model, tokens=None) -> int | None
  normalize(model) -> (base_id, has_1m_suffix)
  lookup(model) -> Price | None

Sources: Anthropic first-party API prices (claude-api skill table, cached
2026-10-06); OpenAI prices from third-party aggregators (OpenAI's own page not
reachable). Rows or fields marked `# estimate` are derived, not published.
Cache write = 5-minute write = 1.25x input (Anthropic's standard multiplier).

Matching: lowercase, strip provider prefixes (`anthropic.`, `us.anthropic.`,
`openai/`), strip `[1m]`-style suffixes and date suffixes (`-20250514`,
`@20250514`, `-v1:0`), then the longest table key that equals the id or is a
`-`-boundary prefix of it wins. Unknown models return None, never 0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

M = 1_000_000


@dataclass(frozen=True)
class Price:
    input: float          # $/1M uncached input
    cache_read: float     # $/1M cache read
    cache_write: float    # $/1M cache write (5 min)
    output: float         # $/1M output
    window: int | None    # context window, tokens
    # long-context tier: above `long_threshold` context tokens, input/cache rates x long_in, output x long_out
    long_threshold: int | None = None
    long_in: float = 1.0
    long_out: float = 1.0


PRICES: dict[str, Price] = {
    # --- Anthropic (first-party API) ---------------------------------------------------------------
    "claude-fable-5-1":  Price(10.00, 0.25, 12.50, 50.00, M),   # cache read $0.25 as published; write = estimate
    "claude-mythos-5-1": Price(10.00, 0.25, 12.50, 50.00, M),   # same as Fable 5.1; write = estimate
    "claude-fable-5":    Price(10.00, 1.00, 12.50, 50.00, M),   # cache read/write = estimate (0.1x / 1.25x)
    "claude-mythos-5":   Price(10.00, 1.00, 12.50, 50.00, M),   # estimate (Fable 5 tier)
    "claude-opus-5-5":   Price(4.00, 0.20, 5.00, 20.00, M),     # cache read published; write = estimate
    "claude-opus-5":     Price(5.00, 0.50, 6.25, 25.00, M),     # cache read/write = estimate
    "claude-opus-4-8":   Price(5.00, 0.50, 6.25, 25.00, M),     # cache read/write = estimate
    "claude-opus-4-7":   Price(5.00, 0.50, 6.25, 25.00, M),     # cache read/write = estimate
    "claude-opus-4-6":   Price(5.00, 0.50, 6.25, 25.00, M),     # cache read/write = estimate
    "claude-opus-4-5":   Price(5.00, 0.50, 6.25, 25.00, 200_000),
    "claude-opus-4-1":   Price(15.00, 1.50, 18.75, 75.00, 200_000),
    "claude-opus-4":     Price(15.00, 1.50, 18.75, 75.00, 200_000),
    "claude-sonnet-5-5": Price(2.00, 0.20, 2.50, 10.00, M),     # cache read published; write = estimate
    "claude-sonnet-5":   Price(2.00, 0.20, 2.50, 10.00, M),     # cache read/write = estimate
    "claude-sonnet-4-6": Price(3.00, 0.30, 3.75, 15.00, M),
    "claude-sonnet-4-5": Price(3.00, 0.30, 3.75, 15.00, 200_000),
    "claude-sonnet-4":   Price(3.00, 0.30, 3.75, 15.00, 200_000),
    "claude-3-7-sonnet": Price(3.00, 0.30, 3.75, 15.00, 200_000),
    "claude-3-5-sonnet": Price(3.00, 0.30, 3.75, 15.00, 200_000),
    # Haiku 5.5: $0.50/$2.50 beyond 100K-token prompts (published). cache read/write = estimate
    "claude-haiku-5-5":  Price(0.10, 0.01, 0.125, 0.50, M, long_threshold=100_000, long_in=5.0, long_out=5.0),
    "claude-haiku-4-5":  Price(1.00, 0.10, 1.25, 5.00, 200_000),
    "claude-3-5-haiku":  Price(0.80, 0.08, 1.00, 4.00, 200_000),
    # --- OpenAI (Codex) -----------------------------------------------------------------------------
    # window = model_context_window reported by Codex token_count events on this machine.
    # gpt-6-astra: aggregator figures ($10/$1/$50); cache write = estimate (OpenAI normally bills none).
    # >272K input reportedly 2x input/cache, 1.5x output.
    "gpt-6-astra":       Price(10.00, 1.00, 12.50, 50.00, 258_400, long_threshold=272_000, long_in=2.0, long_out=1.5),
    "gpt-6.1-sol":       Price(2.00, 0.20, 2.50, 10.00, 258_400),   # estimate: "GPT-6 Sol" family price
    "gpt-6-sol":         Price(2.00, 0.20, 2.50, 10.00, 258_400),   # estimate
    "gpt-5.6-sol":       Price(4.00, 0.40, 5.00, 20.00, 258_400),   # aggregator figures; window = estimate
}

_PREFIXES = ("us.anthropic.", "eu.anthropic.", "global.anthropic.", "anthropic.", "anthropic/", "openai/")
_BRACKET = re.compile(r"\[([^\]]*)\]")
_DATE = re.compile(r"(?:[-@]\d{8})?(?:-v\d+(?::\d+)?)?$")


def normalize(model: str | None) -> tuple[str, bool]:
    """-> (base id, has_1m_suffix)."""
    m = (model or "").strip().lower()
    for p in _PREFIXES:
        if m.startswith(p):
            m = m[len(p):]
    has_1m = any(x.strip() == "1m" for x in _BRACKET.findall(m))
    m = _BRACKET.sub("", m).strip()
    m = _DATE.sub("", m)
    return m, has_1m


def _key(base: str) -> str | None:
    best = None
    for k in PRICES:
        if base == k or base.startswith(k + "-"):
            if best is None or len(k) > len(best):
                best = k
    return best


def lookup(model: str | None) -> Price | None:
    base, _ = normalize(model)
    k = _key(base) if base else None
    return PRICES[k] if k else None


def cost_usd(model, input_tok, cached_tok, cache_write_tok, output_tok):
    """USD for one API call, or None if the model is unknown (never 0 for unknown)."""
    p = lookup(model)
    if p is None:
        return None
    i, c, w, o = (int(x or 0) for x in (input_tok, cached_tok, cache_write_tok, output_tok))
    mi = mo = 1.0
    if p.long_threshold and (i + c + w) > p.long_threshold:
        mi, mo = p.long_in, p.long_out
    return (i * p.input * mi + c * p.cache_read * mi + w * p.cache_write * mi + o * p.output * mo) / M


def context_window(model, tokens=None):
    """Context window in tokens. `[1m]` suffix -> 1M. Claude models default to 200k when
    unknown; if `tokens` (observed context) exceeds the window of a Claude model, assume 1M.
    Unknown non-Claude -> None."""
    base, has_1m = normalize(model)
    if has_1m:
        return M
    p = lookup(model)
    w = p.window if p else (200_000 if base.startswith("claude") else None)
    if w and tokens and tokens > w and base.startswith("claude") and w < M:
        w = M
    return w
