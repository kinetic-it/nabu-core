"""R18: Ein Stopp muss vor jeder anderen Alarm- und Gespraechslogik greifen.

R17 (2026-11-22, Test 5) zeigte zwei Ausfaelle: "Oh, Stopp." wurde nicht als
Stopp erkannt, und eine offene Rueckfrage haette den Stopp verschluckt. Der
Konversationsagent muss deshalb:

- einen reinen Stoppbefehl zuerst behandeln, auch bei offener Rueckfrage;
- ohne Ring hoechstens ehrlich antworten, aber kein Geraet ansprechen;
- alarmfremde Saetze unveraendert an den Chatpfad geben.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.kinetic_voice_router.conversation import (
    KineticVoiceRouterConversationEntity,
)

# genau das, was Home Assistant laedt: die gebuendelte Kopie im Integration
from custom_components.kinetic_voice_router.nabu_core.alarm_engine import AlarmStore
from custom_components.kinetic_voice_router.nabu_core.api import (
    AlarmService,
    VoiceAlarmAdapter,
)

REGISTRY_KEY = "kinetic_voice_router_alarm_services"


class Stub:
    """Gerade genug Konversationsentitaet, um den Alarmzweig auszufuehren."""

    def __init__(self, registry) -> None:  # noqa: ANN001 - test double
        self.hass = SimpleNamespace(data={})
        if registry is not None:
            self.hass.data[REGISTRY_KEY] = registry
        self._alarm_pending: dict[str, tuple[str, str]] = {}


def build_voice(tmp_path, *, ringing: bool, confirmation: bool = True):
    calls: list[str] = []

    async def stop_callback() -> bool:
        calls.append("stop")
        return confirmation

    service = AlarmService(AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin"))
    voice = VoiceAlarmAdapter(
        service,
        stop_callback=stop_callback,
        ringing_callback=lambda: ringing,
    )
    return voice, SimpleNamespace(layer=SimpleNamespace(voice=voice)), calls


def run(stub: Stub, text: str, *, conversation_id: str = "c1"):
    handler = KineticVoiceRouterConversationEntity._try_alarm.__get__(stub)
    return handler(SimpleNamespace(text=text, conversation_id=conversation_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    ["Stopp.", "Oh, Stopp.", "Wecker aus.", "Hör auf", "Okay, stopp bitte"],
)
async def test_every_stop_form_ends_the_ring(tmp_path, utterance: str) -> None:
    voice, registry, calls = build_voice(tmp_path, ringing=True)
    stub = Stub(registry)

    reply = await run(stub, utterance)

    assert reply == "Wecker gestoppt.", utterance
    assert calls == ["stop"], utterance
    assert len(voice.service.list().alarms) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "Was ist das für ein Geräusch?",
        "Ich muss aufhören zu rauchen",
        "Hör nicht auf",
        "Wie stoppe ich den Wecker morgen?",
    ],
)
async def test_unrelated_speech_while_ringing_does_not_stop(tmp_path, utterance: str) -> None:
    _, registry, calls = build_voice(tmp_path, ringing=True)
    stub = Stub(registry)

    reply = await run(stub, utterance)

    assert reply is None, utterance
    assert calls == [], utterance


@pytest.mark.asyncio
async def test_bare_stop_without_a_ring_belongs_to_the_chat(tmp_path) -> None:
    _, registry, calls = build_voice(tmp_path, ringing=False)
    stub = Stub(registry)

    assert await run(stub, "Stopp.") is None
    assert calls == []


@pytest.mark.asyncio
async def test_named_stop_without_a_ring_is_answered_honestly(tmp_path) -> None:
    _, registry, calls = build_voice(tmp_path, ringing=False)
    stub = Stub(registry)

    reply = await run(stub, "Wecker aus.")

    assert reply == "Es klingelt gerade kein Wecker."
    assert calls == [], "ohne Ring darf kein Player angesprochen werden"


@pytest.mark.asyncio
async def test_a_stop_discards_an_open_clarification(tmp_path) -> None:
    _, registry, calls = build_voice(tmp_path, ringing=True)
    stub = Stub(registry)
    stub._alarm_pending["c1"] = ("Stell einen Wecker", "missing_time")

    reply = await run(stub, "Oh, Stopp.")

    assert reply == "Wecker gestoppt."
    assert stub._alarm_pending == {}
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_is_not_announced_as_silence(tmp_path) -> None:
    _, registry, calls = build_voice(tmp_path, ringing=True, confirmation=False)
    stub = Stub(registry)

    reply = await run(stub, "Oh, Stopp.")

    assert reply is not None
    assert "nicht bestätigt" in reply
    assert reply != "Wecker gestoppt."
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_a_stop_never_touches_existing_alarms(tmp_path) -> None:
    voice, registry, _ = build_voice(tmp_path, ringing=True)
    await voice.handle("Stelle einen Wecker auf 6 Uhr", now=datetime.now(UTC))
    stub = Stub(registry)

    await run(stub, "Oh, Stopp.")

    alarms = voice.service.list().alarms
    assert len(alarms) == 1
    assert alarms[0].enabled is True
