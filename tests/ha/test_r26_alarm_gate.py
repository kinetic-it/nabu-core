"""R26 regression on the shipped files: an unparsed alarm command gets a hint.

Before R26 ``_try_alarm`` answered ``None`` for every utterance the grammar did
not own, so "Wecker auf 7 Uhr" reached the chat agent and a chat model answered
an alarm order. These tests run the *bundled* conversation entity and the
bundled core, so a passing test means the shipped files carry the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.kinetic_voice_router import ALARM_REGISTRY_KEY
from custom_components.kinetic_voice_router import conversation as conv_module
from custom_components.kinetic_voice_router.conversation import (
    KineticVoiceRouterConversationEntity,
)
from custom_components.kinetic_voice_router.nabu_core import voice_alarm as bundled_voice_alarm
from custom_components.kinetic_voice_router.nabu_core.alarm_engine import AlarmStore
from custom_components.kinetic_voice_router.nabu_core.ha_services import AlarmServiceLayer

HINT_COMMAND = bundled_voice_alarm.HINT_COMMAND
HINT_LIST = bundled_voice_alarm.HINT_LIST
HINT_NEGATED = bundled_voice_alarm.HINT_NEGATED
BERLIN = ZoneInfo("Europe/Berlin")


class Stub:
    """As much conversation entity as the alarm branch needs."""

    def __init__(self, registry) -> None:  # noqa: ANN001 - test double
        self.hass = SimpleNamespace(data={ALARM_REGISTRY_KEY: registry})
        self._alarm_pending: dict[str, tuple[str, str]] = {}


def build_layer(tmp_path, *, ringing: bool = False):
    calls: list[str] = []

    async def stop_callback() -> bool:
        calls.append("stop")
        return True

    scheduler = SimpleNamespace(is_ringing=lambda: ringing) if ringing else None
    layer = AlarmServiceLayer(
        AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin"),
        stop_callback=stop_callback,
        scheduler=scheduler,
    )
    return layer, SimpleNamespace(layer=layer), calls


def run(stub: Stub, text: str):  # noqa: ANN201 - test helper
    handler = KineticVoiceRouterConversationEntity._try_alarm.__get__(stub)
    return handler(SimpleNamespace(text=text, conversation_id="c1"))


def test_the_bundled_files_are_the_ones_under_test() -> None:
    assert "custom_components/kinetic_voice_router/nabu_core/voice_alarm.py" in str(
        bundled_voice_alarm.__file__
    )
    assert "custom_components/kinetic_voice_router/conversation.py" in str(conv_module.__file__)


@pytest.mark.asyncio
async def test_an_unparsed_alarm_command_is_answered_by_the_router(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path)
    before = layer.list({}).as_dict()

    reply = await run(Stub(registry), "Wecker auf 7 Uhr")

    assert reply == HINT_COMMAND, "the router handed an alarm command to the chat agent"
    assert layer.list({}).as_dict() == before, "the hint must not touch the alarm store"
    assert calls == []


@pytest.mark.asyncio
async def test_a_question_about_own_alarms_gets_the_list_hint(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path)

    reply = await run(Stub(registry), "Welchen Wecker habe ich")

    assert reply == HINT_LIST
    assert calls == []


@pytest.mark.asyncio
async def test_a_negated_command_changes_nothing(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path)
    before = layer.list({}).as_dict()

    reply = await run(Stub(registry), "Nein, lösche meinen Wecker Arbeit")

    assert reply == HINT_NEGATED
    assert layer.list({}).as_dict() == before, "a negated command changed the store"
    assert calls == []


@pytest.mark.asyncio
async def test_chat_about_alarms_still_reaches_the_agent(tmp_path) -> None:
    """The gate must not swallow ordinary questions about alarms."""

    _, registry, calls = build_layer(tmp_path)

    assert await run(Stub(registry), "Wie funktioniert ein Wecker?") is None
    assert await run(Stub(registry), "Warum hat mein Wecker nicht geklingelt?") is None
    assert calls == []


@pytest.mark.asyncio
async def test_the_gate_does_not_swallow_a_working_command(tmp_path) -> None:
    """A command the grammar understands keeps its normal answer - and really acts.

    The reply alone proves nothing: an answer without a stored alarm would look
    identical. So the store is read back: exactly one more alarm, at the spoken
    time, enabled, and due within the next day.
    """

    layer, registry, calls = build_layer(tmp_path)
    before = layer.list({}).as_dict()["data"]["count"]
    now = datetime.now(UTC)

    reply = await run(Stub(registry), "Stell einen Wecker auf 7 Uhr.")

    assert reply is not None
    assert reply != HINT_COMMAND
    assert calls == []

    stored = layer.list({}).as_dict()["data"]
    assert stored["count"] == before + 1, "the spoken command stored no alarm"
    alarm = stored["alarms"][-1]
    assert alarm["kind"] == "once"
    assert alarm["enabled"] is True
    due = datetime.fromisoformat(alarm["when_utc"])
    local = due.astimezone(BERLIN)
    assert (local.hour, local.minute) == (7, 0), f"stored time was {local:%H:%M}, not 07:00"
    assert now < due <= now + timedelta(days=1)
