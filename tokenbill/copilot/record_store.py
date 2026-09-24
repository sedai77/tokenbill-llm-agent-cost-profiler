"""GitHub Copilot record store (CP-STORE; addendum §7.2, DC7, DC24; CORE-AMENDMENTS C-10, C-27;
rulings R-E21, R-E45).

:class:`CopilotRecordStore` implements :class:`tokenbill.core.protocols.ExtRecordStore` for the
three people-level Copilot record types — ``LicenseSnapshot`` (seats), ``ActivityDay``
(usage-metrics user days) and ``ConfigSnapshot`` (budgets, cost centers, org settings, run flags
and the count rows of aggregate-only handoff bundles) — in Copilot-owned tables of the ledger's
SQLite file (its own connection, WAL; the file is created ``0600`` when this store creates it).

Rules (binding text in the addendum and the CP-STORE brief; choices where they are silent are
marked *decision*):

* **Natural keys.** ``rec_key = stable_id("rk", core.records.record_key(rec))`` — never NULL (an
  activity-report seat with ``org=None`` re-ingests idempotently). An upsert keeps the version with
  the larger ``fetched_ms``; equal ``fetched_ms`` go to the canonically larger version (the
  canonical JSON of ``core.records.to_json``, as ``core.testing.MemoryRecordStore``), so ingest is
  idempotent and order independent. ``org`` is stored as ``''`` for None (§7.2 DDL,
  ``NOT NULL DEFAULT ''``) and read back as None; ``assigned_via_team`` is a nullable INTEGER
  (None = unknown, the activity report).
* **Key ids (R-E21).** ``put(result, principal_key_id=…)`` stores license and activity rows only
  when *principal_key_id* is one of the key ids the ledger's SPEC §7.1 ``meta`` accepts —
  ``org_key_id`` and, after an adoption, ``adopted_key_id`` (read at every ``put``, read-only).
  Otherwise every person row of the batch is skipped and counted as
  ``dq.principal_key_mismatch``; a file without a ledger ``meta`` (or a keyless ledger that has not
  adopted yet) accepts none. Configuration rows carry no person and are always stored. Each
  person row keeps its ``principal_key_id`` (:meth:`CopilotRecordStore.principal_key_id_of`,
  :meth:`CopilotRecordStore.licenses_with_key_ids`).
* **Counting people** (``count_users``, SPEC §8.4, addendum §8.2): distinct principals of the rows
  in the window matching *where* (keys :data:`RECORD_WHERE_KEYS`; ``""`` matches a missing value;
  ``date_from`` / ``date_to`` inclusive; ``principal`` / ``session`` / ``session_key`` →
  ``PrivacyError``). Principals under different key ids are never joined (*decision*: the count is
  the largest per-key-id distinct count, a lower bound, like ``core.kanon.scope_counter`` across
  stores). Only when no license (activity) row exists in the window does it fall back to the
  ``n_people`` of ``seat_counts`` (``activity_counts``) rows of aggregate-only bundles: seat-count
  rows are summed per entity and snapshot day and the largest sum is returned; activity-count rows
  give their largest ``n_people`` (both lower bounds, as the fake).
* **Retention and purge** (SPEC §7.5): ``retain`` deletes license and activity rows dated before the
  UTC day of the cut (configuration and count rows are team-level and kept); ``purge`` deletes one
  principal's rows (``p_`` only; any other value is refused) and / or every row before a time.
  Deleted content is overwritten (``PRAGMA secure_delete``) and the WAL is checkpointed, so erased
  rows do not survive in free pages. A run that removed rows writes one audit row — into STORE's
  SPEC §7.1 ``audit`` table when the file has it (documented DDL; the only STORE table written),
  else into this store's ``copilot_audit`` table of the same shape; the detail names no person.
* Only ``TokenbillError`` subclasses escape for bad input: an unreadable file, an unknown window
  key, a value SQLite cannot hold (an integer beyond 64 bits, text that is not UTF-8) raise
  ``UsageError``; wrong record types raise ``ContractViolation``.

No reconciler decision is persisted (CORE-AMENDMENTS C-17 withdraws the addendum's revision-3
``copilot_decisions`` table).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.jsonl import open_private
from tokenbill.core.kanon import PERSON_DIMS
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    LicenseSnapshot,
    record_key,
    to_json,
)
from tokenbill.core.types import IngestResult

__all__ = [
    "COPILOT_TABLES",
    "COUNT_SOURCES",
    "DQ_PRINCIPAL_KEY_MISMATCH",
    "RECORD_WHERE_KEYS",
    "SCHEMA_VERSION",
    "STORE_NAME",
    "CopilotRecordStore",
]

#: ``ExtRecordStore.name`` (the ``ExtensionSpec`` name it belongs to).
STORE_NAME = "copilot"
#: ``copilot_meta.schema_version`` of the tables below.
SCHEMA_VERSION = "1"
#: Data-quality code of person rows refused under a key id the ledger does not accept (R-E21).
DQ_PRINCIPAL_KEY_MISMATCH = "dq.principal_key_mismatch"
#: ``count_users`` sources.
COUNT_SOURCES = ("licenses", "activity")
#: ``count_users`` *where* keys (``surface`` and ``editor_family`` both name a seat's last activity
#: surface), the same set as ``core.testing.RECORD_WHERE_KEYS``.
RECORD_WHERE_KEYS = frozenset({"team", "cost_center", "org", "plan", "bucket", "product",
                               "editor_family", "surface", "date_from", "date_to"})
#: Every table this store owns in the shared file.
COPILOT_TABLES = ("copilot_meta", "copilot_license_snapshots", "copilot_activity_days",
                  "copilot_config_snapshots", "copilot_audit")

_DDL = """
CREATE TABLE IF NOT EXISTS copilot_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS copilot_license_snapshots(rec_key TEXT PRIMARY KEY,
  snapshot_date TEXT NOT NULL, product TEXT NOT NULL, plan TEXT NOT NULL, principal TEXT,
  team TEXT, cost_center TEXT, org TEXT NOT NULL DEFAULT '', seat_created TEXT,
  pending_cancellation TEXT, last_activity_bucket TEXT NOT NULL, last_activity_surface TEXT,
  last_authenticated_bucket TEXT NOT NULL, assigned_via_team INTEGER, fetched_ms INTEGER NOT NULL,
  source_kind TEXT NOT NULL, principal_key_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS copilot_activity_days(rec_key TEXT PRIMARY KEY, date_utc TEXT NOT NULL,
  product TEXT NOT NULL, principal TEXT, team TEXT, cost_center TEXT, reported_cost_nano INTEGER,
  counts_json TEXT NOT NULL, flags TEXT NOT NULL, fetched_ms INTEGER NOT NULL,
  source_kind TEXT NOT NULL, principal_key_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS copilot_config_snapshots(rec_key TEXT PRIMARY KEY,
  snapshot_ms INTEGER NOT NULL, source_kind TEXT NOT NULL, kind TEXT NOT NULL,
  entity_id TEXT NOT NULL, attrs_json TEXT NOT NULL, fetched_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS copilot_audit(ts_ms INTEGER NOT NULL, actor TEXT,
  action TEXT NOT NULL, detail_json TEXT);
CREATE INDEX IF NOT EXISTS cls_date_team ON copilot_license_snapshots(snapshot_date, team);
CREATE INDEX IF NOT EXISTS cad_date_team ON copilot_activity_days(date_utc, team);
CREATE INDEX IF NOT EXISTS cls_principal ON copilot_license_snapshots(principal);
CREATE INDEX IF NOT EXISTS cad_principal ON copilot_activity_days(principal);
CREATE INDEX IF NOT EXISTS ccs_time ON copilot_config_snapshots(snapshot_ms);
"""

_LICENSE_COLS = ("rec_key", "snapshot_date", "product", "plan", "principal", "team", "cost_center",
                 "org", "seat_created", "pending_cancellation", "last_activity_bucket",
                 "last_activity_surface", "last_authenticated_bucket", "assigned_via_team",
                 "fetched_ms", "source_kind", "principal_key_id")
_ACTIVITY_COLS = ("rec_key", "date_utc", "product", "principal", "team", "cost_center",
                  "reported_cost_nano", "counts_json", "flags", "fetched_ms", "source_kind",
                  "principal_key_id")
_CONFIG_COLS = ("rec_key", "snapshot_ms", "source_kind", "kind", "entity_id", "attrs_json",
                "fetched_ms")
_LICENSES = "copilot_license_snapshots"
_ACTIVITY = "copilot_activity_days"
_CONFIG = "copilot_config_snapshots"
_OWN_AUDIT = "copilot_audit"
_STORE_AUDIT = "audit"
_STORE_META = "meta"
_ACCEPTED_META_KEYS = ("org_key_id", "adopted_key_id")

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MIN_MS = -62_135_596_800_000            # 0001-01-01T00:00:00Z
_MAX_MS = 253_402_300_799_999            # 9999-12-31T23:59:59.999Z
_INT64 = 2**63 - 1
_CHUNK = 400                              # bound parameters per IN (...) query
_P_RE = re.compile(r"p_[0-9a-f]{20}\Z")
_DIGITS_RE = re.compile(r"[0-9]{1,18}\Z")
_FOREVER_MS = 2**53


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def _canonical(rec: object) -> str:
    """The canonical JSON of a record (the latest-fetch-wins tie-break, as the fake's)."""
    return json.dumps(to_json(rec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _day_of_ms(ms: int) -> str:
    """The UTC date of *ms*, clamped to 0001-01-01 … 9999-12-31."""
    ms = min(max(ms, _MIN_MS), _MAX_MS)
    return (_EPOCH + _dt.timedelta(days=ms // _DAY_MS)).isoformat()


def _window(window: Mapping[str, object]) -> tuple[int, int]:
    unknown = set(window) - {"since_ms", "until_ms"}
    if unknown:
        raise UsageError(f"unknown window key(s): {', '.join(sorted(map(str, unknown)))}")
    lo, hi = window.get("since_ms"), window.get("until_ms")
    for value in (lo, hi):
        if value is not None and type(value) is not int:
            raise UsageError("window bounds must be int milliseconds")
    return (lo if lo is not None else 0,             # type: ignore[return-value]
            hi if hi is not None else _FOREVER_MS)


def _date_range(lo: int, hi: int) -> tuple[str, str] | None:
    """Inclusive UTC dates whose day overlaps ``[lo, hi)``; None when no day does."""
    if hi <= lo or hi <= _MIN_MS or lo > _MAX_MS:
        return None
    return _day_of_ms(lo), _day_of_ms(hi - 1)


def _ms_arg(value: object, what: str) -> int:
    if type(value) is not int:
        raise UsageError(f"{what}: expected int milliseconds")
    return value  # type: ignore[return-value]


def _chunks(items: Sequence[str], n: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _count_of(value: object) -> int:
    """A non-negative count from a configuration attr (garbage → 0: counts are lower bounds)."""
    if type(value) is int:
        return max(value, 0)  # type: ignore[call-overload]
    if isinstance(value, str) and _DIGITS_RE.match(value.strip()):
        return int(value.strip())
    return 0


def _eq(actual: object, wanted: str) -> bool:
    """``where`` equality: ``""`` matches a missing (None) value."""
    return actual is None if wanted == "" else actual == wanted


def _check_where(where: Mapping[str, str] | None) -> dict[str, str]:
    if where is not None and not isinstance(where, Mapping):
        raise UsageError("count_users: where must be a mapping")
    clause = dict(where or {})
    person = sorted(str(k) for k in set(clause) & PERSON_DIMS)
    if person:
        raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
    unknown = sorted(str(k) for k in set(clause) - RECORD_WHERE_KEYS)
    if unknown:
        raise UsageError(f"unknown record filter key(s): {', '.join(unknown)}")
    if not all(isinstance(v, str) for v in clause.values()):
        raise UsageError("count_users: where values must be strings")
    return clause


def _dates_ok(date: str, clause: Mapping[str, str]) -> bool:
    lo, hi = clause.get("date_from"), clause.get("date_to")
    return (lo is None or date >= lo) and (hi is None or date <= hi)


def _config_match(cfg: ConfigSnapshot, clause: Mapping[str, str]) -> bool:
    """Whether a count row of an aggregate-only bundle matches *clause* (the fake's rule)."""
    attrs = dict(cfg.attrs)
    for k, v in clause.items():
        if k in ("date_from", "date_to"):
            continue
        if k == "org":
            ok = cfg.entity_id == f"org:{v}"
        elif k == "cost_center":
            ok = cfg.entity_id == f"cc:{v}"
        elif k == "product":
            ok = v == "github_copilot"
        elif k in ("editor_family", "surface"):
            ok = _eq(attrs.get("surface"), v)
        else:
            ok = _eq(attrs.get(k), v)
        if not ok:
            return False
    return _dates_ok(_day_of_ms(cfg.snapshot_ms), clause)


def _check_int64(value: int | None, what: str) -> None:
    if value is not None and not -_INT64 - 1 <= value <= _INT64:
        raise UsageError(f"record store: {what} out of range")


# ---------------------------------------------------------------------------------------------
# row codecs
# ---------------------------------------------------------------------------------------------


def _license_row(rk: str, rec: LicenseSnapshot, kid: str) -> tuple:
    avt = None if rec.assigned_via_team is None else int(rec.assigned_via_team)
    return (rk, rec.snapshot_date, rec.product, rec.plan, rec.principal, rec.team,
            rec.cost_center, rec.org if rec.org is not None else "", rec.seat_created,
            rec.pending_cancellation, rec.last_activity_bucket, rec.last_activity_surface,
            rec.last_authenticated_bucket, avt, rec.fetched_ms, rec.source_kind, kid)


def _license_of(row: Sequence[Any]) -> LicenseSnapshot:
    (_, date, product, plan, principal, team, cc, org, created, pending, bucket, surface, auth,
     avt, fetched, source_kind, _kid) = row
    return LicenseSnapshot(
        snapshot_date=date, product=product, plan=plan, principal=principal, team=team,
        cost_center=cc, org=org or None, seat_created=created, pending_cancellation=pending,
        last_activity_bucket=bucket, last_activity_surface=surface,
        last_authenticated_bucket=auth, assigned_via_team=None if avt is None else bool(avt),
        fetched_ms=fetched, source_kind=source_kind)


def _activity_row(rk: str, rec: ActivityDay, kid: str) -> tuple:
    _check_int64(rec.reported_cost_nano, "reported_cost_nano")
    counts = json.dumps([[k, v] for k, v in rec.counts], separators=(",", ":"),
                        ensure_ascii=False)
    return (rk, rec.date_utc, rec.product, rec.principal, rec.team, rec.cost_center,
            rec.reported_cost_nano, counts, ",".join(rec.flags), rec.fetched_ms, rec.source_kind,
            kid)


def _activity_of(row: Sequence[Any]) -> ActivityDay:
    (_, date, product, principal, team, cc, cost, counts, flags, fetched, source_kind,
     _kid) = row
    return ActivityDay(
        date_utc=date, product=product, principal=principal, team=team, cost_center=cc,
        reported_cost_nano=cost, counts=tuple((k, v) for k, v in json.loads(counts)),
        flags=tuple(f for f in flags.split(",") if f), fetched_ms=fetched,
        source_kind=source_kind)


def _config_row(rk: str, rec: ConfigSnapshot, _kid: str) -> tuple:
    attrs = json.dumps([[k, v] for k, v in rec.attrs], separators=(",", ":"),
                       ensure_ascii=False)
    return (rk, rec.snapshot_ms, rec.source_kind, rec.kind, rec.entity_id, attrs, rec.fetched_ms)


def _config_of(row: Sequence[Any]) -> ConfigSnapshot:
    _, snapshot_ms, source_kind, kind, entity_id, attrs, fetched = row
    return ConfigSnapshot(snapshot_ms=snapshot_ms, source_kind=source_kind, kind=kind,
                          entity_id=entity_id,
                          attrs=tuple((k, v) for k, v in json.loads(attrs)), fetched_ms=fetched)


_TABLES: dict[str, tuple[tuple[str, ...], Callable[..., tuple], Callable[..., Any]]] = {
    _LICENSES: (_LICENSE_COLS, _license_row, _license_of),
    _ACTIVITY: (_ACTIVITY_COLS, _activity_row, _activity_of),
    _CONFIG: (_CONFIG_COLS, _config_row, _config_of),
}


# ---------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------


class CopilotRecordStore:
    """The Copilot :class:`~tokenbill.core.protocols.ExtRecordStore` on the ledger's SQLite file.

    ``CopilotRecordStore(db_path, *, create=True, now_ms=None)``: opens (with *create*, creates
    ``0600``) the database file, switches it to WAL and creates the Copilot tables (addendum §7.2
    DDL). *now_ms* fixes the audit timestamp (tests); None uses the wall clock. See the module
    docstring for the rules; ``close()`` (or a ``with`` block) releases the connection.
    """

    name = STORE_NAME

    def __init__(self, db_path: Path | str, *, create: bool = True,
                 now_ms: int | None = None) -> None:
        path = Path(db_path)
        if now_ms is not None and type(now_ms) is not int:
            raise UsageError("record store: now_ms must be int milliseconds")
        self._now_ms = now_ms
        if not path.exists():
            if not create:
                raise UsageError("record store: the database file does not exist")
            try:
                open_private(path, "ab").close()
            except OSError as exc:
                raise UsageError(
                    f"record store: cannot create the database ({type(exc).__name__})") from None
        try:
            self._conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        except sqlite3.Error as exc:
            raise UsageError(
                f"record store: cannot open the database ({type(exc).__name__})") from None
        try:
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA secure_delete = ON")
            self._ensure_schema()
        except sqlite3.Error as exc:
            self._conn.close()
            raise UsageError(
                f"record store: not a usable database ({type(exc).__name__})") from None

    def _ensure_schema(self) -> None:
        self._conn.executescript(_DDL)
        row = self._conn.execute(
            "SELECT value FROM copilot_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT OR IGNORE INTO copilot_meta(key, value) "
                               "VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            raise UsageError("record store: unsupported Copilot schema version")

    # ---------- lifecycle ----------

    def close(self) -> None:
        """Close the connection (idempotent)."""
        self._conn.close()

    def __enter__(self) -> CopilotRecordStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise UsageError(
                f"record store: cannot start a transaction ({type(exc).__name__})") from None
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _has_table(self, name: str) -> bool:
        return self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                  (name,)).fetchone() is not None

    def _query(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, tuple(params))
        except (sqlite3.Error, OverflowError, UnicodeEncodeError) as exc:
            raise UsageError(f"record store: query failed ({type(exc).__name__})") from None

    # ---------- writes ----------

    def accepted_key_ids(self) -> frozenset[str]:
        """The principal key ids :meth:`put` accepts now: the ledger ``meta`` ``org_key_id`` and
        ``adopted_key_id`` (empty values ignored; no ``meta`` table → none)."""
        if not self._has_table(_STORE_META):
            return frozenset()
        marks = ",".join("?" * len(_ACCEPTED_META_KEYS))
        rows = self._query(f"SELECT value FROM {_STORE_META} WHERE key IN ({marks})",
                           _ACCEPTED_META_KEYS).fetchall()
        return frozenset(str(v) for (v,) in rows if v)

    def put(self, result: IngestResult, *, principal_key_id: str | None) -> dict[str, int]:
        """Store the licenses, activity days and configuration rows of *result* (see the module
        docstring for the key-id check and the upsert). Returns the counts ``licenses``,
        ``activity``, ``config``, ``skipped`` and ``dq.principal_key_mismatch``."""
        if not isinstance(result, IngestResult):
            raise UsageError("put expects an IngestResult")
        lics, acts, confs = list(result.licenses), list(result.activity), list(result.config)
        for items, cls in ((lics, LicenseSnapshot), (acts, ActivityDay),
                           (confs, ConfigSnapshot)):
            if not all(isinstance(x, cls) for x in items):
                raise ContractViolation(f"put: expected {cls.__name__} records")
        if principal_key_id is not None and not isinstance(principal_key_id, str):
            raise UsageError("put: principal_key_id must be a str or None")
        counts = {"licenses": 0, "activity": 0, "config": 0, "skipped": 0,
                  DQ_PRINCIPAL_KEY_MISMATCH: 0}
        people = len(lics) + len(acts)
        with self._tx():
            accepted = self.accepted_key_ids() if people else frozenset()
            if people and (principal_key_id is None or principal_key_id not in accepted):
                counts["skipped"] = counts[DQ_PRINCIPAL_KEY_MISMATCH] = people
            elif people:
                kid = principal_key_id or ""
                self._upsert(_LICENSES, lics, kid)
                self._upsert(_ACTIVITY, acts, kid)
                counts["licenses"], counts["activity"] = len(lics), len(acts)
            self._upsert(_CONFIG, confs, "")
            counts["config"] = len(confs)
        return counts

    def _upsert(self, table: str, recs: Iterable[Any], kid: str) -> None:
        cols, row_of, _ = _TABLES[table]
        best: dict[str, tuple[Any, str]] = {}
        for rec in recs:
            rk = stable_id("rk", record_key(rec))
            canon = _canonical(rec)
            cur = best.get(rk)
            if cur is None or (rec.fetched_ms, canon) > (cur[0].fetched_ms, cur[1]):
                best[rk] = (rec, canon)
        if not best:
            return
        keys = sorted(best)
        existing: dict[str, int] = {}
        for chunk in _chunks(keys, _CHUNK):
            marks = ",".join("?" * len(chunk))
            existing.update(self._query(
                f"SELECT rec_key, fetched_ms FROM {table} WHERE rec_key IN ({marks})", chunk))
        rows = []
        for rk in keys:
            rec, canon = best[rk]
            old_fetched = existing.get(rk)
            if old_fetched is not None:
                if rec.fetched_ms < old_fetched:
                    continue
                if rec.fetched_ms == old_fetched:
                    old = self._load(table, rk)
                    if old is not None and canon <= _canonical(old):
                        continue
            rows.append(row_of(rk, rec, kid))
        marks = ",".join("?" * len(cols))
        try:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({marks})", rows)
        except (sqlite3.Error, OverflowError, UnicodeEncodeError) as exc:
            raise UsageError(
                f"record store: cannot store a record ({type(exc).__name__})") from None

    def _load(self, table: str, rk: str) -> Any | None:
        cols, _, of = _TABLES[table]
        row = self._query(f"SELECT {','.join(cols)} FROM {table} WHERE rec_key = ?",
                          (rk,)).fetchone()
        return of(row) if row is not None else None

    # ---------- reads ----------

    def _iter_people(self, table: str, date_col: str, order: str,
                     window: Mapping[str, object]) -> Iterator[tuple[Any, str]]:
        lo, hi = _window(window)
        rng = _date_range(lo, hi)
        if rng is None:
            return
        cols, _, of = _TABLES[table]
        cur = self._query(f"SELECT {','.join(cols)} FROM {table} WHERE {date_col} >= ? AND "
                          f"{date_col} <= ? ORDER BY {order}", rng)
        for row in cur:
            yield of(row), row[-1]

    def iter_licenses(self, **window: int) -> Iterator[LicenseSnapshot]:
        """Stream the seat snapshots dated in the window (day overlap), ordered by snapshot date and
        natural key."""
        for rec, _ in self._iter_people(_LICENSES, "snapshot_date",
                                        "snapshot_date, product, principal, org", window):
            yield rec

    def iter_activity(self, **window: int) -> Iterator[ActivityDay]:
        """Stream the activity days dated in the window, ordered by date and natural key."""
        for rec, _ in self._iter_people(_ACTIVITY, "date_utc", "date_utc, product, principal",
                                        window):
            yield rec

    def licenses(self, **window: int) -> list[LicenseSnapshot]:
        """Stored seat snapshots dated in the window, by (snapshot date, natural key)."""
        return list(self.iter_licenses(**window))

    def activity(self, **window: int) -> list[ActivityDay]:
        """Stored activity days dated in the window, by (date, natural key)."""
        return list(self.iter_activity(**window))

    def licenses_with_key_ids(self, **window: int) -> list[tuple[LicenseSnapshot, str]]:
        """Like :meth:`licenses`, each with the principal key id it was accepted under."""
        return list(self._iter_people(_LICENSES, "snapshot_date",
                                      "snapshot_date, product, principal, org", window))

    def activity_with_key_ids(self, **window: int) -> list[tuple[ActivityDay, str]]:
        """Like :meth:`activity`, each with the principal key id it was accepted under."""
        return list(self._iter_people(_ACTIVITY, "date_utc", "date_utc, product, principal",
                                      window))

    def config(self, **window: int) -> list[ConfigSnapshot]:
        """Stored configuration rows whose ``snapshot_ms`` is in ``[since_ms, until_ms)``, by
        (time, natural key)."""
        lo, hi = _window(window)
        if hi <= lo:
            return []
        lo, hi = max(lo, -_INT64 - 1), min(hi, _INT64)
        rows = self._query(f"SELECT {','.join(_CONFIG_COLS)} FROM {_CONFIG} WHERE "
                           "snapshot_ms >= ? AND snapshot_ms < ?", (lo, hi)).fetchall()
        recs = [_config_of(row) for row in rows]
        return sorted(recs, key=lambda c: (c.snapshot_ms, record_key(c)))

    def principal_key_id_of(self, rec: LicenseSnapshot | ActivityDay) -> str | None:
        """The principal key id a stored license / activity row was accepted under (None when this
        exact record is not stored)."""
        if isinstance(rec, LicenseSnapshot):
            table = _LICENSES
        elif isinstance(rec, ActivityDay):
            table = _ACTIVITY
        else:
            raise UsageError("principal_key_id_of expects a LicenseSnapshot or ActivityDay")
        cols, _, of = _TABLES[table]
        row = self._query(f"SELECT {','.join(cols)} FROM {table} WHERE rec_key = ?",
                          (stable_id("rk", record_key(rec)),)).fetchone()
        if row is None or of(row) != rec:
            return None
        return str(row[-1])

    # ---------- counting people ----------

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str) -> int:
        """Distinct people (never ids) of *source* (``licenses`` | ``activity``) in the window
        matching *where*; see the module docstring for key ids and the count-row fallback."""
        if source not in COUNT_SOURCES:
            raise UsageError(f"unknown record count source {source!r} (licenses | activity)")
        clause = _check_where(where)
        lo, hi = _ms_arg(since_ms, "since_ms"), _ms_arg(until_ms, "until_ms")
        rng = _date_range(lo, hi)
        if rng is None:
            return 0
        table, date_col = ((_LICENSES, "snapshot_date") if source == "licenses"
                           else (_ACTIVITY, "date_utc"))
        base = f"FROM {table} WHERE {date_col} >= ? AND {date_col} <= ?"
        if self._query(f"SELECT 1 {base} LIMIT 1", rng).fetchone() is None:
            return self._count_from_summaries(source, clause, lo, hi)
        filt = self._filter_sql(source, date_col, clause)
        if filt is None:
            return 0
        sql, params = filt
        rows = self._query(f"SELECT principal_key_id, COUNT(DISTINCT principal) {base}{sql} "
                           "GROUP BY principal_key_id", (*rng, *params)).fetchall()
        return max((int(n) for _, n in rows), default=0)

    @staticmethod
    def _filter_sql(source: str, date_col: str,
                    clause: Mapping[str, str]) -> tuple[str, list[str]] | None:
        if source == "licenses":
            columns = {"team": "team", "cost_center": "cost_center", "org": "org", "plan": "plan",
                       "bucket": "last_activity_bucket", "product": "product",
                       "editor_family": "last_activity_surface",
                       "surface": "last_activity_surface"}
        else:
            columns = {"team": "team", "cost_center": "cost_center", "product": "product"}
        sql: list[str] = []
        params: list[str] = []
        for key in sorted(clause):
            value = clause[key]
            if key == "date_from":
                sql.append(f" AND {date_col} >= ?")
                params.append(value)
            elif key == "date_to":
                sql.append(f" AND {date_col} <= ?")
                params.append(value)
            elif key not in columns:
                if value != "":
                    return None   # activity days carry no org / plan / bucket / surface
            elif value == "" and key != "org":
                sql.append(f" AND {columns[key]} IS NULL")
            else:
                sql.append(f" AND {columns[key]} = ?")   # org: '' is the stored None
                params.append(value)
        return "".join(sql), params

    def _count_from_summaries(self, source: str, clause: Mapping[str, str], lo: int,
                              hi: int) -> int:
        kind = "seat_counts" if source == "licenses" else "activity_counts"
        rows = [c for c in self.config(since_ms=lo, until_ms=hi)
                if c.kind == kind and _config_match(c, clause)]
        if kind == "activity_counts":
            return max((_count_of(dict(c.attrs).get("n_people")) for c in rows), default=0)
        per_snapshot: dict[tuple[str, str], int] = {}
        for c in rows:
            key = (c.entity_id, _day_of_ms(c.snapshot_ms))
            per_snapshot[key] = per_snapshot.get(key, 0) + _count_of(dict(c.attrs).get("n_people"))
        return max(per_snapshot.values(), default=0)

    # ---------- retention, purge, audit ----------

    def retain(self, *, identity_before_ms: int) -> int:
        """Delete license and activity rows dated before the UTC day of *identity_before_ms*
        (configuration rows are team-level and kept); returns the rows deleted."""
        cut = _day_of_ms(_ms_arg(identity_before_ms, "identity_before_ms"))
        with self._tx() as conn:
            removed = conn.execute(f"DELETE FROM {_LICENSES} WHERE snapshot_date < ?",
                                   (cut,)).rowcount
            removed += conn.execute(f"DELETE FROM {_ACTIVITY} WHERE date_utc < ?",
                                    (cut,)).rowcount
            if removed:
                self._audit_row(conn, "retention", "retain",
                                {"before": cut, "rows": removed, "store": STORE_NAME})
        if removed:
            self._checkpoint()
        return removed

    def purge(self, *, principal: str | None, before_ms: int | None, actor: str) -> int:
        """Delete one principal's license and activity rows and / or every row (configuration
        included) before *before_ms*; returns the rows deleted and writes an audit row without the
        identity. *principal* must be a ``p_`` pseudonym (raw identities are refused)."""
        if principal is None and before_ms is None:
            raise UsageError("purge needs principal or before_ms")
        if principal is not None and (not isinstance(principal, str)
                                      or not _P_RE.match(principal)):
            raise UsageError("purge: principal must be a p_ pseudonym")
        if not isinstance(actor, str):
            raise UsageError("purge: actor must be a str")
        if before_ms is not None:
            before_ms = _ms_arg(before_ms, "before_ms")
        removed = 0
        with self._tx() as conn:
            if principal is not None:
                for table in (_LICENSES, _ACTIVITY):
                    removed += conn.execute(f"DELETE FROM {table} WHERE principal = ?",
                                            (principal,)).rowcount
            if before_ms is not None:
                cut = _day_of_ms(before_ms)
                removed += conn.execute(f"DELETE FROM {_LICENSES} WHERE snapshot_date < ?",
                                        (cut,)).rowcount
                removed += conn.execute(f"DELETE FROM {_ACTIVITY} WHERE date_utc < ?",
                                        (cut,)).rowcount
                removed += conn.execute(f"DELETE FROM {_CONFIG} WHERE snapshot_ms < ?",
                                        (min(max(before_ms, -_INT64 - 1), _INT64),)).rowcount
            detail: dict[str, object] = {
                "by": "principal" if principal is not None else "before_ms",
                "rows": removed, "store": STORE_NAME}
            if before_ms is not None:
                detail["before_ms"] = before_ms
            self._audit_row(conn, actor, "purge", detail)
        self._checkpoint()
        return removed

    def _audit_row(self, conn: sqlite3.Connection, actor: str, action: str,
                   detail: Mapping[str, object]) -> None:
        table = _STORE_AUDIT if self._has_table(_STORE_AUDIT) else _OWN_AUDIT
        ts = self._now_ms if self._now_ms is not None else time.time_ns() // 1_000_000
        text = json.dumps(dict(detail), sort_keys=True, separators=(",", ":"))
        try:
            conn.execute(f"INSERT INTO {table}(ts_ms, actor, action, detail_json) "
                         "VALUES (?, ?, ?, ?)", (ts, actor, action, text))
        except (sqlite3.Error, UnicodeEncodeError) as exc:
            raise UsageError(f"record store: cannot write the audit row ({type(exc).__name__})"
                             ) from None

    def _checkpoint(self) -> None:
        """Move erased pages out of the WAL (best effort: a busy reader keeps the WAL)."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:  # pragma: no cover - a concurrent writer holds the lock
            pass

    def audit_log(self) -> list[tuple[int, str, str, str]]:
        """This store's audit rows ``(ts_ms, actor, action, detail_json)``, from STORE's ``audit``
        table (rows whose detail names ``"store":"copilot"``) and from ``copilot_audit``, oldest
        first, in write order within one timestamp (extension, not protocol)."""
        out: list[tuple[int, str, str, str]] = []
        for table in (_STORE_AUDIT, _OWN_AUDIT):
            if not self._has_table(table):
                continue
            rows = self._query(f"SELECT ts_ms, actor, action, detail_json FROM {table} "
                               "ORDER BY rowid").fetchall()
            out.extend(r for r in rows
                       if isinstance(r[3], str) and '"store":"copilot"' in r[3])
        return sorted(out, key=lambda r: r[0])
