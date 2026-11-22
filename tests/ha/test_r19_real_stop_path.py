"""R19-Review: der reale Stopp-Pfad mit den gebuendelten Kandidatendateien.

Nachweis, dass nicht nur der Parser, sondern die ganze Kette
Konversationsentitaet -> VoiceAlarmAdapter -> AlarmServiceLayer -> Stop-Callback
mit genau den Dateien laeuft, die als Kandidat ausgeliefert werden (die Kopie
unter custom_components/kinetic_voice_router/nabu_core).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.kinetic_voice_router import conversation as conv_module
from custom_components.kinetic_voice_router.conversation import (
    KineticVoiceRouterConversationEntity,
)
from custom_components.kinetic_voice_router.nabu_core import voice_alarm as bundled_voice_alarm
from custom_components.kinetic_voice_router.nabu_core.alarm_engine import AlarmStore
from custom_components.kinetic_voice_router.nabu_core.ha_services import AlarmServiceLayer

REGISTRY_KEY = "kinetic_voice_router_alarm_services"


class Stub:
    """So viel Konversationsentitaet, wie der Alarmzweig braucht."""

    def __init__(self, registry) -> None:  # noqa: ANN001 - test double
        self.hass = SimpleNamespace(data={REGISTRY_KEY: registry})
        self._alarm_pending: dict[str, tuple[str, str]] = {}


class SchedulerStub:
    def __init__(self, ringing: bool) -> None:
        self.ringing = ringing
        self.stops = 0

    def is_ringing(self) -> bool:
        return self.ringing

    async def stop(self, now=None):  # noqa: ANN001 - never used by the layer
        self.stops += 1
        return True


def build_layer(tmp_path, *, ringing: bool, confirmed: bool = True, with_scheduler: bool = True):
    calls: list[str] = []

    async def stop_callback() -> bool:
        calls.append("stop")
        return confirmed

    layer = AlarmServiceLayer(
        AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin"),
        stop_callback=stop_callback,
        scheduler=SchedulerStub(ringing) if with_scheduler else None,
    )
    return layer, SimpleNamespace(layer=layer), calls


def run(stub: Stub, text: str):  # noqa: ANN201 - test helper
    handler = KineticVoiceRouterConversationEntity._try_alarm.__get__(stub)
    return handler(SimpleNamespace(text=text, conversation_id="c1"))


def test_the_bundled_candidate_files_are_the_ones_under_test() -> None:
    assert "custom_components/kinetic_voice_router/nabu_core/voice_alarm.py" in str(
        bundled_voice_alarm.__file__
    )
    assert "custom_components/kinetic_voice_router/conversation.py" in str(conv_module.__file__)


@pytest.mark.asyncio
async def test_explicit_alarm_stop_reaches_the_stop_callback_while_ringing(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path, ringing=True)

    reply = await run(Stub(registry), "Wecker aus.")

    assert calls == ["stop"]
    assert reply == "Wecker gestoppt."


@pytest.mark.asyncio
async def test_context_stop_reaches_the_stop_callback_while_ringing(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path, ringing=True)

    reply = await run(Stub(registry), "Hör auf.")

    assert calls == ["stop"]
    assert reply == "Wecker gestoppt."


@pytest.mark.asyncio
async def test_context_stop_without_ring_never_reaches_the_callback(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path, ringing=False)

    assert await run(Stub(registry), "Hör auf.") is None
    assert calls == []


@pytest.mark.asyncio
async def test_explicit_stop_without_ring_answers_without_touching_anything(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path, ringing=False)

    reply = await run(Stub(registry), "Wecker aus.")

    assert reply == "Es klingelt gerade kein Wecker."
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_ring_state_is_forwarded_not_refused(tmp_path) -> None:
    """Ohne Scheduler ist der Ringzustand unbekannt - dann wird gefragt, nicht behauptet."""

    layer, registry, calls = build_layer(tmp_path, ringing=False, with_scheduler=False)

    reply = await run(Stub(registry), "Wecker aus.")

    assert calls == ["stop"]
    assert reply == "Wecker gestoppt."


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_is_reported_as_unconfirmed(tmp_path) -> None:
    layer, registry, calls = build_layer(tmp_path, ringing=True, confirmed=False)

    reply = await run(Stub(registry), "Wecker aus.")

    assert calls == ["stop"]
    assert reply is not None
    assert "nicht bestätigt" in reply
    assert "gestoppt." not in reply


@pytest.mark.asyncio
async def test_the_layer_stop_goes_through_the_adapter_to_the_callback(tmp_path) -> None:
    """Die gebuendelte Dienstschicht meldet bestaetigte Stille nur bei bestaetigtem Stopp."""

    layer, registry, calls = build_layer(tmp_path, ringing=True)

    result = await layer.utterance({"text": "Wecker aus."})

    assert result.ok is True
    assert result.data["confirmation"] == "Wecker gestoppt."
    assert calls == ["stop"]
