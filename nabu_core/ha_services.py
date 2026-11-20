"""Home-Assistant-shaped service layer on top of the alarm engine.

This module is deliberately importable without Home Assistant: it exposes plain
callables that take a data mapping and return a JSON-serialisable mapping. The
integration module only registers these callables with Home Assistant, so the
engine stays the single authority and no second writable alarm store exists.

Every write goes through :class:`nabu_core.alarm_engine.AlarmStore`; a success
result is produced only after the store committed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .alarm_engine import Alarm, AlarmError, AlarmStore, ValidationError
from .api import AlarmService, VoiceAlarmAdapter
from .player import PlaybackStatus

StopCallback = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ServiceResult:
    """A stable answer for Home Assistant; never a bare truthy value."""

    ok: bool
    code: str = "OK"
    data: dict[str, Any] | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "code": self.code}
        if self.data is not None:
            payload["data"] = self.data
        if self.message is not None:
            payload["message"] = self.message
        return payload


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def alarm_state(alarm: Alarm) -> dict[str, Any]:
    """Serialise one alarm for Home Assistant attributes and dashboards."""
    return {
        "id": alarm.id,
        "instance_id": alarm.instance_id,
        "name": alarm.name,
        "enabled": alarm.enabled,
        "kind": alarm.kind,
        "when_utc": _iso(alarm.when_utc) if alarm.when_utc else None,
        "local_time": alarm.local_time,
        "weekdays": list(alarm.weekdays),
        "catchup_seconds": alarm.catchup_seconds,
        "catchup_enabled": alarm.catchup_enabled,
        "schedule": _describe(alarm),
    }


def _describe(alarm: Alarm) -> str:
    if alarm.kind == "once" and alarm.when_utc is not None:
        return f"once {_iso(alarm.when_utc)}"
    days = ("mo", "di", "mi", "do", "fr", "sa", "so")
    picked = ",".join(days[day] for day in alarm.weekdays)
    return f"weekly {alarm.local_time} [{picked}]"


def _failure(error: Exception) -> ServiceResult:
    if isinstance(error, AlarmError):
        return ServiceResult(False, error.code, None, str(error))
    return ServiceResult(False, "INTERNAL_ERROR", None, type(error).__name__)


def _parse_when(data: Mapping[str, Any]) -> datetime | timedelta:
    if "in_minutes" in data:
        try:
            minutes = int(data["in_minutes"])
        except (TypeError, ValueError) as error:
            raise ValidationError("in_minutes must be an integer") from error
        if minutes <= 0:
            raise ValidationError("in_minutes must be positive")
        return timedelta(minutes=minutes)
    raw = data.get("when")
    if not raw:
        raise ValidationError("either when or in_minutes is required")
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as error:
        raise ValidationError("when must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None:
        raise ValidationError("when must include a timezone offset")
    return parsed


def _weekdays(data: Mapping[str, Any]) -> Iterable[int] | None:
    if "weekdays" not in data:
        return None
    raw = data["weekdays"]
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        return [int(part) for part in raw.split(",") if part.strip()]
    return list(raw)


class AlarmServiceLayer:
    """The nine operations Home Assistant needs, all backed by one engine."""

    def __init__(
        self,
        store: AlarmStore,
        *,
        stop_callback: StopCallback | None = None,
        scheduler=None,
    ) -> None:
        self.store = store
        self.service = AlarmService(store)
        self.stop_callback = stop_callback
        self.scheduler = scheduler
        self.voice = VoiceAlarmAdapter(
            self.service,
            stop_callback=self._voice_stop,
            ringing_callback=self._voice_ringing,
            snooze_callback=self._voice_snooze,
        )

    async def utterance(self, data: Mapping[str, Any]) -> ServiceResult:
        """Run one spoken sentence through the same parser the router uses.

        Home Assistant answers spoken alarms itself (``prefer_local_intents``)
        before this integration is asked. A sentence trigger runs earlier than
        the local intents and can hand the recognised sentence over here, so the
        parsing rules stay in one place instead of being copied into YAML.
        """
        text = str(data.get("text") or "").strip()
        if not text:
            return ServiceResult(ok=False, code="INVALID_TEXT", message="text is required")
        try:
            result = await self.voice.handle(text, now=datetime.now(UTC))
        except Exception as error:  # noqa: BLE001 - a spoken service must not crash
            return _failure(error)
        return ServiceResult(
            ok=result.code
            in {"OK", "STOP_REQUESTED", "SNOOZE_REQUESTED", "CLARIFICATION_REQUIRED"},
            code=result.code,
            data={
                "confirmation": result.confirmation,
                "question": result.question,
                "alarm": alarm_state(result.alarm) if result.alarm is not None else None,
            },
            message=result.confirmation or result.question or result.code,
        )

    def _voice_ringing(self) -> bool:
        return bool(self.scheduler is not None and self.scheduler.is_ringing())

    async def _voice_stop(self) -> bool:
        if self.stop_callback is None:
            raise RuntimeError("stop is not wired")
        result = await self.stop()
        if not result.ok:
            raise RuntimeError(result.code)
        return bool(result.data and result.data.get("confirmed") is True)

    async def _voice_snooze(self, minutes: int) -> bool | None:
        if self.scheduler is None:
            return None
        return await self.scheduler.snooze(datetime.now(UTC), timedelta(minutes=minutes))

    # ----------------------------------------------------------------- write
    def create(self, data: Mapping[str, Any]) -> ServiceResult:
        try:
            alarm = self.store.create(
                _parse_when(data),
                name=data.get("name") or None,
                weekdays=_weekdays(data),
                catchup_seconds=data.get("catchup_seconds"),
                catchup_enabled=bool(data.get("catchup_enabled", True)),
            )
            return ServiceResult(True, "OK", alarm_state(alarm))
        except Exception as error:
            return _failure(error)

    def update(self, data: Mapping[str, Any]) -> ServiceResult:
        alarm_id = str(data.get("id", ""))
        changes: dict[str, Any] = {}
        if "when" in data or "in_minutes" in data:
            changes["when"] = _parse_when(data)
        if "name" in data:
            changes["name"] = data["name"] or None
        if "weekdays" in data:
            changes["weekdays"] = _weekdays(data)
        if "catchup_seconds" in data:
            changes["catchup_seconds"] = data["catchup_seconds"]
        if "catchup_enabled" in data:
            changes["catchup_enabled"] = bool(data["catchup_enabled"])
        if not changes:
            return ServiceResult(False, "INVALID_INPUT", None, "no field to change")
        try:
            return ServiceResult(True, "OK", alarm_state(self.store.update(alarm_id, **changes)))
        except Exception as error:
            return _failure(error)

    def enable(self, data: Mapping[str, Any]) -> ServiceResult:
        return self._toggle(data, True)

    def disable(self, data: Mapping[str, Any]) -> ServiceResult:
        return self._toggle(data, False)

    def _toggle(self, data: Mapping[str, Any], enabled: bool) -> ServiceResult:
        try:
            alarm = (
                self.store.enable(str(data["id"]))
                if enabled
                else self.store.disable(str(data["id"]))
            )
            return ServiceResult(True, "OK", alarm_state(alarm))
        except Exception as error:
            return _failure(error)

    def delete(self, data: Mapping[str, Any]) -> ServiceResult:
        try:
            self.store.delete(str(data["id"]))
            return ServiceResult(True, "OK", {"id": str(data["id"])})
        except Exception as error:
            return _failure(error)

    def configure_catch_up(self, data: Mapping[str, Any]) -> ServiceResult:
        """Set the per-alarm catch-up window or switch it off."""
        payload: dict[str, Any] = {"id": str(data.get("id", ""))}
        if "catchup_seconds" in data:
            payload["catchup_seconds"] = data["catchup_seconds"]
        if "catchup_enabled" in data:
            payload["catchup_enabled"] = bool(data["catchup_enabled"])
        if len(payload) == 1:
            return ServiceResult(
                False, "INVALID_INPUT", None, "catchup_seconds or catchup_enabled required"
            )
        return self.update(payload)

    # ------------------------------------------------------------------ read
    def list(self, data: Mapping[str, Any] | None = None) -> ServiceResult:
        try:
            alarms = [alarm_state(alarm) for alarm in self.store.list()]
            return ServiceResult(True, "OK", {"alarms": alarms, "count": len(alarms)})
        except Exception as error:
            return _failure(error)

    def status(self, data: Mapping[str, Any] | None = None) -> ServiceResult:
        """Aggregate state for a dashboard: alarms, missed and schedule errors."""
        try:
            alarms = [alarm_state(alarm) for alarm in self.store.list()]
        except Exception as error:
            return _failure(error)
        missed: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if self.scheduler is not None:
            missed = [
                {
                    "alarm_id": item.alarm_id,
                    "due_utc": _iso(item.due_utc),
                    "status": item.status,
                    "detail": item.detail,
                }
                for item in self.scheduler.missed()
            ]
            errors = [
                {
                    "alarm_id": item.alarm_id,
                    "code": item.code,
                    "detail": item.detail,
                    "created_at": _iso(item.created_at),
                }
                for item in self.scheduler.schedule_errors()
            ]
        return ServiceResult(
            True,
            "OK",
            {
                "alarms": alarms,
                "count": len(alarms),
                "free_slots": 10 - len(alarms),
                "missed": missed,
                "schedule_errors": errors,
            },
        )

    # ------------------------------------------------------------------ stop
    async def stop(self, data: Mapping[str, Any] | None = None) -> ServiceResult:
        """Request an abort. Never reports confirmed silence."""
        if self.stop_callback is None:
            return ServiceResult(False, "NOT_IMPLEMENTED", None, "no playback channel is wired")
        try:
            confirmed = (await self.stop_callback()) is True
        except Exception as error:
            return ServiceResult(False, "STOP_FAILED", None, type(error).__name__)
        return ServiceResult(
            True,
            "STOP_REQUESTED",
            {"confirmed": confirmed},
            "abort requested",
        )

    async def duck(self, data: Mapping[str, Any] | None = None) -> ServiceResult:
        """Report requested vs observed ducking without claiming physical silence."""
        try:
            raw = (data or {}).get("seconds", 12)
            seconds = 0.0 if isinstance(raw, bool) else float(raw)
        except (TypeError, ValueError, OverflowError):
            seconds = 0.0
        if not 0 < seconds <= 120:
            return ServiceResult(False, "INVALID_SECONDS", None, "seconds must be in (0, 120]")
        if self.scheduler is None:
            return ServiceResult(False, "NOT_RINGING", None, "no alarm scheduler")
        outcome = await self.scheduler.duck_alarm(seconds)
        if outcome.status is PlaybackStatus.DUCK_CONFIRMED:
            return ServiceResult(True, "DUCKED", {"seconds": seconds}, outcome.detail)
        code = "NOT_RINGING" if outcome.detail == "not_ringing" else outcome.status.value.upper()
        return ServiceResult(False, code, None, outcome.detail)

    async def ring(self, data: Mapping[str, Any] | None = None) -> ServiceResult:
        """Trigger one scheduler tick; used by tests and the HA time source."""
        if self.scheduler is None:
            return ServiceResult(False, "NOT_IMPLEMENTED", None, "no scheduler wired")
        now = data.get("now") if data else None
        moment = datetime.fromisoformat(str(now)) if now else datetime.now(UTC)
        try:
            finished = await self.scheduler.tick(moment)
        except Exception as error:
            return _failure(error)
        return ServiceResult(
            True,
            "OK",
            {
                "occurrences": [
                    {
                        "alarm_id": item.alarm_id,
                        "due_utc": _iso(item.due_utc),
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in finished
                ]
            },
        )

    # ----------------------------------------------------------------- voice
    async def handle_voice(self, utterance: str, *, now: datetime | None = None) -> ServiceResult:
        """Route one German utterance through the parser into the same engine."""
        moment = now or datetime.now(UTC)
        result = await self.voice.handle(utterance, now=moment)
        payload = {
            "alarm": alarm_state(result.alarm) if result.alarm is not None else None,
            "count": len(result.alarms),
        }
        return ServiceResult(
            result.code == "OK" or result.code == "STOP_REQUESTED",
            result.code,
            payload,
            result.confirmation or result.question,
        )
