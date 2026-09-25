"""Retention, purge and key rotation (SPEC §7.5, §8.2).

* :func:`apply_retention` — identity retention (``principal = NULL`` in ``requests``,
  ``inferences``, ``cost_lines`` and the merge records, after the rollups of those dates are
  refreshed), then request, event and rollup retention; an audit row.
* :func:`purge` — delete a person's data (``principal=p_…``, own or adopted key id) or everything
  before a time, ``VACUUM``, and an audit row that never names the identity.
* :func:`rotate` — re-key the stored pseudonyms of one key role and record a ``key_events`` row.

Every run writes an audit row. Defaults (config ``retention.*``): identity 90 days, requests 395,
events 90, rollups 395. Cut-offs are aligned to whole UTC days, so a day is either fully retained or
not (rollups of a retained day keep their ``active_users``, see ``store.rollups``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.store import rollups
from tokenbill.store.merge import canonical

__all__ = ["DEFAULTS", "KEY_KINDS", "apply_retention", "purge", "rotate"]

#: Retention defaults in days (SPEC §7.5).
DEFAULTS: Mapping[str, int] = {"identity_days": 90, "request_days": 395, "event_days": 90,
                               "rollup_days": 395}
#: Key roles :func:`rotate` re-keys: the org (principal) key and the name key.
KEY_KINDS = ("org", "name")
_RETAINED_KEY = "identity_retained_before"
_HASHED_REQUEST_COLS = ("repo", "workspace_id", "api_key_id", "skill", "mcp_server", "plugin",
                        "cwd_key")


def _now_ms(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else time.time_ns() // 1_000_000


def _audit(conn: sqlite3.Connection, ts: int, actor: str, action: str,
           detail: Mapping[str, object]) -> None:
    conn.execute("INSERT INTO audit(ts_ms, actor, action, detail_json) VALUES (?, ?, ?, ?)",
                 (ts, actor, action, canonical(dict(detail))))


def _meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row is not None else ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (key, value))


def _cut_date(now_ms: int, days: int) -> str:
    if type(days) is not int or days < 0:
        raise UsageError("retention periods are non-negative whole days")
    return rollups.date_of(max(now_ms - days * rollups.DAY_MS, 0))


def _rewrite_docs(conn: sqlite3.Connection, where: str, params: Sequence[Any],
                  edit: Callable[[dict[str, Any]], bool]) -> int:
    """Rewrite the JSON of matching ``merge_members`` rows with *edit* (returns True when it
    changed the document); returns the number of rows changed."""
    rows = conn.execute(f"SELECT rowid, adapter, doc FROM merge_members WHERE {where}",
                        tuple(params)).fetchall()
    changed = []
    for rowid, adapter, doc in rows:
        data = json.loads(doc)
        if edit(data):
            principal = data.get("attribution", {}).get("principal")
            text = canonical(data)
            member_key = f"{adapter}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
            changed.append((text, principal, member_key, rowid))
    conn.executemany("UPDATE merge_members SET doc=?, principal=?, member_key=? WHERE rowid=?",
                     changed)
    return len(changed)


def _null_principal(data: dict[str, Any]) -> bool:
    attr = data.get("attribution", {})
    if attr.get("principal") is None:
        return False
    attr["principal"] = None
    return True


def apply_retention(conn: sqlite3.Connection, now_ms: int, *, identity_days: int = 90,
                    request_days: int = 395, event_days: int = 90, rollup_days: int = 395,
                    actor: str = "retention") -> dict[str, int]:
    """Apply the retention periods at *now_ms* in one transaction; returns row counts per step
    (``identities_nulled``, ``requests_deleted``, ``events_deleted``, ``rollups_deleted``,
    ``rollup_dates_refreshed``). Rollups of the dates whose principals are nulled are refreshed
    first."""
    from tokenbill.store import db  # the bulk delete helpers (db imports this module)

    identity_cut = _cut_date(now_ms, identity_days)
    request_cut = _cut_date(now_ms, request_days)
    event_cut_ms = rollups.date_start_ms(_cut_date(now_ms, event_days))
    rollup_cut = _cut_date(now_ms, rollup_days)
    counts: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        dates = {d for (d,) in conn.execute(
            "SELECT DISTINCT date_utc FROM requests WHERE date_utc < ? AND principal IS NOT NULL",
            (identity_cut,))}
        dates |= {d for d in rollups.dirty_dates(conn) if d < identity_cut}
        counts["rollup_dates_refreshed"] = rollups.refresh_rollups(conn, sorted(dates))
        nulled = conn.execute("UPDATE requests SET principal=NULL WHERE date_utc < ? AND "
                              "principal IS NOT NULL", (identity_cut,)).rowcount
        conn.execute("UPDATE inferences SET principal=NULL WHERE principal IS NOT NULL AND "
                     "request_id IN (SELECT request_id FROM requests WHERE date_utc < ?)",
                     (identity_cut,))
        nulled += conn.execute("UPDATE cost_lines SET principal=NULL WHERE date_utc < ? AND "
                               "principal IS NOT NULL", (identity_cut,)).rowcount
        nulled += _rewrite_docs(conn, "principal IS NOT NULL AND ts_start_ms < ?",
                                (rollups.date_start_ms(identity_cut),), _null_principal)
        counts["identities_nulled"] = nulled
        if identity_cut > _meta(conn, _RETAINED_KEY):
            _meta_set(conn, _RETAINED_KEY, identity_cut)
        counts["requests_deleted"] = db.delete_requests(conn, "date_utc < ?", (request_cut,))
        for table in ("aggregates", "cost_lines", "outcomes"):
            col = "bucket_start_ms" if table == "aggregates" else "date_utc"
            bound: Any = (rollups.date_start_ms(request_cut) if table == "aggregates"
                          else request_cut)
            conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (bound,))
        counts["events_deleted"] = conn.execute("DELETE FROM events WHERE ts_ms < ?",
                                                (event_cut_ms,)).rowcount
        deleted = conn.execute("DELETE FROM daily_rollup WHERE date_utc < ?",
                               (rollup_cut,)).rowcount
        deleted += conn.execute("DELETE FROM cluster_day WHERE date_utc < ?",
                                (rollup_cut,)).rowcount
        counts["rollups_deleted"] = deleted
        _audit(conn, now_ms, actor, "retention", {
            **counts, "identity_before": identity_cut, "requests_before": request_cut,
            "rollups_before": rollup_cut})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def purge(conn: sqlite3.Connection, *, principal: str | None = None,
          before_ms: int | None = None, actor: str, now_ms: int | None = None,
          regroup: Callable[[sqlite3.Connection, Sequence[str], Callable[[Any], bool]], int]
          | None = None) -> int:
    """Delete every request of *principal* (merged requests attributed to it, and its
    contributions to requests attributed to others, which are re-merged without them; its cost
    lines) or everything before *before_ms* (requests, events, provider records, rollups); then
    ``VACUUM`` and an audit row without the identity. Returns the number of merged requests
    deleted. *regroup* re-merges partially affected requests (``SqliteStore`` passes its own;
    without it those requests are deleted whole)."""
    from tokenbill.store import db

    if principal is None and before_ms is None:
        raise UsageError("purge needs principal or before_ms")
    if principal is not None and not isinstance(principal, str):
        raise UsageError("purge principal must be a p_ pseudonym")
    if before_ms is not None and (type(before_ms) is not int or before_ms < 0):
        raise UsageError("purge before_ms must be a non-negative int")
    if not isinstance(actor, str) or not actor:
        raise UsageError("purge needs an actor")
    ts = _now_ms(now_ms)
    conn.execute("BEGIN IMMEDIATE")
    try:
        deleted = 0
        if principal is not None:
            deleted += db.delete_requests(conn, "principal = ?", (principal,))
            partial = [g for (g,) in conn.execute(
                "SELECT DISTINCT request_id FROM merge_members WHERE principal = ?",
                (principal,))]
            if partial and regroup is not None:
                regroup(conn, partial, lambda m: m.request.attribution.principal == principal)
            elif partial:
                deleted += _delete_ids(conn, partial)
            conn.execute("DELETE FROM cost_lines WHERE principal = ?", (principal,))
        if before_ms is not None:
            deleted += db.delete_requests(conn, "ts_start_ms < ?", (before_ms,))
            partial = [g for (g,) in conn.execute(
                "SELECT DISTINCT request_id FROM merge_members WHERE ts_start_ms < ?",
                (before_ms,))]
            if partial and regroup is not None:
                regroup(conn, partial, lambda m: m.request.ts_start_ms < before_ms)
            elif partial:
                deleted += _delete_ids(conn, partial)
            cut = rollups.date_of(min(before_ms, rollups.MAX_MS))
            conn.execute("DELETE FROM events WHERE ts_ms < ?", (before_ms,))
            conn.execute("DELETE FROM aggregates WHERE bucket_end_ms <= ?", (before_ms,))
            conn.execute("DELETE FROM cost_lines WHERE date_utc < ?", (cut,))
            conn.execute("DELETE FROM outcomes WHERE date_utc < ?", (cut,))
            conn.execute("DELETE FROM daily_rollup WHERE date_utc < ?", (cut,))
            conn.execute("DELETE FROM cluster_day WHERE date_utc < ?", (cut,))
        rollups.refresh_rollups(conn)
        detail: dict[str, object] = {"by": "principal" if principal is not None else "before_ms",
                                     "rows": deleted}
        if before_ms is not None:
            detail["before_ms"] = before_ms
        _audit(conn, ts, actor, "purge", detail)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("VACUUM")
    return deleted


def _delete_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> int:
    from tokenbill.store import db

    total = 0
    for i in range(0, len(ids), 400):
        chunk = list(ids[i:i + 400])
        total += db.delete_requests(conn, f"request_id IN ({','.join('?' * len(chunk))})",
                                    chunk)
    return total


def _rekey_value(new: bytes, prefix: str) -> Callable[[Any], Any]:
    def fn(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(prefix + "_"):
            return pseudonym(new, prefix, value)
        return value
    return fn


def rotate(conn: sqlite3.Connection, old: bytes, new: bytes, kind: str, *,
           actor: str = "rotate", now_ms: int | None = None) -> int:
    """Re-key the stored pseudonyms of key role *kind* (``org``: every ``p_`` principal; ``name``:
    every ``h_`` name) from *old* to *new* and record a ``key_events`` row (plus ``meta`` and an
    audit row). A stored value *v* becomes ``pseudonym(new, prefix, v)``: equal values stay equal
    (joins inside the ledger survive), the old key no longer relates to stored values, and data
    ingested under the old key later will not join (rotation breaks linkability on purpose).
    *old* must be the key the ledger records for that role. A ledger holding pseudonyms under an
    adopted Copilot export key id (R-E21) refuses: rows of the two key spaces cannot be told
    apart. Returns the number of values re-keyed."""
    if kind not in KEY_KINDS:
        raise UsageError(f"unknown key kind {kind!r} (org | name)")
    if not old or not new or old == new:
        raise UsageError("rotate needs two different non-empty keys")
    meta_key = "org_key_id" if kind == "org" else "name_key_id"
    ts = _now_ms(now_ms)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _meta(conn, meta_key) != key_id(old):
            raise UsageError("the old key is not the ledger's current key for this role")
        if _meta(conn, "adopted_key_id") or _meta(conn, "adopted_name_key_id"):
            raise UsageError("this ledger holds pseudonyms under an adopted export key id; "
                             "rotation would mix key spaces")
        prefix = "p" if kind == "org" else "h"
        fn = _rekey_value(new, prefix)
        conn.create_function("tb_rekey", 1, fn, deterministic=True)
        like = prefix + r"\_%"
        n = 0
        if kind == "org":
            for table in ("requests", "inferences", "cost_lines"):
                n += conn.execute(f"UPDATE {table} SET principal=tb_rekey(principal) WHERE "
                                  "principal LIKE ? ESCAPE '\\'", (like,)).rowcount

            def edit(data: dict[str, Any]) -> bool:
                attr = data.get("attribution", {})
                if isinstance(attr.get("principal"), str) and attr["principal"].startswith("p_"):
                    attr["principal"] = fn(attr["principal"])
                    return True
                return False
        else:
            for col in _HASHED_REQUEST_COLS:
                n += conn.execute(f"UPDATE requests SET {col}=tb_rekey({col}) WHERE {col} LIKE ? "
                                  "ESCAPE '\\'", (like,)).rowcount
            n += conn.execute("UPDATE inferences SET workspace_id=tb_rekey(workspace_id) WHERE "
                              "workspace_id LIKE ? ESCAPE '\\'", (like,)).rowcount
            for col in ("workspace_id", "repo", "workflow"):
                n += conn.execute(f"UPDATE cost_lines SET {col}=tb_rekey({col}) WHERE {col} LIKE "
                                  "? ESCAPE '\\'", (like,)).rowcount
            n += _rekey_json_column(conn, "requests", "request_id", "attr_extra_json", fn,
                                    pairs=True)
            n += _rekey_json_column(conn, "requests", "request_id", "appended_json", fn,
                                    pairs=False)
            n += _rekey_json_column(conn, "aggregates", "agg_id", "dims_json", fn, pairs=True)

            def edit(data: dict[str, Any]) -> bool:
                changed = False
                attr = data.get("attribution", {})
                for col in _HASHED_REQUEST_COLS:
                    if isinstance(attr.get(col), str) and attr[col].startswith("h_"):
                        attr[col] = fn(attr[col])
                        changed = True
                extra = attr.get("extra") or []
                for pair in extra:
                    if isinstance(pair[1], str) and pair[1].startswith("h_"):
                        pair[1] = fn(pair[1])
                        changed = True
                for item in data.get("appended") or []:
                    if isinstance(item.get("name"), str) and item["name"].startswith("h_"):
                        item["name"] = fn(item["name"])
                        changed = True
                return changed
        _rewrite_docs(conn, "1=1", (), edit)
        conn.execute("INSERT INTO key_events(ts_ms, key_kind, old_key_id, new_key_id) VALUES "
                     "(?, ?, ?, ?)", (ts, kind, key_id(old), key_id(new)))
        _meta_set(conn, meta_key, key_id(new))
        _audit(conn, ts, actor, "rotate", {"kind": kind, "old_key_id": key_id(old),
                                           "new_key_id": key_id(new), "values": n})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def _rekey_json_column(conn: sqlite3.Connection, table: str, key: str, col: str,
                       fn: Callable[[Any], Any], *, pairs: bool) -> int:
    rows = conn.execute(f"SELECT {key}, {col} FROM {table} WHERE {col} LIKE '%\"h\\_%' "
                        "ESCAPE '\\'").fetchall()
    updates = []
    for rid, text in rows:
        data = json.loads(text)
        if pairs:
            new = [[k, fn(v)] for k, v in data]
        else:
            new = [dict(item, name=fn(item.get("name"))) for item in data]
        if new != data:
            updates.append((canonical(new), rid))
    conn.executemany(f"UPDATE {table} SET {col}=? WHERE {key}=?", updates)
    return len(updates)
