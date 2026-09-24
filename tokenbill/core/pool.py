"""GitHub Copilot pool ledger and plan detection (addendum §3.9 CA-40 / CA-48, CORE-AMENDMENTS P-1).

The single implementation of the Copilot pool rule (addendum R11, DC3) and of plan detection (R17,
ruling R-E22). Pure functions: no I/O, no floats, money in integer nano-USD, ``Decimal`` only under
:data:`tokenbill.core.money.EXACT_CTX`; every output is deterministic and independent of input
order.

**Cells.** :func:`build_cells` joins AI usage report cost lines and token aggregates on (date, team,
cost center, org, SKU, model, routing, speed, pseudo) — the CA-4 ``CostLine`` fields and the CA-5
aggregate dims, never ``description`` — and splits them by cost type (``ai_credit.user`` = pooled,
``ai_credit.direct`` = metered to the organization, ``ai_credit.legacy_pru``). An aggregate whose
key carries several cost types has its tokens split in proportion to the cost types' gross amounts
(all rows of one key share model, date and routing, hence rates); a key with tokens but no cost line
becomes a zero-money cell of cost type ``other``.

**Entities.** ``enterprise`` (entity mode ``enterprise``) or ``org:<login>`` (mode ``org``); a
capped cost center (``capped_cost_centers``) is its own entity ``cc:<name>`` whose pool is its cap.
The parent entity keeps the other seats and usage (partition: its ``pool_credits`` are its own
seats' allowance, minus the caps of cost centers whose seats its seat source does not attribute to
them, so Σ pools is the one pool GitHub forms); :func:`overage_total` applies the binding
shared-pool formula. In enterprise mode the enterprise's regime, scenario overage and overage
forecast also count the caps' unused part at each consumption level (a cost center under its cap
leaves the rest to everyone), consistent with :func:`overage_total`.

**Plans (R17).** :func:`detect_plans` counts seats per entity × month (:func:`seat_months`
precedence) and places them on plans from the first evidence source in the order seat SKU lines >
seats API ``plan_type`` > org billing ``plan_type`` > report quota (``plan_quota`` rows) > admin
statement (``plan.<entity>``, per-org ``plan.org:<o>`` applied to each org's seats, or stated
``pool_seats``). Only that source places seats; seats it cannot place stay ``unknown``. Another
source *conflicts* (``dq.copilot_plan_conflict``) when one of the two speaks for every seat of the
entity and the other claims a plan outside it, or when no assignment of the seats satisfies both
claims' counts. While any seat is unknown :func:`pool_months` emits one
``PoolMonth`` per scenario (``business`` / ``enterprise``; unknown seats counted under the
scenario's plan, the ``unknown`` key kept in ``seats``) and never a merged figure. In a scenario
``overage_observed_nano`` is the scenario's overage ``max(0, consumed − pool)``; with a known plan
it is Σ net of the pooled rows (data).

Additive keyword arguments beyond the addendum signatures (defaults keep the documented calls):
``seat_months(…, *, entity_mode)``, ``pool_months(…, *, entity_mode=None)`` (None: inferred from the
cells' and plans' entity ids) and ``gross_is_list`` also accepting a mapping
``"<entity>:<YYYY-MM>"`` → ``True`` / ``False`` / ``None`` (or the CP-RECON decision strings
``true`` / ``false`` / ``unknown``), or CP-RECON's decision pairs as
``AnalysisContext.recon_decisions`` carries them. :func:`forecast` returns ``None`` when no day of
the month is observed yet. Every decimal read from records or callers is bounded (at most 10**15
credits or seats per value), so exact sums never overflow ``EXACT_CTX`` and only ``TokenbillError``
escapes.
"""

from __future__ import annotations

import datetime as _dt
import re
import types
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from tokenbill.core import facts as _facts
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality, unpriced
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, fmt_usd, nano_to_credits_str
from tokenbill.core.records import (
    GITHUB_COST_TYPES,
    LICENSE_PLANS,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
    UsageBuckets,
    record_key,
)
from tokenbill.core.types import PlanEvidence, PoolMonth

__all__ = [
    "CELL_COST_TYPES", "CONVENTIONS", "DIRECT_COST_TYPES", "ENTITY_MODES", "GRAINS",
    "PLAN_CONFLICT_DQ", "POOLED_COST_TYPES", "SCENARIOS", "SEATS_LOWER_BOUND_NOTE", "SEAT_SOURCES",
    "Cell", "billing_modes", "build_cells", "capped_cost_centers", "capped_policies",
    "classify_discounts", "detect_plans", "direct_draws_pool", "entity_of", "forecast",
    "invoice_delta", "overage_total", "pool_credits", "pool_months", "realize_credit_saving",
    "realize_seat_change", "regime", "run_flags", "seat_months",
]

#: Cost types whose credits draw on the pool (AI usage report rows with a username).
POOLED_COST_TYPES = ("ai_credit.user",)
#: Cost types metered straight to the organization (rows without a username).
DIRECT_COST_TYPES = ("ai_credit.direct",)
#: Cost types that become cells (legacy premium requests are cells but neither pooled nor direct).
CELL_COST_TYPES = ("ai_credit.user", "ai_credit.direct", "ai_credit.legacy_pru")
#: Pool entity modes: one enterprise pool, or one pool per organization.
ENTITY_MODES = ("enterprise", "org")
#: Report token conventions (addendum §5.1 rule 6, decided per file by CP-RECON).
CONVENTIONS = ("excl", "incl")
#: Cell grains.
GRAINS = ("day", "month")
#: Plan scenarios while a seat's plan is unknown (R17).
SCENARIOS = ("business", "enterprise")
#: Seat-count sources in precedence order (``PoolMonth.seats_source``).
SEAT_SOURCES = ("seat_lines", "run_flags", "licenses", "seat_counts", "report_users", "none")
#: Data-quality code of a plan-evidence conflict (named in ``PlanEvidence.evidence``).
PLAN_CONFLICT_DQ = "dq.copilot_plan_conflict"
#: Note of pool months whose seat count comes from report users (a lower bound).
SEATS_LOWER_BOUND_NOTE = ("seats lower bound (report_users): the pool is a lower bound and overage "
                          "figures are upper bounds")

_KNOWN_PLANS = ("business", "enterprise")
_REPORT_SOURCE = "github.ai_usage_report"
_SEATS_API_SOURCE = "github.copilot_seats"
_COPILOT_CHANNEL = "github_copilot"
_STATEMENT_SOURCES = frozenset({"tokenbill.cli", "tokenbill.admin_answers"})
_FLAG_RANK = {"admin_answers": 0, "run": 2}
_CAP_POLICIES = ("block", "continue")
_BILLING_MODES = ("metered", "volume", "azure", "unknown")
_MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
#: ``Cell.credits``: at most 30 integer and 20 fractional digits, so exact sums of up to 10**9 cells
#: stay within EXACT_CTX's 60 digits.
_DECIMAL_STR_RE = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,20})?\Z")
_ENTITY_RE = re.compile(r"(?:enterprise|(?:org|cc):[^\x00-\x1f]+)\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ZERO = Decimal(0)
_ONE_CENT = Decimal("0.01")      # USD per AI credit
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MAX_DATE_MS = 253_402_300_799_999      # 9999-12-31T23:59:59.999Z
#: Largest magnitude (10**_MAX_ADJ) and finest scale (10**-_MIN_EXP) of a decimal read from records;
#: anything else is ignored, so sums stay exact under EXACT_CTX's 60 digits.
_MAX_ADJ = 15
_MIN_EXP = -20
#: Largest seat count or seat change accepted from callers (keeps Decimal products exact).
_MAX_COUNT = 10**15
_EMPTY: Mapping[str, Decimal] = types.MappingProxyType({})
_FORECAST_NOTE = ("forecast: month-end pooled consumption; observed days plus nearest-rank "
                  "p10/p50/p90 of the last 10 business and 4 weekend days")


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _month_bounds(month: object, what: str = "month") -> tuple[_dt.date, _dt.date]:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise UsageError(f"{what}: expected a YYYY-MM string")
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        raise UsageError(f"{what}: year out of range")
    first = _dt.date(year, mon, 1)
    if mon == 12:
        return first, _dt.date(year, 12, 31)
    return first, _dt.date(year, mon + 1, 1) - _dt.timedelta(days=1)


def _parse_date(value: object, what: str) -> _dt.date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise UsageError(f"{what}: expected a YYYY-MM-DD string")
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"{what}: not a calendar date") from None


def _utc_date_of_ms(ms: int) -> str:
    if ms > _MAX_DATE_MS:     # records allow up to 2**53 ms; dates end on 9999-12-31
        raise UsageError("timestamp after 9999-12-31")
    return (_EPOCH + _dt.timedelta(days=ms // _DAY_MS)).isoformat()


def _days_before(day: _dt.date, days: int) -> _dt.date:
    try:
        return day - _dt.timedelta(days=days)
    except OverflowError:
        raise UsageError("today: too early for the report lag") from None


def _check_mode(entity_mode: object) -> None:
    if entity_mode not in ENTITY_MODES:
        raise UsageError("entity_mode: expected 'enterprise' or 'org'")


def _dsum(values: Iterable[Decimal]) -> Decimal:
    total = _ZERO
    for v in values:
        total = EXACT_CTX.add(total, v)
    return total


def _as_decimal(value: object) -> Decimal | None:
    """A bounded finite ``Decimal`` from an int or a decimal string (None for anything else)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 64:
            return None
        try:
            d = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if not d.is_finite():
        return None
    if d and (d.adjusted() > _MAX_ADJ or d.as_tuple().exponent < _MIN_EXP):  # type: ignore[operator]
        return None
    return d


def _dec_str(d: Decimal) -> str:
    """An exact decimal string without exponent (``Decimal("1E+2")`` → ``"100"``)."""
    if not d:
        return "0"
    return format(EXACT_CTX.normalize(d), "f")


def _ceil_int(d: Decimal) -> int:
    whole = int(d)
    return whole + 1 if d > whole else whole


def _credits_to_nano(credits: Decimal) -> int:
    """AI credits → nano-USD (1 credit = $0.01), half-even once."""
    return decimal_to_nano(EXACT_CTX.multiply(credits, _ONE_CENT))


def _credits_of_nano(nano: int) -> Decimal:
    return Decimal(f"{nano}E-7")          # exact: the constructor never rounds


def _clean(name: str | None) -> str | None:
    if name is None:
        return None
    text = _CONTROL_RE.sub("_", name).strip()
    return text or None


def _cc_name(key: str) -> str:
    return key[3:] if key.startswith("cc:") else key


def _config_order(c: ConfigSnapshot) -> tuple:
    """A total order over configuration snapshots: data sources after statements, then time."""
    return (c.source_kind not in _STATEMENT_SOURCES, c.snapshot_ms, c.fetched_ms, c.source_kind,
            repr(c.attrs))


def _config_records(config: Iterable[ConfigSnapshot]) -> list[ConfigSnapshot]:
    items = list(config)
    if not all(isinstance(c, ConfigSnapshot) for c in items):
        raise UsageError("config: expected ConfigSnapshot records")
    return items


def _dedupe_config(config: Iterable[ConfigSnapshot], kind: str) -> list[ConfigSnapshot]:
    """Snapshots of *kind*, one per natural key (the latest fetch wins), in natural-key order."""
    best: dict[str, ConfigSnapshot] = {}
    for c in config:
        if c.kind != kind:
            continue
        key = record_key(c)
        cur = best.get(key)
        if cur is None or (c.fetched_ms, _config_order(c)) > (cur.fetched_ms, _config_order(cur)):
            best[key] = c
    return [best[k] for k in sorted(best)]


# ---------------------------------------------------------------------------------------------
# run flags, cost-center caps, entities, billing modes
# ---------------------------------------------------------------------------------------------


def run_flags(config: Iterable[ConfigSnapshot]) -> dict[str, str | int | bool]:
    """The merged run flags: every ``ConfigSnapshot(kind="run_flags")``, the admin answers (entity
    ``admin_answers``) first and the CLI snapshot (entity ``run``) last, so the CLI wins per key;
    within one entity the later snapshot wins. ``None`` values carry no flag."""
    snaps = [c for c in _config_records(config) if c.kind == "run_flags"]
    snaps.sort(key=lambda c: (_FLAG_RANK.get(c.entity_id, 1), *_config_order(c)))
    merged: dict[str, str | int | bool] = {}
    for snap in snaps:
        for key, value in snap.attrs:
            if value is not None:
                merged[key] = value
    return dict(sorted(merged.items()))


def capped_cost_centers(config: Iterable[ConfigSnapshot]) -> dict[str, Decimal]:
    """Cost-center name → pool cap in credits, for every cost center whose latest
    ``ConfigSnapshot(kind="cost_center")`` has ``pool_enabled`` true and a ``pool_target_credits``
    value (pulled data beats an admin statement of the same cost center)."""
    latest: dict[str, ConfigSnapshot] = {}
    for c in _config_records(config):
        if c.kind != "cost_center":
            continue
        if not c.entity_id.startswith("cc:"):
            continue
        name = c.entity_id[3:]
        cur = latest.get(name)
        if cur is None or _config_order(c) > _config_order(cur):
            latest[name] = c
    out: dict[str, Decimal] = {}
    for name, snap in latest.items():
        attrs = dict(snap.attrs)
        if attrs.get("pool_enabled") not in (True, "true"):
            continue
        cap = _as_decimal(attrs.get("pool_target_credits"))
        if cap is not None and cap >= 0:
            out[name] = cap
    return dict(sorted(out.items()))


def capped_policies(config: Iterable[ConfigSnapshot]) -> dict[str, str]:
    """Cost-center name → cap policy (``block`` | ``continue`` | ``unknown``): the run flag
    ``capped_policy.<cost center>`` (the CLI over the admin answers); every capped cost center
    without a flag is ``unknown``."""
    config = list(config)
    out = {name: "unknown" for name in capped_cost_centers(config)}
    for key, value in run_flags(config).items():
        if key.startswith("capped_policy.") and len(key) > len("capped_policy."):
            name = _cc_name(key[len("capped_policy."):])
            out[name] = value if value in _CAP_POLICIES else "unknown"  # type: ignore[assignment]
    return dict(sorted(out.items()))


def entity_of(cost_center: str | None, org: str | None, *, capped: Mapping[str, Decimal],
              entity_mode: str = "enterprise") -> str:
    """The pool entity of a row: ``cc:<name>`` iff its cost center is capped, else ``org:<login>``
    in entity mode ``org`` (when the org is known), else ``enterprise``."""
    _check_mode(entity_mode)
    cc = _clean(cost_center)
    if cc is not None and (cc in capped or f"cc:{cc}" in capped):
        return f"cc:{cc}"
    login = _clean(org)
    if entity_mode == "org" and login is not None:
        return f"org:{login}"
    return "enterprise"


def billing_modes(cost_lines: Iterable[CostLine], config: Iterable[ConfigSnapshot],
                  licenses: Iterable[LicenseSnapshot]) -> dict[str, str]:
    """Entity → billing mode (``metered`` | ``volume`` | ``azure`` | ``unknown``) for every entity
    id a seat line or license can belong to in either entity mode: seat SKU lines → ``metered``;
    seats known only from licenses → ``unknown``; the run flag ``billing_mode.<entity>`` wins over
    both."""
    config = list(config)
    capped = capped_cost_centers(config)
    out: dict[str, str] = {}
    for line in cost_lines:
        if line.cost_type != "seat" or line.channel != _COPILOT_CHANNEL:
            continue
        for mode in ENTITY_MODES:
            out[entity_of(line.cost_center, line.workspace_id, capped=capped,
                          entity_mode=mode)] = "metered"
    for lic in licenses:
        for mode in ENTITY_MODES:
            out.setdefault(entity_of(lic.cost_center, lic.org, capped=capped, entity_mode=mode),
                           "unknown")
    for key, value in run_flags(config).items():
        if key.startswith("billing_mode.") and value in _BILLING_MODES:
            entity = key[len("billing_mode."):]
            if _ENTITY_RE.match(entity):
                out[entity] = value  # type: ignore[assignment]
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cell:
    """One cell of the Copilot cell table (addendum §9.2): the report tokens and money of one (day
    or month, entity, team, cost center, org, SKU, cost type, model, routing, speed, pseudo)."""

    month: str                  # "YYYY-MM"
    date_utc: str | None        # "YYYY-MM-DD" at day grain, None at month grain
    entity_id: str              # "enterprise" | "org:<login>" | "cc:<name>"
    team: str | None
    cost_center: str | None
    org: str | None
    sku: str
    cost_type: str              # CELL_COST_TYPES, or "other" (tokens without a cost line)
    model: str                  # canonical Copilot model id; "" for pseudo labels
    routing: str                # "direct" | "auto" | "unknown"
    speed: str                  # "standard" | "fast"
    pseudo: str | None
    workload: str | None
    usage: UsageBuckets         # report tokens under the chosen convention
    credits: str                # Σ quantity (decimal string)
    gross_nano: int
    discount_nano: int          # gross − net
    net_nano: int
    n_users: int                # distinct principals of the contributing cost lines
    final: bool                 # every contributing row final

    def __post_init__(self) -> None:
        if not isinstance(self.month, str) or not _MONTH_RE.match(self.month):
            raise ContractViolation("Cell.month: must be a YYYY-MM string")
        d = self.date_utc
        if d is not None and (not isinstance(d, str) or not _DATE_RE.match(d)
                              or d[:7] != self.month):
            raise ContractViolation("Cell.date_utc: must be a YYYY-MM-DD date of the cell month")
        if not isinstance(self.entity_id, str) or not _ENTITY_RE.match(self.entity_id):
            raise ContractViolation("Cell.entity_id: not a pool entity id")
        for name in ("team", "cost_center", "org", "pseudo", "workload"):
            v = getattr(self, name)
            if v is not None and not isinstance(v, str):
                raise ContractViolation(f"Cell.{name}: must be a str or None")
        for name in ("sku", "model", "routing", "speed"):
            if not isinstance(getattr(self, name), str):
                raise ContractViolation(f"Cell.{name}: must be a str")
        if self.cost_type not in GITHUB_COST_TYPES:
            raise ContractViolation("Cell.cost_type: not a GitHub cost type")
        if not isinstance(self.usage, UsageBuckets):
            raise ContractViolation("Cell.usage: must be UsageBuckets")
        if not isinstance(self.credits, str) or not _DECIMAL_STR_RE.match(self.credits):
            raise ContractViolation("Cell.credits: must be a finite decimal string")
        for name in ("gross_nano", "discount_nano", "net_nano", "n_users"):
            if type(getattr(self, name)) is not int:
                raise ContractViolation(f"Cell.{name}: must be an int")
        if self.discount_nano != self.gross_nano - self.net_nano:
            raise ContractViolation("Cell.discount_nano: must equal gross_nano - net_nano")
        if self.n_users < 0:
            raise ContractViolation("Cell.n_users: must be >= 0")
        if type(self.final) is not bool:
            raise ContractViolation("Cell.final: must be a bool")


@dataclass
class _LineAcc:
    credits: Decimal = _ZERO
    gross: int = 0
    net: int = 0
    principals: set[str] = field(default_factory=set)
    final: bool = True
    workloads: set[str] = field(default_factory=set)


_Key = tuple[str, "str | None", "str | None", "str | None", "str | None", str, str, str, str,
             "str | None"]
_BUCKET_FIELDS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                  "cache_write_other", "cache_write_unknown", "output", "web_search_requests",
                  "web_fetch_requests")


def _cell_key(date_utc: str, grain: str, team: str | None, cost_center: str | None,
              org: str | None, sku: str | None, model: str | None, routing: str | None,
              speed: str | None, pseudo: str | None) -> _Key:
    return (date_utc[:7], date_utc if grain == "day" else None, team or None, cost_center or None,
            org or None, sku or "", model or "", routing or "unknown", speed or "standard",
            pseudo or None)


def _key_order(key: tuple) -> tuple:
    return tuple((v is not None, "" if v is None else v) for v in key)


def _line_credits(line: CostLine, gross: int) -> Decimal:
    """The line's credits: its ``quantity``, else gross ÷ $0.01; bounded like every decimal read
    from records (a line beyond 10**15 credits raises ``UsageError``)."""
    q = _as_decimal(line.quantity) if line.quantity is not None else None
    if q is None:
        q = _as_decimal(_credits_of_nano(gross))
    if q is None:
        raise UsageError("build_cells: cost line credits out of range")
    return q


def _to_incl(u: UsageBuckets) -> tuple[UsageBuckets, bool]:
    """Re-derive uncached input from stored ``excl`` buckets for an ``incl`` report (whose ``input``
    included cache reads and writes); a negative result is clamped to 0."""
    uncached = u.uncached_input - u.cache_read - u.cache_write
    if uncached >= 0:
        return replace(u, uncached_input=uncached), False
    return replace(u, uncached_input=0), True


def _split_int(total: int, weights: Sequence[int], fallback: int) -> list[int]:
    """Largest-remainder split of *total* in proportion to *weights* (ties to the lower index)."""
    w = [max(0, x) for x in weights]
    denom = sum(w)
    if denom == 0:
        return [total if i == fallback else 0 for i in range(len(w))]
    parts = [total * x // denom for x in w]
    left = total - sum(parts)
    order = sorted(range(len(w)), key=lambda i: (-(total * w[i] % denom), i))
    for i in order[:left]:
        parts[i] += 1
    return parts


def _split_usage(u: UsageBuckets, weights: Sequence[int], fallback: int) -> list[UsageBuckets]:
    if len(weights) == 1:
        return [u]
    columns = {name: _split_int(getattr(u, name), weights, fallback) for name in _BUCKET_FIELDS}
    reasoning = (_split_int(u.output_reasoning, weights, fallback)
                 if u.output_reasoning is not None else None)
    out = []
    for i in range(len(weights)):
        values = {name: columns[name][i] for name in _BUCKET_FIELDS}
        out.append(UsageBuckets(
            **values,
            cache_write_other_ttl_s=(u.cache_write_other_ttl_s
                                     if values["cache_write_other"] > 0 else None),
            output_reasoning=(min(reasoning[i], values["output"])
                              if reasoning is not None else None),
        ))
    return out


def build_cells(aggregates: Iterable[UsageAggregate], cost_lines: Iterable[CostLine], *,
                grain: str = "day", convention: str = "excl",
                capped: Mapping[str, Decimal] = _EMPTY,
                entity_mode: str = "enterprise") -> tuple[list[Cell], int]:
    """The Copilot cell table and the number of cells whose ``incl`` conversion clamped a negative
    uncached count.

    Cost lines on channel ``github_copilot`` with a cost type of :data:`CELL_COST_TYPES` and
    ``github.ai_usage_report`` aggregates are joined on (date, team, cost center, org, SKU, model,
    routing, speed, pseudo) — the CA-4 ``CostLine`` fields (org = ``workspace_id``) and the
    aggregate dims (``organization``) — never on ``description``. ``convention="incl"`` re-derives
    uncached input from the stored ``excl`` buckets per aggregate (``input − cache_read −
    cache_write``, clamped at 0). ``credits`` = Σ ``quantity`` (gross ÷ $0.01 when a line has none);
    gross = Σ ``list_amount_nano`` (else ``amount_nano``), net = Σ ``amount_nano``; ``final`` iff
    every contributing row is final. Cells are sorted by key and cost type.
    """
    if grain not in GRAINS:
        raise UsageError("build_cells: grain must be 'day' or 'month'")
    if convention not in CONVENTIONS:
        raise UsageError("build_cells: convention must be 'excl' or 'incl'")
    _check_mode(entity_mode)
    lines: dict[_Key, dict[str, _LineAcc]] = defaultdict(dict)
    for line in cost_lines:
        if not isinstance(line, CostLine):
            raise UsageError("build_cells: cost_lines must be CostLine records")
        if line.channel != _COPILOT_CHANNEL or line.cost_type not in CELL_COST_TYPES:
            continue
        key = _cell_key(line.date_utc, grain, line.team, line.cost_center, line.workspace_id,
                        line.sku, line.model, line.routing, line.speed, line.pseudo)
        acc = lines[key].setdefault(line.cost_type, _LineAcc())
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        acc.credits = EXACT_CTX.add(acc.credits, _line_credits(line, gross))
        acc.gross += gross
        acc.net += line.amount_nano
        if line.principal is not None:
            acc.principals.add(line.principal)
        acc.final = acc.final and line.finality == "final"
        if line.workload is not None:
            acc.workloads.add(line.workload)
    usage: dict[_Key, tuple[UsageBuckets, bool, bool]] = {}
    for agg in aggregates:
        if not isinstance(agg, UsageAggregate):
            raise UsageError("build_cells: aggregates must be UsageAggregate records")
        if agg.source_kind != _REPORT_SOURCE:
            continue
        dims = dict(agg.dims)
        if dims.get("channel", _COPILOT_CHANNEL) != _COPILOT_CHANNEL:
            continue
        key = _cell_key(_utc_date_of_ms(agg.bucket_start_ms), grain, dims.get("team"),
                        dims.get("cost_center"), dims.get("organization"), dims.get("sku"),
                        dims.get("model"), dims.get("routing"), dims.get("speed"),
                        dims.get("pseudo"))
        u, clamped = (agg.usage, False) if convention == "excl" else _to_incl(agg.usage)
        prev = usage.get(key)
        if prev is None:
            usage[key] = (u, agg.finality == "final", clamped)
        else:
            usage[key] = (prev[0] + u, prev[1] and agg.finality == "final", prev[2] or clamped)
    cells: list[Cell] = []
    n_clamped = 0
    for key in sorted(set(lines) | set(usage), key=_key_order):
        month, date, team, cc, org, sku, model, routing, speed, pseudo = key
        groups = sorted(lines.get(key, {}).items())
        agg_entry = usage.get(key)
        if not groups:
            groups = [("other", _LineAcc())]
        types_ = [t for t, _ in groups]
        fallback = types_.index("ai_credit.user") if "ai_credit.user" in types_ else 0
        if agg_entry is not None:
            parts = _split_usage(agg_entry[0], [acc.gross for _, acc in groups], fallback)
        else:
            parts = [UsageBuckets()] * len(groups)
        entity = entity_of(cc, org, capped=capped, entity_mode=entity_mode)
        for (cost_type, acc), part in zip(groups, parts, strict=True):
            final = acc.final and (agg_entry[1] if agg_entry is not None else True)
            cells.append(Cell(
                month=month, date_utc=date, entity_id=entity, team=team, cost_center=cc, org=org,
                sku=sku, cost_type=cost_type, model=model, routing=routing, speed=speed,
                pseudo=pseudo, workload=min(acc.workloads) if acc.workloads else None,
                usage=part, credits=_dec_str(acc.credits), gross_nano=acc.gross,
                discount_nano=acc.gross - acc.net, net_nano=acc.net,
                n_users=len(acc.principals), final=final,
            ))
        if agg_entry is not None and agg_entry[2]:
            n_clamped += len(groups)
    return cells, n_clamped


# ---------------------------------------------------------------------------------------------
# seats: counts per entity and plan
# ---------------------------------------------------------------------------------------------


@dataclass
class _Split:
    """Seats of one entity: per plan and per org (for org-level plan evidence)."""

    plans: dict[str, Decimal] = field(default_factory=dict)
    orgs: dict[str | None, Decimal] = field(default_factory=dict)

    def add(self, plan: str, org: str | None, n: Decimal) -> None:
        self.plans[plan] = EXACT_CTX.add(self.plans.get(plan, _ZERO), n)
        self.orgs[org] = EXACT_CTX.add(self.orgs.get(org, _ZERO), n)

    def cleaned(self) -> _Split:
        return _Split({p: n for p, n in self.plans.items() if n > 0},
                      {o: n for o, n in self.orgs.items() if n > 0})

    def total(self) -> Decimal:
        return _dsum(self.plans.values())


@dataclass
class _Census:
    source: str
    split: _Split


def _in_month(date_utc: str, month: str) -> bool:
    return date_utc[:7] == month


def _seat_line_splits(cost_lines: Sequence[CostLine], month: str, capped: Mapping[str, Decimal],
                      mode: str) -> tuple[dict[str, _Split], dict[str, dict[str, Decimal]]]:
    skus = _facts.copilot_skus()
    splits: dict[str, _Split] = {}
    by_sku: dict[str, dict[str, Decimal]] = {}
    for line in cost_lines:
        if line.cost_type != "seat" or line.channel != _COPILOT_CHANNEL:
            continue
        if not _in_month(line.date_utc, month) or line.quantity is None:
            continue
        n = _as_decimal(line.quantity)
        if n is None:
            continue
        fact = skus.get(line.sku or "")
        plan = fact.plan if fact is not None and fact.plan in _KNOWN_PLANS else "unknown"
        entity = entity_of(line.cost_center, line.workspace_id, capped=capped, entity_mode=mode)
        splits.setdefault(entity, _Split()).add(plan, _clean(line.workspace_id), n)
        per = by_sku.setdefault(entity, {})
        sku = line.sku or "(none)"
        per[sku] = EXACT_CTX.add(per.get(sku, _ZERO), n)
    return {e: s.cleaned() for e, s in splits.items()}, by_sku


def _flag_seat_splits(flags: Mapping[str, str | int | bool], capped: Mapping[str, Decimal],
                      mode: str) -> dict[str, _Split]:
    """Stated seats (``pool_seats.<entity>.<plan>``) per pool entity: a statement naming a pool
    entity of this entity mode counts as is; statements naming a smaller scope (``org:<o>`` in
    enterprise mode, an uncapped ``cc:<n>``) add up into their pool entity unless that entity has
    its own statement."""
    direct: dict[str, _Split] = {}
    parts: dict[str, _Split] = {}
    for key, value in flags.items():
        if not key.startswith("pool_seats."):
            continue
        entity, sep, plan = key[len("pool_seats."):].rpartition(".")
        if not sep or plan not in LICENSE_PLANS or not _ENTITY_RE.match(entity):
            continue
        n = _as_decimal(value)
        if n is None or n < 0:
            continue
        org = entity[4:] if entity.startswith("org:") else None
        target = _config_entity(entity, capped, mode)
        (direct if target == entity else parts).setdefault(target, _Split()).add(plan, org, n)
    splits = {**parts, **direct}
    return {e: s.cleaned() for e, s in sorted(splits.items())}


def _license_splits(licenses: Sequence[LicenseSnapshot], month: str,
                    capped: Mapping[str, Decimal], mode: str) -> dict[str, _Split]:
    """Per entity: the seats on the snapshot date with the most seats in the month (ties → the later
    date); one seat per principal (a multi-org seat is billed once), a known plan preferred."""
    per: dict[str, dict[str, dict[str, tuple]]] = {}
    for lic in licenses:
        if not _in_month(lic.snapshot_date, month) or lic.product != _COPILOT_CHANNEL:
            continue
        entity = entity_of(lic.cost_center, lic.org, capped=capped, entity_mode=mode)
        rank = (lic.plan != "unknown", lic.fetched_ms, lic.org or "", lic.plan)
        day = per.setdefault(entity, {}).setdefault(lic.snapshot_date, {})
        cur = day.get(lic.principal)
        if cur is None or rank > cur:
            day[lic.principal] = rank
    splits: dict[str, _Split] = {}
    for entity, days in per.items():
        best = max(days, key=lambda d: (len(days[d]), d))
        split = _Split()
        for rank in days[best].values():
            split.add(rank[3], _clean(rank[2]), Decimal(1))
        splits[entity] = split
    return splits


def _seat_count_rows(config: Sequence[ConfigSnapshot], month: str, capped: Mapping[str, Decimal],
                     mode: str) -> dict[str, dict[str, list[tuple[str, str | None, Decimal]]]]:
    rows: dict[str, dict[str, list[tuple[str, str | None, Decimal]]]] = {}
    for c in _dedupe_config(config, "seat_counts"):
        date = _utc_date_of_ms(c.snapshot_ms)
        if not _in_month(date, month):
            continue
        attrs = dict(c.attrs)
        if attrs.get("bucket") == "*":          # per-team summary row (n_people)
            continue
        n = _as_decimal(attrs.get("n"))
        if n is None or n <= 0:
            continue
        plan = attrs.get("plan")
        plan = plan if plan in LICENSE_PLANS else "unknown"
        if c.entity_id.startswith("cc:"):
            org, entity = None, entity_of(c.entity_id[3:], None, capped=capped, entity_mode=mode)
        else:
            org = c.entity_id[4:] if c.entity_id.startswith("org:") else None
            entity = entity_of(None, org, capped=capped, entity_mode=mode)
        rows.setdefault(entity, {}).setdefault(date, []).append((str(plan), org, n))
    return rows


def _seat_count_splits(rows: Mapping[str, Mapping[str, list[tuple[str, str | None, Decimal]]]]
                       ) -> dict[str, _Split]:
    splits: dict[str, _Split] = {}
    for entity, days in rows.items():
        totals = {d: _dsum(n for _, _, n in r) for d, r in days.items()}
        best = max(totals, key=lambda d: (totals[d], d))
        split = _Split()
        for plan, org, n in days[best]:
            split.add(plan, org, n)
        splits[entity] = split
    return splits


def _report_user_splits(cost_lines: Sequence[CostLine], month: str,
                        capped: Mapping[str, Decimal], mode: str) -> dict[str, _Split]:
    per: dict[str, dict[str, str]] = {}
    for line in cost_lines:
        if (line.cost_type not in POOLED_COST_TYPES or line.channel != _COPILOT_CHANNEL
                or line.principal is None or not _in_month(line.date_utc, month)):
            continue
        entity = entity_of(line.cost_center, line.workspace_id, capped=capped, entity_mode=mode)
        org = _clean(line.workspace_id) or ""
        users = per.setdefault(entity, {})
        cur = users.get(line.principal)
        users[line.principal] = org if cur is None else min(cur, org)
    splits: dict[str, _Split] = {}
    for entity, users in per.items():
        split = _Split()
        for org in users.values():
            split.add("unknown", org or None, Decimal(1))
        splits[entity] = split
    return splits


@dataclass
class _Inputs:
    """Per-month inputs of seat counting and plan detection, each computed in one pass."""

    month: str
    capped: Mapping[str, Decimal]
    mode: str
    flags: Mapping[str, str | int | bool]
    seat_lines: dict[str, _Split]
    seat_line_skus: dict[str, dict[str, Decimal]]
    flag_seats: dict[str, _Split]
    licenses: dict[str, _Split]
    seat_counts: dict[str, _Split]
    report_users: dict[str, _Split]
    seats_api: dict[str, dict[str, Decimal]]
    quota: dict[str, tuple[dict[str, Decimal], list[str], list[str]]]
    org_plans: dict[str, str]

    def layers(self) -> tuple[tuple[str, dict[str, _Split]], ...]:
        return (("seat_lines", self.seat_lines), ("run_flags", self.flag_seats),
                ("licenses", self.licenses), ("seat_counts", self.seat_counts),
                ("report_users", self.report_users))

    def census(self) -> dict[str, _Census]:
        layers = self.layers()
        entities = set().union(*(layer for _, layer in layers))
        out: dict[str, _Census] = {}
        for entity in sorted(entities):
            for name, layer in layers:
                split = layer.get(entity)
                if split is not None and split.total() > 0:
                    out[entity] = _Census(name, split)
                    break
        return out


def _config_entity(entity_id: str, capped: Mapping[str, Decimal], mode: str) -> str:
    """The pool entity of an org- or cost-center-scoped configuration row."""
    if entity_id.startswith("cc:"):
        return entity_of(entity_id[3:], None, capped=capped, entity_mode=mode)
    org = entity_id[4:] if entity_id.startswith("org:") else None
    return entity_of(None, org, capped=capped, entity_mode=mode)


def _seats_api_counts(licenses: Sequence[LicenseSnapshot], month: str,
                      capped: Mapping[str, Decimal], mode: str) -> dict[str, dict[str, Decimal]]:
    """Per entity with seats-API snapshots in *month*: known plan → seats, from the latest snapshot
    per principal and org (a principal counted once, its latest known plan)."""
    latest: dict[tuple[str, str, str], LicenseSnapshot] = {}
    for lic in licenses:
        if (lic.source_kind != _SEATS_API_SOURCE or lic.product != _COPILOT_CHANNEL
                or not _in_month(lic.snapshot_date, month)):
            continue
        entity = entity_of(lic.cost_center, lic.org, capped=capped, entity_mode=mode)
        key = (entity, lic.principal, lic.org or "")
        cur = latest.get(key)
        if cur is None or (lic.snapshot_date, lic.fetched_ms, lic.plan) > (
                cur.snapshot_date, cur.fetched_ms, cur.plan):
            latest[key] = lic
    plan_of: dict[tuple[str, str], tuple] = {}
    out: dict[str, dict[str, Decimal]] = {}
    for (entity, principal, org), lic in latest.items():
        out.setdefault(entity, {})
        if lic.plan in _KNOWN_PLANS:
            rank = (lic.snapshot_date, lic.fetched_ms, org, lic.plan)
            if (entity, principal) not in plan_of or rank > plan_of[(entity, principal)]:
                plan_of[(entity, principal)] = rank
    for (entity, _), rank in plan_of.items():
        out[entity][rank[3]] = EXACT_CTX.add(out[entity].get(rank[3], _ZERO), Decimal(1))
    return out


def _quota_counts(config: Sequence[ConfigSnapshot], month: str, capped: Mapping[str, Decimal],
                  mode: str) -> dict[str, tuple[dict[str, Decimal], list[str], list[str]]]:
    """Per entity: plan → users from ``plan_quota`` rows of *month*, the evidence lines and the
    lines of unmapped quota values."""
    quota_map = _facts.copilot_plan_quota_map()
    out: dict[str, tuple[dict[str, Decimal], list[str], list[str]]] = {}
    for c in _dedupe_config(config, "plan_quota"):
        attrs = dict(c.attrs)
        if attrs.get("month") != month:
            continue
        counts, lines, info = out.setdefault(_config_entity(c.entity_id, capped, mode),
                                             ({}, [], []))
        quota = _as_decimal(attrs.get("quota"))
        n = _as_decimal(attrs.get("n_users"))
        if quota is None or n is None or n <= 0:
            continue
        fact = quota_map.get(int(quota)) if quota == int(quota) else None
        plan = (fact.plan if fact is not None and (not fact.months or month in fact.months)
                else None)
        text = f"report_quota: {c.entity_id} quota {_dec_str(quota)} x {_dec_str(n)}"
        if plan is None:
            info.append(text + " (unmapped)")
        else:
            lines.append(f"{text} ({plan})")
            counts[plan] = EXACT_CTX.add(counts.get(plan, _ZERO), n)
    return out


def _org_plans(config: Sequence[ConfigSnapshot], month: str) -> dict[str, str]:
    """Org login → ``plan_type`` of its org-billing snapshot nearest to *month* (the latest one on
    or before the month's end, else the earliest after it); pulled data only."""
    _, last = _month_bounds(month)
    end_ms = ((last - _EPOCH).days + 1) * _DAY_MS
    snaps: dict[str, list[ConfigSnapshot]] = {}
    for c in config:
        if (c.kind != "org_settings" or c.source_kind in _STATEMENT_SOURCES
                or not c.entity_id.startswith("org:")):
            continue
        if dict(c.attrs).get("plan_type") is None:
            continue
        snaps.setdefault(c.entity_id[4:], []).append(c)
    out: dict[str, str] = {}
    for org, items in snaps.items():
        before = [c for c in items if c.snapshot_ms < end_ms]
        chosen = (max(before, key=_config_order) if before
                  else min(items, key=lambda c: (c.snapshot_ms, _config_order(c))))
        plan = dict(chosen.attrs).get("plan_type")
        if plan in _KNOWN_PLANS:
            out[org] = str(plan)
    return out


def _inputs(cost_lines: Sequence[CostLine], licenses: Sequence[LicenseSnapshot],
            config: Sequence[ConfigSnapshot], month: str, mode: str) -> _Inputs:
    capped = capped_cost_centers(config)
    flags = run_flags(config)
    seat_lines, skus = _seat_line_splits(cost_lines, month, capped, mode)
    return _Inputs(month=month, capped=capped, mode=mode, flags=flags, seat_lines=seat_lines,
                   seat_line_skus=skus, flag_seats=_flag_seat_splits(flags, capped, mode),
                   licenses=_license_splits(licenses, month, capped, mode),
                   seat_counts=_seat_count_splits(_seat_count_rows(config, month, capped, mode)),
                   report_users=_report_user_splits(cost_lines, month, capped, mode),
                   seats_api=_seats_api_counts(licenses, month, capped, mode),
                   quota=_quota_counts(config, month, capped, mode),
                   org_plans=_org_plans(config, month))


def _as_lists(cost_lines: Iterable[CostLine], licenses: Iterable[LicenseSnapshot],
              config: Iterable[ConfigSnapshot]
              ) -> tuple[list[CostLine], list[LicenseSnapshot], list[ConfigSnapshot]]:
    lines, lics, conf = list(cost_lines), list(licenses), list(config)
    if not all(isinstance(x, CostLine) for x in lines):
        raise UsageError("cost_lines: expected CostLine records")
    if not all(isinstance(x, LicenseSnapshot) for x in lics):
        raise UsageError("licenses: expected LicenseSnapshot records")
    return lines, lics, _config_records(conf)


def seat_months(cost_lines: Iterable[CostLine], licenses: Iterable[LicenseSnapshot],
                config: Iterable[ConfigSnapshot], month: str, *,
                entity_mode: str = "enterprise") -> tuple[dict[tuple[str, str], Decimal], str]:
    """Seat-months per (entity, plan) in *month* and the seat source.

    Per entity the first source with seats wins: seat SKU cost lines (Σ ``quantity``; the unit is
    assumed to be seat-months, **VERIFY** addendum §19.5 #10) → run flags
    ``pool_seats.<entity>.<plan>`` → licenses (the snapshot date with the most seats; plan
    ``unknown`` allowed) → ``seat_counts`` configuration rows (aggregate-only bundles) →
    ``report_users`` (distinct principals with ``ai_credit.user`` rows: a **lower bound**) → none.
    The returned source is the lowest-precedence source any entity used (it qualifies the whole map;
    ``"none"`` when no entity has seats).
    """
    _month_bounds(month)
    _check_mode(entity_mode)
    lines, lics, conf = _as_lists(cost_lines, licenses, config)
    census = _inputs(lines, lics, conf, month, entity_mode).census()
    out = {(e, p): n for e, c in census.items() for p, n in c.split.plans.items() if n > 0}
    used = {c.source for c in census.values()}
    source = max(used, key=SEAT_SOURCES.index) if used else "none"
    return dict(sorted(out.items())), source


# ---------------------------------------------------------------------------------------------
# plan detection (CA-48, R17)
# ---------------------------------------------------------------------------------------------


@dataclass
class _Claim:
    """What one evidence source says about an entity's plan."""

    source: str
    counts: dict[str, Decimal]      # business / enterprise → seats this source places
    plans: frozenset[str]           # plans the source says exist
    exclusive: bool                 # the source speaks for every seat of the entity
    lines: list[str]


def _count_claim(source: str, counts: Mapping[str, Decimal], total: Decimal,
                 lines: list[str]) -> _Claim | None:
    placed = {p: n for p, n in counts.items() if p in _KNOWN_PLANS and n > 0}
    if not placed:
        return None
    return _Claim(source, placed, frozenset(placed), _dsum(placed.values()) >= total, lines)


def _counts_line(prefix: str, counts: Mapping[str, Decimal]) -> str:
    return prefix + ", ".join(f"{p} {_dec_str(n)}" for p, n in sorted(counts.items()) if n > 0)


def _seat_line_claim(inp: _Inputs, entity: str) -> _Claim | None:
    split = inp.seat_lines.get(entity)
    if split is None:
        return None
    skus = _facts.copilot_skus()
    lines = []
    for sku, n in sorted(inp.seat_line_skus.get(entity, {}).items()):
        fact = skus.get(sku)
        text = f"seat_lines: {sku} {_dec_str(n)}"
        if fact is None or fact.plan not in _KNOWN_PLANS:
            text += " (no plan)"
        elif not fact.verified:
            text += f" (plan {fact.plan} assumed, unverified)"
        lines.append(text)
    claim = _count_claim("seat_lines", split.plans, split.total(), lines)
    if claim is not None:
        claim.exclusive = True      # seat lines are the seat count
    return claim


def _seats_api_claim(inp: _Inputs, entity: str, total: Decimal) -> _Claim | None:
    counts = inp.seats_api.get(entity)
    if counts is not None:
        return _count_claim("seats_api", counts, total, [_counts_line("seats_api: ", counts)])
    split = inp.seat_counts.get(entity)          # aggregate-only bundles carry the seats API plan
    if split is None:
        return None
    known = {p: n for p, n in split.plans.items() if p in _KNOWN_PLANS}
    return _count_claim("seats_api", known, total,
                        [_counts_line("seats_api (seat_counts): ", known)])


def _org_claim(source: str, org_plans: Mapping[str, str], census: _Census | None, entity: str,
               total: Decimal, line: str) -> _Claim | None:
    """A per-org plan claim (org billing ``plan_type`` or per-org admin statements): each org's
    seats of the census take its org's plan (``mixed`` claims both plans and places none). Without
    an org split of the census the entity's own org applies (org mode), or — for the enterprise — a
    unanimous plan of every org. *line* formats one evidence line from ``org`` and ``plan``."""
    if not org_plans:
        return None
    orgs = census.split.orgs if census is not None else {}
    counts: dict[str, Decimal] = {}
    relevant: list[str] = []
    for org, n in sorted(orgs.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
        if org is not None and org in org_plans:
            relevant.append(org)
            plan = org_plans[org]
            if plan in _KNOWN_PLANS:
                counts[plan] = EXACT_CTX.add(counts.get(plan, _ZERO), n)
    if not relevant and not any(o is not None for o in orgs):
        if entity.startswith("org:") and entity[4:] in org_plans:
            relevant = [entity[4:]]
        elif entity == "enterprise":
            relevant = sorted(org_plans)
        stated = {org_plans[o] for o in relevant}
        if len(stated) == 1 and stated <= set(_KNOWN_PLANS):
            counts = {stated.pop(): total}
    if not relevant:
        return None
    lines = [line.format(org=o, plan=org_plans[o]) for o in relevant]
    plans_claimed = frozenset(
        p for o in relevant for p in (_KNOWN_PLANS if org_plans[o] == "mixed" else (org_plans[o],)))
    placed = {p: n for p, n in counts.items() if n > 0}
    return _Claim(source, placed, plans_claimed, _dsum(placed.values()) >= total > 0, lines)


def _org_settings_claim(org_plans: Mapping[str, str], census: _Census | None, entity: str,
                        total: Decimal) -> _Claim | None:
    return _org_claim("org_settings", org_plans, census, entity, total,
                      "org_settings: org:{org} plan_type={plan}")


def _plan_statement(key: str, value: object, total: Decimal) -> _Claim | None:
    line = [f"admin_statement: {key}={value}"]
    if value in _KNOWN_PLANS:
        return _Claim("admin_statement", {str(value): total}, frozenset({str(value)}), True, line)
    if value == "mixed":
        return _Claim("admin_statement", {}, frozenset(_KNOWN_PLANS), True, line)
    return None


def _statement_claim(inp: _Inputs, entity: str, census: _Census | None,
                     total: Decimal) -> _Claim | None:
    """The admin statement: stated seats per plan (``pool_seats``), else ``plan.<entity>``, else the
    per-org statements (``plan.org:<o>``, the answers' ``plan_as_shown``) applied to each org's
    seats, else ``plan.enterprise`` for an org or cost-center entity."""
    split = inp.flag_seats.get(entity)
    if split is not None:
        claim = _count_claim("admin_statement", split.plans, total,
                             [f"admin_statement: pool_seats.{entity}.{p}={_dec_str(n)}"
                              for p, n in sorted(split.plans.items())])
        if claim is not None:
            return claim
    claim = _plan_statement(f"plan.{entity}", inp.flags.get(f"plan.{entity}"), total)
    if claim is not None:
        return claim
    if not entity.startswith("org:"):
        per_org = {k[len("plan.org:"):]: str(v) for k, v in inp.flags.items()
                   if k.startswith("plan.org:") and len(k) > len("plan.org:")
                   and v in (*_KNOWN_PLANS, "mixed")}
        claim = _org_claim("admin_statement", per_org, census, entity, total,
                           "admin_statement: plan.org:{org}={plan}")
        if claim is not None and claim.plans:
            return claim
    if entity != "enterprise":
        return _plan_statement("plan.enterprise", inp.flags.get("plan.enterprise"), total)
    return None


def _min_counts(claim: _Claim) -> dict[str, Decimal]:
    """Seats per plan the claim implies at least (1 for a plan it names without a count)."""
    return {p: max(claim.counts.get(p, _ZERO), Decimal(1)) for p in claim.plans}


def _disagrees(decider: _Claim, other: _Claim, seats: Decimal) -> bool:
    """Two claims conflict when one speaks for every seat and the other names a plan outside it,
    or when no assignment of the seats satisfies both (Σ over plans of the larger claimed count
    exceeds the seats, counted as the larger of the seat total and either claim's total)."""
    if (decider.exclusive and other.plans - decider.plans) or (
            other.exclusive and decider.plans - other.plans):
        return True
    d, o = _min_counts(decider), _min_counts(other)
    need = _dsum(max(d.get(p, _ZERO), o.get(p, _ZERO)) for p in _KNOWN_PLANS)
    return need > max(seats, _dsum(d.values()), _dsum(o.values()))


def _detect(inp: _Inputs, census: Mapping[str, _Census]) -> list[PlanEvidence]:
    out: list[PlanEvidence] = []
    for entity in sorted(set(census) | set(inp.seat_lines) | set(inp.quota)):
        cen = census.get(entity)
        total = cen.split.total() if cen is not None else _ZERO
        q_counts, q_lines, info = inp.quota.get(entity, ({}, [], []))
        claims = [c for c in (
            _seat_line_claim(inp, entity),
            _seats_api_claim(inp, entity, total),
            _org_settings_claim(inp.org_plans, cen, entity, total),
            _count_claim("report_quota", q_counts, total, q_lines),
            _statement_claim(inp, entity, cen, total),
        ) if c is not None and c.plans]
        decider = claims[0] if claims else None
        known = dict(decider.counts) if decider is not None else {}
        placed = _dsum(known.values())
        seats_total = max(total, placed)
        unknown = EXACT_CTX.subtract(seats_total, placed)
        if seats_total <= 0:
            continue
        seats = {p: _ceil_int(n) for p, n in known.items() if n > 0}
        if unknown > 0:
            seats["unknown"] = _ceil_int(unknown)
        conflicts = [c.source for c in claims[1:]
                     if decider is not None and _disagrees(decider, c, seats_total)]
        evidence = [line for c in claims for line in c.lines] + info
        if decider is not None and placed > total and cen is not None:
            evidence.append(f"seat count raised from {_dec_str(total)} to {_dec_str(placed)} "
                            f"by {decider.source}")
        if decider is not None and conflicts:
            evidence.append(f"{PLAN_CONFLICT_DQ}: {decider.source} vs {', '.join(conflicts)}")
        if unknown > 0:
            plan = "unknown"
        elif all(seats.get(p, 0) > 0 for p in _KNOWN_PLANS):
            plan = "mixed"
        else:
            plan = next(p for p in _KNOWN_PLANS if seats.get(p, 0) > 0)
        out.append(PlanEvidence(
            entity_id=entity, month=inp.month, plan=plan,
            source=decider.source if decider is not None else "none",
            seats=tuple(sorted(seats.items())), conflict=bool(conflicts),
            evidence=tuple(evidence),
        ))
    return out


def detect_plans(cost_lines: Iterable[CostLine], licenses: Iterable[LicenseSnapshot],
                 config: Iterable[ConfigSnapshot], *, month: str,
                 entity_mode: str = "enterprise") -> list[PlanEvidence]:
    """One ``PlanEvidence`` per entity (``enterprise`` | ``org:<o>`` | capped ``cc:<n>``) with seats
    in *month*, sorted by entity id (R17, ruling R-E22).

    Seats are counted as in :func:`seat_months`. Plan sources in precedence order — (1) seat SKU
    lines (``facts.copilot.skus``: ``copilot_for_business`` / ``copilot_standalone`` (**VERIFY**) →
    business, ``copilot_enterprise`` → enterprise); (2) the seats API (``LicenseSnapshot`` of source
    kind ``github.copilot_seats`` with a known plan, the latest snapshot per principal and org in
    the month; ``seat_counts`` rows of aggregate-only bundles carry the same plan); (3) org billing
    ``plan_type`` (``org_settings`` snapshots, applied to that org's seats); (4) report quota
    (``plan_quota`` rows emitted by CP-BILL under ``copilot-report-quota``, mapped by
    ``facts.copilot.plan_quota_map`` — counts, not seats); (5) the admin statement (run flags
    ``plan.<entity>`` or ``pool_seats.<entity>.<plan>``). The first source with evidence places
    seats; the others confirm or conflict (``conflict=True`` plus an evidence line naming
    ``dq.copilot_plan_conflict``). Data always beats a statement; a statement is used only when
    (1)–(4) are silent. ``plan`` is ``business`` / ``enterprise`` when every seat is known and
    equal, ``mixed`` when known and both, else ``unknown``. Fractional seat-months count as whole
    seats here (ceiling); pool figures keep them exact.
    """
    _month_bounds(month)
    _check_mode(entity_mode)
    lines, lics, conf = _as_lists(cost_lines, licenses, config)
    inp = _inputs(lines, lics, conf, month, entity_mode)
    return _detect(inp, inp.census())


# ---------------------------------------------------------------------------------------------
# the pool rule
# ---------------------------------------------------------------------------------------------


def _plan_fact(plan: str) -> _facts.CopilotPlanFact:
    fact = _facts.copilot_plans().get(plan)
    if fact is None:
        raise UsageError("unknown Copilot plan")
    return fact


def _allowance(plan: str, month: str, *, promo_eligible: bool) -> tuple[Decimal, str | None]:
    """Included credits per seat-month of *plan* in *month* and the promo applied (or None)."""
    fact = _plan_fact(plan)
    first = f"{month}-01"
    if (promo_eligible and fact.promo_credits is not None and fact.promo_from is not None
            and fact.promo_to is not None and fact.promo_from <= first < fact.promo_to):
        return Decimal(fact.promo_credits), f"promo:{fact.promo_from}/{fact.promo_to}"
    return Decimal(fact.included_credits), None


def pool_credits(seats: Mapping[str, Decimal], month: str, *,
                 promo_eligible: bool) -> tuple[Decimal, str | None]:
    """The pool of one entity-month in credits and the promo applied (``promo:<from>/<to>``) or
    None: Σ seat-months × included credits (Business 1,900, Enterprise 3,900; the existing-customer
    promo 3,000 / 7,000 for months in [2026-06-01, 2026-09-01) when *promo_eligible*), from
    ``core.facts.copilot_plans()``. A plan ``unknown`` raises ``UsageError`` — callers split unknown
    seats into scenarios first (R17)."""
    _month_bounds(month)
    total = _ZERO
    promo: str | None = None
    for plan, n in sorted(seats.items()):
        if plan == "unknown":
            raise UsageError("pool_credits: plan unknown; split the seats into scenarios first")
        if plan not in _KNOWN_PLANS:
            raise UsageError("pool_credits: unknown plan")
        count = _as_decimal(n)
        if count is None or count < 0:
            raise UsageError("pool_credits: seats must be non-negative decimals")
        per_seat, applied = _allowance(plan, month, promo_eligible=promo_eligible)
        if count > 0 and applied is not None:
            promo = applied
        total = EXACT_CTX.add(total, EXACT_CTX.multiply(count, per_seat))
    return total, promo


def regime(consumed_low: int, consumed_high: int, pool_nano: int | None) -> str:
    """``slack`` when even the high consumption fits the pool, ``overage`` when even the low
    consumption exceeds it, else ``straddling``; ``unknown`` when the pool is unknown (None)."""
    if pool_nano is None:
        return "unknown"
    for v in (consumed_low, consumed_high, pool_nano):
        if type(v) is not int:
            raise UsageError("regime: expected int nano values")
    if consumed_low > consumed_high:
        raise UsageError("regime: consumed_low > consumed_high")
    if consumed_high <= pool_nano:
        return "slack"
    if consumed_low > pool_nano:
        return "overage"
    return "straddling"


def invoice_delta(consumed_before: int, consumed_after: int, pool_nano: int) -> int:
    """Invoice dollars (nano) a consumption change realizes against a pool:
    ``max(0, before − pool) − max(0, after − pool)`` (Appendix C.P8)."""
    for v in (consumed_before, consumed_after, pool_nano):
        if type(v) is not int:
            raise UsageError("invoice_delta: expected int nano values")
    return max(0, consumed_before - pool_nano) - max(0, consumed_after - pool_nano)


def overage_total(entity_consumption: Mapping[str, int], pools: Mapping[str, int],
                  capped: Mapping[str, int], *, policies: Mapping[str, str]) -> tuple[int, int]:
    """(low, high) overage nano of one billing entity with capped cost centers (Appendix C.P7).

    A capped cost center ``c`` (keys ``cc:<name>`` or the bare name) with use ``u`` and cap ``k``:
    policy ``continue`` → ``max(0, u − k)``; ``block`` → 0 (the excess demand is blocked and
    reported apart); ``unknown`` → low uses block, high uses continue. The remainder is ``max(0,
    uncapped_use + Σ_c min(u_c, k_c) − pool)`` where ``pool`` = Σ *pools* (the parent's pool,
    optionally with the capped cost centers' own entries). Several uncapped entities without caps
    are independent pools (Σ of their overages); several uncapped entities with caps raise
    ``UsageError`` (one call per billing entity).
    """
    caps = {("cc:" + _cc_name(k)): v for k, v in capped.items()}
    pol = {("cc:" + _cc_name(k)): v for k, v in policies.items()}
    for mapping in (entity_consumption, pools, caps):
        if any(type(v) is not int for v in mapping.values()):
            raise UsageError("overage_total: expected int nano values")
    low = high = 0
    draws = 0
    for cc, cap in sorted(caps.items()):
        use = entity_consumption.get(cc, 0)
        over = max(0, use - cap)
        draws += min(use, cap)
        policy = pol.get(cc, "unknown")
        if policy == "continue":
            low += over
            high += over
        elif policy != "block":
            high += over
    uncapped = sorted({k for k in (*entity_consumption, *pools) if k not in caps})
    if len(uncapped) > 1 and not caps:
        rest = sum(max(0, entity_consumption.get(e, 0) - pools.get(e, 0)) for e in uncapped)
        return low + rest, high + rest
    if len(uncapped) > 1:
        raise UsageError("overage_total: capped cost centers need a single parent pool entity")
    uncapped_use = sum(entity_consumption.get(e, 0) for e in uncapped)
    remainder = max(0, uncapped_use + draws - sum(pools.values()))
    return low + remainder, high + remainder


def direct_draws_pool(cells: Sequence[Cell]) -> str:
    """Whether direct-org usage draws on the pool (addendum §19.5 #7), read from the direct cells'
    discounts: ``yes`` if any direct cell has a discount > 0; ``no`` if direct cells have discount 0
    on days where pooled cells still had a discount > 0 (pool left); else ``unknown``."""
    direct = [c for c in cells if c.cost_type in DIRECT_COST_TYPES]
    if not direct:
        return "unknown"
    if any(c.discount_nano > 0 for c in direct):
        return "yes"
    pool_days = {(c.month, c.date_utc) for c in cells
                 if c.cost_type in POOLED_COST_TYPES and c.discount_nano > 0}
    if any((c.month, c.date_utc) in pool_days for c in direct):
        return "no"
    return "unknown"


def _is_auto_tenth(discount: int, gross: int) -> bool:
    """``discount`` is 10% of ``gross`` (within 1 nano or 1 ppm of gross)."""
    return gross > 0 and abs(10 * discount - gross) <= max(10, gross // 1_000_000)


def classify_discounts(cells: Sequence[Cell], *, gross_is_list: bool | None,
                       pool_nano: int) -> tuple[int | None, int, int]:
    """(pool_draw, other, unclassified) nano of the pooled and direct cells' discounts (DC22).

    Auto cells whose discount is 10% of gross while net > 0 → ``other`` (the Auto discount). The
    rest is the pool draw only when ``gross_is_list`` is True (CP-RECON's L1: gross = tokens × list)
    and it fits the pool (Σ ≤ ``pool_nano``, L3); otherwise it stays unclassified (``pool_draw``
    None).
    """
    other = rest = 0
    for c in cells:
        if c.cost_type not in POOLED_COST_TYPES + DIRECT_COST_TYPES or c.discount_nano == 0:
            continue
        if c.routing == "auto" and c.net_nano > 0 and _is_auto_tenth(c.discount_nano,
                                                                      c.gross_nano):
            other += c.discount_nano
        else:
            rest += c.discount_nano
    if gross_is_list is True and rest <= pool_nano:
        return rest, other, 0
    return None, other, rest


def _rank_value(values: Sequence[int], num: int) -> int:
    """Nearest-rank percentile ``num``/10: the value at index ``ceil(num·n/10) − 1`` of the sorted
    series."""
    ordered = sorted(values)
    idx = (num * len(ordered) + 9) // 10 - 1
    return ordered[idx]


def forecast(daily_nano: Sequence[tuple[str, int]], *, month: str, today: str,
             lag_days: int = 3) -> tuple[int, int, int, int] | None:
    """(observed, point, low, high) month-end consumption nano (binding rule, Appendix C.P9).

    Observed days are the days of *month* ≤ ``today − lag_days`` (days without a row count as 0);
    each remaining business day (Mon–Fri) adds the nearest-rank p50 (point), p10 (low) and p90
    (high) of the last 10 observed business days, each remaining weekend day those of the last 4
    observed weekend days (fewer when fewer exist; none → the other class's statistics). No observed
    day → None; no remaining day → all four equal the observed total.
    """
    first, last = _month_bounds(month)
    cutoff = _days_before(_parse_date(today, "today"), _lag(lag_days))
    by_day: dict[_dt.date, int] = defaultdict(int)
    for date_utc, nano in daily_nano:
        day = _parse_date(date_utc, "daily date")
        if type(nano) is not int:
            raise UsageError("forecast: daily values must be int nano")
        if first <= day <= last:
            by_day[day] += nano
    days = [first + _dt.timedelta(days=i) for i in range(last.day)]
    observed = [d for d in days if d <= cutoff]
    if not observed:
        return None
    total = sum(by_day.get(d, 0) for d in observed)
    remaining = [d for d in days if d > cutoff]
    business = [by_day.get(d, 0) for d in observed if d.weekday() < 5][-10:]
    weekend = [by_day.get(d, 0) for d in observed if d.weekday() >= 5][-4:]
    stats_b = [_rank_value(business or weekend, q) for q in (1, 5, 9)]
    stats_w = [_rank_value(weekend or business, q) for q in (1, 5, 9)]
    add = [0, 0, 0]
    for d in remaining:
        stats = stats_b if d.weekday() < 5 else stats_w
        for i in range(3):
            add[i] += stats[i]
    return total, total + add[1], total + add[0], total + add[2]


def _lag(lag_days: object) -> int:
    if type(lag_days) is not int or lag_days < 0:
        raise UsageError("lag_days: expected a non-negative int")
    return lag_days


# ---------------------------------------------------------------------------------------------
# pool months
# ---------------------------------------------------------------------------------------------


def _infer_mode(cells: Sequence[Cell], plans: Sequence[PlanEvidence] | None) -> str:
    ids = [c.entity_id for c in cells] + [p.entity_id for p in plans or ()]
    return "org" if any(e.startswith("org:") for e in ids) else "enterprise"


def _data_months(cost_lines: Sequence[CostLine], licenses: Sequence[LicenseSnapshot],
                 config: Sequence[ConfigSnapshot]) -> set[str]:
    months: set[str] = set()
    for line in cost_lines:
        if line.channel == _COPILOT_CHANNEL and line.cost_type in ("seat", *POOLED_COST_TYPES):
            months.add(line.date_utc[:7])
    months.update(lic.snapshot_date[:7] for lic in licenses if lic.product == _COPILOT_CHANNEL)
    for c in config:
        if c.kind == "seat_counts":
            months.add(_utc_date_of_ms(c.snapshot_ms)[:7])
        elif c.kind == "plan_quota":
            m = dict(c.attrs).get("month")
            if isinstance(m, str) and _MONTH_RE.match(m):
                months.add(m)
    return months


def _gross_is_list_arg(value: object) -> bool | None | Mapping[object, object]:
    """Validate *gross_is_list*: a bool, None, a mapping, or CP-RECON's decision pairs
    (``AnalysisContext.recon_decisions``: ``(key, value)`` string pairs), which become a mapping."""
    if value is None or isinstance(value, (bool, Mapping)):
        return value
    if isinstance(value, (tuple, list)) and all(
            isinstance(p, tuple) and len(p) == 2 and isinstance(p[0], str) for p in value):
        return dict(value)
    raise UsageError("gross_is_list: expected a bool, None, a mapping or decision pairs")


def _gross_is_list_for(value: bool | None | Mapping[object, object], entity: str,
                       month: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    for key in (f"{entity}:{month}", f"gross_is_list:{entity}:{month}"):
        if key in value:
            v = value[key]
            if isinstance(v, bool) or v is None:
                return v
            return {"true": True, "false": False}.get(str(v))
    return None


def _flag_false(value: object) -> bool:
    """A run-flag value that says no (``False`` or the strings ``false`` / ``no`` / ``0``)."""
    if isinstance(value, str):
        return value.strip().lower() in ("false", "no", "0")
    return value is False


def _estimates(recent: Sequence[tuple[str, str | None, int]], mode: str) -> dict[tuple[str, str],
                                                                               int]:
    """(entity, date) → provider-estimate nano; a None entity is the enterprise (enterprise mode
    only)."""
    out: dict[tuple[str, str], int] = defaultdict(int)
    for item in recent:
        if not isinstance(item, tuple) or len(item) != 3:
            raise UsageError("recent_estimates: expected (date, entity or None, nano) tuples")
        date_utc, entity, nano = item
        _parse_date(date_utc, "recent_estimates date")
        if type(nano) is not int:
            raise UsageError("recent_estimates: nano must be an int")
        if entity is None:
            if mode != "enterprise":
                continue
            entity = "enterprise"
        if not isinstance(entity, str) or not _ENTITY_RE.match(entity):
            raise UsageError("recent_estimates: not a pool entity id")
        out[(entity, date_utc)] += nano
    return out


def _resolve_seats(pe: PlanEvidence | None, cen: _Census | None) -> tuple[dict[str, Decimal],
                                                                            str]:
    """Seat-months per plan (``unknown`` for seats no evidence placed) and the seat source."""
    if pe is not None and any(n > _MAX_COUNT for _, n in pe.seats):
        raise UsageError("pool_months: plan evidence seat count out of range")
    if cen is not None and cen.source == "seat_lines":
        return dict(cen.split.plans), "seat_lines"
    known = {p: Decimal(n) for p, n in (pe.seats if pe is not None else ()) if p in _KNOWN_PLANS
             and n > 0}
    if cen is not None:
        placed = _dsum(known.values())
        total = max(cen.split.total(), placed)
        seats = dict(known)
        unknown = EXACT_CTX.subtract(total, placed)
        if unknown > 0:
            seats["unknown"] = unknown
        return seats, cen.source
    if pe is not None:
        return {p: Decimal(n) for p, n in pe.seats if n > 0}, "none"
    return {}, "none"


def _figure_range(point: int, low: int, high: int, *, basis: Basis, note: str,
                  upper: bool = False) -> Figure:
    return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis,
                  finality=Finality.PROVISIONAL, low_nano=low, high_nano=high,
                  calibration=Calibration.UNCALIBRATED, upper_bound=upper, note=note)


def _overage_forecast(fc: tuple[int, int, int, int], pools: tuple[int, int, int],
                      policy: str | None, lower_bound_seats: bool) -> Figure:
    """The overage forecast at the p10 / p50 / p90 consumption against the pool available at each
    level (*pools*: low, point, high)."""
    _, point, low, high = fc
    cont = (max(0, low - pools[0]), max(0, point - pools[1]), max(0, high - pools[2]))
    if policy == "block":
        return _figure_range(0, 0, 0, basis=Basis.LIST, upper=lower_bound_seats,
                             note=f"cap policy block: overage 0; blocked demand "
                                  f"{nano_to_credits_str(cont[1])} credits at p50 (ESTIMATED)")
    if policy == "unknown":
        return _figure_range(cont[1], 0, cont[2], basis=Basis.LIST, upper=lower_bound_seats,
                             note="overage forecast; cap policy unknown: low assumes block, point "
                                  "and high continue")
    return _figure_range(cont[1], cont[0], cont[2], basis=Basis.LIST, upper=lower_bound_seats,
                         note="overage forecast: month-end consumption over the pool at "
                              "p50 (p10-p90), at $0.01 per credit")


@dataclass
class _Common:
    """The plan-independent part of one entity-month."""

    consumed: int
    estimate: int
    observed_net: int
    direct_net: int
    direct_draws: str
    days_final: int
    days_provisional: int
    days_in_month: int
    finality: str
    fc: tuple[int, int, int, int] | None
    ai_cells: list[Cell]


def _common(entity: str, month: str, cells: Sequence[Cell], today: _dt.date, lag: int,
            estimates: Mapping[tuple[str, str], int]) -> _Common:
    first, last = _month_bounds(month)
    pooled = [c for c in cells if c.cost_type in POOLED_COST_TYPES]
    direct = [c for c in cells if c.cost_type in DIRECT_COST_TYPES]
    consumed = _credits_to_nano(_dsum(Decimal(c.credits) for c in pooled))
    month_grain = any(c.date_utc is None for c in cells)
    if month_grain:
        all_final = all(c.final for c in cells)
        days_final = last.day if cells and all_final else 0
        days_prov = last.day if cells and not all_final else 0
        report_days: set[str] = {(first + _dt.timedelta(days=i)).isoformat()
                                 for i in range(last.day)} if cells else set()
    else:
        per_day: dict[str, bool] = {}
        for c in cells:
            per_day[c.date_utc] = per_day.get(c.date_utc, True) and c.final  # type: ignore[index]
        days_final = sum(1 for v in per_day.values() if v)
        days_prov = len(per_day) - days_final
        report_days = {c.date_utc for c in pooled + direct}  # type: ignore[misc]
    estimate = sum(n for (e, d), n in estimates.items()
                   if e == entity and d[:7] == month and d not in report_days)
    lag_passed = last <= _days_before(today, lag)
    finality = "closed" if lag_passed and days_prov == 0 else "open"
    fc = None
    if finality == "open" and not month_grain:
        daily: dict[str, Decimal] = {}
        for c in pooled:
            daily[c.date_utc] = EXACT_CTX.add(  # type: ignore[index]
                daily.get(c.date_utc, _ZERO), Decimal(c.credits))  # type: ignore[arg-type]
        series = [(d, _credits_to_nano(v)) for d, v in sorted(daily.items())]
        fc = forecast(series, month=month, today=today.isoformat(), lag_days=lag)
    return _Common(consumed=consumed, estimate=estimate,
                   observed_net=sum(c.net_nano for c in pooled),
                   direct_net=sum(c.net_nano for c in direct),
                   direct_draws=direct_draws_pool(pooled + direct), days_final=days_final,
                   days_provisional=days_prov, days_in_month=last.day, finality=finality, fc=fc,
                   ai_cells=pooled + direct)


@dataclass
class _Shared:
    """The enterprise's share of its capped cost centers in one month (enterprise mode): caps to
    deduct from its seat allowance (cost centers whose seats its seat source does not attribute to
    them) and the caps' unused part at the low / point / high consumption (shared with everyone)."""

    deduct: Decimal
    deducted: tuple[str, ...]
    unused: tuple[int, int, int]


def _levels(cm: _Common) -> tuple[int, int, int]:
    """(low, point, high) month-end pooled consumption of an entity-month."""
    if cm.finality == "open" and cm.fc is not None:
        return cm.fc[2], cm.fc[1], cm.fc[3]
    return cm.consumed, cm.consumed, cm.consumed


def _shared(month: str, commons: Mapping[tuple[str, str], _Common], inp: _Inputs,
            census: Mapping[str, _Census], capped: Mapping[str, Decimal]) -> _Shared | None:
    ccs = sorted(e for e, m in commons if m == month and e.startswith("cc:") and e[3:] in capped)
    parent = census.get("enterprise")
    if not ccs or parent is None:
        return None
    layer = dict(inp.layers())[parent.source]
    deduct = _ZERO
    deducted: list[str] = []
    unused = [0, 0, 0]
    for cc in ccs:
        cap = capped[cc[3:]]
        own = layer.get(cc)
        if own is None or own.total() <= 0:
            deduct = EXACT_CTX.add(deduct, cap)
            deducted.append(cc)
        cap_nano = _credits_to_nano(cap)
        for i, use in enumerate(_levels(commons[(cc, month)])):
            unused[i] += max(0, cap_nano - use)
    return _Shared(deduct, tuple(deducted), (unused[0], unused[1], unused[2]))


def _scenario_seats(seats: Mapping[str, Decimal], scenario: str | None) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for plan, n in seats.items():
        target = scenario if plan == "unknown" else plan
        if target is None:
            continue
        out[target] = EXACT_CTX.add(out.get(target, _ZERO), n)
    return out


def _entity_month(entity: str, month: str, cm: _Common, pe: PlanEvidence | None,
                  cen: _Census | None, *, capped: Mapping[str, Decimal],
                  policies: Mapping[str, str], billing_mode: str, eligible: bool,
                  gross_is_list: bool | None, shared: _Shared | None) -> list[PoolMonth]:
    seats, seats_source = _resolve_seats(pe, cen)
    scenarios: tuple[str | None, ...] = SCENARIOS if seats.get("unknown", _ZERO) > 0 else (None,)
    is_cc = entity.startswith("cc:") and entity[3:] in capped
    policy = policies.get(entity[3:], "unknown") if is_cc else None
    lower_bound = seats_source == "report_users"
    out: list[PoolMonth] = []
    for scenario in scenarios:
        allowance, promo = pool_credits(_scenario_seats(seats, scenario), month,
                                        promo_eligible=eligible)
        pool_known = is_cc or bool(seats)
        share = shared if pool_known and not is_cc else None
        if is_cc:
            pool_c = capped[entity[3:]]
        elif share is not None:
            pool_c = max(_ZERO, EXACT_CTX.subtract(allowance, share.deduct))
        else:
            pool_c = allowance
        pool_nano = _credits_to_nano(pool_c)
        unused = share.unused if share is not None else (0, 0, 0)
        # the pool this entity can draw at the low / point / high consumption
        avail = (pool_nano + unused[0], pool_nano + unused[1], pool_nano + unused[2])
        draw, other, unclassified = classify_discounts(cm.ai_cells, gross_is_list=gross_is_list,
                                                       pool_nano=avail[1])
        if scenario is None or is_cc:
            overage = cm.observed_net
        else:
            overage = max(0, cm.consumed - avail[1])
        if not pool_known:
            reg = "unknown"
        elif cm.finality == "closed":
            reg = regime(cm.consumed, cm.consumed, avail[1])
        elif cm.fc is None:
            reg = "unknown"
        else:
            reg = regime(cm.fc[2] - unused[0], cm.fc[3] - unused[2], pool_nano)
        fc_fig = over_fig = None
        if cm.finality == "open" and cm.fc is not None:
            fc_fig = _figure_range(cm.fc[1], cm.fc[2], cm.fc[3], basis=Basis.LIST_EQUIVALENT,
                                   note=_FORECAST_NOTE)
            if pool_known:
                over_fig = _overage_forecast(cm.fc, avail, policy, lower_bound)
        notes: list[str] = []
        if scenario is not None:
            notes.append(f"plan unknown: scenario {scenario}")
            if not is_cc:
                notes.append(f"scenario overage = consumption - scenario pool; observed net of "
                             f"pooled rows {nano_to_credits_str(cm.observed_net)} credits")
        if pe is not None and pe.conflict:
            notes.append(f"plan evidence conflict ({PLAN_CONFLICT_DQ})")
        if lower_bound:
            notes.append(SEATS_LOWER_BOUND_NOTE)
        if not pool_known:
            notes.append("seats unknown: pool and regime unknown")
        if share is not None and share.deducted:
            notes.append(f"caps of {', '.join(share.deducted)} deducted from this pool (the seat "
                         f"source does not attribute their seats to them): "
                         f"{_dec_str(share.deduct)} credits")
        if share is not None and unused[1] > 0:
            notes.append(f"shared pool: unused caps of capped cost centers "
                         f"({nano_to_credits_str(unused[1])} credits at the point) count toward "
                         f"this entity's regime and overage, not toward pool_credits; seat-change "
                         f"savings ignore them (conservative)")
        if is_cc:
            notes.append(f"capped cost center: cap {_dec_str(pool_c)} credits, policy {policy}")
            if seats and _dec_str(allowance) != _dec_str(pool_c):
                notes.append(f"cap differs from the seat allowance {_dec_str(allowance)} credits")
        if cm.direct_draws == "yes":
            notes.append("direct-org rows draw on the pool (their discounts); consumption counts "
                         "pooled rows only, so the regime may understate pool use")
        if cm.estimate:
            notes.append("consumed_estimate_nano: provider estimate (ai_credits_used) for days not "
                         "yet in the report; never added to report sums")
        if billing_mode in ("volume", "azure"):
            notes.append(f"billing mode {billing_mode}: calendar-month pool assumed (VERIFY); "
                         "seat savings only at renewal")
        out.append(PoolMonth(
            entity_id=entity, month=month, billing_mode=billing_mode,
            seats=tuple(sorted((p, _dec_str(n)) for p, n in seats.items() if n > 0)),
            seats_source=seats_source, pool_credits=_dec_str(pool_c), pool_nano=pool_nano,
            promo=promo, consumed_report_nano=cm.consumed, consumed_estimate_nano=cm.estimate,
            pool_draw_nano=draw, discount_other_nano=other,
            discount_unclassified_nano=unclassified, overage_observed_nano=overage,
            direct_net_nano=cm.direct_net, direct_draws_pool=cm.direct_draws,
            capped_policy=policy, days_final=cm.days_final,
            days_provisional=cm.days_provisional, days_in_month=cm.days_in_month,
            finality=cm.finality, forecast=fc_fig, overage_forecast=over_fig, regime=reg,
            notes=tuple(notes), plan_source=pe.source if pe is not None else "none",
            plan_scenario=scenario, plan_conflict=bool(pe is not None and pe.conflict),
        ))
    return out


def pool_months(cells: Iterable[Cell], cost_lines: Iterable[CostLine],
                licenses: Iterable[LicenseSnapshot], config: Iterable[ConfigSnapshot], *,
                today: str, promo_eligible: bool = True,
                recent_estimates: Sequence[tuple[str, str | None, int]] = (),
                gross_is_list: bool | None | Mapping[str, object]
                | Sequence[tuple[str, str]] = None,
                plans: Sequence[PlanEvidence] | None = None,
                entity_mode: str | None = None) -> list[PoolMonth]:
    """One ``PoolMonth`` per entity × month with cells or seats — two (``plan_scenario``
    ``business`` and ``enterprise``) while any seat's plan is unknown, never one merged figure
    (R17).

    *plans* None → :func:`detect_plans` for every month with Copilot data. Consumption
    (``consumed_report_nano``) = Σ credits × $0.01 of the pooled cells (final and provisional);
    ``consumed_estimate_nano`` = *recent_estimates* ``(date, entity or None, nano)`` for days
    without a report cell of the entity (provider estimates, never added to report sums); overage =
    Σ net of the pooled cells (a scenario: ``max(0, consumed − pool)``); ``finality="closed"`` only
    when the month ended at least ``report_lag_days`` ago and no day is provisional; open months
    carry an ESTIMATED ``forecast`` (LIST_EQUIVALENT) and ``overage_forecast`` (LIST; widened by an
    unknown cap policy). A ``report_users`` seat source makes the pool a lower bound: its overage
    forecast is an upper bound with :data:`SEATS_LOWER_BOUND_NOTE`. Promo eligibility needs
    *promo_eligible* and no run flag ``promo_eligible`` saying no (``False``, ``"false"``). The
    enterprise of enterprise mode shares its capped cost centers' unused caps (see the module
    docstring). *gross_is_list*: one decision for every entity-month, or a mapping / CP-RECON's
    decision pairs keyed ``gross_is_list:<entity>:<YYYY-MM>``. Sorted by entity, month, scenario.
    """
    cell_list = list(cells)
    if not all(isinstance(c, Cell) for c in cell_list):
        raise UsageError("pool_months: cells must be Cell objects")
    lines, lics, conf = _as_lists(cost_lines, licenses, config)
    today_d = _parse_date(today, "today")
    plan_list = list(plans) if plans is not None else None
    if plan_list is not None and not all(isinstance(p, PlanEvidence) for p in plan_list):
        raise UsageError("pool_months: plans must be PlanEvidence objects")
    mode = entity_mode if entity_mode is not None else _infer_mode(cell_list, plan_list)
    _check_mode(mode)
    gil = _gross_is_list_arg(gross_is_list)
    capped = capped_cost_centers(conf)
    policies = capped_policies(conf)
    flags = run_flags(conf)
    eligible = bool(promo_eligible) and not _flag_false(flags.get("promo_eligible"))
    modes = billing_modes(lines, conf, lics)
    lag = _facts.copilot_report_lag_days()
    by_em: dict[tuple[str, str], list[Cell]] = defaultdict(list)
    for c in cell_list:
        by_em[(c.entity_id, c.month)].append(c)
    evidence: dict[tuple[str, str], PlanEvidence] = {}
    census_by_month: dict[str, dict[str, _Census]] = {}
    inputs_by_month: dict[str, _Inputs] = {}

    def census_of(month: str) -> dict[str, _Census]:
        if month not in census_by_month:
            inp = _inputs(lines, lics, conf, month, mode)
            inputs_by_month[month] = inp
            census_by_month[month] = inp.census()
            if plan_list is None:
                for pe in _detect(inp, census_by_month[month]):
                    evidence[(pe.entity_id, month)] = pe
        return census_by_month[month]

    if plan_list is None:
        for month in sorted({m for _, m in by_em} | _data_months(lines, lics, conf)):
            census_of(month)
    else:
        for pe in plan_list:
            key = (pe.entity_id, pe.month)
            if key in evidence and evidence[key] != pe:
                raise UsageError("pool_months: two different plan evidences for one entity-month")
            evidence[key] = pe
    estimates = _estimates(recent_estimates, mode)
    keys = sorted(set(by_em) | set(evidence))
    commons = {(entity, month): _common(entity, month, by_em.get((entity, month), []), today_d,
                                        lag, estimates) for entity, month in keys}
    out: list[PoolMonth] = []
    for entity, month in keys:
        census = census_of(month)
        shared = (_shared(month, commons, inputs_by_month[month], census, capped)
                  if mode == "enterprise" and entity == "enterprise" else None)
        out.extend(_entity_month(
            entity, month, commons[(entity, month)], evidence.get((entity, month)),
            census.get(entity), capped=capped, policies=policies,
            billing_mode=modes.get(entity, "unknown"), eligible=eligible,
            gross_is_list=_gross_is_list_for(gil, entity, month), shared=shared))
    return out


# ---------------------------------------------------------------------------------------------
# conversions to invoice dollars (R11)
# ---------------------------------------------------------------------------------------------


def _consumption_states(pm: PoolMonth) -> tuple[int, int, int]:
    """(low, point, high) month-end pooled consumption of *pm*: the forecast of an open month, else
    the report consumption."""
    f = pm.forecast
    if pm.finality == "open" and f is not None and f.nano is not None:
        lo = f.low_nano if f.low_nano is not None else f.nano
        hi = f.high_nano if f.high_nano is not None else f.nano
        return lo, f.nano, hi
    return pm.consumed_report_nano, pm.consumed_report_nano, pm.consumed_report_nano


def _realize(saving: int, overage: int, slack: int) -> int:
    if saving >= 0:
        return min(saving, overage)
    if overage > 0:
        return saving
    return -max(0, -saving - slack)


def _scenario_note(pm: PoolMonth) -> str:
    return f"; plan unknown: scenario {pm.plan_scenario}" if pm.plan_scenario else ""


def realize_credit_saving(saving: Figure, pm: PoolMonth) -> tuple[Figure, Figure]:
    """(invoice, headroom) of a list-equivalent credit saving in one pool month (R11, Appendix
    C.P5).

    invoice = ``min(saving, overage)`` — ESTIMATED LIST; headroom = saving − invoice — ESTIMATED
    LIST_EQUIVALENT (never invoice dollars). The overage is the month's observed overage (closed;
    the scenario's overage in a scenario month) or its overage forecast point (open). A negative
    saving (a cost increase) is billed beyond the pool's slack. Ranges follow the saving's range, so
    invoice + headroom == saving at the point, low and high. The invoice is an upper bound when the
    pool is a lower bound or a cap policy is unknown. Both notes name the regime and the scenario;
    an unknown regime makes both unpriced.
    """
    if not isinstance(saving, Figure) or not isinstance(pm, PoolMonth):
        raise ContractViolation("realize_credit_saving: expects a Figure and a PoolMonth")
    if saving.basis not in (Basis.LIST_EQUIVALENT, Basis.LIST):
        raise ContractViolation("realize_credit_saving: a credit saving is LIST_EQUIVALENT")
    scen = _scenario_note(pm)
    if saving.nano is None or pm.regime == "unknown":
        reason = ("unpriced: saving unpriced" if saving.nano is None
                  else "unpriced: pool regime unknown")
        return (unpriced(reason + scen, Basis.LIST),
                unpriced(reason + scen, Basis.LIST_EQUIVALENT))
    upper = pm.seats_source == "report_users"
    if pm.finality == "open" and pm.overage_forecast is not None and (
            pm.overage_forecast.nano is not None):
        overage = pm.overage_forecast.nano
        upper = upper or pm.overage_forecast.upper_bound or pm.capped_policy == "unknown"
        basis_note = f"overage forecast {nano_to_credits_str(overage)} credits"
    else:
        overage = pm.overage_observed_nano
        basis_note = f"overage {nano_to_credits_str(overage)} credits"
    slack = max(0, pm.pool_nano - _consumption_states(pm)[1])
    s_point = saving.nano
    s_low = saving.low_nano if saving.low_nano is not None else s_point
    s_high = saving.high_nano if saving.high_nano is not None else s_point
    inv = [_realize(s, overage, slack) for s in (s_low, s_point, s_high)]
    head = [s - i for s, i in zip((s_low, s_point, s_high), inv, strict=True)]
    ranged = saving.low_nano is not None
    calibration = (saving.calibration if saving.calibration is not Calibration.NA
                   else Calibration.UNCALIBRATED)
    note = (f"pool rule (R11): invoice = min(saving, overage); regime {pm.regime}; {basis_note}"
            f"{scen}")
    invoice = Figure(nano=inv[1], evidence=Evidence.ESTIMATED, basis=Basis.LIST,
                     low_nano=inv[0] if ranged else None, high_nano=inv[2] if ranged else None,
                     calibration=calibration, upper_bound=upper or saving.upper_bound,
                     provenance=saving.provenance, note=note)
    headroom = Figure(nano=head[1], evidence=Evidence.ESTIMATED, basis=Basis.LIST_EQUIVALENT,
                      low_nano=head[0] if ranged else None, high_nano=head[2] if ranged else None,
                      calibration=calibration, upper_bound=saving.upper_bound,
                      provenance=saving.provenance,
                      note=note + "; headroom: list-equivalent pool credits, not invoice dollars")
    return invoice, headroom


def _seat_fee(month_fee: Mapping[str, Decimal], plan: str, effective: str) -> Decimal:
    for key in (plan, effective):
        if key in month_fee:
            fee = _as_decimal(month_fee[key])
            if fee is None:
                raise UsageError("realize_seat_change: month_fee values must be decimals")
            return fee
    return _plan_fact(effective).seat_usd_per_month


def realize_seat_change(pm: PoolMonth, delta: Mapping[str, int], *,
                        month_fee: Mapping[str, Decimal]) -> Figure | None:
    """The monthly invoice saving of a seat change in one pool month: ``−Δfees − Δoverage``
    (Appendix C.P2–P4, P10–P13b), ESTIMATED basis LIST (fees are list prices); unpriced when the
    pool month's regime is ``unknown`` (no pool, or no observed day yet).

    *delta*: plan → change in seats (negative = removal; ``unknown`` seats count under the pool
    month's scenario plan, and raise ``UsageError`` without one). *month_fee*: plan → list price per
    seat-month (plans it lacks use ``core.facts``). The pool changes by Δseats × included credits of
    the month (promo when the pool month has one); the overage is recomputed at the month's
    consumption (open months: the forecast's p10, p50 and p90 give the range; a capped cost center's
    unknown policy spans block and continue). Returns None for ``volume`` / ``azure`` entities
    (savings only at renewal). In promo months a removal can cost more than it saves (a negative
    figure).
    """
    if not isinstance(pm, PoolMonth):
        raise ContractViolation("realize_seat_change: expects a PoolMonth")
    if pm.billing_mode in ("volume", "azure"):
        return None
    fees = _ZERO
    dpool = _ZERO
    promo = pm.promo is not None
    for plan, n in sorted(delta.items()):
        if type(n) is not int:
            raise UsageError("realize_seat_change: seat changes must be ints")
        if abs(n) > _MAX_COUNT:
            raise UsageError("realize_seat_change: seat change out of range")
        if plan not in LICENSE_PLANS:
            raise UsageError("realize_seat_change: unknown plan")
        if n == 0:
            continue
        if plan == "unknown" and pm.plan_scenario is None:
            raise UsageError("realize_seat_change: unknown seats need a scenario pool month")
        effective = pm.plan_scenario if plan == "unknown" else plan
        assert effective is not None
        fees = EXACT_CTX.add(fees, EXACT_CTX.multiply(Decimal(n),
                                                      _seat_fee(month_fee, plan, effective)))
        per_seat, _ = _allowance(effective, pm.month, promo_eligible=promo)
        dpool = EXACT_CTX.add(dpool, EXACT_CTX.multiply(Decimal(n), per_seat))
    if pm.regime == "unknown":     # no pool or no observed day: the overage change is unknown (R2)
        return unpriced(f"unpriced: pool regime unknown{_scenario_note(pm)}", Basis.LIST)
    dfees = decimal_to_nano(fees)
    pool0 = pm.pool_nano
    pool1 = max(0, pool0 + _credits_to_nano(dpool))
    policies = ("block", "continue") if pm.capped_policy == "unknown" else (pm.capped_policy,)
    results = []
    for consumed in _consumption_states(pm):
        row = []
        for policy in policies:
            if policy == "block":
                d_over = 0
            else:
                d_over = max(0, consumed - pool1) - max(0, consumed - pool0)
            row.append(-dfees - d_over)
        results.append(row)
    point = results[1][-1]
    flat = [v for row in results for v in row]
    low, high = min(flat), max(flat)
    note = (f"seat change at list price (fees saved {fmt_usd(-dfees)}); regime {pm.regime}"
            f"{_scenario_note(pm)}; proration, upfront charges and volume/EA pricing not modeled")
    if pm.billing_mode == "unknown":
        note += "; billing mode unknown (volume/azure entities save only at renewal)"
    if pm.capped_policy == "unknown":
        note += "; cap policy unknown: range spans block and continue"
    if pm.seats_source == "report_users":
        note += "; " + SEATS_LOWER_BOUND_NOTE
    ranged = low != high
    return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=Basis.LIST,
                  low_nano=low if ranged else None, high_nano=high if ranged else None,
                  calibration=Calibration.UNCALIBRATED, note=note)

