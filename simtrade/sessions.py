"""Sessions intraday : journée UTC, dernière entrée et clôture forcée des comptes automatiques.

Calculs purs (aucun I/O). Une session va de 00:00 à 24:00 UTC ; un compte auto peut ouvrir
une position de 00:00 à `last_entry` (exclu), et tout est fermé à `close`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from simtrade.errors import InvalidInput

DEFAULT_LAST_ENTRY = time(23, 0)
DEFAULT_CLOSE = time(23, 55)
LATE_AFTER = timedelta(seconds=5)  # une clôture plus de 5 s après l'heure prévue est marquée tardive


@dataclass(frozen=True)
class SessionRules:
    last_entry: time = DEFAULT_LAST_ENTRY
    close: time = DEFAULT_CLOSE


def parse_hhmm(raw: object, label: str) -> time:
    text = str(raw).strip()
    try:
        hours, minutes = text.split(":")
        if len(minutes) != 2 or not hours.isdigit() or not minutes.isdigit():
            raise ValueError
        return time(int(hours), int(minutes))
    except ValueError:
        raise InvalidInput(f"{label} invalide : '{raw}' (format HH:MM attendu, heure UTC).") from None


def validate_rules(last_entry: object, close: object) -> SessionRules:
    entry = parse_hhmm(last_entry, "Heure de dernière entrée")
    end = parse_hhmm(close, "Heure de clôture")
    if not time(0, 0) < entry <= end:
        raise InvalidInput(
            f"La dernière entrée ({entry:%H:%M}) doit être après 00:00 et au plus tard à la clôture ({end:%H:%M})."
        )
    return SessionRules(entry, end)


def session_date(now: datetime) -> date:
    return now.astimezone(timezone.utc).date()


def at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=timezone.utc)


def entry_allowed(now: datetime, rules: SessionRules) -> bool:
    return now.astimezone(timezone.utc).time() < rules.last_entry


def close_time(day: date, rules: SessionRules) -> datetime:
    return at(day, rules.close)


def close_due(opened_at: datetime, now: datetime, rules: SessionRules) -> bool:
    """Une activité commencée à `opened_at` doit être fermée dès la clôture de sa session."""
    return now >= close_time(session_date(opened_at), rules)


def is_late(opened_at: datetime, now: datetime, rules: SessionRules) -> bool:
    return now > close_time(session_date(opened_at), rules) + LATE_AFTER
