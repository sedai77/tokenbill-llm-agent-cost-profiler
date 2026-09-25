"""Area-local helpers of the CP-STORE tests: a SPEC §7.1 ``meta`` stand-in for the not-yet-merged
``SqliteStore``, ingest-result builders and the Appendix C worlds (C.P9, C.P13, C.P14) built with
``core.builders``. Every record is synthetic."""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

from tokenbill.core import builders as b
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
)
from tokenbill.core.types import IngestResult, SourceInfo

#: nano-USD per AI credit ($0.01) and per USD.
C = 10**7
USD = 10**9
DAY_MS = 86_400_000
FOREVER = 2**53
W = {"since_ms": 0, "until_ms": FOREVER}
ORG_KEY = bytes(range(40, 72))
KEY_A = bytes(range(3, 35))
KEY_B = bytes(range(7, 39))

#: SPEC §7.1 DDL of the two STORE tables the record store touches (``meta`` read, ``audit``
#: written) — the documented contract, used here as a stand-in until STORE is merged.
META_DDL = "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
AUDIT_DDL = ("CREATE TABLE IF NOT EXISTS audit(ts_ms INTEGER NOT NULL, actor TEXT, "
             "action TEXT NOT NULL, detail_json TEXT)")


def ledger_meta(path: Path, *, org_key_id: str = "", adopted_key_id: str = "",
                audit: bool = False) -> None:
    """Write the ledger's SPEC §7.1 ``meta`` rows (and optionally the ``audit`` table) into the
    database file, as ``SqliteStore`` does (R-E21 keys ``org_key_id`` / ``adopted_key_id``)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(META_DDL)
        if audit:
            conn.execute(AUDIT_DDL)
        rows = {"schema_version": "stand-in", "org_key_id": org_key_id,
                "adopted_key_id": adopted_key_id,
                "org_key_mode": ("own" if org_key_id and org_key_id != adopted_key_id
                                 else "adopted" if adopted_key_id else "none")}
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         sorted(rows.items()))
        conn.commit()
    finally:
        conn.close()


def ms(date: str, hour: int = 0) -> int:
    """Epoch milliseconds of a UTC date (plus *hour* hours)."""
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * DAY_MS + hour * 3_600_000


def month_window(month: str) -> tuple[int, int]:
    """``[first day, first day of the next month)`` of ``YYYY-MM`` in ms."""
    first = dt.date.fromisoformat(f"{month}-01")
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return ms(first.isoformat()), ms(nxt.isoformat())


def p(key: bytes, who: str) -> str:
    """The ``p_`` pseudonym of *who* under *key*."""
    return pseudonym(key, "p", who)


def people(n: int, seed: str = "u") -> list[str]:
    return [b.make_principal(f"{seed}{i}") for i in range(n)]


def source(source_id: str = "src", *, adapter: str = "github-copilot-seats",
           key: bytes | None = ORG_KEY) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter=adapter,
                      name_hmac="h_" + hashlib.sha256(source_id.encode()).hexdigest()[:20],
                      sha256=hashlib.sha256(source_id.encode()).hexdigest(), bytes=1,
                      name_key_id=None, principal_key_id=key_id(key) if key else None)


def result(licenses: Iterable[LicenseSnapshot] = (), activity: Iterable[ActivityDay] = (),
           config: Iterable[ConfigSnapshot] = (), *, cost_lines: Iterable[CostLine] = (),
           aggregates: Iterable[UsageAggregate] = (), source_id: str = "src",
           adapter: str = "github-copilot-seats", key: bytes | None = ORG_KEY) -> IngestResult:
    """An ingest result carrying the given records under *key*'s principal key id."""
    r = IngestResult(source=source(source_id, adapter=adapter, key=key), requests=[],
                     sessions=[], events=[], aggregates=list(aggregates),
                     cost_lines=list(cost_lines), outcomes=[], quarantined=[], notes=[],
                     stats={}, capabilities=frozenset())
    r.licenses, r.activity, r.config = list(licenses), list(activity), list(config)
    return r


# ---------------------------------------------------------------------------------------------
# report rows and Appendix C worlds
# ---------------------------------------------------------------------------------------------


def rows(credits: int, *, date: str, users: Sequence[str] | None = None, discount: int = 0,
         model: str = "Claude Sonnet 5", org: str | None = "org-a", team: str | None = None,
         cost_center: str | None = None, finality: str = "final", unattributed: bool = False,
         input_tokens: int = 0, cache_read: int = 0, cache_write: int = 0,
         fetched_ms: int = 0) -> tuple[list[CostLine], list[UsageAggregate]]:
    """AI usage report rows totalling *credits* (and *discount*) over *users* (the first takes the
    remainder); unattributed rows have no principal."""
    who: list[str | None] = [None] if unattributed else list(users or people(1))
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    n = len(who)
    for i, principal in enumerate(who):
        share = credits // n + (credits % n if i == 0 else 0)
        disc = discount // n + (discount % n if i == 0 else 0)
        line, agg = b.make_ai_usage_row(
            date_utc=date, model=model, credits=str(share), discount_credits=str(disc),
            principal=principal, unattributed=unattributed, organization=org, team=team,
            cost_center=cost_center, finality=finality, input_tokens=input_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            output_tokens=input_tokens // 10, fetched_ms=fetched_ms)
        lines.append(line)
        aggs.append(agg)
    return lines, aggs


def coverage(source_id: str, date: str, *, fetched_ms: int = 0) -> UsageAggregate:
    """The report's per file × day coverage aggregate (dims ``channel``, ``source``)."""
    return b.make_aggregate(None, source_kind="github.ai_usage_report.coverage",
                            agg_id=f"cov-{source_id}-{date}", bucket_start_ms=ms(date),
                            bucket_end_ms=ms(date) + DAY_MS,
                            dims={"channel": "github_copilot", "source": source_id},
                            finality="final", fetched_ms=fetched_ms)


def p9_series() -> dict[str, int]:
    """The binding C.P9 series (credits per day, 2026-09-01 … 20; 2,000,000 credits): business
    days 1–4 at 130,000, the last 10 business days 110k … 150k, the weekend Sep 5–6 at 30,000 and
    the last 4 weekend days 20k / 30k / 30k / 40k."""
    series = {f"2026-09-{d:02d}": 130_000 for d in (1, 2, 3, 4)}
    last10 = [110_000, 110_000, 120_000, 120_000, 130_000, 130_000, 140_000, 140_000, 150_000,
              150_000]
    for d, v in zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18), last10, strict=True):
        series[f"2026-09-{d:02d}"] = v
    series.update({"2026-09-05": 30_000, "2026-09-06": 30_000, "2026-09-12": 20_000,
                   "2026-09-13": 30_000, "2026-09-19": 30_000, "2026-09-20": 40_000})
    return series


def p9_ledger_rows() -> tuple[list[CostLine], list[UsageAggregate]]:
    """C.P9: the binding series plus two provisional days (21–22) and the C.P1 seat lines (1,000
    Business + 200 Enterprise, pool 2,680,000 credits)."""
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    series = p9_series() | {"2026-09-21": 100_000, "2026-09-22": 100_000}
    for date, credits in sorted(series.items()):
        ls, ag = rows(credits, date=date, finality="provisional" if date > "2026-09-20"
                      else "final")
        lines += ls
        aggs += ag
    lines += [b.make_seat_line("business", "1000"), b.make_seat_line("enterprise", "200")]
    return lines, aggs


def activity_seats(n: int, *, date: str = "2026-10-15", org: str | None = "org-a",
                   seed: str = "u") -> list[LicenseSnapshot]:
    """*n* activity-report seat holders (plan and assignment unknown)."""
    return [b.make_license(x, snapshot_date=date, plan="unknown", org=org,
                           assigned_via_team=None, source_kind="github.copilot_activity_report")
            for x in people(n, seed)]


def api_seats(n: int, plan: str, *, date: str = "2026-09-05", org: str | None = "org-a",
              seed: str = "u") -> list[LicenseSnapshot]:
    """*n* seats-API seats of *plan*."""
    return [b.make_license(x, snapshot_date=date, plan=plan, org=org) for x in people(n, seed)]


def flags(pairs: dict[str, str | int | bool | None], *, entity: str = "run",
          snapshot_ms: int = 0) -> ConfigSnapshot:
    """A ``run_flags`` snapshot of the CLI (entity ``run``) or of the admin answers."""
    kind = "tokenbill.admin_answers" if entity == "admin_answers" else "tokenbill.cli"
    return b.make_config("run_flags", pairs, entity_id=entity, source_kind=kind,
                         snapshot_ms=snapshot_ms)
