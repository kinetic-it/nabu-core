"""Experimental serial output, with no direct HA/device dependencies.

Adapters implement async play(target) and stop(target). Even a positive stop
acknowledgement is not proof of physical silence. Unsettled adapter calls block
new playback until they finish. All permissions are deny-by-default.
"""

import asyncio
import contextlib
from dataclasses import dataclass


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
        self._target = None
        self._unsettled = set()
        self.last_stop_confirmed: bool | None = None

    def _track(self, task):
        self._unsettled.add(task)
        task.add_done_callback(self._unsettled.discard)
        task.add_done_callback(_observe)

    async def _request_stop(self, target):
        task = asyncio.create_task(self._player.stop(target))
        done, _ = await asyncio.wait({task}, timeout=self._max_seconds)
        if not done:
            task.cancel()
            self._track(task)
            return False
        try:
            return bool(task.result())
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
            signal = self._stop_signal = asyncio.Event()
            self._target = target
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
                    self.last_stop_confirmed = confirmed
                    return DispatchResult(
                        "timeout", "stop_acknowledged" if confirmed else "stop_unconfirmed"
                    )
                try:
                    task.result()
                except Exception as exc:
                    return DispatchResult("failed", type(exc).__name__)
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
        signal, target = self._stop_signal, self._target
        if signal is None:
            return False
        signal.set()
        # Serialize stop against any subsequent output.
        async with self._lock:
            confirmed = await self._request_stop(target)
            self.last_stop_confirmed = confirmed
            return confirmed
