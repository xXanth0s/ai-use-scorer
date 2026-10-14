"""~/.scorer/config.toml (under SCORER_HOME). All keys optional.

  timezone = "Europe/Berlin"      # default: system tz
  day_cutoff = "04:00"            # turns before it belong to the previous day
  report_time = "18:00"           # daily launchd/cron ingest + report
  judge_model = "claude-haiku-5-5"
  analyst_model = "claude-opus-5-5"
  [prices."claude-opus-5-5"]      # USD per 1M tokens; overrides prices.py
  input = 5.0
  cached = 0.5
  cache_write = 6.25
  output = 25.0
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import time
from functools import lru_cache

from .model import scorer_home


@dataclass(frozen=True)
class Config:
    timezone: str | None = None
    day_cutoff: time = time(4, 0)
    report_time: time = time(18, 0)
    judge_model: str = "claude-haiku-5-5"
    analyst_model: str = "claude-opus-5-5"
    prices: dict = field(default_factory=dict)


def _hhmm(s: str, default: time) -> time:
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return default


@lru_cache(maxsize=1)
def load() -> Config:
    p = scorer_home() / "config.toml"
    try:
        d = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        d = {}
    base = Config()
    return Config(
        timezone=d.get("timezone") or None,
        day_cutoff=_hhmm(d.get("day_cutoff", "04:00"), base.day_cutoff),
        report_time=_hhmm(d.get("report_time", "18:00"), base.report_time),
        judge_model=d.get("judge_model", base.judge_model),
        analyst_model=d.get("analyst_model", base.analyst_model),
        prices=d.get("prices") or {},
    )
