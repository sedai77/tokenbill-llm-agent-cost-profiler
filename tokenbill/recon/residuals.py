"""Residual classifier of the reconciliation ledger gate (SPEC §12.3).

The gap of a reconciliation row is ``invoice − ledger`` (int nano; a missing side counts 0). Rows
are classified **in the SPEC order**, each code claiming a part of the gap:

1. ``priority_excluded_from_cost_report`` — Priority Tier usage (usage-report ``service_tier``
   ``priority`` / ``priority_on_demand``) that the Admin cost report leaves out; the row is fully
   explained and the code records the usage priced by our rate card (a magnitude).
2. ``code_execution_cost_report_only`` — code execution and session-runtime lines (cost report
   only).
3. ``revision_window`` — rows dated inside the 30-day revision window (provisional).
4. ``seat_allowance_unmetered`` — list-equivalent ledger dollars on the subscription billing path
   (never invoiced, D26); a magnitude, reported beside the gap (the ledger side excludes it).
5. ``unobserved_traffic`` — provider tokens the ledger never saw (uninstrumented apps): the whole
   gap of a model-day with no ledger tokens, else the price of the missing tokens.
6. ``implied_discount`` — ``invoice − our price of the provider's own tokens`` when it exceeds a
   cent per invoice row (smaller differences are the source's rounding).
7. ``estimated_components`` — up to the ledger's estimated (range) parts.
8. ``ccu_single_line`` / 10. ``no_reporting_api`` — channels billed as one capacity line / without
   a reporting API.
9. ``default_workspace_null_id`` — model-days joined through the default workspace's null id.
11. ``cents_rounding`` — up to Σ source parse remainders (``cents_to_nano`` / ``usd_str_to_nano``).
12. ``unmapped_cost_type`` — invoice lines no rule maps (incl. disabled VERIFY SKU rules).
13. ``cloud_credits`` — credits and promotions (GCP ``credits[]``, CUR credit line items).

What no code claims is **unexplained**. Codes 5–10 work on *model-day groups* (the rows of one
``(channel, date, workspace, model)``: a token deficit or a discount is a property of the model-day,
not of one bucket); the rest on single rows. Signed claims satisfy ``gap = Σ claims + unexplained``
per group; codes 1 and 4 are informational magnitudes. Money is int nano; no floats (SPEC §2.4).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from tokenbill.core.types import ReconRow
from tokenbill.recon.costmap import (
    CCU_CHANNELS,
    NO_REPORTING_API_CHANNELS,
    PRIORITY_TIERS,
    REPORT_ONLY_BUCKETS,
)

__all__ = [
    "RESIDUAL_CODES",
    "Classification",
    "classify",
    "classify_rows",
    "key_value",
    "residual_order",
]

#: Residual codes in the SPEC §12.3 order (``unexplained`` is reported separately).
RESIDUAL_CODES = (
    "priority_excluded_from_cost_report",
    "code_execution_cost_report_only",
    "revision_window",
    "seat_allowance_unmetered",
    "unobserved_traffic",
    "implied_discount",
    "estimated_components",
    "ccu_single_line",
    "default_workspace_null_id",
    "no_reporting_api",
    "cents_rounding",
    "unmapped_cost_type",
    "cloud_credits",
)
_ORDER = {code: i for i, code in enumerate(RESIDUAL_CODES)}
#: Row-key marker of rows whose ledger side joined the provider's default workspace (null id).
DEFAULT_JOIN = ("join", "default_workspace")
#: Row-key dim of informational rows (never classified, never in coverage totals).
INFO_DIM = "info"
#: One cent in nano: a price difference within a cent per invoice row is the source's rounding,
#: not a discount (it is left to ``cents_rounding`` / unexplained).
CENT_NANO = 10_000_000


def residual_order(code: str) -> tuple[int, str]:
    """Sort key of a residual code: the SPEC order, then unknown codes (e.g. a channel extension's)
    alphabetically."""
    return (_ORDER.get(code, len(_ORDER)), code)


def key_value(key: Iterable[tuple[str, str]], dim: str) -> str | None:
    """The value of *dim* in a row key (the first pair with that name), or None."""
    for name, value in key:
        if name == dim:
            return value
    return None


def _claim(remaining: int, cap: int) -> int:
    """The part of *remaining* a component bounded by ``|cap|`` explains (same sign as
    *remaining*)."""
    cap = abs(cap)
    if remaining > 0:
        return min(remaining, cap)
    if remaining < 0:
        return -min(-remaining, cap)
    return 0


def _share(amount: int, num: int, den: int) -> int:
    """``amount × num / den`` rounded half-even (``den > 0``)."""
    q, r = divmod(amount * num, den)
    if 2 * r > den or (2 * r == den and q % 2 == 1):
        q += 1
    return q


@dataclass
class Classification:
    """The detailed result of :func:`classify_rows`.

    ``codes``: residual code → nano (codes that classified at least one row); ``unexplained``: the
    total after ``cents_rounding``; ``row_codes`` / ``row_status``: per row key, the dominant code
    of its group and ``provisional`` | ``unexplained`` | ``explained`` | ``match``;
    ``row_unexplained``: a group's unexplained nano, attributed to its first row (the
    per-workspace-month gate sums it)."""

    codes: dict[str, int] = field(default_factory=dict)
    unexplained: int = 0
    row_codes: dict[tuple, str | None] = field(default_factory=dict)
    row_status: dict[tuple, str] = field(default_factory=dict)
    row_unexplained: dict[tuple, int] = field(default_factory=dict)

    def add(self, code: str, nano: int) -> None:
        """Record *nano* under *code* (a code is listed even when it claims 0)."""
        self.codes[code] = self.codes.get(code, 0) + nano

    def residuals(self) -> tuple[tuple[str, int], ...]:
        """The codes in SPEC order."""
        return tuple(sorted(self.codes.items(), key=lambda kv: residual_order(kv[0])))


def _group_key(key: tuple) -> tuple:
    return tuple(pair for pair in key if pair[0] != "bucket")


def _dominant(claims: Mapping[str, int]) -> str | None:
    if not claims:
        return None
    return min(claims, key=lambda c: (-abs(claims[c]), residual_order(c)))


def _group_claims(rows: Sequence[ReconRow], estimated: int, channel: str | None
                  ) -> tuple[dict[str, int], int]:
    """Codes 5–10 on one closed model-day group: ``(claims, unexplained)``."""
    inv = sum(r.invoice_nano or 0 for r in rows)
    led = sum(r.ledger_nano or 0 for r in rows)
    has_inv = any(r.invoice_nano is not None for r in rows)
    lt = sum(r.ledger_tokens or 0 for r in rows)
    pt = sum(r.provider_tokens or 0 for r in rows)
    priced = [r.priced_provider_nano for r in rows if r.provider_tokens]
    total_mode = any(key_value(r.key, "bucket") == "total" for r in rows)
    p = None if any(v is None for v in priced) else sum(
        r.priced_provider_nano or 0 for r in rows)
    remaining = inv - led
    claims: dict[str, int] = {}
    if lt == 0 and led == 0 and (pt > 0 or has_inv):
        claims["unobserved_traffic"] = remaining
        return claims, 0
    if not total_mode and p is not None and has_inv and p > 0:
        coverage = p - led
        if pt > lt and coverage > 0:
            claims["unobserved_traffic"] = min(coverage, _share(p, pt - lt, pt))
        remaining = coverage - claims.get("unobserved_traffic", 0)
        price = inv - p
        invoiced_rows = sum(1 for r in rows if r.invoice_nano is not None)
        if abs(price) > CENT_NANO * invoiced_rows:  # beyond the source's cent rounding
            claims["implied_discount"] = price
        else:
            remaining += price
    elif pt > lt and remaining > 0 and pt > 0:
        claims["unobserved_traffic"] = min(remaining, _share(inv, pt - lt, pt))
        remaining -= claims["unobserved_traffic"]
    if estimated:
        claims["estimated_components"] = _claim(remaining, estimated)
        remaining -= claims["estimated_components"]
    if channel in CCU_CHANNELS and has_inv:
        claims["ccu_single_line"] = remaining
        remaining = 0
    if any(DEFAULT_JOIN in r.key for r in rows):
        claims["default_workspace_null_id"] = remaining
        remaining = 0
    if channel in NO_REPORTING_API_CHANNELS and not has_inv:
        claims["no_reporting_api"] = remaining
        remaining = 0
    return claims, remaining


def classify_rows(rows: Sequence[ReconRow], *, ledger_estimated_nano: Mapping[tuple, int],
                  allowance_nano: Mapping[tuple, int], provisional_dates: frozenset[str],
                  remainders_nano: int) -> Classification:
    """Classify *rows* per the SPEC §12.3 order (see the module doc) with every per-row detail.

    Rows whose key has an ``info`` dim are skipped. ``ledger_estimated_nano`` / ``allowance_nano``
    are keyed by row key; ``remainders_nano`` is Σ source parse remainders of the rows' invoice
    sources (it explains up to its magnitude of the total unexplained)."""
    out = Classification()
    groups: dict[tuple, list[ReconRow]] = {}
    for row in sorted(rows, key=lambda r: r.key):
        key = row.key
        if key_value(key, INFO_DIM) is not None:
            continue
        allowance = allowance_nano.get(key, 0)
        if allowance:
            out.add("seat_allowance_unmetered", allowance)
        bucket = key_value(key, "bucket")
        date = key_value(key, "date")
        provisional = date in provisional_dates
        gap = (row.invoice_nano or 0) - (row.ledger_nano or 0)
        code: str | None = None
        if key_value(key, "service_tier") in PRIORITY_TIERS and row.invoice_nano is None:
            code = "priority_excluded_from_cost_report"
            amount = row.priced_provider_nano
            out.add(code, amount if amount is not None else (row.ledger_nano or 0))
        elif bucket in REPORT_ONLY_BUCKETS:
            code = "code_execution_cost_report_only"
            out.add(code, gap)
        elif bucket in ("unmapped", "credits"):
            code = "revision_window" if provisional else (
                "unmapped_cost_type" if bucket == "unmapped" else "cloud_credits")
            out.add(code, gap)
        if code is not None:
            out.row_codes[key] = code
            out.row_status[key] = "provisional" if provisional else "explained"
            out.row_unexplained[key] = 0
            continue
        groups.setdefault(_group_key(key), []).append(row)
    unexplained = 0
    for gkey in sorted(groups):
        members = groups[gkey]
        date = key_value(gkey, "date")
        gap = sum((r.invoice_nano or 0) - (r.ledger_nano or 0) for r in members)
        if date in provisional_dates:
            out.add("revision_window", gap)
            code, status, rest = "revision_window", "provisional", 0
        else:
            estimated = sum(ledger_estimated_nano.get(r.key, 0) for r in members)
            claims, rest = _group_claims(members, estimated, key_value(gkey, "channel"))
            for c, nano in claims.items():
                out.add(c, nano)
            code = _dominant({c: n for c, n in claims.items() if n})
            if code is None and claims:
                code = _dominant(claims)
            if rest:
                status = "unexplained"
            elif any(claims.values()):
                status = "explained"
            else:
                status = "match"
            unexplained += rest
        for i, r in enumerate(members):
            allowance = allowance_nano.get(r.key, 0)
            out.row_codes[r.key] = code if code is not None else (
                "seat_allowance_unmetered" if allowance else None)
            out.row_status[r.key] = status if status != "match" or not allowance else "explained"
            out.row_unexplained[r.key] = rest if i == 0 else 0
    if remainders_nano:
        claim = _claim(unexplained, remainders_nano)
        out.add("cents_rounding", claim)
        unexplained -= claim
    out.unexplained = unexplained
    return out


def classify(rows: Sequence[ReconRow], *, ledger_estimated_nano: Mapping[tuple, int],
             allowance_nano: Mapping[tuple, int], provisional_dates: frozenset[str],
             remainders_nano: int) -> tuple[tuple[tuple[str, int], ...], int]:
    """``(residual code → nano, unexplained nano)`` of *rows*, applying SPEC §12.3 in order (SPEC
    §12 signature; see :func:`classify_rows` for the per-row detail)."""
    result = classify_rows(rows, ledger_estimated_nano=ledger_estimated_nano,
                           allowance_nano=allowance_nano, provisional_dates=provisional_dates,
                           remainders_nano=remainders_nano)
    return result.residuals(), result.unexplained
