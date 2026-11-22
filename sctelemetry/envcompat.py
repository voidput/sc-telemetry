"""Environment variable lookup with the pre-rename fallback.

The project was renamed scgameapi -> sc-telemetry (2026-11-21). Settings now live in SCTELEMETRY_<NAME>;
the old SCGAMEAPI_<NAME> is still honoured when the new one is unset, with a one-time stderr deprecation
note per variable. Remove the fallback in a future release.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

PREFIX = "SCTELEMETRY_"
LEGACY_PREFIX = "SCGAMEAPI_"
_warned: set[str] = set()


def getenv(name: str, default: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    """SCTELEMETRY_<name>, else the deprecated SCGAMEAPI_<name> (warns once), else default.
    Empty values count as unset, matching the old `os.environ.get(...) or default` call sites."""
    env = os.environ if env is None else env
    v = env.get(PREFIX + name)
    if v:
        return v
    old = env.get(LEGACY_PREFIX + name)
    if old:
        if name not in _warned:
            _warned.add(name)
            print(f"[sc-telemetry] ${LEGACY_PREFIX}{name} is deprecated; use ${PREFIX}{name}",
                  file=sys.stderr, flush=True)
        return old
    return default
