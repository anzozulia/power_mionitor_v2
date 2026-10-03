"""Raw heartbeats are never stored and the timeline is never pruned (INV-09, DATA-01).

INV-09 reads "heartbeats, if stored at all, are pruned after the retention period; the
timeline is kept indefinitely". Phase 1 D-08 stores no raw heartbeat at all:
``location_state.last_heartbeat_at`` is the only heartbeat value, so there is nothing to
prune, no retention setting and no prune job. Its acceptance examples (a nightly prune of
heartbeats older than 30 days, a 7-day retention) have nothing to act on, so this module
pins the adapted rule instead (05-06 assumption-delta decision):

- heartbeats through the real ``/hb`` endpoint add no row to any table; only the values
  of the location's ``location_state`` row (``last_heartbeat_at``, ``state_version``)
  change, and no table is named for heartbeats. A rejected heartbeat writes nothing;
- closed history older than 40 days stays identical, ids and every field, together with
  its day's chart totals, through detection cycles and I/O passes that cross local
  midnight;
- only two statements in ``powermon/`` delete timeline rows: ``timeline.DELETE_SQL`` (one
  piece by id, inside the timeline writer) and ``history.DELETE_HISTORY_SQL`` (the admin's
  reset, DATA-03). No code truncates ``power_interval`` or ``location``, and no ORM
  ``.delete()`` reaches the timeline: a ``Location`` row delete would cascade to its
  intervals, and a location is only ever soft-deleted through ``deleted_at`` (Phase 4
  D-09). Every ``.delete()`` / ``._raw_delete()`` call in ``powermon/`` must be in
  ``ALLOWED_DELETES``, so a new one fails here until it is reviewed. Both scans are also
  run on source strings, so they cannot pass vacuously.

Heartbeat and pass tests are ``django_db(transaction=True)``: the I/O pass calls
``close_old_connections()``. Time comes only from the ``FakeClock``; Telegram is faked at
the HTTP boundary (``fake_telegram``).
"""

import ast
import dataclasses
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import DEFAULT_BOT_TOKEN, FakeClock
from django.conf import settings
from django.db import connection
from django.db.models import Value
from django.db.models.functions import Greatest
from django.http import HttpResponse
from django.test import Client, RequestFactory

from powermon.chart import source
from powermon.engine import history, timeline, transitions
from powermon.engine.models import LocationState, PowerInterval, SystemState
from powermon.i18n import chart_texts
from powermon.web import views
from powermon.worker import detection, io_loop

DB = pytest.mark.django_db(transaction=True)
KYIV = "Europe/Kyiv"

# The repository root inside the image (/app) and the package the scans read.
REPO = Path(settings.BASE_DIR)
PACKAGE = REPO / "powermon"

# The only two statements that delete timeline rows (compared as text, exactly).
TIMELINE_DELETES = {
    ("powermon/engine/timeline.py", timeline.DELETE_SQL),
    ("powermon/engine/history.py", history.DELETE_HISTORY_SQL),
}

# Every ORM delete in powermon/, one entry per call: (path from the repo root, the root
# name of its receiver chain). An entry is allowed only for a model other than
# PowerInterval and Location whose rows cannot cascade to power_interval.
ALLOWED_DELETES: tuple[tuple[str, str], ...] = (
    # LoginFailure (SEC-03 throttle): the prune of rows older than PRUNE_AFTER.
    ("powermon/throttle/store.py", "LoginFailure"),
    # LoginFailure (SEC-03 throttle): an IP's rows cleared after a successful sign-in.
    ("powermon/throttle/store.py", "LoginFailure"),
    # The auth user model (LOC-01, INV-21): every account other than the env's admin.
    ("powermon/web/admin_sync.py", "user_model"),
)

# A receiver chain through any of these names reaches the timeline: refused even if the
# allowlist names it. ``intervals`` is the PowerInterval related manager of a location.
PROTECTED_NAMES = frozenset({"PowerInterval", "Location", "intervals"})
DELETE_METHODS = frozenset({"delete", "_raw_delete"})

# DELETE FROM power_interval, any case and whitespace, the table name optionally
# double-quoted or schema-qualified; the match runs to the end of the string literal.
DELETE_TIMELINE_RE = re.compile(
    r'\bdelete\s+from\s+(?:"?public"?\s*\.\s*)?"?power_interval"?(?![\w"])[^"\']*',
    re.IGNORECASE,
)
# TRUNCATE [TABLE] [ONLY] name [, name ...]: the names it lists.
TRUNCATE_RE = re.compile(
    r'\btruncate\s+(?:table\s+)?((?:only\s+)?[\w".]+(?:\s*,\s*(?:only\s+)?[\w".]+)*)',
    re.IGNORECASE,
)
TRUNCATE_PROTECTED = frozenset({"power_interval", "location"})


@dataclasses.dataclass(frozen=True)
class DeleteCall:
    """One ``.delete()`` / ``._raw_delete()`` call found in a source file."""

    path: str
    root: str
    names: frozenset[str]
    line: int

    @property
    def protected(self) -> bool:
        return bool(self.names & PROTECTED_NAMES)


@pytest.fixture(autouse=True)
def kyiv_tz(settings: Any) -> Any:
    settings.CFG = dataclasses.replace(settings.CFG, display_tz=KYIV)
    return settings


def kyiv(text: str) -> datetime:
    """A Kyiv wall time such as ``"2026-10-02 23:40"`` as an aware UTC instant."""
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(KYIV)).astimezone(UTC)


# Source scans (pure: they take a source string and its path)


def timeline_sql_deletes(source_text: str, path: str) -> list[tuple[str, str]]:
    """Every ``DELETE FROM power_interval`` statement in ``source_text``, as (path, text)."""
    return [(path, match.group(0)) for match in DELETE_TIMELINE_RE.finditer(source_text)]


def protected_truncates(source_text: str, path: str) -> list[tuple[str, str]]:
    """Every TRUNCATE that lists ``power_interval`` or ``location``, as (path, statement)."""
    found = []
    for match in TRUNCATE_RE.finditer(source_text):
        names = set()
        for item in match.group(1).split(","):
            name = re.sub(r"^only\s+", "", item.strip(), flags=re.IGNORECASE)
            names.add(name.replace('"', "").rsplit(".", 1)[-1].lower())
        if names & TRUNCATE_PROTECTED:
            found.append((path, match.group(0)))
    return found


def orm_deletes(source_text: str, path: str) -> list[DeleteCall]:
    """Every call of a method named delete or _raw_delete, with its receiver chain.

    The receiver is followed through attributes (``a.b``), calls (``a()``) and subscripts
    (``a[0]``) down to its root name: ``LoginFailure`` for
    ``LoginFailure.objects.filter(...).delete()``, ``loc`` for ``loc.delete()``.
    """
    calls = []
    for node in ast.walk(ast.parse(source_text, filename=path)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in DELETE_METHODS
        ):
            continue
        names: set[str] = set()
        receiver: ast.expr = node.func.value
        while True:
            if isinstance(receiver, ast.Attribute):
                names.add(receiver.attr)
                receiver = receiver.value
            elif isinstance(receiver, ast.Call):
                receiver = receiver.func
            elif isinstance(receiver, ast.Subscript):
                receiver = receiver.value
            else:
                break
        root = receiver.id if isinstance(receiver, ast.Name) else type(receiver).__name__
        names.add(root)
        calls.append(DeleteCall(path, root, frozenset(names), node.lineno))
    return calls


def refused_deletes(
    calls: list[DeleteCall], allowed: tuple[tuple[str, str], ...]
) -> list[DeleteCall]:
    """The calls that reach the timeline, or that the allowlist does not name (per call)."""
    budget = Counter(allowed)
    refused = []
    for call in calls:
        key = (call.path, call.root)
        if call.protected or budget[key] == 0:
            refused.append(call)
        else:
            budget[key] -= 1
    return refused


def _package_sources() -> list[tuple[str, str]]:
    """(path from the repo root, text) of every .py file under powermon/, migrations included."""
    files = sorted(PACKAGE.rglob("*.py"))
    assert len(files) > 50, "the scan must read the whole package"
    return [(f.relative_to(REPO).as_posix(), f.read_text(encoding="utf-8")) for f in files]


# Database helpers


def _table_counts() -> dict[str, int]:
    """The row count of every table in the database, by table name."""
    counts = {}
    with connection.cursor() as cur:
        for table in sorted(connection.introspection.table_names(cur)):
            cur.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)}")  # noqa: S608
            counts[table] = cur.fetchone()[0]
    return counts


def _state_values(location: Any) -> dict[str, Any]:
    return LocationState.objects.filter(pk=location.pk).values().get()


def _serve(clock: FakeClock, request: Any) -> HttpResponse:
    response: HttpResponse = views.HeartbeatView.as_view(clock=clock)(request)
    return response


def _no_anchors() -> None:
    """The process anchors stay out of the way: only the location's own window counts."""
    SystemState.objects.update_or_create(
        pk=1, defaults={"detection_resumed_at": None, "web_started_at": None}
    )


def _pass(clock: FakeClock, state: io_loop.RelayState) -> bool:
    """One I/O pass, with the detection cursor moved to the clock's now (never back).

    Copied from tests/chart/test_lifecycle_release.py: the worker's detection thread keeps
    the cursor within a cycle of now, so a day's chart is finished once it has settled.
    """
    SystemState.objects.get_or_create(pk=1)
    SystemState.objects.filter(pk=1).update(
        last_cycle_completed_at=Greatest("last_cycle_completed_at", Value(clock.now()))
    )
    return io_loop.run_iteration(clock, state, charts=True)


def _old_rows(cutoff: datetime) -> list[dict[str, Any]]:
    """Every closed timeline piece that ended before ``cutoff``, every field, by id."""
    rows = PowerInterval.objects.filter(end_at__isnull=False, end_at__lt=cutoff)
    return list(rows.order_by("id").values())


def _day_totals(location: Any, day: date, now: datetime) -> tuple[Any, ...]:
    """The chart's numbers for one local day, as its finished render reads them."""
    row = source.load_week(location.pk, today=day, now=now, tz=KYIV, live=False).today_row
    total = chart_texts.row_total(row.off_us, row.count, row.monitored, "en")
    return (row.day, row.on_us, row.off_us, row.nm_us, row.count, row.monitored, total)


# Heartbeats store nothing (DATA-01, Phase 1 D-08)


@DB
def test_INV09_heartbeats_store_no_row(
    rf: RequestFactory,
    location_factory: Callable[..., Any],
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = location_factory()
    clock = FakeClock(fixed_now)
    first = _serve(clock, rf.get("/hb", {"key": location.device_key}))
    assert (first.status_code, first.content) == (200, b"ok")
    assert _state_values(location)["status"] == "on"
    before = _table_counts()
    state_before = _state_values(location)
    assert before["power_interval"] == 1

    for _ in range(50):
        clock.advance(minutes=1)
        response = _serve(clock, rf.get("/hb", {"key": location.device_key}))
        assert (response.status_code, response.content) == (200, b"ok")
    # One more through the whole middleware stack, with the Authorization header.
    clock.advance(minutes=1)
    monkeypatch.setattr(views.HeartbeatView, "clock", clock)
    response = Client().get("/hb", headers={"authorization": f"Bearer {location.device_key}"})
    assert (response.status_code, response.content) == (200, b"ok")

    assert _table_counts() == before
    state_after = _state_values(location)
    assert state_after["last_heartbeat_at"] == clock.now() == fixed_now + timedelta(minutes=51)
    assert state_after["state_version"] == state_before["state_version"] + 51
    changed = {k for k in state_after if state_after[k] != state_before[k]}
    assert changed == {"last_heartbeat_at", "state_version"}
    assert [t for t in before if "heartbeat" in t.lower()] == []


@DB
def test_INV09_rejected_heartbeat_stores_nothing_either(
    rf: RequestFactory, location_factory: Callable[..., Any], fixed_now: datetime
) -> None:
    location = location_factory()
    clock = FakeClock(fixed_now)
    assert _serve(clock, rf.get("/hb", {"key": location.device_key})).status_code == 200
    before = _table_counts()
    state_before = _state_values(location)
    unknown = "Z" * 32
    assert unknown != location.device_key

    requests = [
        rf.get("/hb", {"key": unknown}),
        rf.post("/hb", headers={"authorization": f"Bearer {unknown}"}),
        rf.get("/hb", {"key": "not a key"}),
        rf.get("/hb"),
    ]
    for request in requests:
        clock.advance(minutes=1)
        response = _serve(clock, request)
        assert (response.status_code, response.content) == (401, b"unauthorized")

    assert _table_counts() == before
    assert _state_values(location) == state_before


# The timeline is never pruned (INV-09)


@DB
def test_INV09_history_older_than_40_days_is_never_pruned(
    location_factory: Callable[..., Any], fake_telegram: Any
) -> None:
    now = kyiv("2026-10-02 23:40")
    cutoff = now - timedelta(days=40)
    old_day = date(2026, 8, 22)
    # Monitoring since 08:00 on 2026-08-22, an outage 09:00-10:00 that day (41 days ago).
    location = location_factory(created_at=kyiv("2026-08-22 07:00"))
    _no_anchors()
    assert transitions.record_heartbeat(location.pk, kyiv("2026-08-22 08:00")) == "started"
    assert transitions.record_heartbeat(location.pk, kyiv("2026-08-22 09:00")) == "plain"
    assert detection.run_cycle(kyiv("2026-08-22 09:01") + timedelta(seconds=31)) == 1
    assert transitions.record_heartbeat(location.pk, kyiv("2026-08-22 10:00")) == "restored"
    # Monitored ever since: a heartbeat at now, so the next transition closes the open
    # piece after the old days, never inside them.
    assert transitions.record_heartbeat(location.pk, now) == "plain"

    snapshot = _old_rows(cutoff)
    # Never vacuous: the on piece before the outage and its off piece are compared. The
    # open on piece begun by the restore is not: the next transition legitimately ends it.
    assert [(r["state"], r["start_at"], r["end_at"]) for r in snapshot] == [
        ("on", kyiv("2026-08-22 08:00"), kyiv("2026-08-22 09:00")),
        ("off", kyiv("2026-08-22 09:00"), kyiv("2026-08-22 10:00")),
    ]
    totals = _day_totals(location, old_day, now)
    assert totals[-1] == ("1h", " · 1")
    rows_before = PowerInterval.objects.count()

    fake_telegram.accept(DEFAULT_BOT_TOKEN)
    fake_telegram.accept_chart(DEFAULT_BOT_TOKEN)
    clock = FakeClock(now)
    state = io_loop.RelayState()
    # Two local midnights (10-03 and 10-04 00:00) pass; the worker settles, finishes and
    # posts charts, and detection records new transitions.
    steps = [
        kyiv("2026-10-02 23:40"),
        kyiv("2026-10-02 23:42"),
        kyiv("2026-10-02 23:58"),
        kyiv("2026-10-03 00:03"),
        kyiv("2026-10-03 00:10"),
        kyiv("2026-10-03 00:30"),
        kyiv("2026-10-03 12:00"),
        kyiv("2026-10-03 23:58"),
        kyiv("2026-10-04 00:03"),
        kyiv("2026-10-04 00:10"),
        kyiv("2026-10-04 00:30"),
    ]
    offs = 0
    for at in steps:
        clock.advance(seconds=(at - clock.now()).total_seconds())
        if at == kyiv("2026-10-03 12:00"):
            # Power is back for a while: a restore, then silence again.
            assert transitions.record_heartbeat(location.pk, at) == "restored"
        offs += detection.run_cycle(at)
        for _ in range(3):
            _pass(clock, state)

    assert offs == 2
    assert PowerInterval.objects.count() > rows_before
    photos = [call for call in fake_telegram.chart_calls if call.method == "sendPhoto"]
    assert len(photos) >= 3
    # The old closed pieces are untouched, ids and every field; nothing new ended there.
    assert _old_rows(cutoff) == snapshot
    assert _day_totals(location, old_day, clock.now()) == totals


# Only the timeline writer and the admin's reset delete timeline rows


def test_INV09_only_two_statements_delete_timeline_rows() -> None:
    deletes: list[tuple[str, str]] = []
    truncates: list[tuple[str, str]] = []
    for path, text in _package_sources():
        deletes += timeline_sql_deletes(text, path)
        truncates += protected_truncates(text, path)

    assert sorted(deletes) == sorted(TIMELINE_DELETES)
    assert timeline.DELETE_SQL == "DELETE FROM power_interval WHERE id = %s"
    assert history.DELETE_HISTORY_SQL == "DELETE FROM power_interval WHERE location_id = %(id)s"
    assert truncates == []


def test_INV09_no_orm_delete_reaches_the_timeline() -> None:
    calls: list[DeleteCall] = []
    for path, text in _package_sources():
        calls += orm_deletes(text, path)

    assert [c for c in calls if c.protected] == []
    assert refused_deletes(calls, ALLOWED_DELETES) == []
    assert sorted((c.path, c.root) for c in calls) == sorted(ALLOWED_DELETES)


def test_INV09_delete_scans_flag_a_timeline_delete() -> None:
    # The SQL scan: a multi-line statement with a quoted table name, and a TRUNCATE.
    sql = 'SQL = """\nDELETE\n  FROM "power_interval"\n WHERE start_at < %s\n"""\n'
    assert timeline_sql_deletes(sql, "x.py") == [
        ("x.py", 'DELETE\n  FROM "power_interval"\n WHERE start_at < %s\n')
    ]
    assert timeline_sql_deletes("Q = 'delete from power_interval_old'\n", "x.py") == []
    truncate = 'cur.execute("TRUNCATE TABLE location CASCADE")\n'
    assert protected_truncates(truncate, "x.py") == [("x.py", "TRUNCATE TABLE location")]
    assert protected_truncates('X = "truncate public.power_interval"\n', "x.py") != []
    assert protected_truncates('X = "TRUNCATE location_state"\n', "x.py") == []

    # The ORM scan: a delete that reaches the timeline is refused even when allowlisted.
    reaching = {
        "PowerInterval.objects.filter(location_id=1).delete()\n": "PowerInterval",
        "location.intervals.all().delete()\n": "location",
        "Location.objects.filter(pk=1).delete()\n": "Location",
    }
    for text, root in reaching.items():
        calls = orm_deletes(text, "x.py")
        assert [(c.root, c.protected) for c in calls] == [(root, True)]
        assert refused_deletes(calls, (("x.py", root),)) == calls
    # A delete on a variable or a model instance is refused until it is reviewed.
    for text, root in {"loc.delete()\n": "loc", "rows[0]._raw_delete(using)\n": "rows"}.items():
        calls = orm_deletes(text, "x.py")
        assert [(c.root, c.protected) for c in calls] == [(root, False)]
        assert refused_deletes(calls, ()) == calls

    # An allowlisted model whose rows cannot reach the timeline passes, once per entry.
    login = 'LoginFailure.objects.filter(client_ip="x").delete()\n'
    allowed = (("x.py", "LoginFailure"),)
    assert refused_deletes(orm_deletes(login, "x.py"), allowed) == []
    assert refused_deletes(orm_deletes(login * 2, "x.py"), allowed) != []
    assert refused_deletes(orm_deletes(login, "y.py"), allowed) != []
