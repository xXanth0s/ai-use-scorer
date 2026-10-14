"""Shared, generic test fixtures. Keep minimal: other test modules depend on this."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def scorer_home(tmp_path, monkeypatch):
    """Every test gets its own SCORER_HOME so nothing touches ~/.scorer."""
    home = tmp_path / "scorer_home"
    monkeypatch.setenv("SCORER_HOME", str(home))
    return home
