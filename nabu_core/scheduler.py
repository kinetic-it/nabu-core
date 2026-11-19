"""Host-driven persistent alarm occurrence orchestration.

``Scheduler`` is deliberately not a service: the host must call
``await scheduler.tick(now)`` at its chosen cadence.  Every due schedule is
atomically claimed as one durable occurrence before its target is handed to the
serial dispatcher.  Terminal results, skipped alarms, and interrupted work are
all retained; failed work is never replayed automatically.

A POSIX advisory ``flock`` tied to the SQLite database is held for each complete
``tick`` and ``recover_interrupted`` operation.  Acquisition is nonblocking, so
recovery refuses to touch claims while another scheduler actively ticks.  This
is intentionally Linux/POSIX-only; safe Windows lock portability is not proven.
The stable lockfile is never deleted.

``stop(now)`` persists a database-wide stop epoch and durable skipped rows before
asking its local dispatcher to stop.  This fences subsequent claims in concurrent
schedulers, but it cannot prove physical silence or close the unavoidable race
between final preflight and an external player's actual start.
"""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

try:  # POSIX only; see module documentation.
    import fcntl
except ImportError:  # pragma: no cover - unavailable on the supported Linux host
    fcntl = None

from .alarm_engine import DEFAULT_CATCHUP_SECONDS, Alarm, AlarmNotFoundError, AlarmStore
from .dispatch import DispatchResult, SerialDispatcher
from .player import PlaybackOutcome
from .recurrence import InvalidLocalOccurrence, next_weekly


@dataclass(frozen=True, slots=True)
class Occurrence:
    """A durable result for one alarm instance at one scheduled UTC instant."""

    occurrence_id: str
    alarm_id: str
    instance_id: str
    due_utc: datetime
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ScheduleError:
    """A durable statement that one recurring rule could not be evaluated."""

    alarm_id: str
    instance_id: str
    code: str
    detail: str
    created_at: datetime


class Scheduler:
    """Persist due occurrences and send them one at a time through a dispatcher.

    Each alarm has its own catch-up window: the owner default is five minutes,
    a per-alarm value may override it and a per-alarm switch may disable
    catching up entirely. Occurrences older than the applicable window are
    persisted as ``skipped``/``missed`` and never played.
    """

    _STOP_EPOCH_KEY = "scheduler_stop_epoch"

    def __init__(
        self,
        store: AlarmStore,
        dispatcher,
        target: str,
        *,
        max_catchup: timedelta = timedelta(seconds=DEFAULT_CATCHUP_SECONDS),
        alarm_filter: Callable[[Alarm], bool] | None = None,
    ) -> None:
        if not isinstance(max_catchup, timedelta) or max_catchup < timedelta(0):
            raise ValueError("max_catchup must be a non-negative timedelta")
        self.store = store
        self.dispatcher = dispatcher
        self.target = target
        self.max_catchup = max_catchup
        # The pilot mode narrows which alarms this scheduler may ring. Stored
        # alarms outside the filter stay untouched and unclaimed, so switching
        # the pilot off restores the full set without any migration.
        self.alarm_filter = alarm_filter
        self.diagnostics: list[tuple[str, str]] = []
        self._owner_id = str(uuid4())
        self._lock_path = store.path.with_name(f"{store.path.name}.scheduler.lock")

    @staticmethod
    def _aware_utc(value: datetime, field: str = "now") -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _schedule_key(alarm: Alarm) -> str:
        if alarm.kind == "once":
            if alarm.when_utc is None:
                raise ValueError("once alarm has no due time")
            return f"once:{alarm.when_utc.astimezone(UTC).isoformat()}"
        return f"weekly:{alarm.local_time}:{','.join(map(str, alarm.weekdays))}"

    @staticmethod
    def _error_from_row(row: sqlite3.Row) -> ScheduleError:
        return ScheduleError(
            alarm_id=row["alarm_id"],
            instance_id=row["instance_id"],
            code=row["code"],
            detail=row["detail"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Occurrence:
        return Occurrence(
            occurrence_id=row["occurrence_id"],
            alarm_id=row["alarm_id"],
            instance_id=row["instance_id"],
            due_utc=datetime.fromisoformat(row["due_utc"]),
            status=row["status"],
            detail=row["detail"],
        )

    def _try_execution_lock(self) -> int | None:
        """Take the stable per-database POSIX execution lock without waiting."""
        if fcntl is None:
            raise RuntimeError("Scheduler requires POSIX flock; lock portability is not proven")
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise
        return fd

    @staticmethod
    def _release_execution_lock(fd: int) -> None:
        try:
            if fcntl is None:
                raise RuntimeError("POSIX flock is required")
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @classmethod
    def _epoch(cls, db: sqlite3.Connection) -> int:
        row = db.execute(
            "SELECT value FROM alarm_metadata WHERE key = ?", (cls._STOP_EPOCH_KEY,)
        ).fetchone()
        if row is None:
            raise RuntimeError("scheduler stop epoch is missing")
        try:
            return int(row["value"])
        except ValueError as error:
            raise RuntimeError("scheduler stop epoch is invalid") from error

    def _current_epoch(self) -> int:
        return self.store._run_read(self._epoch)

    def occurrences(self) -> list[Occurrence]:
        """Return every persisted occurrence, including interrupted and failed rows."""
        rows = self.store._run_read(
            lambda db: db.execute(
                "SELECT * FROM alarm_occurrences ORDER BY due_utc, occurrence_id"
            ).fetchall()
        )
        return [self._from_row(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Mark abandoned work only when no scheduler currently holds the tick lock."""
        lock_fd = self._try_execution_lock()
        if lock_fd is None:
            return 0
        try:
            return self._recover_locked()
        finally:
            self._release_execution_lock(lock_fd)

    def _recover_locked(self) -> int:
        """A claim left while we own the tick lock has no living executor."""
        finished = datetime.now(UTC).isoformat()

        def action(db: sqlite3.Connection) -> int:
            cursor = db.execute(
                "UPDATE alarm_occurrences "
                "SET status='interrupted', detail='interrupted_after_restart', "
                "claimed_by=NULL, finished_at=? WHERE status='in_flight'",
                (finished,),
            )
            return cursor.rowcount

        return self.store._run_write(action)

    def _record_schedule_error(self, alarm: Alarm, code: str, detail: str) -> None:
        """Persist a recurring schedule problem so a restart cannot hide it."""
        created = datetime.now(UTC).isoformat()

        def action(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT OR IGNORE INTO alarm_schedule_errors("
                "instance_id, alarm_id, code, detail, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (alarm.instance_id, alarm.id, code, detail, created),
            )

        self.store._run_write(action)

    def schedule_errors(self) -> list[ScheduleError]:
        """Return durable schedule problems for every alarm, including restarts."""
        rows = self.store._run_read(
            lambda db: db.execute(
                "SELECT * FROM alarm_schedule_errors ORDER BY created_at, alarm_id"
            ).fetchall()
        )
        return [self._error_from_row(row) for row in rows]

    def catchup_window(self, alarm: Alarm) -> timedelta:
        """Return the applicable catch-up window for one alarm."""
        if not alarm.catchup_enabled:
            return timedelta(0)
        if alarm.catchup_seconds is not None:
            return timedelta(seconds=alarm.catchup_seconds)
        return self.max_catchup

    def missed(self) -> list[Occurrence]:
        """Return occurrences that were recorded as missed, never as played."""
        rows = self.store._run_read(
            lambda db: db.execute(
                "SELECT * FROM alarm_occurrences WHERE status = ? AND detail = ? "
                "ORDER BY due_utc, occurrence_id",
                ("skipped", "missed"),
            ).fetchall()
        )
        return [self._from_row(row) for row in rows]

    def _latest_weekly_due(self, alarm: Alarm, now: datetime) -> datetime | None:
        if alarm.local_time is None:
            raise ValueError("weekly alarm has no wall time")
        from datetime import time

        wall_time = time.fromisoformat(alarm.local_time)
        cursor = now - timedelta(days=8)
        latest = None
        for _ in range(8):
            candidate = next_weekly(cursor, wall_time, set(alarm.weekdays), self.store.zone)
            if candidate > now:
                return latest
            latest = candidate
            cursor = candidate
        raise RuntimeError("weekly recurrence exceeded one-week search bound")

    def _due(self, now: datetime) -> list[tuple[Alarm, datetime]]:
        result = []
        for alarm in self.store.list():
            if not alarm.enabled:
                continue
            if self.alarm_filter is not None and not self.alarm_filter(alarm):
                continue
            if alarm.kind == "once":
                if alarm.when_utc is None:
                    raise ValueError("once alarm has no due time")
                due = alarm.when_utc.astimezone(UTC)
                if due <= now:
                    result.append((alarm, due))
                continue
            try:
                due = self._latest_weekly_due(alarm, now)
            except InvalidLocalOccurrence as error:
                self._record_schedule_error(alarm, "invalid_local_time", str(error))
                self.diagnostics.append((alarm.id, str(error)))
                continue
            if due is not None and due >= alarm.active_since.astimezone(UTC):
                result.append((alarm, due))
        return sorted(result, key=lambda item: (item[1], item[0].id))

    @staticmethod
    def _occurrence_id(alarm: Alarm, due: datetime) -> tuple[str, str, str]:
        due_text = due.astimezone(UTC).isoformat()
        return f"{alarm.instance_id}:{due_text}", due_text, Scheduler._schedule_key(alarm)

    def _claim(self, alarm: Alarm, due: datetime, epoch: int) -> Occurrence | None:
        occurrence_id, due_text, schedule_key = self._occurrence_id(alarm, due)
        created = datetime.now(UTC).isoformat()

        def action(db: sqlite3.Connection) -> Occurrence | None:
            if self._epoch(db) != epoch:
                return None
            if db.execute(
                "SELECT 1 FROM alarm_occurrences WHERE status = ? LIMIT 1", ("in_flight",)
            ).fetchone():
                return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO alarm_occurrences("
                "occurrence_id, alarm_id, instance_id, due_utc, schedule_key, status, "
                "detail, claimed_by, created_at, finished_at"
                ") VALUES (?, ?, ?, ?, ?, 'in_flight', '', ?, ?, NULL)",
                (
                    occurrence_id,
                    alarm.id,
                    alarm.instance_id,
                    due_text,
                    schedule_key,
                    self._owner_id,
                    created,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM alarm_occurrences WHERE occurrence_id = ?", (occurrence_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("persisted occurrence is missing")
            return self._from_row(row)

        return self.store._run_write(action)

    def _skip_missed(self, alarm: Alarm, due: datetime, epoch: int) -> Occurrence | None:
        """Persist a stale occurrence only if it is neither stopped nor blocked by work."""
        occurrence_id, due_text, schedule_key = self._occurrence_id(alarm, due)
        created = datetime.now(UTC).isoformat()

        def action(db: sqlite3.Connection) -> Occurrence | None:
            if self._epoch(db) != epoch:
                return None
            if db.execute(
                "SELECT 1 FROM alarm_occurrences WHERE status = ? LIMIT 1", ("in_flight",)
            ).fetchone():
                return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO alarm_occurrences("
                "occurrence_id, alarm_id, instance_id, due_utc, schedule_key, status, "
                "detail, claimed_by, created_at, finished_at"
                ") VALUES (?, ?, ?, ?, ?, 'skipped', 'missed', NULL, ?, ?)",
                (
                    occurrence_id,
                    alarm.id,
                    alarm.instance_id,
                    due_text,
                    schedule_key,
                    created,
                    created,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM alarm_occurrences WHERE occurrence_id = ?", (occurrence_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("persisted occurrence is missing")
            return self._from_row(row)

        return self.store._run_write(action)

    def _finish(self, occurrence_id: str, status: str, detail: str) -> Occurrence:
        if status not in {"completed", "failed", "skipped", "interrupted"}:
            raise ValueError(f"unsupported terminal occurrence status: {status!r}")
        finished = datetime.now(UTC).isoformat()

        def action(db: sqlite3.Connection) -> Occurrence:
            cursor = db.execute(
                "UPDATE alarm_occurrences SET status=?, detail=?, claimed_by=NULL, finished_at=? "
                "WHERE occurrence_id=? AND status='in_flight' AND claimed_by=?",
                (status, detail, finished, occurrence_id, self._owner_id),
            )
            row = db.execute(
                "SELECT * FROM alarm_occurrences WHERE occurrence_id = ?", (occurrence_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("claimed occurrence disappeared")
            if cursor.rowcount != 1 and row["status"] == "in_flight":
                raise RuntimeError("occurrence claim was lost before terminal persistence")
            self._retire_once_alarm(db, row["alarm_id"])
            return self._from_row(row)

        return self.store._run_write(action)

    @staticmethod
    def _retire_once_alarm(db: sqlite3.Connection, alarm_id: str) -> None:
        """A one-shot alarm is spent once it has run; keep the row, drop the schedule.

        Without this an expired one-shot stays ``enabled`` with a due time in the
        past. The occurrence journal stops it from ringing twice, but the alarm
        keeps showing up as active, and hosts that list alarms cannot tell a
        spent alarm from a pending one. Recurring alarms are untouched: they move
        on to their next occurrence.
        """
        db.execute("UPDATE alarms SET enabled=0 WHERE id=? AND kind='once'", (alarm_id,))

    def _is_still_dispatchable(self, occurrence: Occurrence, epoch: int) -> bool:
        """Fence the last DB-visible state before dispatch; player start still races stop."""
        # Preserve the public store seam used by hosts that validate or mutate
        # definitions during this preflight, then perform the final check in one
        # SQLite read transaction with the stop epoch.
        try:
            self.store.get(occurrence.alarm_id)
        except AlarmNotFoundError:
            return False

        def action(db: sqlite3.Connection) -> bool:
            if self._epoch(db) != epoch:
                return False
            row = db.execute("SELECT * FROM alarms WHERE id = ?", (occurrence.alarm_id,)).fetchone()
            if row is None:
                return False
            alarm = self.store._from_row(row)
            return (
                alarm.enabled
                and alarm.instance_id == occurrence.instance_id
                and self._schedule_key(alarm)
                == self._occurrence_schedule_key(db, occurrence.occurrence_id)
            )

        return self.store._run_read(action)

    @staticmethod
    def _occurrence_schedule_key(db: sqlite3.Connection, occurrence_id: str) -> str:
        row = db.execute(
            "SELECT schedule_key FROM alarm_occurrences WHERE occurrence_id = ?", (occurrence_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("claimed occurrence disappeared")
        return row["schedule_key"]

    def _persist_stop(self, now: datetime) -> tuple[str, ...]:
        due_items = self._due(now)
        finished = datetime.now(UTC).isoformat()

        # Only occurrences that were actually playing may later be reported as
        # confirmed; due-but-never-started ones stay "stopped".
        def action(db: sqlite3.Connection) -> tuple[str, ...]:
            epoch = self._epoch(db) + 1
            db.execute(
                "UPDATE alarm_metadata SET value = ? WHERE key = ?",
                (str(epoch), self._STOP_EPOCH_KEY),
            )
            rows = db.execute(
                "SELECT occurrence_id FROM alarm_occurrences WHERE status='in_flight'"
            ).fetchall()
            db.execute(
                "UPDATE alarm_occurrences SET status='skipped', "
                "detail='stop_requested_unconfirmed', claimed_by=NULL, finished_at=? "
                "WHERE status='in_flight'",
                (finished,),
            )
            for alarm, due in due_items:
                occurrence_id, due_text, schedule_key = self._occurrence_id(alarm, due)
                db.execute(
                    "INSERT OR IGNORE INTO alarm_occurrences("
                    "occurrence_id, alarm_id, instance_id, due_utc, schedule_key, status, "
                    "detail, claimed_by, created_at, finished_at"
                    ") VALUES (?, ?, ?, ?, ?, 'skipped', 'stopped', NULL, ?, ?)",
                    (
                        occurrence_id,
                        alarm.id,
                        alarm.instance_id,
                        due_text,
                        schedule_key,
                        finished,
                        finished,
                    ),
                )

            return tuple(row["occurrence_id"] for row in rows)

        return self.store._run_write(action)

    def is_ringing(self) -> bool:
        """Whether the dispatcher is playing right now."""
        return bool(getattr(self.dispatcher, "ringing", False))

    async def stop(self, now: datetime) -> bool:
        """Durably fence due work, then request local adapter stop without claiming silence."""
        now = self._aware_utc(now)
        try:
            ids = self._persist_stop(now)
        except Exception:
            # The journal can fail while audio is already active. Still request
            # a transport stop, then propagate the durable-state error.
            try:
                await self.dispatcher.stop()
            finally:
                raise
        confirmed = (await self.dispatcher.stop()) is True
        if confirmed and ids:
            self._mark_stop_confirmed(ids)
        return confirmed

    def _mark_stop_confirmed(self, ids: tuple[str, ...]) -> None:
        """Upgrade exactly the occurrences fenced by this stop call."""

        def action(db: sqlite3.Connection) -> None:
            db.executemany(
                "UPDATE alarm_occurrences SET detail='stop_confirmed' "
                "WHERE occurrence_id = ? AND status='skipped'",
                [(occurrence_id,) for occurrence_id in ids],
            )

        self.store._run_write(action)

    async def duck_alarm(self, seconds: float) -> PlaybackOutcome:
        return await self.dispatcher.duck(seconds)

    async def tick(self, now: datetime) -> list[Occurrence]:
        """Claim due alarms and persist one terminal result each without catch-up playback.

        The per-database lock spans the await to the dispatcher.  A non-owner
        returns immediately.  A stop arriving after final preflight but before
        an adapter begins physical playback remains an unavoidable external I/O
        boundary; the stop epoch is the latest feasible durable fence.
        """
        now_utc = self._aware_utc(now)
        self.diagnostics.clear()
        lock_fd = self._try_execution_lock()
        if lock_fd is None:
            return []
        try:
            self._recover_locked()
            if isinstance(self.dispatcher, SerialDispatcher):
                await self.dispatcher.reconcile_volume(self.target)
            epoch = self._current_epoch()
            finished = []
            for alarm, due in self._due(now_utc):
                if now_utc - due > self.catchup_window(alarm):
                    occurrence = self._skip_missed(alarm, due, epoch)
                    if occurrence is not None:
                        finished.append(occurrence)
                    continue
                occurrence = self._claim(alarm, due, epoch)
                if occurrence is None:
                    continue
                if not self._is_still_dispatchable(occurrence, epoch):
                    finished.append(
                        self._finish(
                            occurrence.occurrence_id, "skipped", "alarm_changed_or_unavailable"
                        )
                    )
                    continue
                try:
                    result: DispatchResult = await self.dispatcher.ring(self.target)
                except asyncio.CancelledError:
                    # Synchronous journal write: no await, so cancellation cannot
                    # skip it. The stop request is issued by the dispatcher; the
                    # physical result stays unknown and is never called silence.
                    self._finish(
                        occurrence.occurrence_id,
                        "interrupted",
                        "cancelled_stop_unconfirmed",
                    )
                    raise
                except Exception as error:
                    finished.append(
                        self._finish(occurrence.occurrence_id, "failed", type(error).__name__)
                    )
                    continue
                if result.status == "completed":
                    finished.append(
                        self._finish(occurrence.occurrence_id, "completed", result.detail)
                    )
                else:
                    finished.append(
                        self._finish(
                            occurrence.occurrence_id, "failed", result.detail or result.status
                        )
                    )
            return finished
        finally:
            self._release_execution_lock(lock_fd)
