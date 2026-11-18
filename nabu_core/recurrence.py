"""Calculate weekly wall-clock occurrences without silent DST assumptions."""

from datetime import UTC, datetime, timedelta


class InvalidLocalOccurrence(ValueError):
    """A recurrence falls into a DST gap or fold requiring explicit policy."""


def next_weekly(after, wall_time, weekdays, zone):
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("aware datetime required")
    if wall_time.tzinfo is not None:
        raise ValueError("wall_time must be local and naive")
    if not weekdays or any(type(day) is not int or day not in range(7) for day in weekdays):
        raise ValueError("weekdays must be integers from 0 to 6")
    local = after.astimezone(zone)
    for delta in range(8):
        date = local.date() + timedelta(days=delta)
        if date.weekday() not in weekdays:
            continue
        naive = datetime.combine(date, wall_time)
        candidates = set()
        for fold in (0, 1):
            aware = naive.replace(tzinfo=zone, fold=fold)
            utc = aware.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(utc)
        if not candidates:
            if naive < local.replace(tzinfo=None):
                continue
            raise InvalidLocalOccurrence("nonexistent local time")
        future = {instant for instant in candidates if instant > after.astimezone(UTC)}
        if not future:
            continue
        if len(candidates) > 1:
            raise InvalidLocalOccurrence("ambiguous local time")
        return future.pop()
    raise ValueError("no next occurrence found")
