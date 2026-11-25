"""Werkzeugpruefung fuer den reproduzierbaren Klingelzyklus.

Der Rhythmus eines Weckers steckt in der Audiodatei, nicht in Pegelwechseln:
Pegel 0.0 beendet am Echo 2 die Wiedergabe (R31-Hardwarebefund). Damit die Datei
nachvollziehbar bleibt, prueft dieser Test Aufbau, Laenge und die Stille am Ende.
"""

from __future__ import annotations

import importlib.util
import math
import struct
import wave
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "build_ring_loop.py"
_spec = importlib.util.spec_from_file_location("build_ring_loop", _TOOL)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build = _module.build

RATE = 16000


def make_tone(path: Path, seconds: float = 2.0, frequency: float = 440.0) -> Path:
    frames = int(seconds * RATE)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        samples = (
            int(12000 * math.sin(2 * math.pi * frequency * index / RATE)) for index in range(frames)
        )
        out.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    return path


def read(path: Path) -> tuple[int, int, bytes]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes(), handle.getframerate(), handle.readframes(handle.getnframes())


def test_the_default_block_is_four_seconds_tone_and_three_seconds_silence(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav")
    target = tmp_path / "loop7s.wav"

    digest = build(source, target, tone_seconds=4.0, silence_seconds=3.0)

    frames, rate, data = read(target)
    assert frames == 7 * rate
    assert len(data) == frames * 2
    assert all(byte == 0 for byte in data[-3 * rate * 2 :]), "Datei endet nicht mit Stille"
    assert data[: 2 * rate * 2] == data[2 * rate * 2 : 4 * rate * 2], "Ton wurde veraendert"
    assert len(digest) == 64


def test_a_duration_repeats_the_block_for_a_long_ring(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav")
    target = tmp_path / "loop63s.wav"

    build(source, target, tone_seconds=4.0, silence_seconds=3.0, duration=63.0)

    frames, rate, data = read(target)
    assert frames == 63 * rate
    assert all(byte == 0 for byte in data[-3 * rate * 2 :])


def test_a_duration_that_is_no_multiple_of_the_block_is_cut_exactly(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav")
    target = tmp_path / "loop10s.wav"

    build(source, target, tone_seconds=4.0, silence_seconds=3.0, duration=10.0)

    frames, rate, _ = read(target)
    assert frames == 10 * rate


def test_the_build_is_reproducible(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav")
    first = build(source, tmp_path / "a.wav", 4.0, 3.0)
    second = build(source, tmp_path / "b.wav", 4.0, 3.0)

    assert first == second


def test_a_duration_of_zero_is_rejected(tmp_path: Path) -> None:
    source = make_tone(tmp_path / "tone.wav")
    with pytest.raises(SystemExit):
        build(source, tmp_path / "x.wav", 4.0, 3.0, duration=0.0)
