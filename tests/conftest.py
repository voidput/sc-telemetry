import os

import pytest


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch):
    """A developer shell may still export the pre-rename SCGAMEAPI_* names; the envcompat fallback would
    leak them into tests that only clear SCTELEMETRY_*."""
    for k in [k for k in os.environ if k.startswith("SCGAMEAPI_")]:
        monkeypatch.delenv(k)
