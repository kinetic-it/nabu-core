"""Pure parser for a small, explicit German voice-alarm command language."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo


class IntentKind(str, Enum):  # noqa: UP042
    CREATE = "create"
    LIST = "list"
    STATUS = "status"
    UPDATE = "update"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    DELETE = "delete"
    STOP = "stop"
    SNOOZE = "snooze"


class ParseStatus(str, Enum):  # noqa: UP042
    OK = "ok"
    CLARIFICATION = "clarification"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AlarmIntent:
    kind: IntentKind
    at: datetime | None = None
    time_hour: int | None = None
    time_minute: int | None = None
    recurrence_weekday: int | None = None
    recurrence_weekdays: tuple[int, ...] | None = None
    date: date | None = None
    label: str | None = None
    next_only: bool = False
    snooze_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    status: ParseStatus
    intent: AlarmIntent | None = None
    code: str | None = None
    question: str | None = None


_WEEKDAYS: Final[dict[str, int]] = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}
_NUMBERS: Final[dict[str, int]] = {
    "null": 0,
    "ein": 1,
    "eins": 1,
    "eine": 1,
    "einen": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fünfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
    "zwanzig": 20,
    "dreißig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
}
_TENS: Final[dict[str, int]] = {word: value for word, value in _NUMBERS.items() if value >= 20}
_WEEKDAY_PATTERN: Final[str] = "(?:" + "|".join(_WEEKDAYS) + ")"
_TIME_PATTERN: Final[str] = (
    r"(?:(?:halb|viertel nach|viertel vor) (?:\d{1,2}|[a-zäöüß]+)"
    r"|(?:\d{1,2}|[a-zäöüß]+)(?::\d{2})?(?:\s+uhr)?)"
)
_ALARM_PATTERN: Final[str] = r"(?:[a-zäöüß]*wecker)"
_CREATE_VERB: Final[str] = r"(?:stell(?:e)?|erstelle|erzeuge|erinnere(?: mich)?)"
_WAKE_VERB: Final[str] = r"(?:weck(?:e)? mich|wecke mich)"


_TIME_CODES: Final[frozenset[str]] = frozenset(
    {"missing_time", "ambiguous_time", "missing_duration_unit", "ambiguous_local_time"}
)
_DATE_CODES: Final[frozenset[str]] = frozenset(
    {"ambiguous_weekday", "conflicting_date_expressions", "conflicting_time_expressions"}
)
_SELECTOR_CODES: Final[frozenset[str]] = frozenset({"missing_alarm_selector"})


def merge_clarification(original: str, code: str, answer: str) -> str | None:
    """Turn "Wecker." plus "um sieben" into one complete utterance.

    An open question must be answerable with the missing piece alone; the user
    should never have to repeat the whole sentence. Returns ``None`` when the
    answer cannot be merged, so the caller asks again instead of guessing.
    """
    reply = " ".join(answer.split())
    if not reply or len(reply) > 80:
        return None
    text = _normalise(original)
    plain = _normalise(reply)
    if not plain:
        return None
    if code in _TIME_CODES or code in _DATE_CODES:
        return f"{original} {reply}"
    if code in _SELECTOR_CODES:
        single = " " not in plain and len(plain) <= 40
        if "wecker" in text and single and "wecker" not in plain:
            return re.sub(r"\b([a-zäöüß]*)wecker\b", rf"{plain}wecker", text, count=1)
        return f"{original} {reply}"
    return None


class AlarmParser:
    """Parse a narrow grammar and reject anything not fully understood."""

    def __init__(self, *, timezone: ZoneInfo) -> None:
        self.timezone = timezone

    def parse(self, utterance: str, *, now: datetime) -> ParseResult:
        if not isinstance(utterance, str) or not isinstance(now, datetime) or now.tzinfo is None:
            return _error("invalid_input")
        text = _normalise(utterance)
        if not text:
            return _error("invalid_input")
        if re.search(r"\bnicht\b", text):
            return _error("negated_utterance")

        local_now = now.astimezone(self.timezone)
        snooze = re.fullmatch(r"noch (\d{1,2}|[a-zäöüß]+) minuten?", text)
        if snooze:
            word = snooze.group(1)
            minutes = int(word) if word.isdigit() else _NUMBERS.get(word)
            if minutes is None or not 1 <= minutes <= 60:
                return _error("invalid_snooze_duration")
            return _ok(AlarmIntent(kind=IntentKind.SNOOZE, snooze_minutes=minutes))
        if _is_stop(text):
            return _ok(AlarmIntent(kind=IntentKind.STOP))
        if _is_list(text):
            return _ok(AlarmIntent(kind=IntentKind.LIST, next_only=_wants_next(text)))
        if _is_status_question(text):
            return self._parse_status(text)
        if _has_delete_verb(text) and "wecker" in text:
            return self._parse_delete(text)
        if _has_update_verb(text) and "wecker" in text:
            return self._parse_update(text, local_now)
        if _has_activate_verb(text) and "wecker" in text:
            return self._parse_activate(text)
        if _has_deactivate_verb(text) and "wecker" in text:
            return self._parse_deactivate(text, local_now)
        if _is_wake(text):
            return self._parse_wake(text, local_now)
        if "wecker" in text and (_has_create_verb(text) or _matches_relative_create(text)):
            return self._parse_create(text, local_now)
        return _error("unsupported_utterance")

    def _parse_delete(self, text: str) -> ParseResult:
        name = _alarm_name(text)
        if name in {None, "wecker"}:
            return _clarify("missing_alarm_selector", "Welchen Wecker soll ich löschen?")
        if name != "wecker" and re.fullmatch(
            rf"(?:lösche|loesche|entferne) (?:den|meinen) {_ALARM_PATTERN}", text
        ):
            return _ok(AlarmIntent(kind=IntentKind.DELETE, label=name))
        return _error("unsupported_tokens")

    def _parse_deactivate(self, text: str, now: datetime) -> ParseResult:
        name = _alarm_name(text)
        if re.fullmatch(r"deaktiviere den wecker für morgen", text):
            return _ok(
                AlarmIntent(kind=IntentKind.DEACTIVATE, date=(now + timedelta(days=1)).date())
            )
        if name != "wecker" and re.fullmatch(rf"deaktiviere (?:den|meinen) {_ALARM_PATTERN}", text):
            return _ok(AlarmIntent(kind=IntentKind.DEACTIVATE, label=name))
        if name in {None, "wecker"}:
            return _clarify("missing_alarm_selector", "Welchen Wecker soll ich deaktivieren?")
        return _error("unsupported_tokens")

    def _parse_status(self, text: str) -> ParseResult:
        name = _alarm_name(text)
        if name in {None, "wecker"}:
            return _clarify("missing_alarm_selector", "Welchen Wecker meinst du?")
        return _ok(AlarmIntent(kind=IntentKind.STATUS, label=name))

    def _parse_activate(self, text: str) -> ParseResult:
        name = _alarm_name(text)
        if name in {None, "wecker"}:
            return _clarify("missing_alarm_selector", "Welchen Wecker soll ich aktivieren?")
        if not re.fullmatch(
            rf"(?:aktiviere|schalte) (?:den|meinen) {_ALARM_PATTERN}(?: wieder)?(?: ein)?",
            text,
        ):
            return _error("unsupported_tokens")
        return _ok(AlarmIntent(kind=IntentKind.ACTIVATE, label=name))

    def _parse_update(self, text: str, now: datetime) -> ParseResult:
        name = _alarm_name(text)
        if name in {None, "wecker"}:
            return _clarify("missing_alarm_selector", "Welchen Wecker soll ich ändern?")
        if not re.fullmatch(
            rf"(?:ändere|aendere|verschiebe|stell(?:e)?) (?:den|meinen) {_ALARM_PATTERN}"
            rf" (?:auf|um) {_TIME_PATTERN}",
            text,
        ):
            return _error("unsupported_tokens")
        clock_times = _clock_times(text)
        if len(clock_times) != 1:
            return _clarify("ambiguous_time", "Auf welche Uhrzeit soll ich ihn ändern?")
        hour, minute = clock_times[0]
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return _error("invalid_time")
        return _ok(
            AlarmIntent(
                kind=IntentKind.UPDATE,
                time_hour=hour,
                time_minute=minute,
                label=name,
            )
        )

    def _parse_wake(self, text: str, now: datetime) -> ParseResult:
        """ "Weck mich morgen um acht" carries no alarm word at all."""
        day = rf"(?:(?P<day>morgen|heute|{_WEEKDAY_PATTERN}) )?"
        if not re.fullmatch(rf"{_WAKE_VERB} {day}um {_TIME_PATTERN}", text):
            if re.match(rf"{_WAKE_VERB}\b", text):
                return _clarify("missing_time", "Wann soll ich dich wecken?")
            return _error("unsupported_tokens")
        clock_times = _clock_times(text)
        if len(clock_times) != 1:
            return _clarify("ambiguous_time", "Zu welcher Uhrzeit soll ich dich wecken?")
        hour, minute = clock_times[0]
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return _error("invalid_time")
        words = text.split()
        for word in words:
            if word in _WEEKDAYS:
                weekly = AlarmIntent(
                    kind=IntentKind.CREATE,
                    time_hour=hour,
                    time_minute=minute,
                    recurrence_weekday=_WEEKDAYS[word],
                )
                return _ok(weekly)
        target_date = _target_date(now, text, None, hour, minute)
        local_time = _local_datetime(target_date, hour, minute, self.timezone)
        if local_time is None:
            return _error("nonexistent_local_time")
        if local_time == "ambiguous":
            return _clarify(
                "ambiguous_local_time",
                "Meinst du die erste oder zweite Uhrzeit nach der Zeitumstellung?",
            )
        return _ok(AlarmIntent(kind=IntentKind.CREATE, at=local_time))

    def _parse_create(self, text: str, now: datetime) -> ParseResult:
        name = _alarm_name(text)
        if name is None:
            return _error("unsupported_tokens")

        weekday_range = _weekday_range(text)
        if weekday_range is not None:
            clock_times = _clock_times(text)
            if len(clock_times) != 1:
                return _clarify("ambiguous_time", "Zu welcher Uhrzeit soll der Wecker klingeln?")
            hour, minute = clock_times[0]
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return _error("invalid_time")
            return _ok(
                AlarmIntent(
                    kind=IntentKind.CREATE,
                    time_hour=hour,
                    time_minute=minute,
                    recurrence_weekdays=weekday_range,
                    label=_label(name),
                )
            )

        weekdays = _weekday_mentions(text)
        if "morgen" in text and weekdays:
            return _clarify(
                "conflicting_date_expressions", "Meinst du morgen oder den genannten Wochentag?"
            )
        multi_weekday = len(weekdays) > 1 and _matches_weekly_create_multi(text)
        if len(weekdays) > 1 and not multi_weekday:
            return _clarify("ambiguous_weekday", "An welchem Wochentag soll der Wecker klingeln?")

        relative = _relative_minutes(text)
        clock_times = _clock_times(text)
        if relative is not None and clock_times:
            return _clarify(
                "conflicting_time_expressions",
                "Soll der Wecker relativ oder zu einer Uhrzeit klingeln?",
            )
        if _has_invalid_relative_number(text):
            return _error("invalid_number")
        if _has_in_number_without_unit(text):
            return _clarify(
                "missing_duration_unit", "In wie vielen Minuten soll der Wecker klingeln?"
            )
        if relative is not None:
            if not _matches_relative_create(text):
                return _error("unsupported_tokens")
            if relative <= 0:
                return _error("invalid_duration")
            at = (now.astimezone(UTC) + timedelta(minutes=relative)).astimezone(self.timezone)
            return _ok(AlarmIntent(kind=IntentKind.CREATE, at=at, label=_label(name)))

        if len(clock_times) > 1:
            return _clarify("ambiguous_time", "Zu welcher Uhrzeit soll der Wecker klingeln?")
        if not clock_times:
            return _clarify("missing_time", "Wann soll der Wecker klingeln?")
        hour, minute = clock_times[0]
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return _error("invalid_time")

        if multi_weekday:
            return _ok(
                AlarmIntent(
                    kind=IntentKind.CREATE,
                    time_hour=hour,
                    time_minute=minute,
                    recurrence_weekdays=tuple(sorted(weekdays)),
                    label=_label(name),
                )
            )

        weekly = _weekly_weekday(text)
        if weekly is not None:
            if not _matches_weekly_create(text):
                return _error("unsupported_tokens")
            return _ok(
                AlarmIntent(
                    kind=IntentKind.CREATE,
                    time_hour=hour,
                    time_minute=minute,
                    recurrence_weekday=weekly,
                    label=_label(name),
                )
            )

        weekday = weekdays[0] if weekdays else None
        if not _matches_one_time_create(text):
            return _error("unsupported_tokens")
        target_date = _target_date(now, text, weekday, hour, minute)
        local_time = _local_datetime(target_date, hour, minute, self.timezone)
        if local_time is None:
            return _error("nonexistent_local_time")
        if local_time == "ambiguous":
            return _clarify(
                "ambiguous_local_time",
                "Meinst du die erste oder zweite Uhrzeit nach der Zeitumstellung?",
            )
        return _ok(AlarmIntent(kind=IntentKind.CREATE, at=local_time, label=_label(name)))


def _ok(intent: AlarmIntent) -> ParseResult:
    return ParseResult(status=ParseStatus.OK, intent=intent)


def _clarify(code: str, question: str) -> ParseResult:
    return ParseResult(status=ParseStatus.CLARIFICATION, code=code, question=question)


def _error(code: str) -> ParseResult:
    return ParseResult(status=ParseStatus.ERROR, code=code)


def _normalise(text: str) -> str:
    joined = re.sub(r"(\w)-(wecker)\b", r"\1\2", text.casefold())
    return " ".join(re.sub(r"[?!.,]", " ", joined).split())


#: Words a speaker (or the speech recogniser) may put around a stop command.
#: They carry no meaning here and must not hide the command: "Oh, Stopp." and
#: "Okay, stopp bitte" are the same request as "Stopp".
_STOP_FILLERS: Final[frozenset[str]] = frozenset(
    {
        "oh",
        "o",
        "ohh",
        "ooh",
        "ah",
        "ahh",
        "eh",
        "äh",
        "ähm",
        "ehm",
        "hm",
        "hmm",
        "hey",
        "hej",
        "hallo",
        "ok",
        "okay",
        "also",
        "na",
        "nu",
        "so",
        "bitte",
        "doch",
        "mal",
        "jetzt",
        "schnell",
        "sofort",
        "endlich",
        "verdammt",
        "man",
        "du",
        "mir",
        "jarvis",
        "assistent",
        "liebes",
    }
)

#: Short stop verbs. Alone they may belong to music, a vacuum or anything else,
#: so they are acted on only while an alarm is actually ringing.
_STOP_SHORT: Final[frozenset[str]] = frozenset(
    {
        "stopp",
        "stop",
        "stoppe",
        "stoppt",
        "halt",
        "halte",
        "ruhe",
        "still",
        "ende",
        "aus",
        "genug",
        "schluss",
    }
)

#: Explicit alarm commands. They name the alarm themselves, so they stay alarm
#: commands even when nothing rings - the answer is then "no alarm is ringing"
#: and no player is touched.
_STOP_ALARM_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "stopp den wecker",
        "stop den wecker",
        "stoppe den wecker",
        "wecker aus",
        "wecker stopp",
        "wecker stop",
        "wecker ausmachen",
        "wecker abstellen",
        "mach den wecker aus",
        "schalte den wecker aus",
        "alarm aus",
        "alarm stopp",
        "alarm stop",
    }
)

#: Unspecific stop forms. "Hör auf", "sei still" or "Klingel aus" may belong to
#: music, a vacuum, the doorbell or a running alarm, so they are *not* alarm
#: commands by themselves: they only act while our alarm is actually ringing.
#: Without a ring they fall back to the normal conversation path.
_STOP_CONTEXT_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "hör auf",
        "hoer auf",
        "aufhören",
        "aufhoeren",
        "hör auf zu klingeln",
        "hoer auf zu klingeln",
        "sei still",
        "still jetzt",
        "ruhe jetzt",
        "ende jetzt",
        "genug jetzt",
        "klingel aus",
        "klingeln aus",
        "klingel abstellen",
        "klingeln abstellen",
        "nabu stopp",
        "nabu stop",
        "mach aus",
    }
)

#: Negations turn a stop into "keep ringing" ("hör nicht auf", "stopp nicht").
_STOP_NEGATIONS: Final[frozenset[str]] = frozenset(
    {"nicht", "net", "ned", "kein", "keine", "niemals", "nie"}
)


def _without_fillers(text: str) -> str:
    return " ".join(token for token in text.split() if token not in _STOP_FILLERS)


def match_stop(utterance: str) -> str | None:
    """Classify a stop command as ``"alarm"``, ``"context"`` or ``None``.

    ``"alarm"``  - the command names the alarm ("Wecker aus"), so it is an alarm
                   command even when nothing rings.
    ``"context"`` - the command is unspecific ("Stopp", "Hör auf", "Klingel aus").
                   It may belong to music, a vacuum, the doorbell or our alarm,
                   so the caller must decide by the actual ring state: only a
                   running alarm may be stopped, everything else stays chat.

    Only a *pure* stop command matches. An unrelated sentence that merely
    contains a stop word - "ich muss aufhören zu rauchen", "wie stoppe ich den
    Wecker" - must never silence an alarm, therefore the remaining words have to
    equal a known stop form exactly.
    """

    if not isinstance(utterance, str):
        return None
    text = _normalise(utterance)
    if not text:
        return None
    tokens = text.split()
    if _STOP_NEGATIONS.intersection(tokens):
        return None
    core = _without_fillers(text)
    if not core:
        return None
    if core in _STOP_ALARM_PHRASES:
        return "alarm"
    if core in _STOP_SHORT or core in _STOP_CONTEXT_PHRASES:
        return "context"
    return None


_LIST_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "welche wecker habe ich",
        "welche wecker habe ich noch",
        "welche wecker sind aktiv",
        "zeige mir meine wecker",
        "zeig mir meine wecker",
        "liste meine wecker",
        "liste alle wecker",
    }
)


def is_generic_stop(utterance: str) -> bool:
    """Whether this is an unspecific stop that needs the ring state to act."""

    return match_stop(utterance) == "context"


def is_stop_utterance(utterance: str) -> bool:
    """Whether the utterance is purely a stop command, named or bare."""

    return match_stop(utterance) is not None


def _is_stop(text: str) -> bool:
    return match_stop(text) is not None


def _is_list(text: str) -> bool:
    return text in _LIST_PHRASES or _wants_next(text)


def _wants_next(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:wann klingelt |wann kommt )(?:mein |der )?"
            r"(?:nächste|naechste|nächster|naechster) wecker",
            text,
        )
    )


def _is_status_question(text: str) -> bool:
    return bool(
        re.fullmatch(
            rf"ist (?:mein|der|die) {_ALARM_PATTERN} (?:aktiviert|aktiv|an|eingeschaltet)",
            text,
        )
    )


def _has_update_verb(text: str) -> bool:
    return bool(re.search(r"\b(?:ändere|aendere|verschiebe)\b", text))


def _has_activate_verb(text: str) -> bool:
    return bool(re.search(r"\b(?:aktiviere|schalte)\b", text))


def _is_wake(text: str) -> bool:
    return bool(re.match(rf"{_WAKE_VERB}\b", text))


def _has_delete_verb(text: str) -> bool:
    return bool(re.search(r"\b(?:lösche|loesche|entferne)\b", text))


def _has_deactivate_verb(text: str) -> bool:
    return bool(re.search(r"\bdeaktiviere\b", text))


def _has_create_verb(text: str) -> bool:
    return bool(re.search(rf"\b{_CREATE_VERB}\b", text))


def _alarm_name(text: str) -> str | None:
    """Return the spoken alarm word, e.g. ``medikamentenwecker`` or ``wecker``."""
    explicit = re.search(r"\b(?:namens|namen)\s+([a-zäöüß]+)", text)
    if explicit:
        return explicit.group(1)
    words = re.findall(r"\b[\wäöüß]*wecker\b", text)
    if not words:
        return None
    named = [word for word in words if word != "wecker"]
    return named[0] if named else "wecker"


def _label(name: str | None) -> str | None:
    """Any spoken *wecker* compound is a usable alarm name."""
    if name in {None, "wecker"}:
        return None
    return name


def _relative_minutes(text: str) -> int | None:
    match = re.search(r"\bin\s+(\S+)\s+minuten?\b", text)
    return None if match is None else _parse_number(match.group(1))


def _has_invalid_relative_number(text: str) -> bool:
    match = re.search(r"\bin\s+(\S+)\s+minuten?\b", text)
    return match is not None and _parse_number(match.group(1)) is None


def _has_in_number_without_unit(text: str) -> bool:
    return bool(re.search(r"\bin\s+(?:\d+|[a-zäöüß]+)\b(?!\s+minuten?\b)", text))


def _parse_number(value: str) -> int | None:
    if value.isdecimal():
        return int(value)
    if value in _NUMBERS:
        return _NUMBERS[value]
    compound = re.fullmatch(r"(.+)und(.+)", value)
    if compound is None:
        return None
    ones = _NUMBERS.get(compound.group(1))
    tens = _TENS.get(compound.group(2))
    if ones is None or not 1 <= ones <= 9 or tens is None:
        return None
    return ones + tens


def _clock_times(text: str) -> list[tuple[int, int]]:
    """Extract clock times including the common German spoken forms."""
    values: list[tuple[int, int]] = []
    for match in re.finditer(
        r"\b(?:um|auf|für|fuer)\s+(?:halb\s+(\d{1,2}|[a-zäöüß]+)"
        r"|viertel\s+(nach|vor)\s+(\d{1,2}|[a-zäöüß]+)"
        r"|(\d{1,2}|[a-zäöüß]+)(?::(\d{2}))?(?:\s*uhr)?)\b"
        r"|\b(\d{1,2})(?::(\d{2}))\s*uhr\b",
        text,
    ):
        if match.group(1):
            hour = _parse_number(match.group(1))
            if hour is not None:
                values.append(((hour - 1) % 24, 30))
        elif match.group(3):
            hour = _parse_number(match.group(3))
            if hour is not None:
                values.append((hour, 15 if match.group(2) == "nach" else 45))
        elif match.group(4):
            hour = _parse_number(match.group(4))
            if hour is not None:
                values.append((hour, int(match.group(5) or 0)))
        else:
            hour = _parse_number(match.group(6))
            if hour is not None:
                values.append((hour, int(match.group(7) or 0)))
    return values


def _weekday_range(text: str) -> tuple[int, ...] | None:
    """ "Montag bis Freitag" is a range, not an enumeration."""
    match = re.search(rf"\b({_WEEKDAY_PATTERN})\s+bis\s+({_WEEKDAY_PATTERN})\b", text)
    if match is None:
        return None
    start = _WEEKDAYS[match.group(1)]
    end = _WEEKDAYS[match.group(2)]
    if start <= end:
        return tuple(range(start, end + 1))
    return tuple(range(start, 7)) + tuple(range(0, end + 1))


def _weekday_mentions(text: str) -> list[int]:
    return [number for weekday, number in _WEEKDAYS.items() if re.search(rf"\b{weekday}\b", text)]


def _weekly_weekday(text: str) -> int | None:
    match = re.search(rf"\b(?:jeden|am)\s+({_WEEKDAY_PATTERN})\b", text)
    return (
        None
        if match is None
        or "jede woche" not in text
        and not text.startswith("stell jeden ")
        and not text.startswith("stelle jeden ")
        else _WEEKDAYS[match.group(1)]
    )


def _matches_relative_create(text: str) -> bool:
    verb = rf"(?:{_CREATE_VERB} (?:mir )?)?"
    named = r"(?: (?:namens|namen) [a-zäöüß]+)?"
    return bool(
        re.fullmatch(
            rf"{verb}(?:(?:einen )?{_ALARM_PATTERN}{named} in \S+ minuten?"
            rf"|in \S+ minuten (?:einen )?{_ALARM_PATTERN}{named})",
            text,
        )
    )


def _matches_weekly_create(text: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{_CREATE_VERB} (?:mir )?(?:(?:jeden|am) {_WEEKDAY_PATTERN}(?: jede woche)?|"
            rf"jede woche (?:jeden|am) {_WEEKDAY_PATTERN}) "
            rf"um {_TIME_PATTERN} einen {_ALARM_PATTERN}",
            text,
        )
    )


def _matches_weekly_create_multi(text: str) -> bool:
    """Several weekdays in one sentence, e.g. "jeden Dienstag und Donnerstag"."""
    joined = rf"{_WEEKDAY_PATTERN}(?: und (?:{_WEEKDAY_PATTERN}))+"
    return bool(
        re.fullmatch(
            rf"{_CREATE_VERB} (?:mir )?(?:(?:jeden|am) {joined}|jede woche {joined}) "
            rf"um {_TIME_PATTERN} einen {_ALARM_PATTERN}",
            text,
        )
    )


def _matches_one_time_create(text: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{_CREATE_VERB} (?:mir )?(?:(?:morgen|{_WEEKDAY_PATTERN}) )?"
            rf"(?:(?:um|auf|für|fuer) {_TIME_PATTERN})? {_ALARM_PATTERN}"
            rf"|{_CREATE_VERB} (?:mir )?(?:(?:morgen|{_WEEKDAY_PATTERN}) )?"
            rf"(?:um|auf|für|fuer) {_TIME_PATTERN} (?:einen )?{_ALARM_PATTERN}"
            rf"|{_CREATE_VERB} (?:mir )?(?:einen )?{_ALARM_PATTERN}"
            rf"(?: namens [a-zäöüß]+)? (?:für|fuer|auf|um) {_TIME_PATTERN}",
            text,
        )
    )


def _target_date(now: datetime, text: str, weekday: int | None, hour: int, minute: int) -> date:
    if "morgen" in text:
        return (now + timedelta(days=1)).date()
    if weekday is not None:
        days = (weekday - now.weekday()) % 7
        return (now + timedelta(days=days or 7)).date()
    candidate = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
    return now.date() if candidate > now else (now + timedelta(days=1)).date()


def _local_datetime(
    target_date: date, hour: int, minute: int, zone: ZoneInfo
) -> datetime | str | None:
    naive = datetime.combine(target_date, time(hour, minute))
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    first_valid = _round_trips(first, naive, zone)
    second_valid = _round_trips(second, naive, zone)
    if not first_valid and not second_valid:
        return None
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        return "ambiguous"
    return first if first_valid else second


def _round_trips(value: datetime, naive: datetime, zone: ZoneInfo) -> bool:
    return value.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
