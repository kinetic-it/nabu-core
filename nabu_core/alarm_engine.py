"""Persistent standalone SQLite alarm definitions.

The store owns exactly ten stable IDs (``alarm-01`` through ``alarm-10``).
It only persists alarm definitions: it neither schedules execution nor contacts
Home Assistant or a device. A mutation returns only after SQLite has committed
it, so callers can safely treat a returned :class:`Alarm` as durable state.

``create`` accepts an aware absolute :class:`datetime` or a positive
:class:`timedelta` resolved against an aware ``now`` (UTC by default). Passing
``weekdays`` makes the definition weekly; its local wall time is stored in the
configured IANA timezone. Nonexistent and ambiguous DST wall times are rejected
for weekly schedules because they cannot identify one recurring instant.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypeAlias, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

SLOT_IDS: Final = tuple(f"alarm-{number:02d}" for number in range(1, 11))
SCHEMA_VERSION: Final = 2
DEFAULT_CATCHUP_SECONDS: Final = 300
MAX_CATCHUP_SECONDS: Final = 86400
_UNSET = object()
When: TypeAlias = datetime | timedelta
_Result = TypeVar("_Result")


class AlarmError(Exception):
    """Base class for explicit alarm-store failures."""

    code = "ALARM_ERROR"


class ValidationError(AlarmError):
    """The supplied API value cannot form a valid stored schedule."""

    code = "INVALID_INPUT"


class DatabaseCompatibilityError(AlarmError):
    """The SQLite file is not this store's exact configured schema."""

    code = "DATABASE_INCOMPATIBLE"


class AlarmNotFoundError(AlarmError):
    """No persisted alarm has the requested stable ID."""

    code = "NOT_FOUND"


class NameConflictError(AlarmError):
    """Another alarm already owns the supplied casefolded name."""

    code = "NAME_CONFLICT"


class NoFreeSlotError(AlarmError):
    """All ten stable IDs are currently allocated."""

    code = "NO_FREE_SLOT"


@dataclass(frozen=True, slots=True)
class Alarm:
    """An immutable persisted alarm definition.

    ``instance_id`` is a UUID4 that identifies one allocation of a stable slot.
    It changes when a deleted slot is reused. For ``kind == 'once'``,
    ``when_utc`` is an aware UTC instant. For ``kind == 'weekly'``,
    ``local_time`` (``HH:MM:SS``) and zero-based ``weekdays`` (Monday is 0)
    define the recurrence.
    """

    id: str
    instance_id: str
    active_since: datetime
    catchup_seconds: int | None
    catchup_enabled: bool
    name: str | None
    enabled: bool
    kind: str
    when_utc: datetime | None
    local_time: str | None
    weekdays: tuple[int, ...]


class AlarmStore:
    """SQLite authority for isolated, durable alarm definitions.

    Each method opens and explicitly closes its own connection. Writes use
    ``BEGIN IMMEDIATE`` so competing writers serialize before choosing a free
    stable ID. A database is bound to its IANA timezone at initialization;
    existing files with another timezone or an older schema fail closed.
    """

    _ALARM_COLUMNS: Final = frozenset(
        {
            "id",
            "instance_id",
            "active_since",
            "catchup_seconds",
            "catchup_enabled",
            "name",
            "name_key",
            "enabled",
            "kind",
            "when_utc",
            "local_time",
            "weekdays",
        }
    )
    _METADATA_COLUMNS: Final = frozenset({"key", "value"})
    _SCHEDULE_ERROR_COLUMNS: Final = frozenset(
        {"instance_id", "alarm_id", "code", "detail", "created_at"}
    )
    _OCCURRENCE_COLUMNS: Final = frozenset(
        {
            "occurrence_id",
            "alarm_id",
            "instance_id",
            "due_utc",
            "schedule_key",
            "status",
            "detail",
            "claimed_by",
            "created_at",
            "finished_at",
        }
    )

    def __init__(self, path: str | Path, *, timezone_name: str = "UTC") -> None:
        self.path = Path(path)
        try:
            self.zone = ZoneInfo(timezone_name)
        except Exception as error:
            raise ValidationError(f"unknown timezone: {timezone_name!r}") from error
        self.timezone_name = timezone_name
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        return db

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}

    _EXPECTED_TABLES: Final = (
        "alarms",
        "alarm_metadata",
        "alarm_occurrences",
        "alarm_schedule_errors",
    )

    def _create_schema(self, db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE alarm_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE alarms (
                id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL UNIQUE,
                active_since TEXT NOT NULL,
                catchup_seconds INTEGER,
                catchup_enabled INTEGER NOT NULL DEFAULT 1 CHECK (catchup_enabled IN (0, 1)),
                name TEXT,
                name_key TEXT UNIQUE,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                kind TEXT NOT NULL CHECK (kind IN ('once', 'weekly')),
                when_utc TEXT,
                local_time TEXT,
                weekdays TEXT,
                CHECK (id IN ('alarm-01','alarm-02','alarm-03','alarm-04','alarm-05',
                              'alarm-06','alarm-07','alarm-08','alarm-09','alarm-10'))
            )"""
        )
        db.execute(
            """CREATE TABLE alarm_occurrences (
                occurrence_id TEXT PRIMARY KEY,
                alarm_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                due_utc TEXT NOT NULL,
                schedule_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN
                    ('in_flight', 'completed', 'failed', 'skipped', 'interrupted')),
                detail TEXT NOT NULL,
                claimed_by TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE(instance_id, due_utc)
            )"""
        )
        db.execute(
            """CREATE TABLE alarm_schedule_errors (
                instance_id TEXT NOT NULL,
                alarm_id TEXT NOT NULL,
                code TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (instance_id, code)
            )"""
        )
        db.execute(
            "INSERT INTO alarm_metadata(key, value) VALUES ('timezone_name', ?)",
            (self.timezone_name,),
        )
        db.execute("INSERT INTO alarm_metadata(key, value) VALUES ('scheduler_stop_epoch', '0')")

    def _migrate_forward(self, db: sqlite3.Connection) -> None:
        """Add the per-alarm catch-up columns to a pre-catch-up database.

        Only additive, lossless steps are performed. Unknown table layouts fail
        closed instead of being rewritten.
        """
        columns = self._columns(db, "alarms")
        legacy = {
            "id",
            "instance_id",
            "active_since",
            "name",
            "name_key",
            "enabled",
            "kind",
            "when_utc",
            "local_time",
            "weekdays",
        }
        if not columns & {"catchup_seconds", "catchup_enabled"}:
            if columns != legacy:
                raise DatabaseCompatibilityError(
                    "database schema is incompatible; unexpected alarm columns"
                )
            db.execute("ALTER TABLE alarms ADD COLUMN catchup_seconds INTEGER")
            db.execute("ALTER TABLE alarms ADD COLUMN catchup_enabled INTEGER NOT NULL DEFAULT 1")
        self._assert_columns(db)

    def _assert_columns(self, db: sqlite3.Connection) -> None:
        if (
            self._columns(db, "alarms") != self._ALARM_COLUMNS
            or self._columns(db, "alarm_metadata") != self._METADATA_COLUMNS
            or self._columns(db, "alarm_occurrences") != self._OCCURRENCE_COLUMNS
            or self._columns(db, "alarm_schedule_errors") != self._SCHEDULE_ERROR_COLUMNS
        ):
            raise DatabaseCompatibilityError(
                "database schema is incompatible; migration is not supported"
            )

    def _assert_metadata(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT value FROM alarm_metadata WHERE key = 'timezone_name'").fetchone()
        if row is None or row["value"] != self.timezone_name:
            raise DatabaseCompatibilityError(
                "database timezone is incompatible with this AlarmStore instance"
            )
        epoch = db.execute(
            "SELECT value FROM alarm_metadata WHERE key = ?",
            ("scheduler_stop_epoch",),
        ).fetchone()
        if epoch is None or not epoch["value"].isdecimal():
            raise DatabaseCompatibilityError("scheduler stop epoch missing or invalid")

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(20):
            db = self._connect()
            retry = False
            try:
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("BEGIN IMMEDIATE")
                tables = {
                    row["name"]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                    if not row["name"].startswith("sqlite_")
                }
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if not tables:
                    self._create_schema(db)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                else:
                    if tables != set(self._EXPECTED_TABLES):
                        raise DatabaseCompatibilityError(
                            "database schema is incompatible; unexpected table set"
                        )
                    if version in (0, 1):
                        self._migrate_forward(db)
                        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    elif version != SCHEMA_VERSION:
                        raise DatabaseCompatibilityError(
                            f"database schema version {version} is not supported"
                        )
                    self._assert_columns(db)
                self._assert_metadata(db)
                db.commit()
                return
            except sqlite3.OperationalError as error:
                db.rollback()
                if "locked" not in str(error).lower() or attempt == 19:
                    raise
                retry = True
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            if retry:
                time.sleep(0.01 * (attempt + 1))

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(f"{field} must be a timezone-aware datetime")
        return value

    @staticmethod
    def _name(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("name must be a non-empty string or None")
        return value.strip()

    @staticmethod
    def _weekdays(value: Iterable[int] | None) -> tuple[int, ...] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise ValidationError("weekdays must be integers from 0 through 6")
        try:
            result = tuple(sorted(set(value)))
        except TypeError as error:
            raise ValidationError("weekdays must be an iterable of integers") from error
        if not result or any(
            isinstance(day, bool) or not isinstance(day, int) or day not in range(7)
            for day in result
        ):
            raise ValidationError("weekdays must be a non-empty set of integers from 0 through 6")
        return result

    @staticmethod
    def _catchup_seconds(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("catchup_seconds must be an integer or None")
        if not 0 <= value <= MAX_CATCHUP_SECONDS:
            raise ValidationError(f"catchup_seconds must be between 0 and {MAX_CATCHUP_SECONDS}")
        return value

    @staticmethod
    def _catchup_enabled(value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValidationError("catchup_enabled must be a boolean")
        return value

    @staticmethod
    def _validate_existing_instant(value: datetime, field: str) -> datetime:
        value = AlarmStore._aware(value, field)
        local = value.astimezone(value.tzinfo)
        round_trip = local.astimezone(UTC).astimezone(value.tzinfo)
        if (
            round_trip.replace(tzinfo=None) != local.replace(tzinfo=None)
            or round_trip.utcoffset() != local.utcoffset()
        ):
            raise ValidationError(f"{field} is a nonexistent local time during DST")
        return value

    def _resolve_when(self, when: When, now: datetime | None) -> datetime:
        if isinstance(when, datetime):
            return self._validate_existing_instant(when, "when")
        if isinstance(when, timedelta):
            if when <= timedelta(0):
                raise ValidationError("relative when must be positive")
            reference = datetime.now(UTC) if now is None else self._aware(now, "now")
            return reference.astimezone(UTC) + when
        raise ValidationError("when must be a timezone-aware datetime or positive timedelta")

    def _require_future_once(self, instant: datetime, now: datetime | None) -> None:
        reference = datetime.now(UTC) if now is None else self._aware(now, "now")
        if instant.astimezone(UTC) <= reference.astimezone(UTC):
            raise ValidationError("once when must be in the future")

    def _weekly_time(self, instant: datetime) -> str:
        # Preserve an explicitly supplied local ZoneInfo wall time long enough
        # to reject a fabricated gap such as 2026-03-29 02:30 Europe/Berlin.
        if getattr(instant.tzinfo, "key", None) == self.zone.key:
            local = instant
        else:
            local = instant.astimezone(self.zone)
        naive = local.replace(tzinfo=None)
        candidates = {
            naive.replace(tzinfo=self.zone, fold=fold).astimezone(UTC)
            for fold in (0, 1)
            if naive.replace(tzinfo=self.zone, fold=fold)
            .astimezone(UTC)
            .astimezone(self.zone)
            .replace(tzinfo=None)
            == naive
        }
        if len(candidates) != 1:
            raise ValidationError("weekly local time is invalid or ambiguous during DST")
        only = next(iter(candidates)).astimezone(self.zone)
        if local.utcoffset() != only.utcoffset():
            raise ValidationError("weekly local time is invalid during DST")
        return naive.strftime("%H:%M:%S")

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Alarm:
        return Alarm(
            id=row["id"],
            instance_id=row["instance_id"],
            active_since=datetime.fromisoformat(row["active_since"]),
            catchup_seconds=row["catchup_seconds"],
            catchup_enabled=bool(row["catchup_enabled"]),
            name=row["name"],
            enabled=bool(row["enabled"]),
            kind=row["kind"],
            when_utc=datetime.fromisoformat(row["when_utc"]) if row["when_utc"] else None,
            local_time=row["local_time"],
            weekdays=tuple(int(day) for day in row["weekdays"].split(","))
            if row["weekdays"]
            else (),
        )

    def _run_read(self, action: Callable[[sqlite3.Connection], _Result]) -> _Result:
        db = self._connect()
        try:
            return action(db)
        finally:
            db.close()

    def _run_write(self, action: Callable[[sqlite3.Connection], _Result]) -> _Result:
        for attempt in range(20):
            db = self._connect()
            retry = False
            try:
                db.execute("BEGIN IMMEDIATE")
                result = action(db)
                db.commit()
                return result
            except sqlite3.OperationalError as error:
                db.rollback()
                if "locked" not in str(error).lower() or attempt == 19:
                    raise
                retry = True
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            if retry:
                time.sleep(0.01 * (attempt + 1))

    @staticmethod
    def _missing(alarm_id: str) -> AlarmNotFoundError:
        return AlarmNotFoundError(f"unknown alarm ID: {alarm_id!r}")

    def create(
        self,
        when: When,
        *,
        name: str | None = None,
        weekdays: Iterable[int] | None = None,
        now: datetime | None = None,
        catchup_seconds: int | None = None,
        catchup_enabled: bool = True,
    ) -> Alarm:
        """Persist a new enabled alarm in the lowest available stable ID."""
        resolved = self._resolve_when(when, now)
        selected_days = self._weekdays(weekdays)
        clean_name = self._name(name)
        window = self._catchup_seconds(catchup_seconds)
        enabled_catchup = self._catchup_enabled(catchup_enabled)
        kind = "weekly" if selected_days is not None else "once"
        if kind == "once":
            self._require_future_once(resolved, now)
        local_time = self._weekly_time(resolved) if selected_days is not None else None
        instant = resolved.astimezone(UTC).isoformat() if selected_days is None else None
        instance_id = str(uuid4())
        active_since = (
            (self._aware(now, "now") if now else datetime.now(UTC)).astimezone(UTC).isoformat()
        )

        def action(db: sqlite3.Connection) -> Alarm:
            for alarm_id in SLOT_IDS:
                if db.execute("SELECT 1 FROM alarms WHERE id = ?", (alarm_id,)).fetchone() is None:
                    db.execute(
                        "INSERT INTO alarms(id, instance_id, active_since, catchup_seconds, "
                        "catchup_enabled, name, name_key, enabled, kind, when_utc, "
                        "local_time, weekdays) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                        (
                            alarm_id,
                            instance_id,
                            active_since,
                            window,
                            int(enabled_catchup),
                            clean_name,
                            clean_name.casefold() if clean_name else None,
                            kind,
                            instant,
                            local_time,
                            ",".join(map(str, selected_days)) if selected_days else None,
                        ),
                    )
                    return self._from_row(
                        db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
                    )
            raise NoFreeSlotError("NO_FREE_SLOT: all ten alarm IDs are allocated")

        try:
            return self._run_write(action)
        except sqlite3.IntegrityError as error:
            if clean_name is not None:
                raise NameConflictError(
                    f"NAME_CONFLICT: {clean_name!r} is already in use"
                ) from error
            raise

    def get(self, alarm_id: str) -> Alarm:
        """Return one persisted alarm or raise :class:`AlarmNotFoundError`."""
        row = self._run_read(
            lambda db: db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        )
        if row is None:
            raise self._missing(alarm_id)
        return self._from_row(row)

    def list(self) -> list[Alarm]:
        """Return allocated alarms in stable-ID order."""
        rows = self._run_read(lambda db: db.execute("SELECT * FROM alarms ORDER BY id").fetchall())
        return [self._from_row(row) for row in rows]

    def update(
        self,
        alarm_id: str,
        *,
        when: When | object = _UNSET,
        name: str | None | object = _UNSET,
        weekdays: Iterable[int] | None | object = _UNSET,
        now: datetime | None = None,
        catchup_seconds: int | None | object = _UNSET,
        catchup_enabled: bool | object = _UNSET,
    ) -> Alarm:
        """Atomically update schedule and/or name while preserving enabled state and instance ID."""
        if (
            when is _UNSET
            and name is _UNSET
            and weekdays is _UNSET
            and catchup_seconds is _UNSET
            and catchup_enabled is _UNSET
        ):
            raise ValidationError("update requires at least one changed field")
        clean_name = _UNSET if name is _UNSET else self._name(name)  # type: ignore[arg-type]
        window = (
            _UNSET if catchup_seconds is _UNSET else self._catchup_seconds(catchup_seconds)  # type: ignore[arg-type]
        )
        enabled_catchup = (
            _UNSET if catchup_enabled is _UNSET else self._catchup_enabled(catchup_enabled)  # type: ignore[arg-type]
        )

        def action(db: sqlite3.Connection) -> Alarm:
            row = db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
            if row is None:
                raise self._missing(alarm_id)
            existing_days = (
                tuple(int(day) for day in row["weekdays"].split(",")) if row["weekdays"] else None
            )
            chosen_days = existing_days if weekdays is _UNSET else self._weekdays(weekdays)  # type: ignore[arg-type]
            final_name = row["name"] if clean_name is _UNSET else clean_name
            kind = "weekly" if chosen_days is not None else "once"

            if when is _UNSET:
                if row["kind"] == "weekly" and kind == "once":
                    raise ValidationError("weekly-to-once update requires an explicit when")
                if row["kind"] == "weekly":
                    instant = None
                    local_time = row["local_time"]
                else:
                    instant = row["when_utc"]
                    local_time = (
                        self._weekly_time(datetime.fromisoformat(instant))
                        if kind == "weekly"
                        else None
                    )
            else:
                resolved = self._resolve_when(when, now)  # type: ignore[arg-type]
                if kind == "once":
                    self._require_future_once(resolved, now)
                    instant = resolved.astimezone(UTC).isoformat()
                    local_time = None
                else:
                    instant = None
                    local_time = self._weekly_time(resolved)

            schedule_changed = when is not _UNSET or weekdays is not _UNSET
            active_since = (
                (self._aware(now, "now") if now else datetime.now(UTC)).astimezone(UTC).isoformat()
                if schedule_changed
                else row["active_since"]
            )
            db.execute(
                "UPDATE alarms SET name=?, name_key=?, kind=?, when_utc=?, "
                "local_time=?, weekdays=?, active_since=?, catchup_seconds=?, "
                "catchup_enabled=? WHERE id=?",
                (
                    final_name,
                    final_name.casefold() if final_name else None,
                    kind,
                    instant,
                    local_time,
                    ",".join(map(str, chosen_days)) if chosen_days else None,
                    active_since,
                    row["catchup_seconds"] if window is _UNSET else window,
                    int(row["catchup_enabled"])
                    if enabled_catchup is _UNSET
                    else int(enabled_catchup),
                    alarm_id,
                ),
            )
            return self._from_row(
                db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
            )

        try:
            return self._run_write(action)
        except sqlite3.IntegrityError as error:
            raise NameConflictError("NAME_CONFLICT: name is already in use") from error

    def _set_enabled(self, alarm_id: str, enabled: bool) -> Alarm:
        def action(db: sqlite3.Connection) -> Alarm:
            cursor = db.execute(
                "UPDATE alarms SET enabled = ?, active_since = "
                "CASE WHEN ? = 1 AND enabled = 0 THEN ? ELSE active_since END WHERE id = ?",
                (int(enabled), int(enabled), datetime.now(UTC).isoformat(), alarm_id),
            )
            if cursor.rowcount != 1:
                raise self._missing(alarm_id)
            return self._from_row(
                db.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
            )

        return self._run_write(action)

    def enable(self, alarm_id: str) -> Alarm:
        """Persistently enable an existing alarm."""
        return self._set_enabled(alarm_id, True)

    def disable(self, alarm_id: str) -> Alarm:
        """Persistently disable an existing alarm."""
        return self._set_enabled(alarm_id, False)

    def delete(self, alarm_id: str) -> None:
        """Delete a definition; its stable ID becomes available automatically."""

        def action(db: sqlite3.Connection) -> None:
            cursor = db.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
            if cursor.rowcount != 1:
                raise self._missing(alarm_id)
            db.execute("DELETE FROM alarm_schedule_errors WHERE alarm_id = ?", (alarm_id,))

        self._run_write(action)
