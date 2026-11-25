"""Experimental serial output, with no direct HA/device dependencies.

Adapters implement async play(target) and stop(target). Even a positive stop
acknowledgement is not proof of physical silence. Unsettled adapter calls block
new playback until they finish. All permissions are deny-by-default.
"""

import asyncio
import contextlib
from dataclasses import dataclass

from .player import MediaPlayerAdapter, PlaybackOutcome, PlaybackStatus


class TargetDenied(ValueError):
    pass


@dataclass(frozen=True)
class DispatchResult:
    status: str
    detail: str = ""


def _observe(task):
    if not task.cancelled():
        task.exception()


class SerialDispatcher:
    def __init__(self, player, *, allowed_targets, max_seconds):
        if not 0 < max_seconds <= 600:
            raise ValueError("max_seconds must be positive and at most 600")
        self._player = player
        self._allowed = frozenset(allowed_targets)
        self._max_seconds = max_seconds
        self._lock = asyncio.Lock()
        self._generation = 0
        self._stop_signal = None
        self._last_ring_signal = None
        self._target = None
        #: Ziel der laufenden Sitzung; ueberlebt den Wechsel zwischen zwei
        #: Ring-Generationen und wird nur durch einen Stopp geloescht.
        self._session_target: str | None = None
        self._unsettled = set()

    @property
    def ringing(self) -> bool:
        """True while a target is being played, so a stop can be contextual."""
        return self._target is not None

    async def reconcile_volume(self, target):
        if isinstance(self._player, MediaPlayerAdapter):
            await self._player.reconcile_volume(target)

    async def duck(self, seconds: float) -> PlaybackOutcome:
        """Pass a duck request to the adapter, also between two ring generations.

        ``_target`` is cleared when a generation ends, so the session target is kept
        separately: without it a wake word that lands in the pause was answered with
        ``not_ringing`` although the alarm was still the session's own playback.
        """
        if isinstance(self._player, MediaPlayerAdapter):
            session_target = self._target or self._session_target
            if session_target is None:
                return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, "", "not_ringing")
            return await self._player.duck(seconds, session_target)
        if self._target is None:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, "", "not_ringing")
        return await self._player.duck(seconds)

    def session_ended(self) -> None:
        """Die Klingelsitzung ist vorbei - auch ohne Stopp (Auslaufen, Fehler).

        Ohne diesen Aufruf bliebe das Sitzungsziel stehen und ein Duck-Fenster
        koennte einen spaeteren Wecker am selben Ziel geduckt starten.
        """
        self._end_session()

    def _end_session(self) -> None:
        target = self._session_target
        self._session_target = None
        if target is not None and isinstance(self._player, MediaPlayerAdapter):
            self._player.cancel_pending_duck(target)

    def _track(self, task):
        self._unsettled.add(task)
        task.add_done_callback(self._unsettled.discard)
        task.add_done_callback(_observe)

    async def _request_stop(self, target):
        if isinstance(self._player, MediaPlayerAdapter):
            stop = self._player.stop_ring
        else:
            stop = self._player.stop
        task = asyncio.create_task(stop(target))
        done, _ = await asyncio.wait({task}, timeout=self._max_seconds)
        if not done:
            task.cancel()
            self._track(task)
            return False
        try:
            result = task.result()
            if isinstance(result, PlaybackOutcome):
                return result.status is PlaybackStatus.STOPPED_CONFIRMED
            return result is True
        except (Exception, asyncio.CancelledError):
            return False

    async def ring(self, target):
        if target not in self._allowed:
            raise TargetDenied("target is not explicitly allowed")
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                return DispatchResult("cancelled")
            if any(not t.done() for t in self._unsettled):
                return DispatchResult("unsettled", "previous adapter call still active")
            if isinstance(self._player, MediaPlayerAdapter):
                self._player.prepare_ring()
            signal = self._stop_signal = asyncio.Event()
            self._last_ring_signal = signal
            self._target = target
            self._session_target = target
            task = asyncio.create_task(self._player.play(target))
            waiter = asyncio.create_task(signal.wait())
            try:
                done, _ = await asyncio.wait(
                    {task, waiter}, timeout=self._max_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if generation != self._generation:
                    task.cancel()
                    self._track(task)
                    return DispatchResult("stopped", "physical_silence_unconfirmed")
                if task not in done:
                    task.cancel()
                    self._track(task)
                    confirmed = await self._request_stop(target)
                    return DispatchResult(
                        "timeout", "stop_acknowledged" if confirmed else "stop_unconfirmed"
                    )
                try:
                    outcome = task.result()
                except Exception as exc:
                    return DispatchResult("failed", type(exc).__name__)
                if isinstance(outcome, PlaybackOutcome):
                    if outcome.status is not PlaybackStatus.STOPPED_CONFIRMED:
                        return DispatchResult("failed", f"{outcome.status}:{outcome.detail}")
                return DispatchResult("completed")
            except asyncio.CancelledError:
                task.cancel()
                self._track(task)
                # A cancelling caller must not silently leave a playback that may
                # already have started: request a bounded stop and keep that task
                # alive for the event loop. Physical silence stays unproven.
                stopper = asyncio.create_task(self._request_stop(target))
                self._track(stopper)
                # A second cancellation may arrive while the stop is in flight;
                # it must suppress neither the stop request nor the re-raise below.
                with contextlib.suppress(BaseException):
                    await asyncio.shield(stopper)
                raise
            finally:
                waiter.cancel()
                self._stop_signal = None
                self._target = None

    async def stop(self):
        self._generation += 1
        # Der Stopp beendet die Sitzung: ein Duck-Fenster darf danach nicht mehr
        # auf die naechste Wiedergabe uebergreifen.
        self._end_session()
        signal, target = self._stop_signal, self._target
        if signal is None:
            return False
        signal.set()
        # Serialize stop against any subsequent output.
        async with self._lock:
            if self._last_ring_signal is not signal:
                return False
            return await self._request_stop(target)
