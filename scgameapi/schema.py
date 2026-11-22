"""Runtime schema extraction: pull the telemetry FileDescriptorProtos out of the user's own
StarCitizen.exe and cache them per game build. No CIG-derived files are shipped in this repo.

Resolution order (first hit wins):
  1. SCGAMEAPI_PROTO_DIR  - an explicit directory of .proto files (dev override)
  2. SCGAMEAPI_EXE        - extract from this exe (cache key: size+mtime)
  3. install dir + channel -> <SC_DIR>/<CHANNEL>/Bin64/StarCitizen.exe. Install dir: $SC_DIR, else
     auto-detected (scgameapi/paths.py); channel: $SCGAMEAPI_CHANNEL, else the last-played one, else LIVE
     (cache key: Branch-RequestedP4ChangeNum from <channel>/build_manifest.id)
  4. newest existing cache under .tmp/proto/ (so --mock/--replay work without the game installed)
"""
import hashlib
import json
import os
import pathlib
import sys

from google.protobuf import descriptor_pb2 as d

from . import paths

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / ".tmp" / "proto"
MARKER = "sc/game/telemetry/v1/api.proto"


FDP_FIELDS = {1: 2, 2: 2, 3: 2, 10: 0, 11: 0, 4: 2, 5: 2, 6: 2, 7: 2, 8: 2, 9: 2, 12: 2, 14: 0, 15: 2}


def varint(b, i):
    r = s = 0
    while True:
        if i >= len(b):
            raise ValueError
        c = b[i]; i += 1
        r |= (c & 0x7F) << s; s += 7
        if not c & 0x80:
            return r, i
        if s > 63:
            raise ValueError


def walk(b, start):
    """Walk wire-format fields valid for FileDescriptorProto; return end offset."""
    i = start; last = start
    while i < len(b):
        try:
            tag, j = varint(b, i)
            fn, wt = tag >> 3, tag & 7
            if fn not in FDP_FIELDS or FDP_FIELDS[fn] != wt:
                break
            if wt == 0:
                _, j = varint(b, j)
            else:
                ln, j = varint(b, j)
                if j + ln > len(b):
                    break
                j += ln
            i = last = j
        except ValueError:
            break
    return last


LABEL = {1: "optional ", 2: "required ", 3: "repeated "}
TYPES = {1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64", 7: "fixed32",
         8: "bool", 9: "string", 12: "bytes", 13: "uint32", 15: "sfixed32", 16: "sfixed64",
         17: "sint32", 18: "sint64"}


def ftype(f, pkg):
    if f.type in (11, 14):
        n = f.type_name
        return n[len(pkg) + 2:] if n.startswith("." + pkg + ".") else n
    return TYPES[f.type]


def emit_enum(e, ind):
    out = [f"{ind}enum {e.name} {{"]
    for v in e.value:
        out.append(f"{ind}  {v.name} = {v.number};")
    out.append(f"{ind}}}")
    return out


def emit_msg(m, pkg, ind, syntax):
    out = [f"{ind}message {m.name} {{"]
    maps = {n.name: n for n in m.nested_type if n.options.map_entry}
    for e in m.enum_type:
        out += emit_enum(e, ind + "  ")
    for n in m.nested_type:
        if not n.options.map_entry:
            out += emit_msg(n, pkg, ind + "  ", syntax)
    oneofs = {}
    for f in m.field:
        if f.HasField("oneof_index") and not f.proto3_optional:
            oneofs.setdefault(f.oneof_index, []).append(f)
    done = set()
    for f in m.field:
        if f.HasField("oneof_index") and not f.proto3_optional:
            oi = f.oneof_index
            if oi in done:
                continue
            done.add(oi)
            out.append(f"{ind}  oneof {m.oneof_decl[oi].name} {{")
            for g in oneofs[oi]:
                out.append(f"{ind}    {ftype(g, pkg)} {g.name} = {g.number};")
            out.append(f"{ind}  }}")
            continue
        t = ftype(f, pkg)
        short = t.rsplit(".", 1)[-1]
        if f.label == 3 and short in maps:
            me = maps[short]
            out.append(f"{ind}  map<{ftype(me.field[0], pkg)}, {ftype(me.field[1], pkg)}> {f.name} = {f.number};")
            continue
        lab = "optional " if f.proto3_optional else ("repeated " if f.label == 3 else ("" if syntax == "proto3" else LABEL[f.label]))
        out.append(f"{ind}  {lab}{t} {f.name} = {f.number};")
    out.append(f"{ind}}}")
    return out


def emit(fd):
    syn = fd.syntax or "proto2"
    out = [f'syntax = "{syn}";', "", f"package {fd.package};", ""]
    out += [f'import "{dep}";' for dep in fd.dependency]
    if fd.dependency:
        out.append("")
    for e in fd.enum_type:
        out += emit_enum(e, "") + [""]
    for m in fd.message_type:
        out += emit_msg(m, fd.package, "", syn) + [""]
    for s in fd.service:
        out.append(f"service {s.name} {{")
        for r in s.method:
            cs = "stream " if r.client_streaming else ""
            ss = "stream " if r.server_streaming else ""
            out.append(f"  rpc {r.name}({cs}{ftype_name(r.input_type, fd.package)}) returns ({ss}{ftype_name(r.output_type, fd.package)});")
        out.append("}")
    return "\n".join(out) + "\n"


def ftype_name(n, pkg):
    return n[len(pkg) + 2:] if n.startswith("." + pkg + ".") else n


def extract(exe, outdir, prefix="sc/game/telemetry/", quiet=False):
    """Extract matching FileDescriptorProtos from exe into outdir as .proto text. Returns {name: FileDescriptorProto}."""
    outdir = pathlib.Path(outdir)
    prefix = prefix.encode()
    b = pathlib.Path(exe).read_bytes()
    outdir.mkdir(parents=True, exist_ok=True)
    seen = {}
    i = 0
    while (i := b.find(prefix, i)) != -1:
        # descriptor starts with 0x0A <len varint> <name>
        for back in (2, 3):
            s = i - back
            if s >= 0 and b[s] == 0x0A:
                try:
                    ln, j = varint(b, s + 1)
                except ValueError:
                    continue
                if j != i:
                    continue
                end = walk(b, s)
                fd = d.FileDescriptorProto()
                try:
                    fd.ParseFromString(b[s:end])
                except Exception:
                    continue
                if fd.name.startswith(prefix.decode()) and (fd.message_type or fd.service or fd.enum_type):
                    if fd.name not in seen or len(b[s:end]) > len(seen[fd.name].SerializeToString()):
                        seen[fd.name] = fd
        i += 1
    for name, fd in seen.items():
        p = outdir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(emit(fd), encoding="utf-8", newline="\n")
        (p.with_suffix(".pb")).write_bytes(fd.SerializeToString())
        quiet or print(f"{name}: {len(fd.message_type)} msgs, {len(fd.enum_type)} enums, {len(fd.service)} svcs, deps={list(fd.dependency)}")

    return seen


def _manifest_key(channel_dir):
    try:
        m = json.loads((channel_dir / "build_manifest.id").read_text(encoding="utf-8-sig"))
        data = m.get("Data", m)
        return f"{data['Branch']}-{data['RequestedP4ChangeNum']}"
    except Exception:
        return None


def _exe_key(exe):
    st = exe.stat()
    return "exe-" + hashlib.sha1(f"{exe.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]


def _ensure(exe, key, log):
    out = CACHE / key
    if (out / MARKER).exists():
        return out
    log(f"[schema] extracting telemetry schema from {exe} -> {out}")
    tmp = out.with_name(out.name + ".partial")
    seen = extract(exe, tmp, quiet=True)
    if MARKER not in seen:
        raise RuntimeError(f"no telemetry schema found in {exe} (build may not include GameTelemetryService)")
    (tmp / "SOURCE.json").write_text(json.dumps({"exe": str(exe), "key": key, "files": sorted(seen)}, indent=2), encoding="utf-8", newline="\n")
    if out.exists():
        import shutil
        shutil.rmtree(out)
    tmp.rename(out)
    return out


def resolve(log=lambda m: print(m, file=sys.stderr)):
    """Return a directory containing sc/game/telemetry/v1/*.proto for the selected build."""
    if os.environ.get("SCGAMEAPI_PROTO_DIR"):
        p = pathlib.Path(os.environ["SCGAMEAPI_PROTO_DIR"])
        if (p / MARKER).exists():
            return p
        raise RuntimeError(f"SCGAMEAPI_PROTO_DIR={p} has no {MARKER}")
    if os.environ.get("SCGAMEAPI_EXE"):
        exe = pathlib.Path(os.environ["SCGAMEAPI_EXE"])
        return _ensure(exe, _exe_key(exe), log)
    sc_dir = paths.find_sc_dir()
    channel = paths.default_channel(sc_dir)
    exe = (sc_dir / channel / "Bin64" / "StarCitizen.exe") if sc_dir else None
    if exe is not None and exe.exists():
        return _ensure(exe, _manifest_key(exe.parent.parent) or _exe_key(exe), log)
    cached = sorted((p for p in CACHE.glob("*") if (p / MARKER).exists()), key=lambda p: p.stat().st_mtime, reverse=True)
    what = str(exe) if exe is not None else "StarCitizen.exe (no install found)"
    if cached:
        log(f"[schema] {what} not found; using cached schema {cached[0].name}")
        return cached[0]
    raise RuntimeError(f"telemetry schema unavailable: {what} not found and no cache in {CACHE}. "
                       f"Searched: {paths.describe_search()}. Set SC_DIR (install dir holding LIVE/PTU) "
                       "and/or SCGAMEAPI_CHANNEL, or SCGAMEAPI_EXE=<path to StarCitizen.exe>.")
