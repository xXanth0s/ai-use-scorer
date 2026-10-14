"""Price + context-window tables. STUB — owned by agent C; keep these signatures.

  cost_usd(model, input_tok, cached_tok, cache_write_tok, output_tok) -> float | None  # None if unknown model
  context_window(model) -> int | None
"""
from __future__ import annotations


def cost_usd(model, input_tok, cached_tok, cache_write_tok, output_tok):
    return None


def context_window(model):
    return None
