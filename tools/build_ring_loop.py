#!/usr/bin/env python3
"""Baue aus einem Weckerton eine Schleife mit Ton und Stille.

Der Klingelrhythmus steckt dann in der Audiodatei (z. B. 4 s Ton, 3 s Stille)
statt in Pegelwechseln. Pegelwechsel auf 0.0 beenden am Echo 2 die Wiedergabe
(R31-Hardwarebefund), also erzeugte der alte Zyklus die Player-Stopps selbst,
die er vermeiden sollte.

Der Ton wird unveraendert wiederholt - kein Filter, keine Pegelaenderung,
gleiche Abtastrate, Kanalzahl und Bittiefe wie die Quelle.

Usage::

    python3 tools/build_ring_loop.py alarm_2s.wav alarm_loop7s.wav
    python3 tools/build_ring_loop.py in.wav out.wav --tone 4 --silence 3
"""

from __future__ import annotations

import argparse
import hashlib
import wave
from pathlib import Path


def build(source: Path, target: Path, tone_seconds: float, silence_seconds: float) -> str:
    with wave.open(str(source), "rb") as handle:
        params = handle.getparams()
        if params.comptype != "NONE":
            raise SystemExit("source must be uncompressed PCM")
        tone = handle.readframes(handle.getnframes())

    frame_width = params.sampwidth * params.nchannels
    tone_frames = len(tone) // frame_width
    if tone_frames == 0:
        raise SystemExit("source contains no audio frames")
    repeats = max(1, round(tone_seconds * params.framerate / tone_frames))
    silence = b"\x00" * (int(silence_seconds * params.framerate) * frame_width)

    with wave.open(str(target), "wb") as out:
        out.setparams(params)
        out.writeframes(tone * repeats + silence)

    with wave.open(str(target), "rb") as check:
        duration = check.getnframes() / check.getframerate()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(
        f"{target}: {duration:.3f}s "
        f"({tone_frames / params.framerate:.3f}s Ton x{repeats} + {silence_seconds:.3f}s Stille), "
        f"{params.framerate} Hz, {params.nchannels} ch, {params.sampwidth * 8} bit, sha256 {digest}"
    )
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument(
        "--tone", type=float, default=4.0, help="Sekunden Ton (Vielfache der Quelle)"
    )
    parser.add_argument("--silence", type=float, default=3.0, help="Sekunden Stille am Ende")
    args = parser.parse_args()
    if args.tone <= 0 or args.silence < 0:
        raise SystemExit("--tone must be positive and --silence must not be negative")
    build(args.source, args.target, args.tone, args.silence)


if __name__ == "__main__":
    main()
