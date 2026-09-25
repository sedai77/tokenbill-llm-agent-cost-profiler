"""ccusage-compatible JSON export (SPEC §14.5, §14.7; moved from OUT to CLI-LEDGER, addendum §21.5).

:func:`to_ccusage` renders ``LedgerCostRow`` s (grouped at least by ``date`` and ``model``) in the
shape of ``ccusage daily --json`` / ``ccusage monthly --json``::

    {"daily": [{"date": "2026-09-22", "inputTokens": …, "outputTokens": …,
                "cacheCreationTokens": …, "cacheReadTokens": …, "totalTokens": …,
                "totalCost": 1.25, "modelsUsed": ["claude-opus-5-5"],
                "modelBreakdowns": [{"modelName": "claude-opus-5-5", "inputTokens": …,
                                     "outputTokens": …, "cacheCreationTokens": …,
                                     "cacheReadTokens": …, "cost": 1.25}]}],
     "totals": {"inputTokens": …, "outputTokens": …, "cacheCreationTokens": …,
                "cacheReadTokens": …, "totalTokens": …, "totalCost": 1.25}}

(``"monthly"`` / ``"month": "2026-09"`` for the monthly report). It is a **compatibility export**:

* costs are the **exact** ledger lines only (``priced_nano``; estimated ranges never enter), written
  as JSON numbers from exact decimal strings — never through a Python float;
* like ccusage itself, ``totalCost`` is the API-list-equivalent value of the usage: seat-allowance
  and Copilot-pool usage (basis ``list_equivalent``) is included at list rates, beside billed
  usage. It is not an invoice; use ``bill`` / FOCUS for billed dollars;
* token buckets map as ccusage does: uncached input → ``inputTokens``; every cache write (5m, 1h,
  other, unknown TTL) → ``cacheCreationTokens``; cache reads → ``cacheReadTokens``; output →
  ``outputTokens``. Per-request server-tool lines (web search, …) add cost, not tokens;
* entries are sorted by period then model; rows without a model are listed as ``"unknown"``.

The session report is not offered: on a fleet store it would list sessions (SPEC §14.7).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.money import nano_to_usd_str
from tokenbill.core.types import LedgerCostRow

__all__ = ["BUCKET_FIELDS", "REPORTS", "to_ccusage"]

#: The ccusage reports this export offers.
REPORTS = ("daily", "monthly")
#: ``PricedLine`` bucket → ccusage token field (buckets not listed carry cost only).
BUCKET_FIELDS: Mapping[str, str] = {
    "uncached_input": "inputTokens", "input": "inputTokens", "uncached": "inputTokens",
    "cache_read": "cacheReadTokens",
    "cache_write_5m": "cacheCreationTokens", "cache_write_1h": "cacheCreationTokens",
    "cache_write_other": "cacheCreationTokens", "cache_write_unknown": "cacheCreationTokens",
    "output": "outputTokens", "output_reasoning": "outputTokens",
}
_TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")


class _Number(str):
    """A decimal string emitted as a bare JSON number."""


@dataclass
class _Cell:
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_TOKEN_FIELDS, 0))
    nano: int = 0

    def add(self, row: LedgerCostRow) -> None:
        name = BUCKET_FIELDS.get(row.bucket)
        if name is not None:
            self.tokens[name] += row.quantity
        self.nano += row.priced_nano

    def merge(self, other: _Cell) -> None:
        for name in _TOKEN_FIELDS:
            self.tokens[name] += other.tokens[name]
        self.nano += other.nano

    def fields(self, cost_key: str) -> dict[str, object]:
        out: dict[str, object] = dict(self.tokens)
        if cost_key == "totalCost":
            out["totalTokens"] = sum(self.tokens.values())
        out[cost_key] = _Number(nano_to_usd_str(self.nano))
        return out


def _dump(value: object, indent: int = 0) -> str:
    pad = "  " * (indent + 1)
    end = "  " * indent
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ",\n".join(f"{pad}{json.dumps(k)}: {_dump(v, indent + 1)}"
                           for k, v in value.items())
        return "{\n" + items + "\n" + end + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[\n" + ",\n".join(pad + _dump(v, indent + 1) for v in value) + "\n" + end + "]"
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return json.dumps(value, ensure_ascii=False)
    raise ContractViolation(f"ccusage: cannot encode {type(value).__name__}")  # pragma: no cover


def to_ccusage(rows: Iterable[LedgerCostRow], *, report: str = "daily") -> str:
    """The ccusage ``daily`` or ``monthly`` JSON document of *rows* (see the module doc); rows need
    ``date_utc`` (``cost_rows(group_by=("date", "model"))``)."""
    if report not in REPORTS:
        raise UsageError("ccusage report must be daily or monthly")
    periods: dict[str, dict[str, _Cell]] = {}
    for row in rows:
        if not isinstance(row, LedgerCostRow):
            raise ContractViolation("to_ccusage expects LedgerCostRow items")
        if not row.date_utc:
            raise UsageError("ccusage export needs rows grouped by date")
        period = row.date_utc if report == "daily" else row.date_utc[:7]
        model = row.model or "unknown"
        periods.setdefault(period, {}).setdefault(model, _Cell()).add(row)
    key = "date" if report == "daily" else "month"
    entries = []
    totals = _Cell()
    for period in sorted(periods):
        models = periods[period]
        day = _Cell()
        breakdowns = []
        for model in sorted(models):
            cell = models[model]
            day.merge(cell)
            breakdowns.append({"modelName": model, **cell.fields("cost")})
        totals.merge(day)
        entries.append({key: period, **day.fields("totalCost"), "modelsUsed": sorted(models),
                        "modelBreakdowns": breakdowns})
    return _dump({report: entries, "totals": totals.fields("totalCost")}) + "\n"
