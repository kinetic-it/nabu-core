"""Narrow in-process API for durable alarms and a voice-command adapter.

This module has no HTTP transport, scheduler, or Home Assistant integration.
It exposes only committed ``AlarmStore`` state. Voice mutations are attempted
only for fully parsed, unambiguous commands; unsuccessful operations have no
spoken confirmation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from nabu_core.alarm_engine import Alarm, AlarmError, AlarmStore
from nabu_core.voice_alarm import AlarmIntent, AlarmParser, IntentKind, ParseStatus

_UNSET: Final = object()


@dataclass(frozen=True, slots=True)
class ApiResult:
    """A stable result for in-process callers; never an HTTP response."""

    code: str
    alarm: Alarm | None = None
    alarms: tuple[Alarm, ...] = ()
    question: str | None = None
    confirmation: str | None = None


class AlarmService:
    """Thin CRUD/list facade over one ``AlarmStore`` instance.

    ``AlarmStore`` commits mutations before returning; this facade converts
    expected persistence/domain failures into explicit result codes and emits a
    successful result only after that return has completed.
    """

    def __init__(self, store: AlarmStore) -> None:
        self.store = store

    @staticmethod
    def _failure(error: Exception) -> ApiResult:
        if isinstance(error, AlarmError):
            return ApiResult(code=error.code)
        if isinstance(error, sqlite3.Error):
            return ApiResult(code="STORAGE_FAILURE")
        return ApiResult(code="INTERNAL_ERROR")

    def create(
        self,
        when: datetime | timedelta,
        *,
        name: str | None = None,
        weekdays: Iterable[int] | None = None,
        now: datetime | None = None,
    ) -> ApiResult:
        try:
            alarm = self.store.create(when, name=name, weekdays=weekdays, now=now)
            return ApiResult(code="OK", alarm=alarm)
        except Exception as error:
            return self._failure(error)

    def get(self, alarm_id: str) -> ApiResult:
        try:
            return ApiResult(code="OK", alarm=self.store.get(alarm_id))
        except Exception as error:
            return self._failure(error)

    def list(self) -> ApiResult:
        try:
            return ApiResult(code="OK", alarms=tuple(self.store.list()))
        except Exception as error:
            return self._failure(error)

    def update(
        self,
        alarm_id: str,
        *,
        when: datetime | timedelta | object = _UNSET,
        name: str | None | object = _UNSET,
        weekdays: Iterable[int] | None | object = _UNSET,
        now: datetime | None = None,
    ) -> ApiResult:
        changes: dict[str, object] = {}
        if when is not _UNSET:
            changes["when"] = when
        if name is not _UNSET:
            changes["name"] = name
        if weekdays is not _UNSET:
            changes["weekdays"] = weekdays
        try:
            return ApiResult(code="OK", alarm=self.store.update(alarm_id, now=now, **changes))
        except Exception as error:
            return self._failure(error)

    def enable(self, alarm_id: str) -> ApiResult:
        try:
            return ApiResult(code="OK", alarm=self.store.enable(alarm_id))
        except Exception as error:
            return self._failure(error)

    def disable(self, alarm_id: str) -> ApiResult:
        try:
            return ApiResult(code="OK", alarm=self.store.disable(alarm_id))
        except Exception as error:
            return self._failure(error)

    def delete(self, alarm_id: str) -> ApiResult:
        try:
            self.store.delete(alarm_id)
            return ApiResult(code="OK")
        except Exception as error:
            return self._failure(error)

    def delete_matching_label(self, label: str) -> ApiResult:
        return self._mutate_unique(_match_label(label), delete=True)

    def disable_matching_label(self, label: str) -> ApiResult:
        return self._mutate_unique(_match_label(label), delete=False)

    def enable_matching_label(self, label: str, *, now: datetime) -> ApiResult:
        matches = _match_label(label)
        return self._mutate_unique(
            lambda alarm: matches(alarm) and _can_be_enabled(alarm, now),
            delete=False,
            enabled=True,
        )

    def update_matching_label(
        self, label: str, *, hour: int, minute: int, now: datetime
    ) -> ApiResult:
        """Move one named alarm to another clock time, keeping its recurrence."""
        matches = _match_label(label)
        selected = [alarm for alarm in self.store.list() if matches(alarm)]
        if not selected:
            return ApiResult(code="NOT_FOUND")
        if len(selected) != 1:
            return ApiResult(code="AMBIGUOUS_SELECTOR")
        alarm = selected[0]
        zone = self.store.zone
        local_now = now.astimezone(zone)
        if alarm.weekdays:
            return self.update(
                alarm.id,
                when=datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone),
                weekdays=alarm.weekdays,
                now=now,
            )
        target = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
        if target <= local_now:
            target += timedelta(days=1)
        return self.update(alarm.id, when=target, now=now)

    def status_matching_label(self, label: str) -> ApiResult:
        matches = _match_label(label)
        selected = [alarm for alarm in self.store.list() if matches(alarm)]
        if not selected:
            return ApiResult(code="NOT_FOUND")
        if len(selected) != 1:
            return ApiResult(code="AMBIGUOUS_SELECTOR")
        return ApiResult(code="OK", alarm=selected[0])

    def disable_matching_date(self, selected_date: date) -> ApiResult:
        return self._mutate_unique(
            lambda alarm: (
                alarm.kind == "once"
                and alarm.when_utc is not None
                and alarm.when_utc.astimezone(self.store.zone).date() == selected_date
            ),
            delete=False,
        )

    def _mutate_unique(
        self,
        matches: Callable[[Alarm], bool],
        *,
        delete: bool,
        enabled: bool | None = None,
    ) -> ApiResult:
        def action(db: sqlite3.Connection) -> ApiResult:
            selected = [self.store._from_row(row) for row in db.execute("SELECT * FROM alarms")]
            matched = [alarm for alarm in selected if matches(alarm)]
            if not matched:
                return ApiResult(code="NOT_FOUND")
            if len(matched) != 1:
                return ApiResult(code="AMBIGUOUS_SELECTOR")
            alarm = matched[0]
            if delete:
                cursor = db.execute(
                    "DELETE FROM alarms WHERE id = ? AND instance_id = ?",
                    (alarm.id, alarm.instance_id),
                )
                if cursor.rowcount != 1:
                    return ApiResult(code="NOT_FOUND")
                return ApiResult(code="OK")
            cursor = db.execute(
                "UPDATE alarms SET enabled = ? WHERE id = ? AND instance_id = ?",
                (1 if enabled else 0, alarm.id, alarm.instance_id),
            )
            if cursor.rowcount != 1:
                return ApiResult(code="NOT_FOUND")
            row = db.execute(
                "SELECT * FROM alarms WHERE id = ? AND instance_id = ?",
                (alarm.id, alarm.instance_id),
            ).fetchone()
            if row is None:
                return ApiResult(code="NOT_FOUND")
            return ApiResult(code="OK", alarm=self.store._from_row(row))

        try:
            return self.store._run_write(action)
        except Exception as error:
            return self._failure(error)


def _display(name: str | None) -> str:
    text = name or "Wecker"
    return text[:1].upper() + text[1:]


def _label_key(name: str) -> str:
    """Treat "Frühschicht", "frühschicht-wecker" and "Frühschichtwecker" as one name."""
    key = name.casefold().replace("-", " ").strip()
    key = " ".join(key.split())
    compact = key.replace(" ", "")
    if compact.endswith("wecker") and len(compact) > len("wecker"):
        compact = compact[: -len("wecker")]
    return compact


def _match_label(label: str) -> Callable[[Alarm], bool]:
    wanted = _label_key(label)
    return lambda alarm: alarm.name is not None and _label_key(alarm.name) == wanted


def _next_due(alarm: Alarm, *, now: datetime | None = None, zone=None) -> datetime | None:
    """When this alarm rings next, or ``None`` when it cannot ring any more."""
    if alarm.kind == "once":
        return alarm.when_utc
    if not alarm.weekdays or alarm.local_time is None or zone is None:
        return None
    from .recurrence import next_weekly  # noqa: PLC0415

    reference = (now or datetime.now(UTC)).astimezone(UTC)
    wall = time.fromisoformat(alarm.local_time)
    return next_weekly(reference, wall, alarm.weekdays, zone)


def _can_be_enabled(alarm: Alarm, now: datetime) -> bool:
    """An expired one-shot cannot be revived by voice; nothing would ever ring."""
    if alarm.kind != "once" or alarm.when_utc is None:
        return True
    return alarm.when_utc.astimezone(UTC) > now.astimezone(UTC)


class VoiceAlarmAdapter:
    """Adapt fail-closed ``AlarmParser`` intents to the in-process service."""

    def __init__(
        self,
        service: AlarmService,
        *,
        stop_callback: Callable[[], Awaitable[bool | None]] | None = None,
        ringing_callback: Callable[[], bool] | None = None,
        snooze_callback: Callable[[int], Awaitable[bool | None]] | None = None,
    ) -> None:
        self.service = service
        self.parser = AlarmParser(timezone=service.store.zone)
        self.stop_callback = stop_callback
        self.snooze_callback = snooze_callback
        # A bare "stopp" only belongs to us while an alarm is actually ringing;
        # otherwise it has to reach whatever else the user is talking to.
        self.ringing_callback = ringing_callback

    def is_ringing(self) -> bool:
        return bool(self.ringing_callback and self.ringing_callback())

    async def handle(self, utterance: str, *, now: datetime) -> ApiResult:
        parsed = self.parser.parse(utterance, now=now)
        if parsed.status is ParseStatus.CLARIFICATION:
            return ApiResult(code="CLARIFICATION_REQUIRED", question=parsed.question)
        if parsed.status is ParseStatus.ERROR:
            return ApiResult(code=f"PARSE_{(parsed.code or 'ERROR').upper()}")
        if parsed.intent is None:
            return ApiResult(code="INVALID_INTENT")
        return await self._dispatch(parsed.intent, now=now)

    async def _dispatch(self, intent: AlarmIntent, *, now: datetime) -> ApiResult:
        if intent.kind is IntentKind.CREATE:
            result = self._create(intent, now=now)
            return self._confirmed(result, "Wecker wurde gespeichert.")
        if intent.kind is IntentKind.LIST:
            result = self.service.list()
            return self._list_confirmation(result, next_only=intent.next_only)
        if intent.kind is IntentKind.STATUS:
            return self._status(intent)
        if intent.kind is IntentKind.UPDATE:
            return self._update(intent, now=now)
        if intent.kind is IntentKind.ACTIVATE:
            return self._activate(intent, now=now)
        if intent.kind is IntentKind.DELETE:
            return self._delete_by_label(intent.label)
        if intent.kind is IntentKind.DEACTIVATE:
            return self._deactivate(intent)
        if intent.kind is IntentKind.STOP:
            return await self._stop()
        if intent.kind is IntentKind.SNOOZE:
            if self.snooze_callback is None or intent.snooze_minutes is None:
                return ApiResult(code="NOT_IMPLEMENTED")
            try:
                confirmed = await self.snooze_callback(intent.snooze_minutes)
            except Exception:
                return ApiResult(code="SNOOZE_FAILED")
            if confirmed is None:
                return ApiResult(code="NOT_RINGING")
            note = "" if confirmed else " Der Wiedergabestopp ist nicht bestätigt."
            return ApiResult(
                code="SNOOZE_REQUESTED",
                confirmation=f"Schlummern für {intent.snooze_minutes} Minuten angefordert.{note}",
            )
        return ApiResult(code="NOT_IMPLEMENTED")

    def _list_confirmation(self, result: ApiResult, *, next_only: bool) -> ApiResult:
        if result.code != "OK":
            return result
        zone = self.service.store.zone
        alarms = list(result.alarms or ())
        if next_only:
            pending = sorted(
                (alarm for alarm in alarms if alarm.enabled and _next_due(alarm, zone=zone)),
                key=lambda alarm: _next_due(alarm, zone=zone) or datetime.max.replace(tzinfo=UTC),
            )
            if not pending:
                return ApiResult(
                    code="OK", alarms=tuple(alarms), confirmation="Es ist kein Wecker geplant."
                )
            upcoming = pending[0]
            due = _next_due(upcoming, zone=zone)
            return ApiResult(
                code="OK",
                alarms=tuple(alarms),
                confirmation=(
                    f"Der nächste Wecker ist {_display(upcoming.name or upcoming.id)} "
                    f"um {due.astimezone(self.service.store.zone):%H:%M}."
                ),
            )
        active = [alarm for alarm in alarms if alarm.enabled]
        return ApiResult(
            code="OK",
            alarms=tuple(alarms),
            confirmation=f"Es sind {len(active)} von {len(alarms)} Weckern aktiviert.",
        )

    def _status(self, intent: AlarmIntent) -> ApiResult:
        if not intent.label:
            return ApiResult(code="INVALID_INTENT")
        result = self.service.status_matching_label(intent.label)
        if result.code == "AMBIGUOUS_SELECTOR":
            return ApiResult(
                code=result.code,
                question="Mehrere Wecker haben diesen Namen. Welchen meinst du?",
            )
        if result.code != "OK" or result.alarm is None:
            return result
        state = "aktiviert" if result.alarm.enabled else "deaktiviert"
        return ApiResult(
            code="OK",
            alarm=result.alarm,
            confirmation=f"Der Wecker {_display(result.alarm.name)} ist {state}.",
        )

    def _update(self, intent: AlarmIntent, *, now: datetime) -> ApiResult:
        if not intent.label or None in (intent.time_hour, intent.time_minute):
            return ApiResult(code="INVALID_INTENT")
        result = self.service.update_matching_label(
            intent.label, hour=intent.time_hour, minute=intent.time_minute, now=now
        )
        if result.code == "AMBIGUOUS_SELECTOR":
            return ApiResult(
                code=result.code,
                question="Mehrere Wecker haben diesen Namen. Welchen meinst du?",
            )
        if result.code == "NOT_FOUND":
            return ApiResult(code=result.code, question="Diesen Wecker kenne ich nicht.")
        return self._confirmed(result, "Wecker wurde geändert.")

    def _activate(self, intent: AlarmIntent, *, now: datetime) -> ApiResult:
        if not intent.label:
            return ApiResult(code="INVALID_INTENT")
        result = self.service.enable_matching_label(intent.label, now=now)
        if result.code == "AMBIGUOUS_SELECTOR":
            return ApiResult(
                code=result.code,
                question="Mehrere Wecker haben diesen Namen. Welchen meinst du?",
            )
        if result.code == "NOT_FOUND":
            return ApiResult(
                code=result.code,
                question="Diesen Wecker kenne ich nicht oder seine Zeit ist vorbei.",
            )
        return self._confirmed(result, "Wecker wurde aktiviert.")

    def _create(self, intent: AlarmIntent, *, now: datetime) -> ApiResult:
        if intent.at is not None:
            return self.service.create(intent.at, name=intent.label, now=now)
        weekdays = intent.recurrence_weekdays or (
            (intent.recurrence_weekday,) if intent.recurrence_weekday is not None else None
        )
        if None in (intent.time_hour, intent.time_minute) or weekdays is None:
            return ApiResult(code="INVALID_INTENT")
        local_now = now.astimezone(self.service.store.zone)
        local_when = datetime.combine(
            local_now.date(),
            time(intent.time_hour, intent.time_minute),
            tzinfo=self.service.store.zone,
        )
        return self.service.create(
            local_when,
            name=intent.label,
            weekdays=weekdays,
            now=now,
        )

    def _delete_by_label(self, label: str | None) -> ApiResult:
        if not label:
            return ApiResult(code="CLARIFICATION_REQUIRED", question="Welchen Wecker meinst du?")
        result = self.service.delete_matching_label(label)
        if result.code == "AMBIGUOUS_SELECTOR":
            return ApiResult(
                code=result.code,
                question="Mehrere Wecker haben diesen Namen. Welchen meinst du?",
            )
        return self._confirmed(result, "Wecker wurde gelöscht.")

    def _deactivate(self, intent: AlarmIntent) -> ApiResult:
        if intent.label is not None:
            result = self.service.disable_matching_label(intent.label)
            question = "Mehrere Wecker haben diesen Namen. Welchen meinst du?"
        elif intent.date is not None:
            result = self.service.disable_matching_date(intent.date)
            question = "Mehrere Wecker passen zu diesem Datum. Welchen meinst du?"
        else:
            return ApiResult(code="INVALID_INTENT")
        if result.code == "AMBIGUOUS_SELECTOR":
            return ApiResult(code=result.code, question=question)
        return self._confirmed(result, "Wecker wurde deaktiviert.")

    async def _stop(self) -> ApiResult:
        if self.stop_callback is None:
            return ApiResult(code="NOT_IMPLEMENTED")
        # When the ringing state is known, a stop outside a ring must not touch
        # any player: answering "nothing is ringing" is the honest result.
        if self.ringing_callback is not None and not self.is_ringing():
            return ApiResult(
                code="NOT_RINGING",
                confirmation="Es klingelt gerade kein Wecker.",
            )
        try:
            confirmed = await self.stop_callback()
        except Exception:
            return ApiResult(code="STOP_FAILED")
        # The session reports back what it observed: True means the player
        # confirmed silence, anything else means the stop was requested but
        # never confirmed. An adapter acknowledgment alone is not proof that
        # the device is quiet, so the spoken answer must not claim more than
        # the session proved.
        if confirmed is not True:
            return ApiResult(
                code="STOP_REQUESTED",
                confirmation="Stopp angefordert, die Wiedergabe ist aber nicht bestätigt.",
            )
        return ApiResult(code="STOP_REQUESTED", confirmation="Wecker gestoppt.")

    @staticmethod
    def _confirmed(result: ApiResult, confirmation: str) -> ApiResult:
        if result.code != "OK":
            return result
        return ApiResult(
            code=result.code,
            alarm=result.alarm,
            alarms=result.alarms,
            question=result.question,
            confirmation=confirmation,
        )
