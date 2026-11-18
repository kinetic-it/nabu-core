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
from datetime import date, datetime, time, timedelta
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
        return self._mutate_unique(
            lambda alarm: alarm.name is not None and alarm.name.casefold() == label.casefold(),
            delete=True,
        )

    def disable_matching_label(self, label: str) -> ApiResult:
        return self._mutate_unique(
            lambda alarm: alarm.name is not None and alarm.name.casefold() == label.casefold(),
            delete=False,
        )

    def disable_matching_date(self, selected_date: date) -> ApiResult:
        return self._mutate_unique(
            lambda alarm: (
                alarm.kind == "once"
                and alarm.when_utc is not None
                and alarm.when_utc.astimezone(self.store.zone).date() == selected_date
            ),
            delete=False,
        )

    def _mutate_unique(self, matches: Callable[[Alarm], bool], *, delete: bool) -> ApiResult:
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
                "UPDATE alarms SET enabled = 0 WHERE id = ? AND instance_id = ?",
                (alarm.id, alarm.instance_id),
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


class VoiceAlarmAdapter:
    """Adapt fail-closed ``AlarmParser`` intents to the in-process service."""

    def __init__(
        self,
        service: AlarmService,
        *,
        stop_callback: Callable[[], Awaitable[bool | None]] | None = None,
    ) -> None:
        self.service = service
        self.parser = AlarmParser(timezone=service.store.zone)
        self.stop_callback = stop_callback

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
            return self._confirmed(result, "Wecker wurden aufgelistet.")
        if intent.kind is IntentKind.DELETE:
            return self._delete_by_label(intent.label)
        if intent.kind is IntentKind.DEACTIVATE:
            return self._deactivate(intent)
        if intent.kind is IntentKind.STOP:
            return await self._stop()
        return ApiResult(code="NOT_IMPLEMENTED")

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
        try:
            await self.stop_callback()
        except Exception:
            return ApiResult(code="STOP_FAILED")
        # An adapter acknowledgment is not proof that the physical device is silent.
        return ApiResult(
            code="STOP_REQUESTED",
            confirmation="Abbruch des Weckers wurde angefordert.",
        )

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
