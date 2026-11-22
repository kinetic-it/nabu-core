"""The conversation entity must answer alarm commands itself.

The real parser, the real service and the real store run here. Only the Home
Assistant plumbing is replaced, so a green test means: a spoken alarm command is
executed locally and never reaches the chat agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.kinetic_voice_router.conversation import (
    KineticVoiceRouterConversationEntity,
)

# exactly what Home Assistant loads: the bundled copy inside the integration
from custom_components.kinetic_voice_router.nabu_core.alarm_engine import AlarmStore
from custom_components.kinetic_voice_router.nabu_core.api import (
    AlarmService,
    VoiceAlarmAdapter,
)

BERLIN = ZoneInfo("Europe/Berlin")


class Entry:
    def __init__(self, options):
        self.options = options
        self.unload_callbacks = []

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)


class Services:
    def __init__(self):
        self.registered = {}

    def async_register(self, domain, name, handler, schema=None, **kwargs):
        self.registered[(domain, name)] = handler

    def async_remove(self, domain, name):
        self.registered.pop((domain, name), None)


class Config:
    def __init__(self, root):
        self.time_zone = "Europe/Berlin"
        self._root = root

    def path(self, name):
        return str(self._root / name)


class Hass:
    def __init__(self, root):
        self.config = Config(root)
        self.services = Services()
        self.data = {}


def build_voice(tmp_path, *, stopped=None):
    store = AlarmStore(tmp_path / "alarms.sqlite", timezone_name="Europe/Berlin")

    async def default_stop():
        return True

    service = AlarmService(store)
    voice = VoiceAlarmAdapter(service, stop_callback=stopped or default_stop)
    layer = SimpleNamespace(voice=voice)
    return voice, SimpleNamespace(layer=layer)


class Stub:
    """Just enough conversation entity to run the alarm branch."""

    def __init__(self, registry):
        self.hass = SimpleNamespace(data={})
        if registry is not None:
            self.hass.data["kinetic_voice_router_alarm_services"] = registry
        self._alarm_pending = {}


def run(stub, text, *, conversation_id="c1"):
    handler = KineticVoiceRouterConversationEntity._try_alarm.__get__(stub)
    return handler(SimpleNamespace(text=text, conversation_id=conversation_id))


@pytest.mark.asyncio
async def test_alarm_command_is_answered_without_the_chat_agent(tmp_path):
    voice, registry = build_voice(tmp_path)
    stub = Stub(registry)

    reply = await run(stub, "Erstelle einen Wecker namens Frühschicht für 6:30 Uhr.")

    assert reply == "Wecker wurde gespeichert."
    assert len(voice.service.list().alarms) == 1


@pytest.mark.asyncio
async def test_ordinary_speech_falls_through_to_the_chat_agent(tmp_path):
    _, registry = build_voice(tmp_path)
    stub = Stub(registry)

    assert await run(stub, "Wie wird das Wetter morgen?") is None


@pytest.mark.asyncio
async def test_without_the_alarm_option_nothing_is_intercepted(tmp_path):
    stub = Stub(None)

    assert await run(stub, "Wecker in 15 Minuten.") is None


@pytest.mark.asyncio
async def test_a_question_is_answered_by_the_next_short_reply(tmp_path):
    voice, registry = build_voice(tmp_path)
    stub = Stub(registry)

    question = await run(stub, "Stell einen Wecker.")
    assert question == "Wann soll der Wecker klingeln?"

    # The user answers with the missing piece only - no repetition needed.
    confirmation = await run(stub, "um sieben")

    assert confirmation == "Wecker wurde gespeichert."
    assert len(voice.service.list().alarms) == 1


@pytest.mark.asyncio
async def test_stop_is_forwarded_and_never_promised_as_silence(tmp_path):
    calls = []

    async def stopped():
        calls.append(True)
        return True

    _, registry = build_voice(tmp_path, stopped=stopped)
    stub = Stub(registry)

    reply = await run(stub, "Nabu, stopp.")

    assert calls == [True]
    # R18: bestaetigter Stopp heisst "gestoppt"; unbestaetigt heisst "nicht bestaetigt".
    assert reply == "Wecker gestoppt."


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_never_promises_silence(tmp_path):
    async def stopped():
        return False

    _, registry = build_voice(tmp_path, stopped=stopped)
    stub = Stub(registry)

    reply = await run(stub, "Nabu, stopp.")

    assert reply is not None
    assert "nicht bestätigt" in reply
    assert "gestoppt." not in reply


@pytest.mark.asyncio
async def test_a_bare_stop_without_a_ringing_alarm_falls_through(tmp_path):
    """Music and other players must keep their own "stopp"."""
    calls = []

    async def stopped():
        calls.append(True)
        return True

    voice, registry = build_voice(tmp_path, stopped=stopped)
    stub = Stub(registry)

    assert await run(stub, "stopp") is None
    assert calls == []


@pytest.mark.asyncio
async def test_an_explicit_alarm_stop_still_works_without_ringing(tmp_path):
    calls = []

    async def stopped():
        calls.append(True)
        return True

    _, registry = build_voice(tmp_path, stopped=stopped)
    stub = Stub(registry)

    reply = await run(stub, "Nabu, stopp.")

    assert calls == [True]
    # R18: Die Antwort nennt nur, was die Session bestaetigt hat.
    assert reply == "Wecker gestoppt."
