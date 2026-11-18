"""Tests for the pure, German voice-alarm intent parser."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from nabu_core.voice_alarm import AlarmIntent, AlarmParser, IntentKind, ParseStatus

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 3, 18, 12, 30, tzinfo=BERLIN)  # Wednesday


def parse(text: str, *, now: datetime = NOW):
    return AlarmParser(timezone=BERLIN).parse(text, now=now)


class CreateAlarmTests(unittest.TestCase):
    def test_relative_spelled_number(self) -> None:
        result = parse("Stell einen Wecker in zehn Minuten")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertEqual(result.intent.at, datetime(2026, 3, 18, 12, 40, tzinfo=BERLIN))
        self.assertIsNone(result.intent.recurrence_weekday)
        self.assertIsNone(result.intent.label)

    def test_relative_digit_number(self) -> None:
        result = parse("Stelle in 15 Minuten einen Wecker")

        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertEqual(result.intent.at, datetime(2026, 3, 18, 12, 45, tzinfo=BERLIN))

    def test_tomorrow_at_time(self) -> None:
        result = parse("Stell morgen um 7 Uhr einen Wecker")

        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertEqual(result.intent.at, datetime(2026, 3, 19, 7, 0, tzinfo=BERLIN))

    def test_next_named_weekday(self) -> None:
        result = parse("Stell Sonntag um 9 Uhr einen Wecker")

        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertEqual(result.intent.at, datetime(2026, 3, 22, 9, 0, tzinfo=BERLIN))

    def test_today_named_weekday_means_next_occurrence(self) -> None:
        sunday = datetime(2026, 3, 22, 8, 0, tzinfo=BERLIN)

        result = parse("Stell Sonntag um 9 Uhr einen Wecker", now=sunday)

        self.assertEqual(result.intent.at, datetime(2026, 3, 29, 9, 0, tzinfo=BERLIN))

    def test_weekly_alarm(self) -> None:
        result = parse("Stell jeden Montag um 8 Uhr einen Wecker")

        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertIsNone(result.intent.at)
        self.assertEqual(result.intent.time_hour, 8)
        self.assertEqual(result.intent.time_minute, 0)
        self.assertEqual(result.intent.recurrence_weekday, 0)

    def test_medication_alarm_label(self) -> None:
        result = parse("Stell Sonntag um 12 Uhr einen Medikamentenwecker")

        self.assertEqual(result.intent.kind, IntentKind.CREATE)
        self.assertEqual(result.intent.label, "medikamentenwecker")
        self.assertEqual(result.intent.at, datetime(2026, 3, 22, 12, 0, tzinfo=BERLIN))

    def test_uhr_with_minutes(self) -> None:
        result = parse("Stell morgen um 07:05 Uhr einen Wecker")

        self.assertEqual(result.intent.at, datetime(2026, 3, 19, 7, 5, tzinfo=BERLIN))


class ManagementIntentTests(unittest.TestCase):
    def test_list_alarms(self) -> None:
        result = parse("Welche Wecker habe ich?")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent, AlarmIntent(kind=IntentKind.LIST))

    def test_delete_named_alarm(self) -> None:
        result = parse("Lösche den Medikamentenwecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.kind, IntentKind.DELETE)
        self.assertEqual(result.intent.label, "medikamentenwecker")

    def test_deactivate_tomorrow_selector(self) -> None:
        result = parse("Deaktiviere den Wecker für morgen")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.kind, IntentKind.DEACTIVATE)
        self.assertEqual(result.intent.date, datetime(2026, 3, 19, 0, 0, tzinfo=BERLIN).date())

    def test_stop(self) -> None:
        result = parse("Stopp den Wecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent, AlarmIntent(kind=IntentKind.STOP))


class AmbiguityAndErrorTests(unittest.TestCase):
    def test_missing_time_is_clarification(self) -> None:
        result = parse("Stell einen Wecker")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "missing_time")
        self.assertIsNone(result.intent)

    def test_relative_without_unit_is_clarification(self) -> None:
        result = parse("Stell einen Wecker in zehn")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "missing_duration_unit")

    def test_bare_delete_is_clarification(self) -> None:
        result = parse("Lösche den Wecker")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "missing_alarm_selector")

    def test_bare_deactivate_is_clarification(self) -> None:
        result = parse("Deaktiviere den Wecker")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "missing_alarm_selector")

    def test_unknown_number_word_is_error(self) -> None:
        result = parse("Stell einen Wecker in hunderteins Minuten")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_number")

    def test_zero_duration_is_error(self) -> None:
        result = parse("Stell einen Wecker in 0 Minuten")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_duration")

    def test_past_absolute_time_is_error(self) -> None:
        result = parse("Stell morgen um 25 Uhr einen Wecker")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_time")

    def test_multiple_times_are_never_guessed(self) -> None:
        result = parse("Stell morgen um 7 Uhr oder um 8 Uhr einen Wecker")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "ambiguous_time")

    def test_relative_and_absolute_time_are_never_combined(self) -> None:
        result = parse("Stell morgen um 7 Uhr in zehn Minuten einen Wecker")

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "conflicting_time_expressions")

    def test_unrelated_utterance_is_error(self) -> None:
        result = parse("Wie wird das Wetter morgen?")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "unsupported_utterance")

    def test_non_string_is_error(self) -> None:
        result = AlarmParser(timezone=BERLIN).parse(None, now=NOW)  # type: ignore[arg-type]

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_input")

    def test_empty_text_is_error(self) -> None:
        result = parse("   ")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_input")


class TimezoneAndPurityTests(unittest.TestCase):
    def test_nonexistent_dst_local_time_is_error(self) -> None:
        before_spring_forward = datetime(2026, 3, 28, 10, 0, tzinfo=BERLIN)

        result = parse("Stell morgen um 2:30 Uhr einen Wecker", now=before_spring_forward)

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "nonexistent_local_time")

    def test_ambiguous_dst_local_time_is_clarification(self) -> None:
        before_fall_back = datetime(2026, 10, 24, 10, 0, tzinfo=BERLIN)

        result = parse("Stell morgen um 2:30 Uhr einen Wecker", now=before_fall_back)

        self.assertEqual(result.status, ParseStatus.CLARIFICATION)
        self.assertEqual(result.code, "ambiguous_local_time")

    def test_result_is_deterministic_and_has_no_mutable_state(self) -> None:
        parser = AlarmParser(timezone=BERLIN)
        first = parser.parse("Stell morgen um 7 Uhr einen Wecker", now=NOW)
        second = parser.parse("Stell morgen um 7 Uhr einen Wecker", now=NOW)

        self.assertEqual(first, second)
        self.assertEqual(vars(parser), {"timezone": BERLIN})


class NegativeCorpusTests(unittest.TestCase):
    def test_non_alarm_requests_are_rejected_without_an_intent(self) -> None:
        utterances = (
            "Wie wird das Wetter morgen?",
            "Stell die Heizung auf zwanzig Grad",
            "Stell einen Timer auf zehn Minuten",
            "Schalte das Licht um 7 Uhr an",
            "Lösche die Datei morgen",
            "Deaktiviere die Alarmanlage",
            "Stoppe die Musik",
            "Kannst du mir einen Wecker erklären?",
            "Wecker morgen",
            "Stell mich morgen um 7 Uhr vor",
            "Erinnere mich morgen an den Arzttermin",
            "Stell einen Wecker irgendwann",
            "Lösche alles",
            "Bitte führe diesen Befehl aus",
            "Stell um 7 Uhr das Licht an",
        )

        for utterance in utterances:
            with self.subTest(utterance=utterance):
                result = parse(utterance)
                self.assertIsNone(result.intent)
                self.assertIn(result.status, {ParseStatus.ERROR, ParseStatus.CLARIFICATION})

    def test_spelled_compound_number_is_supported(self) -> None:
        result = parse("Stell einen Wecker in einundzwanzig Minuten")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.at, datetime(2026, 3, 18, 12, 51, tzinfo=BERLIN))

    def test_invalid_time_minutes_are_rejected(self) -> None:
        result = parse("Stell morgen um 7:60 Uhr einen Wecker")

        self.assertEqual(result.status, ParseStatus.ERROR)
        self.assertEqual(result.code, "invalid_time")


class AdversarialSafetyTests(unittest.TestCase):
    def assert_rejected_without_intent(self, utterance: str, code: str) -> None:
        result = parse(utterance)

        self.assertNotEqual(result.status, ParseStatus.OK)
        self.assertIsNone(result.intent)
        self.assertEqual(result.code, code)

    def test_spelled_hour_is_recognised(self) -> None:
        result = parse("Stell morgen um acht Uhr einen Wecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.at, datetime(2026, 3, 19, 8, 0, tzinfo=BERLIN))

    def test_weekly_recurrence_is_recognised_regardless_of_word_order(self) -> None:
        result = parse("Stell am Montag jede Woche um 8 Uhr einen Wecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertIsNone(result.intent.at)
        self.assertEqual(result.intent.time_hour, 8)
        self.assertEqual(result.intent.time_minute, 0)
        self.assertEqual(result.intent.recurrence_weekday, 0)

    def test_negated_create_never_produces_an_action(self) -> None:
        self.assert_rejected_without_intent(
            "Stell nicht morgen um 7 Uhr einen Wecker", "negated_utterance"
        )

    def test_negated_delete_never_produces_an_action(self) -> None:
        self.assert_rejected_without_intent(
            "Lösche nicht den Medikamentenwecker", "negated_utterance"
        )

    def test_tomorrow_and_weekday_are_not_silently_prioritised(self) -> None:
        self.assert_rejected_without_intent(
            "Stell morgen Sonntag um 7 Uhr einen Wecker", "conflicting_date_expressions"
        )

    def test_multiple_weekdays_are_not_silently_prioritised(self) -> None:
        self.assert_rejected_without_intent(
            "Stell Montag und Sonntag um 7 Uhr einen Wecker", "ambiguous_weekday"
        )

    def test_named_alarm_can_be_deleted_by_its_name(self) -> None:
        result = parse("Lösche den Frühstückswecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.kind, IntentKind.DELETE)
        self.assertEqual(result.intent.label, "frühstückswecker")

    def test_any_named_alarm_keeps_its_name_instead_of_becoming_unnamed(self) -> None:
        result = parse("Stell morgen um 7 Uhr einen Frühstückswecker")

        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.intent.label, "frühstückswecker")

    def test_unrecognised_extra_words_fail_closed(self) -> None:
        self.assert_rejected_without_intent(
            "Stell morgen um 7 Uhr einen Wecker sofort", "unsupported_tokens"
        )


if __name__ == "__main__":
    unittest.main()
