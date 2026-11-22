"""Probe SC local GameTelemetryService.

usage:
  uv run python scripts/telemetry_probe.py [--addr 127.0.0.1:50051] [--tls] stream [--n 5]
  uv run python scripts/telemetry_probe.py ... actions
Uses the runtime-extracted schema (scgameapi/schema.py); stubs cached in .tmp/gen.
"""
import argparse, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_stubs():
    from scgameapi import stubs
    api, apig, _ = stubs.load()
    return api, apig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:50051")
    ap.add_argument("--tls", action="store_true", help="TLS without verification of CA (uses system roots)")
    ap.add_argument("cmd", choices=["stream", "actions", "trigger"])
    ap.add_argument("--action", help="action_name for trigger")
    ap.add_argument("--hold-ms", type=int)
    ap.add_argument("--n", type=int, default=3, help="frames to print (stream)")
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args()

    import grpc
    from google.protobuf import json_format
    pb, pbg = load_stubs()
    ch = grpc.secure_channel(a.addr, grpc.ssl_channel_credentials()) if a.tls else grpc.insecure_channel(a.addr)
    stub = pbg.GameTelemetryServiceStub(ch)
    try:
        if a.cmd == "trigger":
            if not a.action:
                sys.exit("--action required")
            req = pb.TriggerGameActionRequest(action_name=a.action)
            if a.hold_ms is not None:
                req.hold_ms = a.hold_ms
            print(json_format.MessageToJson(stub.TriggerGameAction(req, timeout=5)))
        elif a.cmd == "actions":
            r = stub.ListTriggerableActions(pb.ListTriggerableActionsRequest(), timeout=5)
            print(json_format.MessageToJson(r))
        else:
            for i, frame in enumerate(stub.TelemetryStream(pb.TelemetryStreamRequest(), timeout=a.timeout)):
                print(f"--- frame {i}")
                print(json_format.MessageToJson(frame, always_print_fields_with_no_presence=False))
                if i + 1 >= a.n:
                    break
    except grpc.RpcError as e:
        sys.exit(f"RPC error: {e.code()} {e.details()}")


main()
