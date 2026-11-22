"""Behavioral tests for the in-process alarm API and voice adapter."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nabu_core.alarm_engine import AlarmStore
from nabu_core.api import AlarmService, VoiceAlarmAdapter

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)


def make_service(tmp_path) -> AlarmService:
    return AlarmService(AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin"))


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("Stell einen Wecker in zehn Minuten", datetime(2026, 3, 18, 12, 40, tzinfo=BERLIN)),
        ("Stell morgen um 7 Uhr einen Wecker", datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN)),
        ("Stell Sonntag um 9 Uhr einen Wecker", datetime(2026, 3, 22, 9, 0, tzinfo=BERLIN)),
    ],
)
async def test_voice_creates_once_alarm_only_after_persistence(tmp_path, utterance, expected):
    service = make_service(tmp_path)
    adapter = VoiceAlarmAdapter(service)

    result = await adapter.handle(utterance, now=NOW)

    assert result.code == "OK"
    assert result.alarm is not None
    assert result.alarm.when_utc == expected.astimezone(ZoneInfo("UTC"))
    assert service.get(result.alarm.id).alarm == result.alarm
    assert result.confirmation is not None


async def test_voice_creates_weekly_alarm_in_store_timezone(tmp_path):
    service = make_service(tmp_path)

    result = await VoiceAlarmAdapter(service).handle(
        "Stell jeden Montag um 8 Uhr einen Wecker", now=NOW
    )

    assert result.code == "OK"
    assert result.alarm is not None
    assert result.alarm.kind == "weekly"
    assert result.alarm.local_time == "08:00:00"
    assert result.alarm.weekdays == (0,)


async def test_medication_alarm_can_be_deleted_by_label(tmp_path):
    service = make_service(tmp_path)
    adapter = VoiceAlarmAdapter(service)
    created = await adapter.handle("Stell Sonntag um 9 Uhr einen Medikamentenwecker", now=NOW)

    result = await adapter.handle("Lösche den Medikamentenwecker", now=NOW)

    assert result.code == "OK"
    assert result.confirmation is not None
    assert service.list().alarms == ()
    assert created.alarm is not None
    assert service.get(created.alarm.id).code == "NOT_FOUND"


async def test_voice_deactivate_tomorrow_resolves_exactly_one_date_match(tmp_path):
    service = make_service(tmp_path)
    adapter = VoiceAlarmAdapter(service)
    created = await adapter.handle("Stell morgen um 7 Uhr einen Wecker", now=NOW)

    result = await adapter.handle("Deaktiviere den Wecker für morgen", now=NOW)

    assert result.code == "OK"
    assert result.alarm is not None and result.alarm.enabled is False
    assert created.alarm is not None
    assert service.get(created.alarm.id).alarm.enabled is False


async def test_ambiguous_date_selection_asks_without_mutating(tmp_path):
    service = make_service(tmp_path)
    adapter = VoiceAlarmAdapter(service)
    await adapter.handle("Stell morgen um 7 Uhr einen Wecker", now=NOW)
    await adapter.handle("Stell morgen um 8 Uhr einen Wecker", now=NOW)

    result = await adapter.handle("Deaktiviere den Wecker für morgen", now=NOW)

    assert result.code == "AMBIGUOUS_SELECTOR"
    assert result.question is not None
    assert result.confirmation is None
    assert [alarm.enabled for alarm in service.list().alarms] == [True, True]


async def test_parser_clarification_never_mutates_or_confirms(tmp_path):
    service = make_service(tmp_path)

    result = await VoiceAlarmAdapter(service).handle("Lösche den Wecker", now=NOW)

    assert result.code == "CLARIFICATION_REQUIRED"
    assert result.question is not None
    assert result.confirmation is None
    assert service.list().alarms == ()


async def test_storage_failure_has_clear_code_and_no_confirmation(tmp_path):
    class CommitFailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    class CommitFailingStore(AlarmStore):
        fail_commits = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_commits = True

        def _connect(self):
            connection = super()._connect()
            return CommitFailingConnection(connection) if self.fail_commits else connection

    path = tmp_path / "alarms.sqlite"
    service = AlarmService(CommitFailingStore(path, timezone_name="Europe/Berlin"))

    result = await VoiceAlarmAdapter(service).handle("Stell morgen um 7 Uhr einen Wecker", now=NOW)

    assert result.code == "STORAGE_FAILURE"
    assert result.alarm is None
    assert result.confirmation is None
    assert AlarmStore(path, timezone_name="Europe/Berlin").list() == []


async def test_stop_requires_injected_callback_and_never_claims_success_without_one(tmp_path):
    service = make_service(tmp_path)

    result = await VoiceAlarmAdapter(service).handle("Stopp den Wecker", now=NOW)

    assert result.code == "NOT_IMPLEMENTED"
    assert result.confirmation is None


async def test_stop_waits_for_injected_async_callback_before_confirming(tmp_path):
    calls = []

    async def stop_player() -> None:
        calls.append("stopped")

    result = await VoiceAlarmAdapter(make_service(tmp_path), stop_callback=stop_player).handle(
        "Stopp den Wecker", now=NOW
    )

    assert calls == ["stopped"]
    assert result.code == "STOP_REQUESTED"
    assert result.confirmation is not None


def test_crud_operations_return_committed_store_results(tmp_path):
    service = make_service(tmp_path)
    created = service.create(datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN), name="Morgen", now=NOW)

    assert created.code == "OK" and created.alarm is not None
    changed = service.update(created.alarm.id, name="Geändert", now=NOW)
    assert changed.code == "OK" and changed.alarm.name == "Geändert"
    assert service.disable(created.alarm.id).alarm.enabled is False
    assert service.enable(created.alarm.id).alarm.enabled is True
    assert service.delete(created.alarm.id).code == "OK"
    assert service.get(created.alarm.id).code == "NOT_FOUND"


async def test_voice_list_returns_committed_alarm_snapshot(tmp_path):
    service = make_service(tmp_path)
    created = service.create(datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN), now=NOW)

    result = await VoiceAlarmAdapter(service).handle("Welche Wecker habe ich?", now=NOW)

    assert result.code == "OK"
    assert result.alarms == (created.alarm,)
    assert result.confirmation is not None


async def test_delete_by_label_does_not_separate_selection_from_mutation(tmp_path):
    class AtomicDeleteService(AlarmService):
        def list(self):
            raise AssertionError("voice deletion must not select through a separate list call")

        def delete(self, alarm_id):
            raise AssertionError(f"voice deletion must not mutate through delete({alarm_id})")

    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")
    service = AtomicDeleteService(store)
    created = service.create(
        datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN),
        name="medikamentenwecker",
        now=NOW,
    )
    assert created.alarm is not None

    result = await VoiceAlarmAdapter(service).handle("Lösche den Medikamentenwecker", now=NOW)

    assert result.code == "OK"
    assert result.confirmation == "Wecker wurde gelöscht."
    assert service.get(created.alarm.id).code == "NOT_FOUND"


async def test_stop_callback_false_only_acknowledges_the_abort_request(tmp_path):
    async def request_stop() -> bool:
        return False

    result = await VoiceAlarmAdapter(make_service(tmp_path), stop_callback=request_stop).handle(
        "Stopp den Wecker", now=NOW
    )

    assert result.code == "STOP_REQUESTED"
    # R18: ein unbestaetigter Stopp darf nicht wie Stille klingen.
    assert result.confirmation is not None
    assert "nicht bestätigt" in result.confirmation
    assert result.confirmation != "Wecker gestoppt."


async def test_delete_label_fails_closed_when_slot_is_reused_before_mutation(tmp_path):
    service = make_service(tmp_path)
    created = service.create(
        datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN),
        name="Medikamentenwecker",
        now=NOW,
    )
    assert created.alarm is not None
    store = service.store
    original_run_write = store._run_write
    replacement_instance_id = "replacement-delete"
    swapped = False

    def swap_slot_before_mutation(action):
        def injected(db):
            nonlocal swapped
            if not swapped:
                swapped = True
                replacement = dict(
                    db.execute("SELECT * FROM alarms WHERE id = ?", (created.alarm.id,)).fetchone()
                )
                replacement.update(
                    instance_id=replacement_instance_id,
                    name="Fremder Wecker",
                    name_key="fremder wecker",
                    when_utc=datetime(2026, 3, 20, 7, 0, tzinfo=BERLIN)
                    .astimezone(ZoneInfo("UTC"))
                    .isoformat(),
                )
                db.execute("DELETE FROM alarms WHERE id = ?", (created.alarm.id,))
                columns = ", ".join(replacement)
                placeholders = ", ".join("?" for _ in replacement)
                db.execute(
                    f"INSERT INTO alarms ({columns}) VALUES ({placeholders})",  # noqa: S608  # synthetic schema keys, values parameterized
                    tuple(replacement.values()),
                )
            return action(db)

        return original_run_write(injected)

    store._run_write = swap_slot_before_mutation
    result = await VoiceAlarmAdapter(service).handle("Lösche den Medikamentenwecker", now=NOW)

    assert swapped is True
    assert result.code == "NOT_FOUND"
    assert result.confirmation is None
    persisted = service.get(created.alarm.id)
    assert persisted.code == "OK" and persisted.alarm is not None
    assert persisted.alarm.instance_id == replacement_instance_id
    assert persisted.alarm.name == "Fremder Wecker"


async def test_deactivate_date_fails_closed_when_slot_is_reused_before_mutation(tmp_path):
    service = make_service(tmp_path)
    created = service.create(datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN), now=NOW)
    assert created.alarm is not None
    store = service.store
    original_run_write = store._run_write
    replacement_instance_id = "replacement-disable"
    swapped = False

    def swap_slot_before_mutation(action):
        def injected(db):
            nonlocal swapped
            if not swapped:
                swapped = True
                replacement = dict(
                    db.execute("SELECT * FROM alarms WHERE id = ?", (created.alarm.id,)).fetchone()
                )
                replacement.update(
                    instance_id=replacement_instance_id,
                    name=None,
                    name_key=None,
                    enabled=1,
                    when_utc=datetime(2026, 3, 20, 7, 0, tzinfo=BERLIN)
                    .astimezone(ZoneInfo("UTC"))
                    .isoformat(),
                )
                db.execute("DELETE FROM alarms WHERE id = ?", (created.alarm.id,))
                columns = ", ".join(replacement)
                placeholders = ", ".join("?" for _ in replacement)
                db.execute(
                    f"INSERT INTO alarms ({columns}) VALUES ({placeholders})",  # noqa: S608  # synthetic schema keys, values parameterized
                    tuple(replacement.values()),
                )
            return action(db)

        return original_run_write(injected)

    store._run_write = swap_slot_before_mutation
    result = await VoiceAlarmAdapter(service).handle("Deaktiviere den Wecker für morgen", now=NOW)

    assert swapped is True
    assert result.code == "NOT_FOUND"
    assert result.confirmation is None
    persisted = service.get(created.alarm.id)
    assert persisted.code == "OK" and persisted.alarm is not None
    assert persisted.alarm.instance_id == replacement_instance_id
    assert persisted.alarm.enabled is True


async def test_adapter_ack_does_not_prove_physical_silence(tmp_path):
    async def request_stop() -> bool:
        return True

    result = await VoiceAlarmAdapter(make_service(tmp_path), stop_callback=request_stop).handle(
        "Stopp den Wecker", now=NOW
    )
    assert result.code == "STOP_REQUESTED"
    # R18: True ist die Bestaetigung der Session, nicht die des Adapters.
    assert result.confirmation == "Wecker gestoppt."
