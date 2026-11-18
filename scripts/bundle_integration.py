#!/usr/bin/env python3
"""Bundle ``nabu_core`` into the Home Assistant integration.

Home Assistant loads custom integrations from ``config/custom_components`` and
cannot import a top-level package that merely sits next to them. Copying the
package inside the integration and rewriting its absolute imports to relative
ones keeps the integration self-contained: no ``sys.path`` tricks, no pip
requirement, no dependency on anything outside this repository.

Usage::

    python3 scripts/bundle_integration.py           # write the bundle
    python3 scripts/bundle_integration.py --check   # fail if it is out of date
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "nabu_core"
TARGET = ROOT / "custom_components" / "kinetic_voice_router" / "nabu_core"
BRIDGE = ROOT / "custom_components" / "kinetic_voice_router" / "alarm_bridge.py"

PATTERN = re.compile(r"^(?P<indent>[ \t]*)from nabu_core(?P<module>\.[\w.]+)? import", re.MULTILINE)


def rewrite(text: str, *, in_package: bool) -> str:
    """Turn absolute ``nabu_core`` imports into relative ones.

    Inside the package ``from nabu_core.player import X`` becomes
    ``from .player import X``. Inside the integration module it becomes
    ``from .nabu_core.player import X``.
    """

    def replace(match: re.Match) -> str:
        indent = match.group("indent")
        module = match.group("module") or ""
        if in_package:
            return f"{indent}from .{module.lstrip('.')} import" if module else (
                f"{indent}from . import"
            )
        return f"{indent}from .nabu_core{module} import"

    return PATTERN.sub(replace, text)


def build() -> list[Path]:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(
        SOURCE, TARGET, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
    )
    written: list[Path] = []
    for path in sorted(TARGET.rglob("*.py")):
        original = path.read_text(encoding="utf8")
        path.write_text(rewrite(original, in_package=True), encoding="utf8")
        written.append(path)

    original = BRIDGE.read_text(encoding="utf8")
    BRIDGE.write_text(rewrite(original, in_package=False), encoding="utf8")
    written.append(BRIDGE)
    return written


def check() -> int:
    problems: list[str] = []
    if not TARGET.exists():
        print("BUNDLE_MISSING")
        return 1
    for source in sorted(SOURCE.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        mirror = TARGET / source.relative_to(SOURCE)
        if not mirror.exists():
            problems.append(f"missing {mirror.relative_to(ROOT)}")
            continue
        expected = rewrite(source.read_text(encoding="utf8"), in_package=True)
        if mirror.read_text(encoding="utf8") != expected:
            problems.append(f"stale {mirror.relative_to(ROOT)}")
    for extra in sorted(TARGET.rglob("*.py")):
        if not (SOURCE / extra.relative_to(TARGET)).exists():
            problems.append(f"orphan {extra.relative_to(ROOT)}")
    if problems:
        print("BUNDLE_OUT_OF_DATE")
        for problem in problems:
            print(" -", problem)
        return 1
    print("BUNDLE_UP_TO_DATE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check()
    print(f"BUNDLED={len(build())} files into {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
