"""Daily rollups (SPEC §7.4): ``daily_rollup`` and ``cluster_day``.

``refresh_rollups(conn, dates)`` recomputes both tables for the given dates (default: every date
touched since the last refresh — the dates of the last ingest(s), kept in ``meta``). A cell's date
is the **request's** start date; ``active_users`` counts distinct principals among requests with at
least one billable inference that day. Money columns: ``exact_nano`` (exact lines on billed bases),
``allowance_nano`` (LIST_EQUIVALENT points on the ``subscription`` path), ``pool_nano``
(LIST_EQUIVALENT points on the Copilot billing paths, addendum §7.1) and the estimated range of the
billed lines; ``allowance_nano`` and ``pool_nano`` never enter ``exact_nano`` (D26).

``cluster_day`` rows exist for the cluster kinds ``team``, ``workspace`` (``workspace_id``),
``mdm_group`` and ``gateway`` (``Attribution.extra`` keys; R-E28). Rollups are refreshed **before**
identity retention nulls principals (``store.retention.apply_retention``), and a refresh of a date
whose principals were already retained keeps the ``active_users`` it had, so cost per active
developer-day survives for showback and verification.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from tokenbill.core.errors import UsageError

__all__ = [
    "CLUSTER_KINDS",
    "DAY_MS",
    "MAX_MS",
    "cluster_rows",
    "daily_rows",
    "date_of",
    "date_start_ms",
    "dirty_dates",
    "mark_dirty",
    "refresh_rollups",
]

DAY_MS = 86_400_000
#: The last millisecond of 9999-12-31 (dates are ``YYYY-MM-DD``).
MAX_MS = 253_402_300_799_999
_EPOCH = _dt.date(1970, 1, 1)
#: Cluster kind → the request dimension that names the cluster.
CLUSTER_KINDS = {"team": "team", "workspace": "workspace_id", "mdm_group": "mdm_group",
                 "gateway": "gateway"}
_COPILOT_PATHS = ("copilot_pool", "copilot_direct")
_DIRTY_KEY = "rollups_dirty"
_RETAINED_KEY = "identity_retained_before"
_CHUNK = 400


def date_of(ts_ms: int) -> str:
    """UTC date (``YYYY-MM-DD``) of *ts_ms*; ``UsageError`` past 9999-12-31."""
    if not 0 <= ts_ms <= MAX_MS:
        raise UsageError("timestamp outside 1970-01-01 … 9999-12-31")
    return (_EPOCH + _dt.timedelta(days=ts_ms // DAY_MS)).isoformat()


def date_start_ms(date: str) -> int:
    """Epoch milliseconds of 00:00 UTC of *date*; ``UsageError`` for a malformed date."""
    try:
        return (_dt.date.fromisoformat(date) - _EPOCH).days * DAY_MS
    except (TypeError, ValueError):
        raise UsageError("dates must be YYYY-MM-DD") from None


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row is not None else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (key, value))


def dirty_dates(conn: sqlite3.Connection) -> set[str]:
    """Dates whose rollups are stale (touched since the last refresh)."""
    text = _meta_get(conn, _DIRTY_KEY)
    return set(json.loads(text)) if text else set()


def mark_dirty(conn: sqlite3.Connection, dates: Iterable[str]) -> None:
    """Add *dates* to the stale set (inside the caller's transaction)."""
    new = {d for d in dates if d}
    if not new:
        return
    current = dirty_dates(conn)
    if new <= current:
        return
    _meta_set(conn, _DIRTY_KEY, json.dumps(sorted(current | new), separators=(",", ":")))


def _chunks(items: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _marks(n: int) -> str:
    return ",".join("?" * n)


_DAILY_SQL = f"""
SELECT r.date_utc, r.team, r.cost_center, r.workspace_id, r.workload_class,
  COALESCE(l.kind, 'unknown'), NULLIF(i.model, ''), i.billing_path, r.arm, r.wave,
  COUNT(DISTINCT r.principal), COUNT(DISTINCT r.request_id),
  SUM(i.uncached_input), SUM(i.cache_read),
  SUM(i.cache_write_5m + i.cache_write_1h + i.cache_write_other + i.cache_write_unknown),
  SUM(i.output),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis <> 'list_equivalent' THEN i.exact_nano
      ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis = 'list_equivalent'
      AND i.billing_path NOT IN {_COPILOT_PATHS!r} THEN i.priced_nano ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis <> 'list_equivalent' THEN i.est_low_nano
      ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis <> 'list_equivalent' THEN i.est_high_nano
      ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NULL THEN 1 ELSE 0 END),
  CASE WHEN COUNT(DISTINCT i.rate_card_sha) = 1 THEN MIN(i.rate_card_sha) END,
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis = 'list_equivalent'
      AND i.billing_path IN {_COPILOT_PATHS!r} THEN i.priced_nano ELSE 0 END)
FROM requests r JOIN inferences i ON i.request_id = r.request_id
LEFT JOIN lanes l ON l.lane_key = r.lane_key
WHERE i.billable IS NOT 0 AND r.date_utc IN ({{}})
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
"""

_REQUEST_CELLS_SQL = f"""
SELECT r.date_utc, r.team, r.workspace_id, r.attr_extra_json, r.arm, r.wave, r.principal,
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis <> 'list_equivalent' THEN i.exact_nano
      ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis = 'list_equivalent'
      AND i.billing_path NOT IN {_COPILOT_PATHS!r} THEN i.priced_nano ELSE 0 END),
  SUM(CASE WHEN i.priced_nano IS NOT NULL AND i.basis = 'list_equivalent'
      AND i.billing_path IN {_COPILOT_PATHS!r} THEN i.priced_nano ELSE 0 END)
FROM requests r JOIN inferences i ON i.request_id = r.request_id
WHERE i.billable IS NOT 0 AND r.date_utc IN ({{}})
GROUP BY r.request_id
"""


def daily_rows(conn: sqlite3.Connection, dates: Iterable[str]) -> list[tuple[Any, ...]]:
    """``daily_rollup`` rows (in column order, ``pool_nano`` last) for *dates*, computed from the
    ledger (read-only)."""
    out: list[tuple[Any, ...]] = []
    wanted = sorted(set(dates))
    for chunk in _chunks(wanted):
        for row in conn.execute(_DAILY_SQL.format(_marks(len(chunk))), tuple(chunk)):
            out.append(tuple(v if v is not None or k < 10 or k == 21 else 0
                             for k, v in enumerate(row)))
    return out


def cluster_rows(conn: sqlite3.Connection, dates: Iterable[str]) -> list[tuple[Any, ...]]:
    """``cluster_day`` rows ``(date, kind, cluster_id, arm, wave, active_users, requests,
    exact_nano, allowance_nano, pool_nano)`` for *dates*, computed from the ledger (read-only)."""
    cells: dict[tuple[str, str, str, str | None, str | None], list[Any]] = {}
    wanted = sorted(set(dates))
    for chunk in _chunks(wanted):
        for (date, team, workspace, extra_json, arm, wave, principal, exact_n, allow_n,
             pool_n) in conn.execute(_REQUEST_CELLS_SQL.format(_marks(len(chunk))),
                                     tuple(chunk)):
            extra = dict(json.loads(extra_json)) if extra_json else {}
            ids = {"team": team, "workspace": workspace, "mdm_group": extra.get("mdm_group"),
                   "gateway": extra.get("gateway")}
            for kind, cid in ids.items():
                if cid is None:
                    continue
                cell = cells.setdefault((date, kind, cid, arm, wave), [set(), 0, 0, 0, 0])
                if principal is not None:
                    cell[0].add(principal)
                cell[1] += 1
                cell[2] += exact_n or 0
                cell[3] += allow_n or 0
                cell[4] += pool_n or 0
    return [(d, k, c, a, w, len(v[0]), v[1], v[2], v[3], v[4])
            for (d, k, c, a, w), v in sorted(cells.items(), key=lambda kv: (
                kv[0][0], kv[0][1], kv[0][2], kv[0][3] or "", kv[0][4] or ""))]


def _retained_before(conn: sqlite3.Connection) -> str:
    return _meta_get(conn, _RETAINED_KEY) or ""


def _keep_users(conn: sqlite3.Connection, table: str, key_cols: Sequence[str],
                rows: list[tuple[Any, ...]], users_at: int, dates: Sequence[str]) -> None:
    """Rows of dates whose principals are retained keep their stored ``active_users``."""
    old: dict[tuple[Any, ...], int] = {}
    cols = ", ".join(key_cols)
    for chunk in _chunks(list(dates)):
        sql = f"SELECT {cols}, active_users FROM {table} WHERE date_utc IN ({_marks(len(chunk))})"
        for row in conn.execute(sql, tuple(chunk)):
            old[tuple(row[:-1])] = row[-1]
    for n, row in enumerate(rows):
        key = tuple(row[:len(key_cols)])
        if key in old and old[key] > row[users_at]:
            rows[n] = (*row[:users_at], old[key], *row[users_at + 1:])


_DAILY_KEY = ("date_utc", "team", "cost_center", "workspace_id", "workload_class", "lane_kind",
              "model", "billing_path", "arm", "wave")
_CLUSTER_KEY = ("date_utc", "cluster_kind", "cluster_id", "arm", "wave")
_DAILY_INSERT = ("INSERT INTO daily_rollup(date_utc, team, cost_center, workspace_id, "
                 "workload_class, lane_kind, model, billing_path, arm, wave, active_users, "
                 "requests, "
                 "uncached_input, cache_read, cache_write, output, exact_nano, allowance_nano, "
                 "est_low_nano, est_high_nano, unpriced_inferences, rate_card_sha, pool_nano) "
                 "VALUES (" + _marks(23) + ")")
_CLUSTER_INSERT = ("INSERT INTO cluster_day(date_utc, cluster_kind, cluster_id, arm, wave, "
                   "active_users, requests, exact_nano, allowance_nano, pool_nano) VALUES ("
                   + _marks(10) + ")")


def refresh_rollups(conn: sqlite3.Connection, dates: Iterable[str] | None = None) -> int:
    """Recompute ``daily_rollup`` and ``cluster_day`` for *dates* (default: the stale dates) inside
    one transaction; clears those dates from the stale set. Returns the number of dates refreshed.
    """
    stale = dirty_dates(conn)
    wanted = sorted(set(dates) if dates is not None else stale)
    for d in wanted:
        date_start_ms(d)  # validates the format
    if not wanted:
        return 0
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        daily = daily_rows(conn, wanted)
        clusters = cluster_rows(conn, wanted)
        retained = _retained_before(conn)
        old_dates = [d for d in wanted if d < retained]
        if old_dates:
            _keep_users(conn, "daily_rollup", _DAILY_KEY, daily, 10, old_dates)
            _keep_users(conn, "cluster_day", _CLUSTER_KEY, clusters, 5, old_dates)
        for chunk in _chunks(wanted):
            marks = _marks(len(chunk))
            conn.execute(f"DELETE FROM daily_rollup WHERE date_utc IN ({marks})", tuple(chunk))
            conn.execute(f"DELETE FROM cluster_day WHERE date_utc IN ({marks})", tuple(chunk))
        conn.executemany(_DAILY_INSERT, daily)
        conn.executemany(_CLUSTER_INSERT, clusters)
        left = stale - set(wanted)
        _meta_set(conn, _DIRTY_KEY, json.dumps(sorted(left), separators=(",", ":")))
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    return len(wanted)
