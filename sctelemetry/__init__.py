"""sc-telemetry: local bridge + dashboard for Star Citizen's Game Telemetry gRPC API (unofficial)."""
from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("sc-telemetry")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
