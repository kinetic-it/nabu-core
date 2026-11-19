"""Generation-fenced, write-ahead volume recovery records in the alarm DB."""

import json
import math
from uuid import uuid4


class RecoveryConflict(RuntimeError):
    """A different alarm generation owns the volume record."""


class VolumeJournal:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _key(target):
        if not isinstance(target, str) or not target.startswith("media_player."):
            raise ValueError("invalid volume target")
        return "volume_recovery:" + target

    @staticmethod
    def _level(level):
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError("invalid volume")
        if not math.isfinite(level) or not 0 <= level <= 1:
            raise ValueError("invalid volume")
        return float(level)

    def get(self, target):
        key = self._key(target)
        row = self.store._run_read(
            lambda db: db.execute("SELECT value FROM alarm_metadata WHERE key=?", (key,)).fetchone()
        )
        return None if row is None else json.loads(row["value"])

    def begin(self, target, baseline):
        key, generation = self._key(target), str(uuid4())
        record = {
            "generation": generation,
            "baseline": self._level(baseline),
            "written": None,
            "attempted": None,
        }

        def action(db):
            if db.execute("SELECT 1 FROM alarm_metadata WHERE key=?", (key,)).fetchone():
                raise RecoveryConflict("volume recovery already pending")
            db.execute(
                "INSERT INTO alarm_metadata(key,value) VALUES (?,?)", (key, json.dumps(record))
            )

        self.store._run_write(action)
        return generation

    def _change(self, target, generation, field=None, level=None):
        key = self._key(target)

        def action(db):
            row = db.execute("SELECT value FROM alarm_metadata WHERE key=?", (key,)).fetchone()
            if row is None:
                raise RecoveryConflict("volume recovery record missing")
            record = json.loads(row["value"])
            if record.get("generation") != generation:
                raise RecoveryConflict("volume recovery generation changed")
            if field is None:
                db.execute("DELETE FROM alarm_metadata WHERE key=?", (key,))
            else:
                record[field] = self._level(level)
                if field == "written":
                    record["attempted"] = None
                db.execute(
                    "UPDATE alarm_metadata SET value=? WHERE key=?", (json.dumps(record), key)
                )

        self.store._run_write(action)

    def attempt(self, target, generation, level):
        self._change(target, generation, "attempted", level)

    def confirm(self, target, generation, level):
        self._change(target, generation, "written", level)

    def clear(self, target, generation):
        self._change(target, generation)
