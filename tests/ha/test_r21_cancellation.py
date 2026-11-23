"""R21: Kein roher Fehlertext in Sprachantworten; kein Neustart mitten im Gespraech.

R20-Befund (T5, 13:43:31): Waehrend der Ring neu startete, wurde der
``trigger_sentence``-Toolaufruf abgebrochen. Das Ergebnis war
``ToolResult {'error': 'CancelledError'}`` und der Agent sprach
``Error: CancelledError`` statt einer deutschen Antwort.

Zwei Regeln:

* Alles, was wie ein roher Fehler/Exception aussieht, wird nie gesprochen.
* Solange ein Gespraech laeuft, wird der Ring nicht neu gestartet; ein Stopp
  gewinnt gegen den Neustart.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.kinetic_voice_router.conversation import _sanitise_reply
from nabu_core.alarm_session import AlarmSession


@pytest.mark.parametrize(
    "leaked",
    [
        "Error: CancelledError",
        "CancelledError",
        "error: TimeoutError",
        "Traceback (most recent call last): ...",
        "asyncio.CancelledError()",
        "ValueError: bad state",
    ],
)
def test_raw_error_text_is_never_spoken(leaked: str) -> None:
    reply = _sanitise_reply(leaked)
    assert reply == "Das habe ich nicht verstanden."
    assert "Cancelled" not in reply
    assert "Error" not in reply


@pytest.mark.parametrize(
    "answer",
    [
        "Wecker gestoppt.",
        "Stopp angefordert, die Wiedergabe ist aber nicht bestätigt.",
        "Es klingelt gerade kein Wecker.",
        "Der nächste Wecker ist morgen um 06:00.",
    ],
)
def test_normal_answers_survive_untouched(answer: str) -> None:
    assert _sanitise_reply(answer) == answer


def test_empty_answer_gets_a_german_fallback() -> None:
    assert _sanitise_reply(None) == "Das habe ich nicht verstanden."
    assert _sanitise_reply("   ") == "Das habe ich nicht verstanden."


async def test_interaction_wait_ends_immediately_on_stop() -> None:
    """A stop must not wait for a listening satellite to go idle."""
    conversation_open = True

    async def ring() -> None:  # never used here
        return None

    async def stop() -> bool:
        return True

    async def state() -> str:
        return "idle"

    async def interaction_active() -> bool:
        return conversation_open

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction_active,
        poll=0.01,
        ring_seconds=30,
    )

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(session._await_interaction_end(loop.time() + 10.0))
    await asyncio.sleep(0.05)
    session._stop_requested.set()
    session._wake.set()
    await asyncio.wait_for(task, 0.5)


async def test_interaction_wait_returns_when_turn_finishes() -> None:
    conversation_open = True

    async def ring() -> None:
        return None

    async def stop() -> bool:
        return True

    async def state() -> str:
        return "idle"

    async def interaction_active() -> bool:
        return conversation_open

    session = AlarmSession(
        ring=ring,
        stop=stop,
        state_of=state,
        interaction_active=interaction_active,
        poll=0.01,
        ring_seconds=30,
    )

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(session._await_interaction_end(loop.time() + 10.0))
    await asyncio.sleep(0.05)
    conversation_open = False
    await asyncio.wait_for(task, 0.5)
