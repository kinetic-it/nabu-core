#!/usr/bin/env python3
"""H-W1: one row per attempt, and a summary that refuses to guess.

    python3 tools/hw1/protokoll.py add --file hw1.jsonl --reihe P1-30 \
        --wakeword ja --pipeline ja --transkript ja --stopp ja \
        --stille-s 1.8 --falschaktivierung nein --wiederhergestellt ja --notiz "..."

    python3 tools/hw1/protokoll.py report --file hw1.jsonl

The four stages stay separate on purpose: a wakeword hit is not a pipeline start, a
pipeline start is not a transcript, and a transcript is not a stop.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

FIELDS = ("reihe", "versuch", "wakeword", "pipeline", "transkript", "stopp",
          "stille_s", "falschaktivierung", "wiederhergestellt", "notiz")


def add(args: argparse.Namespace) -> None:
    row = {name: getattr(args, name) for name in FIELDS}
    row["zeit"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(args.file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"notiert: {row['reihe']} #{row['versuch']} wakeword={row['wakeword']} stopp={row['stopp']}")


def report(args: argparse.Namespace) -> None:
    rows: list[dict] = []
    with open(args.file, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        print("keine Zeilen")
        return
    series: dict[str, list[dict]] = {}
    for row in rows:
        series.setdefault(row["reihe"], []).append(row)
    print(f"{'Reihe':<10} {'n':>2} {'Wakeword':>9} {'Pipeline':>9} {'STT':>5} {'Stopp':>6} {'Stille s':>9}")
    for name, items in series.items():
        total = len(items)
        yes = lambda key: sum(1 for item in items if item.get(key) == "ja")  # noqa: E731
        latencies = [item["stille_s"] for item in items
                     if isinstance(item.get("stille_s"), (int, float))]
        median = f"{statistics.median(latencies):.2f}" if latencies else "-"
        print(f"{name:<10} {total:>2} {yes('wakeword'):>6}/{total:<2} {yes('pipeline'):>6}/{total:<2} "
              f"{yes('transkript'):>2}/{total:<2} {yes('stopp'):>3}/{total:<2} {median:>9}")
    print("\nHinweis: Das sind Zählungen aus dem Protokoll, keine Erkennungsraten. "
          "Erst ab 10 Versuchen je Reihe ist eine Rate überhaupt diskutierbar.")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add")
    add_p.add_argument("--file", required=True)
    add_p.add_argument("--reihe", required=True)
    add_p.add_argument("--versuch", type=int, required=True)
    for name in ("wakeword", "pipeline", "transkript", "stopp", "falschaktivierung", "wiederhergestellt"):
        add_p.add_argument(f"--{name}", choices=("ja", "nein", "unklar"), required=True)
    add_p.add_argument("--stille-s", type=float, default=None)
    add_p.add_argument("--notiz", default="")
    add_p.set_defaults(func=add)

    rep_p = sub.add_parser("report")
    rep_p.add_argument("--file", required=True)
    rep_p.set_defaults(func=report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
