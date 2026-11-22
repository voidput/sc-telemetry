"""Generate the bundled alert tones sctelemetry/data/sounds/{info,warn,critical}.wav (stdlib only).

Plain sine beeps with a short fade in/out: mono, 16-bit PCM, 22.05 kHz, a few KB each. WAV plays
everywhere (winsound on Windows, aplay/pw-play/paplay on Linux, afplay on macOS). The output is
committed; re-run only to change the tones:

  uv run python scripts/gen_tones.py
"""
import math
import pathlib
import struct
import wave

RATE = 22050
OUT = pathlib.Path(__file__).resolve().parents[1] / "sctelemetry" / "data" / "sounds"
FADE_S = 0.008

# (frequency Hz | None for silence, seconds)
TONES = {
    "info": [(880, 0.09), (None, 0.03), (1320, 0.12)],
    "warn": [(660, 0.15), (None, 0.08), (660, 0.15)],
    "critical": [(988, 0.14), (None, 0.04), (784, 0.14), (None, 0.04), (988, 0.14), (None, 0.04), (784, 0.20)],
}
GAIN = {"info": 0.35, "warn": 0.45, "critical": 0.55}


def render(parts, gain):
    frames = bytearray()
    for freq, dur in parts:
        n = int(RATE * dur)
        fade = max(1, int(RATE * FADE_S))
        for i in range(n):
            if freq is None:
                v = 0.0
            else:
                env = min(1.0, i / fade, (n - 1 - i) / fade)
                v = gain * env * math.sin(2 * math.pi * freq * i / RATE)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    return bytes(frames)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, parts in TONES.items():
        p = OUT / f"{name}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(render(parts, GAIN[name]))
        print(f"{p.relative_to(OUT.parents[2])}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
