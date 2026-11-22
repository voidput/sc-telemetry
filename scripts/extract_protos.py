"""Extract the telemetry schema from a StarCitizen.exe into a directory of .proto files.

usage: extract_protos.py <exe> <outdir> [name_prefix]
Normally not needed: the bridge extracts at runtime and caches under .tmp/proto/ (see sctelemetry/schema.py).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sctelemetry.schema import extract  # noqa: E402

extract(sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "sc/game/telemetry/")
