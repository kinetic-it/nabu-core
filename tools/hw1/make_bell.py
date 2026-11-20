#!/usr/bin/env python3
"""H-W1: build the ring file the measurement uses (4 s tone, 3 s pause, 30 s total).

Why this file and not a music track: the alarm has to be boring, repeatable and
recognisable from the next room, and it must not contain speech that the STT could
pick up as a command.

    python3 tools/hw1/make_bell.py out.wav --tone 4 --pause 3 --total 30 --freq 880
"""

from __future__ import annotations

import argparse
import math
import struct
import wave

RATE = 48000          # what hassmic asks Home Assistant for (MEDIA_RATE in proto_esphome.c)


def build(path: str, tone: float, pause: float, total: float, freq: float, amp: float) -> float:
    if tone <= 0 or total <= 0:
        raise SystemExit("tone and total must be positive")
    frames = bytearray()
    cycle = tone + max(pause, 0.0)
    t = 0.0
    while t < total:
        in_tone = (t % cycle) < tone
        if in_tone:
            # 6 ms fade at both ends of every tone burst: no click, no DC step
            edge = min(0.006, (t % cycle), tone - (t % cycle))
            gain = amp * (edge / 0.006 if edge < 0.006 else 1.0)
            value = int(32767 * gain * math.sin(2 * math.pi * freq * t))
        else:
            value = 0
        frames += struct.pack("<h", value)
        t += 1.0 / RATE
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))
    return len(frames) / 2 / RATE


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--tone", type=float, default=4.0)
    parser.add_argument("--pause", type=float, default=3.0)
    parser.add_argument("--total", type=float, default=30.0)
    parser.add_argument("--freq", type=float, default=880.0)
    parser.add_argument("--amp", type=float, default=0.35, help="0..1, keep well below clipping")
    args = parser.parse_args()
    seconds = build(args.path, args.tone, args.pause, args.total, args.freq, args.amp)
    print(f"{args.path}: {seconds:.1f}s, {args.tone:g}s tone / {args.pause:g}s pause @ {args.freq:g} Hz")
