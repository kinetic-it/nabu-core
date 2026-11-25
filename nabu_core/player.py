"""Media adapter that separates command acceptance from audible playback.

A positive ``play_media`` return only proves that the service call was
accepted. Audible playback starts only when the entity reports a playing
state, and a stop is only "confirmed" when the entity stops reporting it.
Nothing here may claim physical silence.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .devices import DevicePolicy
from .volume_journal import VolumeJournal

_LOGGER = logging.getLogger(__name__)
IO_TIMEOUT = 1.0
VOLUME_TOLERANCE = 0.005
STOPPED_STATES = frozenset({"idle", "off", "paused", "standby"})
PLAYING_STATES = frozenset({"playing", "buffering"})
MEDIA_PREFIXES = ("media-source://", "media-source:/", "media_source://")
IDENTITY_MATCH = "match"
IDENTITY_FOREIGN = "foreign"
IDENTITY_UNKNOWN = "unknown"


_PERCENT_BYTES = re.compile(r"(?:%[0-9a-fA-F]{2})+")


def _decode_media_escapes(value: str) -> str:
    """Decode percent-encoded UTF-8 bytes without importing network modules."""

    def decode(match: re.Match[str]) -> str:
        return bytes.fromhex(match.group().replace("%", "")).decode("utf-8", "replace")

    return _PERCENT_BYTES.sub(decode, value)


def normalise_media_id(value: object) -> str | None:
    """Canonical form of a media identifier; None when there is nothing to compare.

    Home Assistant reports the same file in several shapes: as configured
    (``media-source://media_source/local/x.wav``), as ``media-source://local/x.wav``,
    as a plain path or as a resolved URL. Comparing raw strings turns those harmless
    variants into "somebody replaced our alarm".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for _ in range(2):  # tolerate double percent-encoding
        decoded = _decode_media_escapes(text)
        if decoded == text:
            break
        text = decoded
    for _ in range(3):  # media-source URIs nest: media-source://media_source/local/x
        lowered = text.lower()
        for prefix in MEDIA_PREFIXES:
            if lowered.startswith(prefix):
                text = text[len(prefix) :]
                break
        else:
            if lowered.startswith("media_source/"):
                text = text[len("media_source/") :]
            else:
                break
    scheme = ""
    if "://" in text:
        scheme, _, rest = text.partition("://")
        scheme = scheme.lower() + "://"
        host, _, path = rest.partition("/")
        text = host.lower() + "/" + path
    while "//" in text:
        text = text.replace("//", "/")
    text = text.strip().strip("/")
    if not text:
        return None
    return scheme + text


def media_basename(canonical: str | None) -> str | None:
    """File part of a canonical identifier, used as a tolerant fallback."""
    if not canonical:
        return None
    return canonical.rsplit("/", 1)[-1] or None


def media_identity(observed: object, expected: object) -> str:
    """Classify how a reported media identifier relates to the expected one.

    ``match``   - same file, whatever shape the player reports it in
    ``foreign`` - a demonstrably different file; nothing of ours is playing
    ``unknown`` - the player reports nothing, so nothing may be assumed
    """
    left = normalise_media_id(observed)
    right = normalise_media_id(expected)
    if left is None or right is None:
        return IDENTITY_UNKNOWN
    if left == right:
        return IDENTITY_MATCH
    left_name, right_name = media_basename(left), media_basename(right)
    if left_name is not None and left_name == right_name:
        return IDENTITY_MATCH
    if left.endswith("/" + right) or right.endswith("/" + left):
        return IDENTITY_MATCH
    return IDENTITY_FOREIGN


class PlaybackStatus(StrEnum):
    """Explicit, non-overlapping outcomes for one playback step."""

    ACCEPTED = "accepted"
    PLAYING_CONFIRMED = "playing_confirmed"
    START_UNCONFIRMED = "start_unconfirmed"
    STOPPED_CONFIRMED = "stopped_confirmed"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    FAILED = "failed"
    DUCK_CONFIRMED = "duck_confirmed"
    DUCK_UNCONFIRMED = "duck_unconfirmed"
    DUCK_FAILED = "duck_failed"
    #: The session still owns the speaker, but no ring generation is running right
    #: now. The window is remembered and applied to the next generation.
    DUCK_DEFERRED = "duck_deferred"


@dataclass(frozen=True, slots=True)
class PlaybackOutcome:
    status: PlaybackStatus
    entity_id: str
    detail: str = ""


class MediaClient(Protocol):
    """Minimal Home-Assistant-shaped media interface; injected, never imported."""

    async def play_media(self, entity_id: str, media: tuple[str, str]) -> None: ...

    async def stop(self, entity_id: str) -> None: ...

    async def get_state(self, entity_id: str) -> str: ...


@dataclass
class _Ring:
    target: str
    generation: str | None = None
    baseline: float | None = None
    written: float | None = None
    started: bool = False
    play_attempted: bool = False
    stopping: bool = False
    displaced: bool = False
    volume_released: bool = False
    attempted: float | None = None
    media_id: str | None = None
    duck_until: float = 0.0
    duck_active: bool = False
    cleanup: asyncio.Task[PlaybackOutcome] | None = None
    # Diagnostics for the ring loop: how often a restart stayed unconfirmed and
    # how often the player reported a state that is neither playing nor idle.
    restart_failures: int = 0
    interruptions: int = 0


class MediaPlayerAdapter:
    """Gate every call through the device policy and report honest states."""

    def __init__(
        self,
        client: MediaClient,
        policy: DevicePolicy,
        *,
        media: tuple[str, str],
        max_seconds: float = 600.0,
        repeat: bool = False,
        volume: float | None = None,
        duck_floor: float = 0.05,
        cycle_on: float = 0.0,
        cycle_off: float = 0.0,
        recovery_store=None,
        allow_paused: bool = False,
        restart_delay: float = 0.5,
        max_restarts: int = 3,
        max_interruptions: int = 5,
    ) -> None:
        if not 0 < max_seconds <= 3600:
            raise ValueError("max_seconds must be positive and at most 3600")
        if not (math.isfinite(restart_delay) and 0 <= restart_delay <= 5):
            raise ValueError("restart_delay must be finite and between 0 and 5 seconds")
        if not 1 <= max_restarts <= 10:
            raise ValueError("max_restarts must be between 1 and 10")
        if not 1 <= max_interruptions <= 20:
            raise ValueError("max_interruptions must be between 1 and 20")
        self._client = client
        self._policy = policy
        self._media = media
        # A paused player is occupied by somebody's queue. Only a session that
        # owns that pause (the music controller) may start on top of it.
        self._allow_paused = allow_paused
        self._max_seconds = max_seconds
        self._start_timeout = min(max_seconds, 5.0)
        self._stop_timeout = min(max_seconds, 5.0)
        # Re-issuing play_media immediately after the player reported idle races
        # with the tail of the previous stream; a short settle pause plus a small
        # retry budget keeps a short alarm file audible instead of aborting the ring.
        self._restart_delay = restart_delay
        self._max_restarts = max_restarts
        self._max_interruptions = max_interruptions
        self._stop_requested = asyncio.Event()
        if volume is not None and not 0 <= volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        if not (math.isfinite(duck_floor) and 0 < duck_floor < 1):
            # 0.0 ist auf dem Echo 2 kein Mute, sondern ein Player-Stopp - genau der
            # R31-Fehler. Ein Floor von 0 wird deshalb abgelehnt statt still zu stoeren.
            raise ValueError("duck_floor must be greater than 0 and below 1")
        if volume is not None and duck_floor >= volume:
            # Ein Floor auf oder ueber dem Ringpegel macht das Ducking wirkungslos
            # oder stellt den Wecker lauter. Statt die Einrichtung scheitern zu
            # lassen (kleine Ringlautstaerken sind zulaessig), wird gedeckelt.
            duck_floor = volume / 2
            _LOGGER.warning(
                "Alarm duck floor capped to %.3f: it must stay below the ring volume %.3f",
                duck_floor,
                volume,
            )
        if not all(math.isfinite(v) and v >= 0 for v in (cycle_on, cycle_off)):
            raise ValueError("ring cycle must be finite and non-negative")
        if cycle_off and (not cycle_on or volume is None):
            raise ValueError("silent ring cycle requires an on duration and ring volume")
        self._repeat = repeat
        self._volume = volume
        self._duck_floor = duck_floor
        # Ein Duck-Fenster, das zwischen zwei Ring-Generationen angefordert wurde.
        # Es gilt nur fuer dasselbe Ziel, laeuft ab und wird beim Sitzungsende
        # verworfen - niemals auf eine spaetere, fremde Wiedergabe uebertragen.
        self._duck_intent: tuple[str, float] | None = None
        self._cycle_on = cycle_on
        self._cycle_off = cycle_off
        self._media_lock = asyncio.Lock()
        self._ring: _Ring | None = None
        # Zeitpunkt des letzten beobachteten Abspielens je Ziel. Belegt nur den
        # EFFEKT unseres eigenen play_media, niemals die Medienidentitaet (D5).
        self._effect_at: dict[str, float] = {}
        self._play_at: dict[str, float] = {}
        # Sitzungsweite Zuordnung: Ziel -> Zeitpunkt, an dem DIESER Adapter ein
        # eigenes play_media bestaetigt gesehen hat. Sie ueberlebt einen
        # Generationenwechsel des Rings (zwischen zwei Runden ist _ring None),
        # wird aber bei jedem Stopp geloescht. Ohne diesen Eintrag bleibt fremde
        # Wiedergabe unangetastet.
        self._owned: dict[str, float] = {}
        self._pending_restore: dict[str, _Ring] = {}
        self._journal = VolumeJournal(recovery_store) if recovery_store is not None else None

    @property
    def policy(self) -> DevicePolicy:
        return self._policy

    async def start(self, entity_id: str) -> PlaybackOutcome:
        """Request playback. Success means accepted, never audible."""
        target = self._policy.check(entity_id)
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                await self._client.play_media(target, self._media)
        except Exception as error:
            return PlaybackOutcome(PlaybackStatus.FAILED, target, type(error).__name__)
        self._play_at[target] = asyncio.get_running_loop().time()
        return PlaybackOutcome(PlaybackStatus.ACCEPTED, target, "command_accepted")

    async def confirm_playing(
        self, entity_id: str, *, wait: float = 5.0, interval: float = 0.2
    ) -> PlaybackOutcome:
        """Poll until the entity reports playback, without inventing success."""
        target = self._policy.check(entity_id)
        deadline = asyncio.get_running_loop().time() + max(wait, 0.0)
        last_error = ""
        while True:
            try:
                state = await self._state(target)
            except Exception as error:  # unreadable state is not a success
                last_error = type(error).__name__
            else:
                if str(state).lower() in PLAYING_STATES:
                    self._note_effect(target)
                    return PlaybackOutcome(PlaybackStatus.PLAYING_CONFIRMED, target, str(state))
                last_error = f"state={state}"
            if asyncio.get_running_loop().time() >= deadline:
                return PlaybackOutcome(PlaybackStatus.START_UNCONFIRMED, target, last_error)
            await asyncio.sleep(min(interval, max(wait, 0.0)))

    async def stop(
        self, entity_id: str, *, wait: float = 5.0, interval: float = 0.2
    ) -> PlaybackOutcome:
        """Request a stop and report whether the entity stopped reporting playback."""
        target = self._policy.check(entity_id)
        ring = self._ring
        if ring is not None and ring.target == target and ring.cleanup is None:
            return await self.stop_ring(target)
        if ring is not None and ring.target == target:
            return await asyncio.shield(ring.cleanup)
        return await self._stop_transport(target, wait=wait, interval=interval)

    async def _stop_transport(
        self, target: str, *, wait: float, interval: float
    ) -> PlaybackOutcome:
        try:
            async with asyncio.timeout(IO_TIMEOUT):
                await self._client.stop(target)
        except Exception as error:
            return PlaybackOutcome(PlaybackStatus.FAILED, target, type(error).__name__)
        deadline = asyncio.get_running_loop().time() + max(wait, 0.0)
        detail = "stop_requested"
        while True:
            try:
                state = await self._state(target)
            except Exception as error:
                detail = f"state_unreadable:{type(error).__name__}"
            else:
                detail = f"state={state}"
                if state in STOPPED_STATES:
                    return PlaybackOutcome(PlaybackStatus.STOPPED_CONFIRMED, target, detail)
            if asyncio.get_running_loop().time() >= deadline:
                return PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, target, detail)
            await asyncio.sleep(min(interval, max(wait, 0.0)))

    async def _state(self, target: str) -> str:
        async with asyncio.timeout(IO_TIMEOUT):
            return str(await self._client.get_state(target)).lower()

    def _note_effect(self, target: str) -> None:
        """Record playback we started ourselves - never foreign playback.

        Only a playing state that follows our own recent play request counts, so
        an untouched stream on the same speaker can never be stopped on suspicion.
        """
        started = self._play_at.get(target)
        if started is None:
            return
        now = asyncio.get_running_loop().time()
        if now - started > 30.0:
            return
        self._effect_at[target] = now
        self._owned[target] = now

    def _session_owns(self, target: str, *, window: float = 3600.0) -> bool:
        """True while this adapter confirmed its own playback on the target."""
        seen = self._owned.get(target)
        if seen is None:
            return False
        return (asyncio.get_running_loop().time() - seen) <= window

    def _release_ownership(self, target: str) -> None:
        self._owned.pop(target, None)

    def _recent_effect(self, target: str, *, window: float = 300.0) -> bool:
        """True when this adapter saw the target playing recently.

        This proves the effect of our own play_media request, not the identity of
        the stream (D5). It is used only to end a ring we started ourselves.
        """
        seen = self._effect_at.get(target)
        if seen is None:
            return False
        return (asyncio.get_running_loop().time() - seen) <= window

    async def _read_volume(self, target: str) -> float | None:
        reader = getattr(self._client, "get_volume", None)
        if reader is None:
            raise RuntimeError("volume read is not supported")
        async with asyncio.timeout(IO_TIMEOUT):
            value = await reader(target)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            return float(value)
        return None

    @staticmethod
    def _matches(current: float | None, expected: float) -> bool:
        # Do not count an already quiet but nonzero speaker as a verified mute.
        return current is not None and abs(current - expected) <= (
            0.0 if expected == 0 else VOLUME_TOLERANCE
        )

    async def _identify_media(self, ring: _Ring) -> str:
        """How the player's current media relates to our alarm.

        ``unknown`` means the player cannot say what is playing; ``foreign`` means a
        demonstrably different track, so a stop would silence somebody else's audio.
        """
        reader = getattr(self._client, "get_media_id", None)
        if reader is None:
            return IDENTITY_UNKNOWN
        async with asyncio.timeout(IO_TIMEOUT):
            observed = await reader(ring.target)
        relation = media_identity(observed, ring.media_id or self._media[1])
        if relation == IDENTITY_FOREIGN:
            ring.displaced = True
        elif relation == IDENTITY_MATCH and ring.started and observed:
            ring.media_id = normalise_media_id(observed) or ring.media_id
        return relation

    def _owns_playback(self, ring: _Ring, state: str) -> bool:
        """May a stop be sent although no media identifier can be read?

        Only for playback this ring started itself, i.e. after our own ``play_media``
        was confirmed by a playing state. Refusing then would leave the alarm ringing
        forever, which is the worse failure for an alarm clock; anything we did not
        start stays untouched.
        """
        if ring.started:
            return True
        # A play request alone proves nothing: the speaker may have been playing
        # somebody else's stream. Only an observed effect of our own request
        # counts, so foreign media is never stopped on suspicion (D5).
        return ring.play_attempted and self._recent_effect(ring.target)

    async def _set_volume(self, ring: _Ring, level: float) -> bool:
        setter = getattr(self._client, "set_volume", None)
        if setter is None:
            raise RuntimeError("volume write is not supported")
        current = await self._read_volume(ring.target)
        if current is None:
            raise RuntimeError("player volume is not observable")
        if ring.baseline is None:
            ring.baseline = current
        expected = ring.baseline if ring.written is None else ring.written
        if ring.volume_released or not (
            self._matches(current, expected)
            or (ring.attempted is not None and self._matches(current, ring.attempted))
        ):
            ring.volume_released = True
            return False
        if self._matches(current, level):
            ring.written, ring.attempted = level, None
            return True
        # Record the attempted level before awaiting: cancellation or a transport
        # error may occur after the player already applied the value.
        if self._journal is not None:
            if ring.generation is None:
                ring.generation = self._journal.begin(ring.target, ring.baseline)
            self._journal.attempt(ring.target, ring.generation, level)
        ring.attempted = level
        async with asyncio.timeout(IO_TIMEOUT):
            await setter(ring.target, level)
        deadline = asyncio.get_running_loop().time() + min(IO_TIMEOUT, self._start_timeout)
        while True:
            observed = await self._read_volume(ring.target)
            if self._matches(observed, level):
                if self._journal is not None and ring.generation is not None:
                    self._journal.confirm(ring.target, ring.generation, level)
                ring.written, ring.attempted = level, None
                return True
            if observed is not None and not self._matches(observed, current):
                ring.volume_released = True
                return False
            if asyncio.get_running_loop().time() >= deadline:
                # A delayed write can still land; cleanup rechecks the attempted value.
                return False
            await asyncio.sleep(0.05)

    async def _restore_volume(self, ring: _Ring) -> None:
        if ring.baseline is None or ring.displaced or ring.volume_released:
            return
        owned = (ring.written, ring.attempted)
        if owned == (None, None):
            return
        current = await self._read_volume(ring.target)
        if current is None:
            raise RuntimeError("player volume unavailable during cleanup")
        if self._matches(current, ring.baseline):
            return
        if not any(level is not None and self._matches(current, level) for level in owned):
            return  # a distinct external volume wins
        if not await self._set_volume(ring, ring.baseline):
            raise RuntimeError("volume restore not observed")

    async def reconcile_volume(self, target: str) -> None:
        """Retry an uncertain restore on an idle target, never replay or stop audio."""
        ring = self._pending_restore.get(self._policy.check(target))
        if ring is None and self._journal is not None:
            record = self._journal.get(target)
            if record is not None:
                ring = _Ring(
                    target,
                    generation=record["generation"],
                    baseline=record["baseline"],
                    written=record["written"],
                    attempted=record["attempted"],
                )
                self._pending_restore[target] = ring
        if ring is None:
            return
        async with self._media_lock:
            if self._pending_restore.get(target) is not ring:
                return
            try:
                state = await self._state(target)
                if state in PLAYING_STATES or state == "paused":
                    _LOGGER.warning("Alarm volume ownership not recoverable for active %s", target)
                elif state in {"idle", "off", "standby"}:
                    await self._restore_volume(ring)
                    if self._journal is not None and ring.generation is not None:
                        self._journal.clear(target, ring.generation)
                    self._pending_restore.pop(target, None)
            except Exception as error:
                _LOGGER.warning(
                    "Alarm volume retry failed for %s: %s", target, type(error).__name__
                )

    async def reconcile_all(self) -> dict[str, str]:
        """Restore every volume record that outlived the process (F-10).

        ``reconcile_volume`` only runs when the next alarm starts or a stop is
        retried, so after a Home Assistant restart a speaker could keep the alarm
        volume indefinitely.  Returns one status per target and never raises:
        startup must not fail because a single speaker is unreachable.
        """
        if self._journal is None:
            return {}
        status: dict[str, str] = {}
        for target in self._journal.pending():
            try:
                key = self._policy.check(target)
            except Exception:  # noqa: BLE001 - unknown or denied targets stay untouched
                _LOGGER.warning("Alarm volume recovery skipped for %s: not allowlisted", target)
                status[target] = "skipped"
                continue
            await self.reconcile_volume(key)
            status[target] = "deferred" if key in self._pending_restore else "restored"
        return status

    def prepare_ring(self) -> None:
        """Forget a previous outcome before the dispatcher starts a new generation."""
        self._ring = None
        self._stop_requested.clear()

    async def _finish(self, ring: _Ring) -> PlaybackOutcome:
        try:
            return await self._finish_locked(ring)
        finally:
            # Nach dem Stopp ist die Sitzung beendet - die Zuordnung darf nicht
            # in eine spaetere, fremde Wiedergabe hineinreichen.
            if ring.stopping:
                self._release_ownership(ring.target)
                self._drop_duck_intent(ring.target)

    async def _finish_locked(self, ring: _Ring) -> PlaybackOutcome:
        result = PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, ring.target, "no_playback")
        async with self._media_lock:
            try:
                state = await self._state(ring.target)
                if ring.started and state in {"idle", "off", "standby"}:
                    result = PlaybackOutcome(
                        PlaybackStatus.STOPPED_CONFIRMED, ring.target, "already_idle"
                    )
                    return result
                if (
                    not ring.started
                    and ring.play_attempted
                    and self._recent_effect(ring.target)
                    and state in {"idle", "off", "standby"}
                ):
                    # Playback was never confirmed, but our own request produced it
                    # and the target is silent now. Effect proof, not identity proof.
                    return PlaybackOutcome(
                        PlaybackStatus.STOPPED_CONFIRMED,
                        ring.target,
                        "already_idle_after_effect",
                    )
                relation = await self._identify_media(ring)
                if relation == IDENTITY_FOREIGN:
                    return PlaybackOutcome(
                        PlaybackStatus.STOP_UNCONFIRMED, ring.target, "media_replaced"
                    )
                if relation == IDENTITY_UNKNOWN and not self._owns_playback(ring, state):
                    # Nothing was started here, so the playing content is somebody
                    # else's and must keep playing.
                    return PlaybackOutcome(
                        PlaybackStatus.STOP_UNCONFIRMED, ring.target, "media_identity_unavailable"
                    )
                if ring.play_attempted and not ring.started:
                    # A service timeout can occur after the player accepted the
                    # command. Stop only when the current media identifies the
                    # exact requested alarm; unknown ownership remains untouched.
                    reader = getattr(self._client, "get_media_id", None)
                    if reader is not None and await self._state(ring.target) in PLAYING_STATES:
                        async with asyncio.timeout(IO_TIMEOUT):
                            observed = await reader(ring.target)
                        if media_identity(observed, self._media[1]) == IDENTITY_MATCH:
                            ring.started = True
                            ring.media_id = normalise_media_id(observed)
                if ring.started:
                    result = await self._stop_transport(
                        ring.target, wait=self._stop_timeout, interval=0.1
                    )
                    if result.status is not PlaybackStatus.STOPPED_CONFIRMED:
                        _LOGGER.warning(
                            "Alarm stop not confirmed for %s: %s", ring.target, result.detail
                        )
                return result
            finally:
                # Do not raise the level on a still-playing alarm after a failed stop.
                can_restore = result.status is PlaybackStatus.STOPPED_CONFIRMED
                if not ring.started:
                    try:
                        can_restore = not ring.play_attempted or (
                            await self._state(ring.target) in {"idle", "off", "standby"}
                        )
                    except Exception as error:
                        _LOGGER.warning("Alarm cleanup state unreadable: %s", type(error).__name__)
                if can_restore:
                    try:
                        await self._restore_volume(ring)
                        if self._journal is not None and ring.generation is not None:
                            self._journal.clear(ring.target, ring.generation)
                    except Exception as error:
                        self._pending_restore[ring.target] = ring
                        _LOGGER.warning(
                            "Alarm volume restore failed for %s: %s",
                            ring.target,
                            type(error).__name__,
                        )
                elif (ring.written is not None or ring.attempted is not None) and not (
                    ring.displaced or ring.volume_released
                ):
                    self._pending_restore[ring.target] = ring
                    _LOGGER.warning(
                        "Alarm volume restore deferred for %s: stop unconfirmed", ring.target
                    )

    def _cleanup(self, ring: _Ring) -> asyncio.Task[PlaybackOutcome]:
        ring.stopping = True
        self._stop_requested.set()
        if ring.cleanup is None:
            ring.cleanup = asyncio.create_task(self._finish(ring))
            ring.cleanup.add_done_callback(self._observe_cleanup)
        return ring.cleanup

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[PlaybackOutcome]) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("Alarm cleanup failed: %s", type(task.exception()).__name__)

    async def stop_ring(self, entity_id: str) -> PlaybackOutcome:
        target = self._policy.check(entity_id)
        ring = self._ring
        if ring is None or ring.target != target:
            # Zwischen zwei Ring-Generationen ist _ring None. Nur wenn diese
            # Sitzung auf dem Ziel nachweislich selbst abgespielt hat, darf ein
            # Stopp noch greifen; fremde Wiedergabe bleibt unberuehrt.
            # Vor dem Eigentumsnachweis entscheidet der tatsaechliche Zustand:
            # ist das Ziel bereits still, ist der Stopp ohne Befehl bestaetigt
            # und wird nicht als Fehlschlag weitergereicht.
            try:
                state = await self._state(target)
            except Exception:  # noqa: BLE001 - unlesbarer Zustand ist kein Erfolg
                state = ""
            if state in STOPPED_STATES:
                self._drop_duck_intent(target)
                return PlaybackOutcome(PlaybackStatus.STOPPED_CONFIRMED, target, "already_idle")
            if not self._session_owns(target):
                return PlaybackOutcome(PlaybackStatus.STOP_UNCONFIRMED, target, "no_owned_playback")
            result = await self._stop_transport(target, wait=self._stop_timeout, interval=0.1)
            self._release_ownership(target)
            self._drop_duck_intent(target)
            return result
        return await asyncio.shield(self._cleanup(ring))

    async def duck(self, seconds: float, target: str | None = None) -> PlaybackOutcome:
        """Lower the ringing level without ending the playback.

        Zero is not a mute on every speaker: on the Echo 2 a volume of 0.0 stops
        the stream, which turned a duck into a dead alarm. The configured floor is
        therefore written instead of silence, and the ring volume is restored when
        the window closes.

        Between two ring generations this adapter holds no live ring although the
        session still owns the speaker. Refusing there left NABU deaf for the whole
        pause, so the window is remembered and applied to the next generation of the
        same target. It never covers playback this session did not start itself.
        """
        ring = self._ring
        if ring is None or not ring.started or ring.stopping:
            return self._defer_duck(seconds, target)
        target = ring.target
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "invalid_seconds")
        async with self._media_lock:
            if ring.stopping or ring is not self._ring:
                return self._defer_duck(seconds, target)
            try:
                if await self._state(target) not in PLAYING_STATES | {"idle"}:
                    return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "not_available")
                relation = await self._identify_media(ring)
                if relation == IDENTITY_FOREIGN:
                    return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, "media_replaced")
                if relation == IDENTITY_UNKNOWN and not ring.started:
                    return PlaybackOutcome(
                        PlaybackStatus.DUCK_FAILED, target, "media_identity_unavailable"
                    )
                # Remember a mute attempt even if observation fails: no proof of
                # ducking is not proof that the player stayed at the previous level.
                ring.duck_active = True
                confirmed = await self._set_volume(ring, self._duck_floor)
            except Exception as error:
                _LOGGER.warning("Alarm duck failed for %s: %s", target, type(error).__name__)
                return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target, type(error).__name__)
            if not confirmed or ring.stopping:
                return PlaybackOutcome(
                    PlaybackStatus.DUCK_UNCONFIRMED, target, "duck_floor_not_confirmed"
                )
            ring.duck_until = max(ring.duck_until, asyncio.get_running_loop().time() + seconds)
            return PlaybackOutcome(
                PlaybackStatus.DUCK_CONFIRMED, target, f"reported_volume_{self._duck_floor}"
            )

    def _defer_duck(self, seconds: float, target: str | None) -> PlaybackOutcome:
        """Remember a duck window for the next ring generation of this session.

        Only a speaker this adapter recently played on qualifies, so a stale intent
        can never reach somebody else's audio. Expired intents are dropped instead of
        being applied late.
        """
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target or "", "invalid_seconds")
        candidate = target or self._single_owned_target()
        if candidate is None:
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target or "", "not_ringing")
        try:
            candidate = self._policy.check(candidate)
        except Exception:  # noqa: BLE001 - denied targets are never ducked
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, target or "", "not_ringing")
        if not self._recent_effect(candidate):
            return PlaybackOutcome(PlaybackStatus.DUCK_FAILED, candidate, "not_ringing")
        deadline = asyncio.get_running_loop().time() + seconds
        self._duck_intent = (candidate, deadline)
        _LOGGER.info("Alarm duck deferred to the next ring generation on %s", candidate)
        return PlaybackOutcome(PlaybackStatus.DUCK_DEFERRED, candidate, "ring_generation_paused")

    def _single_owned_target(self) -> str | None:
        owned = [target for target in self._owned if self._recent_effect(target)]
        return owned[0] if len(owned) == 1 else None

    def _take_duck_intent(self, target: str) -> float:
        """Consume a pending duck window; an expired or foreign one counts as none."""
        intent = self._duck_intent
        self._duck_intent = None
        if intent is None:
            return 0.0
        owner, deadline = intent
        if owner != target:
            _LOGGER.info("Alarm duck window for %s ignored on %s", owner, target)
            return 0.0
        if deadline <= asyncio.get_running_loop().time():
            _LOGGER.info("Alarm duck window for %s expired before the next generation", target)
            return 0.0
        return deadline

    def _drop_duck_intent(self, target: str) -> None:
        if self._duck_intent is not None and self._duck_intent[0] == target:
            self._duck_intent = None

    def cancel_pending_duck(self, target: str | None = None) -> None:
        """Verwirft ein vorgemerktes Duck-Fenster (Sitzungsende, Stopp).

        Ein Fenster, das eine beendete Sitzung ueberlebt, wuerde den naechsten
        Wecker am selben Ziel geduckt starten - genau die Uebertragung, die
        ausgeschlossen sein muss.
        """
        if target is None or self._duck_intent is None or self._duck_intent[0] == target:
            self._duck_intent = None

    async def _require_start(self, ring: _Ring) -> None:
        ring.play_attempted = True
        outcome = await self.start(ring.target)
        if outcome.status is PlaybackStatus.FAILED:
            raise RuntimeError(f"play_media failed: {outcome.detail}")
        ring.started = True
        observed = await self.confirm_playing(ring.target, wait=self._start_timeout, interval=0.05)
        if observed.status is not PlaybackStatus.PLAYING_CONFIRMED:
            raise RuntimeError(f"playback not confirmed: {observed.detail}")
        # At start, unknown identity is expected (a player may expose none); only a
        # positive mismatch is a failure. Stop and duck fall back to ownership later.
        relation = await self._identify_media(ring)
        if relation == IDENTITY_FOREIGN:
            raise RuntimeError("alarm media replaced by other content")
        if relation == IDENTITY_UNKNOWN:
            _LOGGER.warning(
                "Alarm media identifier unreadable on %s; ringing without identity proof",
                ring.target,
            )

    async def _pause_before_restart(self, ring: _Ring) -> None:
        """Give the player time to close the previous stream before re-issuing."""
        if self._restart_delay <= 0 or ring.stopping:
            return
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=self._restart_delay)
        except TimeoutError:
            pass

    async def _restart_ring(self, ring: _Ring) -> bool:
        """Restart a running ring.

        A start that is not confirmed within the confirmation window is retried
        instead of aborting the ring: the media call was accepted, the player can
        simply be slower than the window (observed on Echo hardware: the state
        stayed ``playing`` long after the confirmation had given up). Only a
        rejected media call or a positively foreign medium stays fatal.
        """
        for attempt in range(1, self._max_restarts + 1):
            await self._pause_before_restart(ring)
            if ring.stopping:
                return False
            try:
                await self._require_start(ring)
            except RuntimeError as error:
                detail = str(error)
                ring.restart_failures = attempt
                if detail.startswith("play_media failed") or "replaced by other content" in detail:
                    raise
                _LOGGER.warning(
                    "Alarm restart %d/%d not confirmed on %s: %s",
                    attempt,
                    self._max_restarts,
                    ring.target,
                    detail,
                )
                continue
            ring.restart_failures = 0
            return True
        _LOGGER.warning(
            "Alarm restarts exhausted on %s after %d attempts",
            ring.target,
            self._max_restarts,
        )
        return False

    async def play(self, entity_id: str) -> PlaybackOutcome:
        """Own one alarm ring; perform cleanup exactly once even on cancellation."""
        target = self._policy.check(entity_id)
        if self._ring is not None and (self._ring.cleanup is None or not self._ring.cleanup.done()):
            raise RuntimeError("previous alarm cleanup is still active")
        await self.reconcile_volume(target)
        if target in self._pending_restore:
            raise RuntimeError("previous alarm volume restore is pending")
        self.prepare_ring()
        ring = self._ring = _Ring(target)
        # A duck requested between two generations applies to this one from its
        # first frame: starting loud and lowering afterwards would defeat the point.
        pending = self._take_duck_intent(target)
        if pending and self._volume is not None and self._duck_floor < self._volume:
            ring.duck_until = pending
            ring.duck_active = True
        start_level = self._duck_floor if ring.duck_active else self._volume
        try:
            async with self._media_lock:
                allowed_states = (
                    {"idle", "off", "standby", "paused"}
                    if self._allow_paused
                    else {"idle", "off", "standby"}
                )
                if await self._state(target) not in allowed_states:
                    raise RuntimeError("alarm target is busy or unavailable")
                if start_level is not None and not await self._set_volume(ring, start_level):
                    raise RuntimeError("ring volume not confirmed")
                if not ring.stopping:
                    await self._require_start(ring)
            await self._ring_phase(ring)
        finally:
            finished = await asyncio.shield(self._cleanup(ring))
        return finished

    async def _ring_phase(self, ring: _Ring) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._max_seconds
        phase_end = loop.time() + self._cycle_on
        audible = True
        while not ring.stopping and loop.time() < deadline:
            try:
                await asyncio.wait_for(self._stop_requested.wait(), timeout=0.1)
            except TimeoutError:
                pass
            async with self._media_lock:
                now = loop.time()
                if ring.stopping or now >= deadline:
                    break
                try:
                    state = await self._state(ring.target)
                except Exception as error:
                    # A single unreadable poll is not a reason to give up the ring.
                    state = "unavailable"
                    _LOGGER.warning(
                        "Alarm state unreadable on %s: %s", ring.target, type(error).__name__
                    )
                if state == "idle" and not self._repeat:
                    return
                if await self._identify_media(ring) == IDENTITY_FOREIGN:
                    raise RuntimeError("alarm media replaced by other content")
                if state not in PLAYING_STATES | {"idle"}:
                    # Neither playing nor finished: the player dropped out (off,
                    # unavailable, paused by somebody else). Bounded recovery, then
                    # an honest failure instead of a silent dead alarm.
                    ring.interruptions += 1
                    _LOGGER.warning(
                        "Alarm playback interruption %d/%d on %s: state=%s",
                        ring.interruptions,
                        self._max_interruptions,
                        ring.target,
                        state,
                    )
                    if ring.interruptions > self._max_interruptions:
                        raise RuntimeError("alarm playback interrupted or unavailable")
                    if not await self._restart_ring(ring):
                        raise RuntimeError("alarm restart after interruption failed")
                    continue
                if now < ring.duck_until:
                    continue
                if ring.duck_active:
                    level = self._volume if self._volume is not None else ring.baseline
                    if level is not None:
                        current = await self._read_volume(ring.target)
                        if current is None:
                            raise RuntimeError("duck recovery volume unavailable")
                        # A mute refused by the player need not be undone.
                        if not self._matches(current, level):
                            if not await self._set_volume(ring, level):
                                raise RuntimeError("post-duck ring volume not confirmed")
                    ring.duck_active = False
                    audible = True
                    phase_end = now + self._cycle_on
                    if not ring.stopping and (self._repeat or state == "idle"):
                        if not await self._restart_ring(ring):
                            raise RuntimeError("alarm restarts exhausted")
                    continue
                if self._cycle_off and now >= phase_end:
                    audible = not audible
                    level = self._volume if audible else 0.0
                    if level is None:
                        raise RuntimeError("ring cycle has no volume")
                    if not await self._set_volume(ring, level):
                        raise RuntimeError("ring phase volume not confirmed")
                    phase_end = now + (self._cycle_on if audible else self._cycle_off)
                if state == "idle" and self._repeat and audible and not ring.stopping:
                    if not await self._restart_ring(ring):
                        raise RuntimeError("alarm restarts exhausted")

    async def stop_request(self) -> bool:
        """Compatibility helper; only a positively observed stop returns True."""
        if self._ring is None:
            return False
        outcome = await self.stop_ring(self._ring.target)
        return outcome.status is PlaybackStatus.STOPPED_CONFIRMED
