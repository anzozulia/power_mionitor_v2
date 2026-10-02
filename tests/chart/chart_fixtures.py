"""Chart test data and helpers, imported as ``from chart_fixtures import ...``.

This is a helper module, not a conftest: test directories have no ``__init__.py``, and a
second conftest would shadow the root one (03-PATTERNS).

- The sample week is docs/chart-spec.md section 10 ("Sample week fixture"), the same data
  as ``TIMELINE`` in docs/assets/mock_generator.py.txt: local Europe/Kyiv wall times with
  now = Thu 2026-10-01 14:37 local (11:37 UTC). Every off interval is its own outage.
- Kyiv offsets: UTC+3 (EEST) until 2026-10-25 01:00 UTC, UTC+2 (EET) until 2027-03-28
  01:00 UTC, then UTC+3 again. The sample week has no DST day.
- The DB helpers write ``power_interval`` and ``location_state`` rows as the engine would
  leave them, for tests that read the chart through ``source.load_week``.
"""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F

from powermon.chart.model import Piece
from powermon.engine.models import LocationState, PowerInterval

KYIV = "Europe/Kyiv"
SAMPLE_TODAY = date(2026, 10, 1)
# Thu 2026-10-01 14:37 local (UTC+3).
SAMPLE_NOW = datetime(2026, 10, 1, 11, 37, tzinfo=UTC)
SAMPLE_NAMES = {"uk": "Дім, Оболонь", "en": "Home, Obolon", "ru": "Дом, Оболонь"}

# (state, local start, local end or None while open): chart-spec §10 rows 1-27, verbatim.
LocalRow = tuple[str, str, str | None]
SAMPLE_INTERVALS: list[LocalRow] = [
    ("on", "2026-09-25 10:42", "2026-09-25 14:00"),  # 1 monitoring starts
    ("off", "2026-09-25 14:00", "2026-09-25 17:55"),  # 2
    ("on", "2026-09-25 17:55", "2026-09-26 04:00"),  # 3
    ("off", "2026-09-26 04:00", "2026-09-26 08:00"),  # 4
    ("on", "2026-09-26 08:00", "2026-09-26 11:00"),  # 5
    ("not_monitored", "2026-09-26 11:00", "2026-09-26 12:30"),  # 6 maintenance
    ("on", "2026-09-26 12:30", "2026-09-26 19:00"),  # 7
    ("off", "2026-09-26 19:00", "2026-09-26 22:40"),  # 8
    ("on", "2026-09-26 22:40", "2026-09-28 08:00"),  # 9 Sun: no outages
    ("off", "2026-09-28 08:00", "2026-09-28 12:05"),  # 10
    ("on", "2026-09-28 12:05", "2026-09-28 18:00"),  # 11
    ("off", "2026-09-28 18:00", "2026-09-28 21:30"),  # 12
    ("on", "2026-09-28 21:30", "2026-09-29 12:02"),  # 13
    ("off", "2026-09-29 12:02", "2026-09-29 15:58"),  # 14
    ("on", "2026-09-29 15:58", "2026-09-29 21:40"),  # 15
    ("off", "2026-09-29 21:40", "2026-09-30 01:15"),  # 16 crosses midnight
    ("on", "2026-09-30 01:15", "2026-09-30 03:10"),  # 17
    ("not_monitored", "2026-09-30 03:10", "2026-09-30 03:52"),  # 18 server downtime
    ("on", "2026-09-30 03:52", "2026-09-30 10:30"),  # 19
    ("off", "2026-09-30 10:30", "2026-09-30 10:50"),  # 20 20-min outage
    ("on", "2026-09-30 10:50", "2026-09-30 16:05"),  # 21
    ("off", "2026-09-30 16:05", "2026-09-30 19:35"),  # 22
    ("on", "2026-09-30 19:35", "2026-10-01 05:57"),  # 23
    ("off", "2026-10-01 05:57", "2026-10-01 09:03"),  # 24
    ("on", "2026-10-01 09:03", "2026-10-01 11:58"),  # 25
    ("off", "2026-10-01 11:58", "2026-10-01 13:02"),  # 26
    ("on", "2026-10-01 13:02", None),  # 27 open, drawn up to now
]


def kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-09-25 10:42"`` (fold 0) as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


def local_pieces(rows: list[LocalRow]) -> list[Piece]:
    """Pieces from ``(state, local start, local end or None)``; each off piece starts an outage."""
    pieces = []
    for state, start_text, end_text in rows:
        start = kyiv(start_text)
        end = None if end_text is None else kyiv(end_text)
        pieces.append(Piece(state, start, end, start if state == "off" else None))
    return pieces


def sample_pieces() -> list[Piece]:
    """The chart-spec §10 sample week as pieces (the last one open)."""
    return local_pieces(SAMPLE_INTERVALS)


def insert_pieces(location: Any, pieces: list[Piece]) -> None:
    """Store ``pieces`` as the location's ``power_interval`` rows."""
    for p in pieces:
        PowerInterval.objects.create(
            location=location,
            state=p.state,
            start_at=p.start,
            end_at=p.end,
            outage_start_at=p.outage_start,
        )


def set_status(location: Any, status: str, *, at: datetime) -> None:
    """Put the location's live state in ``status`` as of ``at`` (on since / heartbeat at ``at``)."""
    LocationState.objects.filter(location=location).update(
        status=status,
        on_since=at,
        last_heartbeat_at=at,
        outage_started_at=at if status == "off" else None,
        state_version=F("state_version") + 1,
    )


def monitor(location: Any, since: datetime) -> None:
    """A location that has been on since ``since``: live state "on" and one open on piece."""
    set_status(location, "on", at=since)
    insert_pieces(location, [Piece("on", since, None, None)])
