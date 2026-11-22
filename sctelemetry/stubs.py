"""Compile the runtime-extracted telemetry schema into Python stubs and import them.

The schema comes from the user's own StarCitizen.exe (see schema.py); stubs are generated
per game build under .tmp/gen/<build-key>/. Nothing generated or CIG-derived is committed.
"""
import importlib
import pathlib
import sys

from . import schema

ROOT = schema.ROOT
GEN_ROOT = ROOT / ".tmp" / "gen"
_loaded = None


def proto_dir() -> pathlib.Path:
    return schema.resolve()


def load():
    """Return (api_pb2, api_pb2_grpc, types_pb2) for the selected game build."""
    global _loaded
    if _loaded:
        return _loaded
    from grpc_tools import protoc

    src = proto_dir()
    gen = GEN_ROOT / src.name
    marker = gen / "sc" / "game" / "telemetry" / "v1" / "api_pb2_grpc.py"
    if not marker.exists():
        gen.mkdir(parents=True, exist_ok=True)
        protos = [p.relative_to(src).as_posix() for p in src.rglob("*.proto")]  # protoc virtual paths use "/"
        rc = protoc.main(["protoc", f"-I{src}", f"--python_out={gen}", f"--grpc_python_out={gen}", *protos])
        if rc:
            raise RuntimeError(f"protoc failed rc={rc}")
    if str(gen) not in sys.path:
        sys.path.insert(0, str(gen))
    _loaded = (
        importlib.import_module("sc.game.telemetry.v1.api_pb2"),
        importlib.import_module("sc.game.telemetry.v1.api_pb2_grpc"),
        importlib.import_module("sc.game.telemetry.v1.types_pb2"),
    )
    return _loaded
