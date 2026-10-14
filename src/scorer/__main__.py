"""Entry point. `scorer event` takes a fast path that never imports click or sqlite."""
from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["event"]:
        from .hookio import main_event  # stdlib only; exits 0 in finally
        main_event(argv[1:])
        return
    from .cli import cli
    cli()


if __name__ == "__main__":
    main()
