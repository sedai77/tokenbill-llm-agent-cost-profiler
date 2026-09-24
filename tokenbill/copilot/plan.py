"""GitHub Copilot aggregate plan: cell replay, reach, Shapley and pool conversion (CP-PLAN).

One honest, ranked plan in invoice dollars for the whole billed Copilot usage of one month
(addendum §9.2, §9.3, §11.2; DC3, DC10, DC19, DC20; R7, R11, R17; rulings R-E20, R-E22).

**Players.** The aggregate levers (``core.catalog.COPILOT_LEVERS`` with ``replay="aggregate"``)
linked by Copilot findings — through ``Finding.lever_ids`` or
``levers_for_kind(kind, family="copilot")``. Behavioral and ``replay="none"`` levers never play;
trade-off levers play only with ``include_tradeoffs``; ``copilot.seat_downgrade`` never plays
while a plan is unknown. A player is one canonical aggregate spec (``LeverResult.params``, which
round-trips through ``parse_aggregate_spec``): ``copilot:auto=on@all``, one
``copilot:remap=<t>@model:<m>`` per remap pair whose source model has cells,
``copilot:fast=off@all``, the better of ``copilot:seats_idle=30d@all`` / ``60d@all``,
``copilot:seat_policy=assign_selected`` (``@org:<o>`` when one org is linked, else ``@all``),
``copilot:plan=business@all`` and ``copilot:runner=actions_linux@all``. A linked lever with
nothing to act on (no fast cells, no readable seat counts, …) is not a player; the headline note
names it.

**Transforms** (fixed order of addendum §9.2, per cell of the month): (1) model-policy remap —
the cell's tokens priced at the target's Copilot rates on the cell's date (``Pricer.unit_rates``,
so an aggregate never trips a per-request long-context band), tokens × the ``TOKENIZER_BAND``
high (1.35) as the low-saving bound only when ``tokenizer_same`` is False, the target at standard
speed; (2) fast off — ``speed="fast"`` cells re-priced at standard rates on identical tokens;
(3) Auto default — eligible cells (routing ``direct``, cost type ``ai_credit.user`` /
``ai_credit.direct``, not pseudo ``code_review``) × (1 − 0.1 × reach), the 0.1 being
``1 −`` the ``github.auto`` modifier factor of ``core.facts``; without any ``Auto:`` label in the
month the eligible share of a team is ``1 −`` its metrics ``model:auto`` interaction share
(ESTIMATED, noted); (4) seats — Δseats per entity and plan from seat findings (removable and
seat-policy seats; unknown-assignment seats only as an upper bound with ``needs_eval``;
``volume`` / ``azure`` entities none, with the renewal note); (5) runners — Copilot-workload
Actions lines on larger runners re-priced as the ``larger-runner`` range (point = low = net −
minutes × the ``actions_linux`` rate, high = net).

**Reach** (§9.3): per team, ``1 −`` the share of interactions on editor families the managed
``model`` key does not reach (:data:`EXCLUDED_EDITOR_FAMILIES`: ``jetbrains``; ``visual_studio``,
``xcode``, ``eclipse`` unverified → excluded) among ``ide:*`` + CLI + Copilot-app interactions,
from ``ActivityDay`` counts (``ide:*``, ``cli_requests``, ``app_requests``) or, in aggregate-only
bundles (no ``ActivityDay`` at all), ``ConfigSnapshot(kind="activity_counts")`` rows (``ide:*``,
``cli_requests``, ``app_interactions``); family names through ``core.catalog.editor_family``.
Cloud-agent cells reach 1; a team without activity: point 0, high 1 ("reach unknown").

**Value.** ``invoice_e(S) = fees + overage + direct + Actions`` per billing group (the enterprise
with its capped cost centers, or each organization) with the overage from
``core.pool.overage_total`` and the seat pool change from ``core.pool.pool_credits``;
``v(S) = invoice(∅) − invoice(S)``. Consumption (pooled credits) comes from each entity's
``PoolMonth`` (``consumed_report_nano``; with ``forecast`` and an open month its forecast p10 /
p50 / p90) scaled by the cells' relative change. ``headroom(S)`` (LIST_EQUIVALENT) is the pooled
consumption saving that the pool rule does not turn into invoice dollars at ``S``'s pools —
``ΔC − (overage(C₀, P_S) − overage(C_S, P_S))`` — plus the discount share of direct rows, so a seat
change frees no headroom (Appendix C.P2 overage entity: seat lever invoice 0 and headroom 0).
Direct (org-metered) cells save their net share in dollars.

**Month and unknown pools.** The plan values *month*: with ``forecast`` an open month's forecast
p10 / p50 / p90, without it the month-to-date consumption (noted); direct rows and Actions lines
stay observed. An entity whose ``PoolMonth.regime`` is ``unknown`` (no pool or no observed day)
cannot convert credits: its pooled cells and seats are left out and named in the notes ("unpriced,
not zero"), its direct rows stay (metered dollars); when every entity's pool is unknown and
pooled cells exist, every figure of the plan is unpriced (R2).

**Uncertainty.** Each dimension that varies (consumption level p10 / p50 / p90, an unknown cap
policy block / continue, unknown reach 0 / 1, the tokenizer band, the runner range) spans the
state grid; exact Shapley (≤ 6 players; ``core.shapley.shapley_exact``, largest remainder) or
``shapley_mc`` (200 permutations, seed 0) is played in every state; a figure's point is the point
state and its range the envelope over all states. Projections apply the ``core.catalog.RR_PRIORS``
of the lever class (interval product); the headline sums the projected invoice figures with
comonotone bounds (standalone values are never summed, R7).

**Results** (SPEC §11.2 allowance convention): one ``LeverResult`` per player with basis LIST
(invoice Shapley, group ``copilot``) and — whenever a credit lever (Auto, remap, fast) plays or
any coalition frees headroom — a second per player with basis LIST_EQUIVALENT (pool headroom
Shapley, group ``copilot:pool_headroom``; 0 in an overage entity), which only
``pool_headroom_monthly`` sums. A plan of seat and runner levers alone has no headroom results
(their headroom is 0 by construction). Every figure is ESTIMATED;
notes name the pool regime, the scenario, the reach (JetBrains share) and exclusions.

**Scenarios (R17).** :func:`plan_copilot_scenarios` returns ``(("known", plan),)`` when every
entity's plan is known, else ``(("business", plan_b), ("enterprise", plan_e))`` — the same cells
played on each scenario's ``PoolMonth``s and findings (``plan_scenario`` dim), never averaged or
summed. CP-WIRE maps ``"known"`` to ``CopilotSummary.plan`` and the scenario pairs to
``plans_by_scenario``.

**Seat findings.** The addendum does not pin the evidence attribute names of ``idle-seat`` /
``seat-auto-assign`` / ``plan-mix`` counts, so :func:`seat_counts` reads integer evidence attrs by
normalized name (``removable``, ``team_assigned``, ``auto_assigned``, ``assignment_unknown``,
totals ``n`` / ``count`` / ``seats`` / ``idle``) summed over evidence items, with the plan and
bucket from scope dims or string attrs (tests/v2/copilot_plan/CONTRACT-CHANGE-CP-PLAN-1.md).

Pure and deterministic: no I/O, no floats (``Fraction`` / ``Decimal`` under ``EXACT_CTX``, integer
nano-USD, half-even rounding), identical output for any input order. Additive keywords beyond the
brief's signature: ``plan_copilot(…, config=(), scenario=None)``.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from tokenbill.core import catalog as _catalog
from tokenbill.core import facts as _facts
from tokenbill.core import pool as _pool
from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.evidence import TOKENIZER_BAND
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, unpriced
from tokenbill.core.money import EXACT_CTX, decimal_to_nano
from tokenbill.core.pool import Cell
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    COPILOT_WORKLOADS,
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    PricingContext,
    UsageBuckets,
)
from tokenbill.core.shapley import shapley_exact, shapley_mc
from tokenbill.core.types import ActionPlan, Finding, LeverResult, PoolMonth, UnitRates

__all__ = [
    "AUTO_ELIGIBLE_COST_TYPES",
    "EXCLUDED_EDITOR_FAMILIES",
    "MAX_EXACT_PLAYERS",
    "MC_PERMUTATIONS",
    "MC_SEED",
    "SCENARIO_KNOWN",
    "SeatCounts",
    "TeamReach",
    "plan_copilot",
    "plan_copilot_scenarios",
    "seat_counts",
    "team_reach",
]

#: Editor families the managed ``model`` key does not reach (§9.3): JetBrains (fact-checked) and
#: Visual Studio / Xcode / Eclipse (unverified → excluded).
EXCLUDED_EDITOR_FAMILIES = ("jetbrains", "visual_studio", "xcode", "eclipse")
#: Cost types an Auto default can reach (§9.2 step 3).
AUTO_ELIGIBLE_COST_TYPES = ("ai_credit.user", "ai_credit.direct")
#: Exact Shapley up to this many players, Monte Carlo above (SPEC §11.2 step 5).
MAX_EXACT_PLAYERS = 6
#: Monte Carlo Shapley: seeded permutations.
MC_PERMUTATIONS = 200
MC_SEED = 0
#: Key of the single plan when every entity's plan is known (:func:`plan_copilot_scenarios`).
SCENARIO_KNOWN = "known"

_SCENARIOS = ("business", "enterprise")
_AUTO_ID = "copilot.default_model_auto"
_POLICY_ID = "copilot.model_policy"
_FAST_ID = "copilot.fast_mode_off"
_RECLAIM_ID = "copilot.seat_reclaim"
_SEAT_POLICY_ID = "copilot.seat_policy_selected"
_DOWNGRADE_ID = "copilot.seat_downgrade"
_RUNNER_ID = "copilot.agent_runner_standard"
_SEAT_LEVERS = (_RECLAIM_ID, _SEAT_POLICY_ID, _DOWNGRADE_ID)
_STANDARD_RUNNER = "actions_linux"
_POOLED = "ai_credit.user"
_DIRECT = "ai_credit.direct"
_CLOUD_AGENT_SKU = "coding_agent_ai_credit"
_GROUP = "copilot"
_CREDIT_KINDS = ("auto", "remap", "fast")
_HEADROOM_GROUP = "copilot:pool_headroom"
_MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_ONE_CENT = Decimal("0.01")
_PRICE_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                  "cache_write_other", "cache_write_unknown", "output")
_IDLE_BUCKETS = {"30d": frozenset({"31-90", "none_90d"}), "60d": frozenset({"none_90d"})}
_ACTIVE_BUCKETS = frozenset({"0-7", "8-30"})
_MAX_COUNT = 10**9            # largest seat count read from a finding (a bound, not a fact)
_MAX_NOTE_ENTITIES = 12


# ---------------------------------------------------------------------------------------------
# small exact helpers
# ---------------------------------------------------------------------------------------------


def _round(x: Fraction | int) -> int:
    """Half-even rounding of an exact value to an int (nano)."""
    if isinstance(x, int):
        return x
    q, r = divmod(x.numerator, x.denominator)
    twice = 2 * r
    if twice > x.denominator or (twice == x.denominator and q % 2 == 1):
        q += 1
    return q


def _pct(share: Fraction) -> str:
    """A share as a whole percentage string (half-even), e.g. ``20%``."""
    return f"{_round(share * 100)}%"


def _credits_nano(credits: str) -> int:
    """AI credits (decimal string) → nano-USD at $0.01 per credit, half-even once."""
    try:
        value = Decimal(credits)
    except InvalidOperation:
        raise UsageError("plan_copilot: cell credits must be a decimal string") from None
    return decimal_to_nano(EXACT_CTX.multiply(value, _ONE_CENT))


def _check_month(month: object) -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise UsageError("plan_copilot: month must be a YYYY-MM string")
    if int(month[:4]) < 1:
        raise UsageError("plan_copilot: month out of range")
    return month


def _month_days(month: str) -> tuple[_dt.date, _dt.date]:
    year, mon = int(month[:4]), int(month[5:])
    first = _dt.date(year, mon, 1)
    if mon == 12:
        return first, _dt.date(year, 12, 31)
    return first, _dt.date(year, mon + 1, 1) - _dt.timedelta(days=1)


def _noon_ms(day: _dt.date) -> int:
    return (day - _EPOCH).days * _DAY_MS + _DAY_MS // 2


def _as_list(items: Iterable[object] | None, cls: type, what: str) -> list:
    if items is None:
        return []
    if isinstance(items, (str, bytes)):
        raise UsageError(f"plan_copilot: {what} must be a sequence of {cls.__name__}")
    out = list(items)
    if not all(isinstance(x, cls) for x in out):
        raise UsageError(f"plan_copilot: {what} must be {cls.__name__} records")
    return out


# ---------------------------------------------------------------------------------------------
# delivery reach (addendum §9.3)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamReach:
    """Interactions of one team in the month and the share the managed ``model`` key reaches.

    ``interactions`` = Σ ``ide:*`` + CLI + Copilot-app interactions; ``excluded`` = the part on
    :data:`EXCLUDED_EDITOR_FAMILIES`; ``jetbrains`` = the JetBrains part; ``reach`` = ``1 −
    excluded / interactions`` (None when the team has no interactions: reach unknown)."""

    interactions: int
    excluded: int
    jetbrains: int

    @property
    def reach(self) -> Fraction | None:
        """``1 − excluded / interactions`` or None (unknown)."""
        if self.interactions <= 0:
            return None
        return 1 - Fraction(self.excluded, self.interactions)


def _count_interactions(pairs: Iterable[tuple[str, object]], app_key: str,
                        acc: dict[str, int]) -> None:
    for key, value in pairs:
        if type(value) is not int or value <= 0:
            continue
        if key.startswith("ide:"):
            family = _catalog.editor_family(key)
            acc["total"] += value
            if family in EXCLUDED_EDITOR_FAMILIES:
                acc["excluded"] += value
            if family == "jetbrains":
                acc["jetbrains"] += value
        elif key in ("cli_requests", app_key):
            acc["total"] += value


def team_reach(activity: Iterable[ActivityDay], config: Iterable[ConfigSnapshot] = (), *,
               month: str) -> dict[str | None, TeamReach]:
    """Per team (``None`` = unattributed) interaction counts for Auto reach (§9.3).

    Reads ``ActivityDay`` rows (``ide:*`` counts, ``cli_requests``, ``app_requests``) of *month*
    (every row when none falls in the month); only when there is no ``ActivityDay`` at all, the
    ``activity_counts`` rows of aggregate-only bundles (``ide:<family>`` sums, ``cli_requests``,
    ``app_interactions``; rows of *month*, else every row), summed over organizations."""
    _check_month(month)
    days = _as_list(activity, ActivityDay, "activity")
    snaps = _as_list(config, ConfigSnapshot, "config")
    accs: dict[str | None, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "excluded": 0, "jetbrains": 0})
    if days:
        in_month = [d for d in days if d.date_utc[:7] == month]
        for day in in_month or days:
            _count_interactions(day.counts, "app_requests", accs[day.team])
    else:
        rows = [c for c in snaps if c.kind == "activity_counts"]
        in_month = [c for c in rows if dict(c.attrs).get("month") == month]
        for row in in_month or rows:
            attrs = dict(row.attrs)
            team = attrs.get("team")
            key = team if isinstance(team, str) and team else None
            _count_interactions(row.attrs, "app_interactions", accs[key])
    return {team: TeamReach(a["total"], a["excluded"], a["jetbrains"])
            for team, a in sorted(accs.items(), key=lambda kv: (kv[0] is not None, kv[0] or ""))}


def _auto_shares(activity: Sequence[ActivityDay], month: str) -> dict[str | None, Fraction]:
    """Per team: the ``model:auto`` share of ``model:*`` interactions (metrics), for months whose
    report carries no ``Auto:`` label (§9.2 step 3, VERIFY)."""
    totals: dict[str | None, list[int]] = defaultdict(lambda: [0, 0])
    in_month = [d for d in activity if d.date_utc[:7] == month]
    for day in in_month or activity:
        for key, value in day.counts:
            if key.startswith("model:") and type(value) is int and value > 0:
                totals[day.team][1] += value
                if key == "model:auto":
                    totals[day.team][0] += value
    return {team: Fraction(auto, total) for team, (auto, total) in totals.items() if total > 0}


# ---------------------------------------------------------------------------------------------
# seat counts from seat findings (CONTRACT-CHANGE-CP-PLAN-1)
# ---------------------------------------------------------------------------------------------

_COUNT_ALIASES: Mapping[str, str] = {
    "removable": "removable", "direct": "removable", "direct_assigned": "removable",
    "removable_direct": "removable",
    "team_assigned": "team_assigned", "via_team": "team_assigned",
    "assigned_via_team": "team_assigned",
    "auto_assigned": "auto_assigned", "auto": "auto_assigned", "assign_all": "auto_assigned",
    "assignment_unknown": "unknown", "unknown_assignment": "unknown", "unknown": "unknown",
    "unknown_assigned": "unknown",
    "idle": "total", "total": "total", "n": "total", "count": "total", "seats": "total",
    "downgradable": "total", "enterprise": "enterprise",
}
_CLASS_NAMES: Mapping[str, str] = {
    "removable": "removable", "direct": "removable", "team_assigned": "team_assigned",
    "team": "team_assigned", "auto_assigned": "auto_assigned", "auto": "auto_assigned",
    "unknown": "unknown", "assignment_unknown": "unknown", "unknown_assignment": "unknown",
}


def _norm_key(key: str) -> str:
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    for prefix in ("n_", "num_", "count_", "seats_"):
        if k.startswith(prefix) and len(k) > len(prefix):
            k = k[len(prefix):]
    for suffix in ("_seats", "_count", "_n"):
        if k.endswith(suffix) and len(k) > len(suffix):
            k = k[:-len(suffix)]
    return k


@dataclass(frozen=True, slots=True)
class SeatCounts:
    """Seat counts one seat finding carries (evidence attrs summed over items; 0 when absent)."""

    removable: int = 0
    team_assigned: int = 0
    auto_assigned: int = 0
    unknown: int = 0
    total: int = 0
    enterprise: int = 0
    plan: str | None = None
    bucket: str | None = None

    @property
    def classified(self) -> bool:
        """True when any assignment class was named."""
        return bool(self.removable or self.team_assigned or self.auto_assigned or self.unknown)


def seat_counts(finding: Finding) -> SeatCounts:
    """The seat counts of a seat finding (``idle-seat``, ``seat-auto-assign``, ``plan-mix``).

    Integer evidence attrs are summed over the finding's evidence items by normalized name
    (lower case; ``n_`` / ``num_`` / ``count_`` / ``seats_`` prefixes and ``_seats`` /
    ``_count`` / ``_n`` suffixes dropped): ``removable``, ``team_assigned``, ``auto_assigned``,
    ``assignment_unknown`` (also ``unknown``), totals ``n`` / ``count`` / ``seats`` / ``idle`` /
    ``total`` / ``downgradable`` and ``enterprise``; an item with a string attr ``assignment`` /
    ``class`` naming a class puts its total into that class. The plan (``business`` |
    ``enterprise`` | ``unknown``) and the activity bucket come from the scope dims ``plan`` /
    ``bucket`` or string attrs of those names. Values that are not ints ≥ 0 are ignored; counts
    above 10**9 raise ``UsageError``."""
    if not isinstance(finding, Finding):
        raise UsageError("seat_counts: expects a Finding")
    sums = {"removable": 0, "team_assigned": 0, "auto_assigned": 0, "unknown": 0, "total": 0,
            "enterprise": 0}
    dims = dict(finding.scope.dims)
    plan = dims.get("plan")
    bucket = dims.get("bucket")
    for item in finding.evidence:
        attrs = item.attrs if isinstance(item.attrs, tuple) else ()
        item_class: str | None = None
        item_total = 0
        named = False
        for key, value in attrs:
            if not isinstance(key, str):
                continue
            nk = _norm_key(key)
            if isinstance(value, str):
                text = value.strip().lower()
                if nk == "plan" and plan is None and text in ("business", "enterprise", "unknown"):
                    plan = text
                elif nk == "bucket" and bucket is None and text:
                    bucket = value.strip()
                elif nk in ("assignment", "class", "kind"):
                    item_class = _CLASS_NAMES.get(_norm_key(text))
                continue
            if type(value) is not int or value < 0:
                continue
            if value > _MAX_COUNT:
                raise UsageError("seat_counts: seat count out of range")
            target = _COUNT_ALIASES.get(nk)
            if target is None:
                continue
            if target == "total":
                item_total += value
            else:
                sums[target] += value
                named = True
        if item_class is not None and not named:
            sums[item_class] += item_total
        else:
            sums["total"] += item_total
    if plan not in ("business", "enterprise", "unknown"):
        plan = None
    return SeatCounts(plan=plan, bucket=bucket, **sums)


# ---------------------------------------------------------------------------------------------
# cells → units
# ---------------------------------------------------------------------------------------------


@dataclass
class _Unit:
    """Cells of one entity that every transform treats alike (their sums)."""

    entity: str
    pooled: bool
    team: str | None
    cloud: bool                 # cloud-agent cells: reach 1
    auto: bool                  # eligible for the Auto default
    remap_model: str | None     # source model of a priced remap pair
    fast: bool                  # a priced fast-mode cell
    value: int = 0              # Σ credits × $0.01 (nano, list-equivalent)
    d_remap: int = 0            # Σ (price at source − price at target), band point
    d_remap_hi: int = 0         # the same with the target's tokens × the band high
    d_fast: int = 0             # Σ (fast − standard) on identical tokens
    net: int = 0                # direct units: Σ net and Σ gross (invoice share)
    gross: int = 0


@dataclass
class _Pricing:
    """Cached unit rates and the cells a transform could not price."""

    pricer: Pricer
    compliance: str | None
    cache: dict[tuple, UnitRates | None] = field(default_factory=dict)
    unpriced: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def rates(self, model: str, speed: str, routing: str, pooled: bool,
              ts_list: Sequence[int]) -> UnitRates | None:
        for ts in ts_list:
            key = (model, speed, routing, pooled, ts)
            if key not in self.cache:
                try:
                    ctx = PricingContext(
                        provider="github", channel="github_copilot", model=model,
                        model_raw=model, speed=speed,
                        billing_path="copilot_pool" if pooled else "copilot_direct",
                        routing=routing, compliance=self.compliance)
                    self.cache[key] = self.pricer.unit_rates(ctx, ts_ms=ts)
                except TokenbillError:
                    self.cache[key] = None
            got = self.cache[key]
            if got is not None:
                return got
        return None


def _tokens_nano(rates: UnitRates, usage: UsageBuckets, band: Fraction = Fraction(1)) -> int:
    total = 0
    for name in _PRICE_BUCKETS:
        qty = getattr(usage, name)
        if qty:
            if band != 1:
                qty = _round(qty * band)
            total += rates.bucket_nano(name, qty)
    if usage.web_search_requests:
        total += rates.bucket_nano("web_search", usage.web_search_requests)
    return total


def _cell_ts(cell: Cell) -> list[int]:
    """Pricing dates: the cell's day, or (month grain) the month's last then first day."""
    if cell.date_utc is not None:
        return [_noon_ms(_dt.date.fromisoformat(cell.date_utc))]
    first, last = _month_days(cell.month)
    return [_noon_ms(last), _noon_ms(first)]


def _is_cloud(cell: Cell) -> bool:
    return (cell.pseudo == "cloud_agent" or cell.workload == "copilot_cloud_agent"
            or cell.sku == _CLOUD_AGENT_SKU)


# ---------------------------------------------------------------------------------------------
# players
# ---------------------------------------------------------------------------------------------


@dataclass
class _Player:
    pid: str                                   # canonical aggregate spec (unique)
    lever: _catalog.LeverDef
    kind: str                                  # auto | remap | fast | seats | runner
    finding_ids: tuple[str, ...]
    remap_model: str | None = None
    tokenizer_same: bool = True
    seat_delta: dict[tuple[str, str], int] = field(default_factory=dict)
    dpool: dict[str, int] = field(default_factory=dict)     # entity → Δpool nano
    fee_saving: int = 0                                     # −Δfees nano
    upper_bound: bool = False
    needs_eval: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _State:
    level: int = 1              # 0 low, 1 point, 2 high consumption
    cap: str = "continue"       # resolution of unknown cap policies
    reach_high: bool = False    # unknown reach at 1 (else 0)
    band_high: bool = False     # tokenizer band high for remaps with different tokenizers
    runner_high: bool = False   # runner range high


_POINT = _State()


@dataclass
class _Ent:
    pm: PoolMonth
    levels: tuple[int, int, int]
    group: str
    cap: int | None
    policy: str | None
    pooled0: int = 0


# ---------------------------------------------------------------------------------------------
# the game
# ---------------------------------------------------------------------------------------------


class _Game:
    """The cell game of one scenario: ``evaluate(S, state) -> (v, headroom)`` in int nano."""

    def __init__(self, units: list[_Unit], ents: dict[str, _Ent],
                 runner_lines: list[tuple[int, int]], auto_discount: Fraction,
                 reach: Mapping[str | None, Fraction | None],
                 auto_scale: Mapping[str | None, Fraction]) -> None:
        self.units = units
        self.ents = ents
        self.runner_lines = runner_lines
        self.players: dict[str, _Player] = {}
        self.groups: dict[str, list[str]] = defaultdict(list)
        for name in sorted(ents):
            self.groups[ents[name].group].append(name)
        for e in ents.values():
            e.pooled0 = 0
        for u in units:
            if u.pooled:
                ents[u.entity].pooled0 += u.value
        # Auto factor per unit at reach point / high
        self._auto_factor: list[tuple[Fraction, Fraction]] = []
        for u in units:
            if u.cloud:
                lo = hi = Fraction(1)
            else:
                r = reach.get(u.team)
                lo = r if r is not None else Fraction(0)
                hi = r if r is not None else Fraction(1)
            s = auto_scale.get(u.team, Fraction(1))
            self._auto_factor.append((1 - auto_discount * lo * s, 1 - auto_discount * hi * s))
        # evaluation units: pooled units that every transform treats alike (same entity, remap
        # model, fast flag, eligibility and Auto factors) merged across teams; direct units kept
        # apart (their net / gross share is per unit)
        merged: dict[tuple, _Unit] = {}
        self._eval: list[tuple[_Unit, tuple[Fraction, Fraction]]] = []
        for u, factor in zip(units, self._auto_factor, strict=True):
            if not u.pooled:
                self._eval.append((u, factor))
                continue
            key = (u.entity, u.remap_model or "", u.fast, u.auto, factor)
            m = merged.get(key)
            if m is None:
                m = merged[key] = _Unit(u.entity, True, None, u.cloud, u.auto, u.remap_model,
                                        u.fast)
                self._eval.append((m, factor))
            m.value += u.value
            m.d_remap += u.d_remap
            m.d_remap_hi += u.d_remap_hi
            m.d_fast += u.d_fast
        self._memo: dict[tuple[frozenset[str], _State], tuple[int, int]] = {}
        self._sums_memo: dict[tuple, tuple[dict[str, Fraction], Fraction, Fraction]] = {}
        self._ov_memo: dict[tuple, int] = {}

    def add(self, player: _Player) -> None:
        self.players[player.pid] = player

    def _cell_sums(self, active: Sequence[_Player], st: _State
                   ) -> tuple[dict[str, Fraction], Fraction, Fraction]:
        """Transformed cells of a coalition: pooled value per entity, the direct rows' invoice
        and discount savings. Only the band and reach dimensions of *st* matter (memoized)."""
        remaps = frozenset(p.remap_model for p in active if p.kind == "remap")
        fast = any(p.kind == "fast" for p in active)
        auto = any(p.kind == "auto" for p in active)
        key = (remaps, fast, auto, st.band_high, st.reach_high)
        got = self._sums_memo.get(key)
        if got is not None:
            return got
        pooled: dict[str, Fraction] = defaultdict(Fraction)
        direct_inv = Fraction(0)
        direct_disc = Fraction(0)
        pick = 1 if st.reach_high else 0
        for u, factor in self._eval:
            v: int = u.value
            if u.remap_model is not None and u.remap_model in remaps:
                v -= u.d_remap_hi if st.band_high else u.d_remap
            elif fast and u.fast:
                v -= u.d_fast
            v = max(v, 0)
            val = Fraction(v)
            if auto and u.auto:
                val *= factor[pick]
            if u.pooled:
                pooled[u.entity] += val
            else:
                saved = u.value - val
                if u.gross > 0:
                    direct_inv += saved * u.net / u.gross
                    direct_disc += saved * (u.gross - u.net) / u.gross
                else:
                    direct_inv += saved
        got = (dict(pooled), direct_inv, direct_disc)
        self._sums_memo[key] = got
        return got

    def _overage(self, group: str, cons: Mapping[str, int], pools: Mapping[str, int],
                 cap: str) -> int:
        key = (group, cap, tuple(sorted(cons.items())), tuple(sorted(pools.items())))
        got = self._ov_memo.get(key)
        if got is None:
            caps: dict[str, int] = {}
            policies: dict[str, str] = {}
            for name in cons:
                ent = self.ents[name]
                if ent.cap is not None:
                    caps[name] = ent.cap
                    policies[name] = (ent.policy if ent.policy in ("block", "continue")
                                      else cap)
            low, _high = _pool.overage_total(cons, pools, caps, policies=policies)
            got = low
            self._ov_memo[key] = got
        return got

    def evaluate(self, coalition: frozenset[str], st: _State) -> tuple[int, int]:
        key = (coalition, st)
        got = self._memo.get(key)
        if got is not None:
            return got
        active = [self.players[p] for p in sorted(coalition)]
        pooled, direct_inv, direct_disc = self._cell_sums(active, st)
        dpool: dict[str, int] = defaultdict(int)
        fee_saving = 0
        runner = 0
        for p in active:
            for name, d in p.dpool.items():
                dpool[name] += d
            fee_saving += p.fee_saving
            if p.kind == "runner":
                runner += sum(hi if st.runner_high else lo for lo, hi in self.runner_lines)
        value = Fraction(fee_saving + runner) + direct_inv
        headroom = direct_disc
        for group, members in sorted(self.groups.items()):
            cons0: dict[str, int] = {}
            cons_s: dict[str, int] = {}
            pools0: dict[str, int] = {}
            pools_s: dict[str, int] = {}
            for name in members:
                ent = self.ents[name]
                level = ent.levels[st.level]
                cons0[name] = level
                if ent.pooled0 > 0:
                    cons_s[name] = _round(Fraction(level) * pooled.get(name, Fraction(0))
                                          / ent.pooled0)
                else:
                    cons_s[name] = level
                pools0[name] = ent.pm.pool_nano
                pools_s[name] = max(0, ent.pm.pool_nano + dpool.get(name, 0))
            ov00 = self._overage(group, cons0, pools0, st.cap)
            ov_ss = self._overage(group, cons_s, pools_s, st.cap)
            ov_0s = self._overage(group, cons0, pools_s, st.cap)
            value += ov00 - ov_ss
            headroom += sum(cons0[n] - cons_s[n] for n in members) - (ov_0s - ov_ss)
        result = (_round(value), _round(headroom))
        self._memo[key] = result
        return result


# ---------------------------------------------------------------------------------------------
# building the game
# ---------------------------------------------------------------------------------------------


def _levels(pm: PoolMonth, forecast: bool) -> tuple[tuple[int, int, int], bool]:
    """(low, point, high) month-end pooled consumption and whether a forecast was used."""
    f = pm.forecast
    if forecast and pm.finality == "open" and f is not None and f.nano is not None:
        lo = f.low_nano if f.low_nano is not None else f.nano
        hi = f.high_nano if f.high_nano is not None else f.nano
        return (lo, f.nano, hi), True
    c = pm.consumed_report_nano
    return (c, c, c), False


def _billing_groups(names: Sequence[str]) -> dict[str, str]:
    """Entity → billing group: the enterprise with its capped cost centers, each organization
    alone (org mode; cost centers join the only organization, else stand alone)."""
    orgs = [n for n in names if n.startswith("org:")]
    out: dict[str, str] = {}
    for n in names:
        if n == "enterprise" or n.startswith("org:"):
            out[n] = n
        elif "enterprise" in names:
            out[n] = "enterprise"
        elif len(orgs) == 1:
            out[n] = orgs[0]
        else:
            out[n] = n
    return out


@dataclass
class _Context:
    """Everything one scenario's plan needs, validated and indexed."""

    month: str
    scenario: str | None
    pools: dict[str, PoolMonth]
    excluded: list[str]
    cells: list[Cell]
    findings: list[Finding]
    lines: list[CostLine]
    activity: list[ActivityDay]
    config: list[ConfigSnapshot]
    pricer: Pricer
    include_tradeoffs: bool
    forecast: bool
    notes: list[str] = field(default_factory=list)


def _is_copilot_finding(f: Finding) -> bool:
    dims = f.scope.dims
    return ("product", "copilot") in dims or f.detector_id.startswith("copilot.")


def _scenario_findings(findings: Sequence[Finding], scenario: str | None) -> list[Finding]:
    out = []
    for f in findings:
        if not _is_copilot_finding(f):
            continue
        fs = dict(f.scope.dims).get("plan_scenario")
        if fs is not None and fs != scenario:
            continue
        out.append(f)
    return sorted(out, key=lambda f: (f.finding_id, f.kind))


def _units(ctx: _Context, pricing: _Pricing, remap_targets: Mapping[str, tuple[str, bool]]
           ) -> tuple[list[_Unit], int, int]:
    """Cells → units: pooled cells of the included entities and direct (org-metered) cells of
    every entity with a pool month (their dollars do not depend on the pool). Returns the units,
    the count of cells used and the count of pooled cells of excluded entities (unpriced)."""
    band_hi = Fraction(TOKENIZER_BAND.value[1])  # type: ignore[index]
    excluded = frozenset(ctx.excluded)
    acc: dict[tuple, _Unit] = {}
    used = 0
    unconverted = 0
    for cell in ctx.cells:
        if cell.cost_type not in (_POOLED, _DIRECT):
            continue
        pooled = cell.cost_type == _POOLED
        if cell.entity_id in excluded and pooled:
            unconverted += 1
            continue
        if cell.entity_id not in ctx.pools and cell.entity_id not in excluded:
            continue
        used += 1
        value = _credits_nano(cell.credits)
        model = cell.model
        routing = cell.routing if cell.routing in ("direct", "auto") else "direct"
        ts_list = _cell_ts(cell)
        own = None
        d_remap = d_remap_hi = d_fast = 0
        remap_model: str | None = None
        is_fast = False
        pair = remap_targets.get(model) if model and cell.pseudo is None else None
        if pair is not None or (cell.speed == "fast" and model and cell.pseudo is None):
            own = pricing.rates(model, cell.speed, routing, pooled, ts_list)
        if pair is not None:
            target, same = pair
            tgt = pricing.rates(target, "standard", routing, pooled, ts_list)
            if own is None or tgt is None:
                pricing.unpriced[f"remap {model}"] += 1
            else:
                base = _tokens_nano(own, cell.usage)
                d_remap = base - _tokens_nano(tgt, cell.usage)
                d_remap_hi = (base - _tokens_nano(tgt, cell.usage, band_hi) if not same
                              else d_remap)
                remap_model = model
        if cell.speed == "fast" and model and cell.pseudo is None:
            std = pricing.rates(model, "standard", routing, pooled, ts_list)
            if own is None or std is None:
                pricing.unpriced[f"fast {model}"] += 1
            else:
                d_fast = _tokens_nano(own, cell.usage) - _tokens_nano(std, cell.usage)
                is_fast = True
        cloud = _is_cloud(cell)
        eligible = (cell.routing == "direct" and cell.cost_type in AUTO_ELIGIBLE_COST_TYPES
                    and cell.pseudo != "code_review")
        key = (cell.entity_id, pooled, cell.team, cloud, eligible, remap_model, is_fast)
        unit = acc.get(key)
        if unit is None:
            unit = acc[key] = _Unit(cell.entity_id, pooled, cell.team, cloud, eligible,
                                    remap_model, is_fast)
        unit.value += value
        unit.d_remap += d_remap
        unit.d_remap_hi += d_remap_hi
        unit.d_fast += d_fast
        unit.net += cell.net_nano
        unit.gross += value
    order = sorted(acc, key=lambda k: tuple((v is not None, "" if v is None else str(v))
                                            for v in k))
    return [acc[k] for k in order], used, unconverted


def _entity_for(dims: Mapping[str, str], pools: Mapping[str, PoolMonth]) -> str | None:
    """The pool entity a seat finding speaks for: its ``entity`` dim, its ``org`` dim in org mode,
    else the enterprise, else the only entity (None when none applies)."""
    ent = dims.get("entity")
    if ent is not None:
        return ent if ent in pools else None
    org = dims.get("org")
    if org is not None and f"org:{org}" in pools:
        return f"org:{org}"
    if "enterprise" in pools:
        return "enterprise"
    if len(pools) == 1:
        return next(iter(pools))
    return None


def _scope_match(spec: _catalog.AggregateSpec, dims: Mapping[str, str], entity: str) -> bool:
    if spec.scope == "all":
        return True
    kind, _, value = spec.scope.partition(":")
    if kind == "entity":
        return entity == value
    if kind == "org":
        return dims.get("org") == value or entity == f"org:{value}"
    if kind == "team":
        return dims.get("team") == value
    return False


def _known_plan(pm: PoolMonth) -> str | None:
    plans = {p for p, n in pm.seats if p in ("business", "enterprise") and Decimal(n) > 0}
    return next(iter(plans)) if len(plans) == 1 else None


def _seat_player(ctx: _Context, lever: _catalog.LeverDef, spec: _catalog.AggregateSpec,
                 linked: Sequence[Finding], pools: Mapping[str, PoolMonth]) -> _Player:
    """Δseats of one seat lever candidate from its linked findings."""
    pid = _catalog.to_aggregate_spec(spec)
    p = _Player(pid, lever, "seats", tuple(sorted({f.finding_id for f in linked})))
    skipped: list[str] = []
    unknown_seats = 0
    renewal = _renewal_dates(ctx.config)
    blocked: set[str] = set()
    for f in linked:
        dims = dict(f.scope.dims)
        entity = _entity_for(dims, pools)
        if entity is None:
            skipped.append("seat finding without a known pool entity")
            continue
        if not _scope_match(spec, dims, entity):
            continue
        counts = seat_counts(f)
        pm = pools[entity]
        plan = counts.plan
        if plan in (None, "unknown"):
            plan = ctx.scenario if ctx.scenario is not None else _known_plan(pm)
        if lever.lever_id == _RECLAIM_ID:
            if f.kind != "idle-seat":
                continue
            bucket = counts.bucket
            threshold = spec.value
            if bucket in _ACTIVE_BUCKETS:
                continue
            if bucket is not None and bucket not in _IDLE_BUCKETS.get(threshold, frozenset()):
                continue
            if bucket is None and threshold != "30d":
                continue
            if counts.classified:
                n_rem, n_unk = counts.removable, counts.unknown
            else:
                n_rem, n_unk = 0, counts.total
            n = n_rem + n_unk
            deltas = {plan: -n} if n else {}
        elif lever.lever_id == _SEAT_POLICY_ID:
            if f.kind != "seat-auto-assign":
                continue
            n = counts.auto_assigned or counts.total or counts.removable
            n_unk = 0
            deltas = {plan: -n} if n else {}
        else:  # seat downgrade (plan-mix): Enterprise seats → Business
            if f.kind != "plan-mix":
                continue
            n = counts.enterprise or counts.total
            n_unk = 0
            plan = "enterprise"
            deltas = {"enterprise": -n, "business": n} if n else {}
        if not deltas:
            skipped.append(f"{f.kind}: no seat counts in the finding's evidence")
            continue
        if plan not in ("business", "enterprise"):
            skipped.append(f"{f.kind}: seat plan unknown")
            continue
        if pm.billing_mode in ("volume", "azure"):
            blocked.add(entity)
            continue
        unknown_seats += n_unk
        for pl, d in deltas.items():
            p.seat_delta[(entity, pl)] = p.seat_delta.get((entity, pl), 0) + d
    for entity in sorted(blocked):
        when = renewal.get(entity)
        p.notes.append(f"{entity}: billing mode {pools[entity].billing_mode} - seat savings only "
                       f"at renewal{f' ({when})' if when else ''}; value 0 this month")
    if unknown_seats:
        p.upper_bound = True
        p.needs_eval = True
        p.notes.append(f"{unknown_seats} seats with unknown assignment counted as an upper bound: "
                       "assumes direct assignment in an assign_selected org; confirm with the "
                       "seats API assigning_team")
    for reason in sorted(set(skipped)):
        p.notes.append(f"not counted: {reason}")
    _price_seats(p, ctx.month, pools)
    return p


def _price_seats(p: _Player, month: str, pools: Mapping[str, PoolMonth]) -> None:
    """Δpool (``core.pool.pool_credits``) and −Δfees (list seat prices) of a seat player."""
    plans = _facts.copilot_plans()
    fee = Decimal(0)
    parts: list[str] = []
    for (entity, plan), n in sorted(p.seat_delta.items()):
        if n == 0:
            continue
        pm = pools[entity]
        credits, _promo = _pool.pool_credits({plan: Decimal(abs(n))}, month,
                                             promo_eligible=pm.promo is not None)
        nano = decimal_to_nano(EXACT_CTX.multiply(credits, _ONE_CENT))
        p.dpool[entity] = p.dpool.get(entity, 0) + (nano if n > 0 else -nano)
        fee = EXACT_CTX.add(fee, EXACT_CTX.multiply(Decimal(n), plans[plan].seat_usd_per_month))
        parts.append(f"{entity} {plan} {n:+d}")
    p.fee_saving = -decimal_to_nano(fee)
    if parts:
        p.notes.append("seats " + ", ".join(parts) + " at list seat prices (proration, upfront "
                       "charges and volume/EA pricing not modeled; effective next month, at "
                       "unchanged use)")


def _renewal_dates(config: Sequence[ConfigSnapshot]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _pool.run_flags(config).items():
        if key.startswith("renewal_date.") and isinstance(value, str):
            out[key[len("renewal_date."):]] = value
    return out


def _runner_lines(ctx: _Context) -> tuple[list[tuple[int, int]], int]:
    """(low, high) saving per Copilot-workload Actions line on a larger runner; lines without
    minutes count low 0."""
    std = _catalog.runner_rate(_STANDARD_RUNNER)
    out: list[tuple[int, int]] = []
    no_minutes = 0
    keyed = sorted(ctx.lines, key=lambda ln: (ln.date_utc, ln.line_id))
    for line in keyed:
        if line.cost_type != "actions" or line.date_utc[:7] != ctx.month:
            continue
        if line.workload not in COPILOT_WORKLOADS:
            continue
        rate = _catalog.runner_rate(line.sku)
        if rate is None or rate.runner_class == "standard":
            continue
        net = max(0, line.amount_nano)
        low = 0
        # CostLine.quantity is a validated finite decimal string (minutes for Actions lines)
        minutes = Decimal(line.quantity) if line.quantity is not None else None
        if minutes is not None and minutes >= 0 and std is not None:
            std_cost = decimal_to_nano(EXACT_CTX.multiply(minutes, std.usd_per_minute))
            low = max(0, net - std_cost)
        else:
            no_minutes += 1
        out.append((low, net))
    return out, no_minutes


def _linked_levers(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    copilot_ids = {lv.lever_id for lv in _catalog.COPILOT_LEVERS}
    linked: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        ids = {lid for lid in f.lever_ids if lid in copilot_ids}
        ids |= {lv.lever_id for lv in _catalog.levers_for_kind(f.kind, family="copilot")}
        for lid in sorted(ids):
            linked[lid].append(f)
    return linked


def _remap_pairs() -> list[tuple[str, str, bool, str]]:
    """(source, target, tokenizer_same, spec) of the model-policy grid."""
    out = []
    for spec in _catalog.AGGREGATE_GRIDS.get(_POLICY_ID, ()):
        parsed = _catalog.parse_aggregate_spec(spec)
        source = parsed.scope.partition(":")[2]
        fact = _catalog.copilot_remap(source)
        same = fact[1] if fact is not None else True
        out.append((source, parsed.value, same, spec))
    return out


def _auto_discount() -> Fraction:
    for m in _facts.copilot_modifiers():
        if m.modifier_id == "github.auto" and m.factor is not None:
            return 1 - Fraction(m.factor)
    raise UsageError(  # pragma: no cover - core.facts always carries github.auto
        "plan_copilot: the github.auto modifier is missing from core.facts")


# ---------------------------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------------------------


@dataclass
class _Triple:
    point: int
    low: int
    high: int


def _envelope(values: Mapping[_State, int]) -> _Triple:
    return _Triple(values[_POINT], min(values.values()), max(values.values()))


def _fig(t: _Triple, basis: Basis, note: str, *, upper: bool = False) -> Figure:
    ranged = not (t.low == t.point == t.high)
    return Figure(nano=t.point, evidence=Evidence.ESTIMATED, basis=basis,
                  low_nano=t.low if ranged else None, high_nano=t.high if ranged else None,
                  calibration=Calibration.UNCALIBRATED, upper_bound=upper, note=note)


def _project(t: _Triple, lever_class: str) -> _Triple:
    priors = _catalog.RR_PRIORS.get(lever_class)
    if priors is None:   # pragma: no cover - behavioral levers never play (Build 1)
        raise UsageError(f"plan_copilot: lever class {lever_class!r} is never projected")
    p10, p50, p90 = (Fraction(x) for x in priors)
    point = _round(t.point * p50)
    cands = [_round(x * r) for x in (t.low, t.high) for r in (p10, p90)] + [point]
    return _Triple(point, min(cands), max(cands))


def _prior_note(lever_class: str, proj: _Triple) -> str:
    """The realization-prior label of a projected figure (SPEC §11.2 #8)."""
    p10, p50, p90 = _catalog.RR_PRIORS[lever_class] or (Decimal(0),) * 3
    text = f"; x realization prior {lever_class} p10/p50/p90 = {p10}/{p50}/{p90}"
    if proj.low < 0 < proj.high:
        text += " (range crosses zero: the p50 is a design judgment)"
    return text


def _states(game: _Game, players: Sequence[_Player], ents: Mapping[str, _Ent]) -> list[_State]:
    levels = [1]
    if any(e.levels[0] != e.levels[1] or e.levels[2] != e.levels[1] for e in ents.values()):
        levels = [1, 0, 2]
    caps = ["continue"]
    if any(e.cap is not None and e.policy not in ("block", "continue") for e in ents.values()):
        caps = ["continue", "block"]
    reach = [False]
    auto = any(p.kind == "auto" for p in players)
    if auto and any(u.auto and f[0] != f[1] for u, f in zip(game.units, game._auto_factor,
                                                             strict=True)):
        reach = [False, True]
    band = [False]
    remaps = {p.remap_model for p in players if p.kind == "remap"}
    if any(u.remap_model in remaps and u.d_remap != u.d_remap_hi for u in game.units):
        band = [False, True]
    runner = [False]
    if any(p.kind == "runner" for p in players) and any(lo != hi for lo, hi in game.runner_lines):
        runner = [False, True]
    return [_State(level=lv, cap=c, reach_high=r, band_high=b, runner_high=rn)
            for lv, c, r, b, rn in itertools.product(levels, caps, reach, band, runner)]


def _shapley(pids: Sequence[str], fn: Callable[[frozenset[str]], int]
             ) -> tuple[dict[str, int], dict[str, int]]:
    if len(pids) <= MAX_EXACT_PLAYERS:
        return shapley_exact(pids, fn), {}
    return shapley_mc(pids, fn, permutations=MC_PERMUTATIONS, seed=MC_SEED)


def _empty_plan(ctx: _Context, n_cells: int, note: str) -> ActionPlan:
    zero_list = _fig(_Triple(0, 0, 0), Basis.LIST, note)
    zero_pool = _fig(_Triple(0, 0, 0), Basis.LIST_EQUIVALENT,
                     _scenario_prefix(ctx.scenario) + "no pool credits freed; list-equivalent "
                     "pool headroom, not invoice dollars")
    return ActionPlan(joint_saving=zero_list, headline_monthly=zero_list,
                      allowance_headroom_monthly=None, levers=(), groups=(),
                      method="shapley-exact", shapley_se=(), sample=_sample(ctx, n_cells),
                      observed_rr=(), pool_headroom_monthly=zero_pool)


def _sample(ctx: _Context, n_cells: int) -> str:
    text = f"copilot cells, {n_cells} cells, month {ctx.month}"
    if ctx.scenario is not None:
        text += f", scenario {ctx.scenario}"
    return text


def _scenario_prefix(scenario: str | None) -> str:
    if scenario is None:
        return ""
    return (f"If {scenario.capitalize()}: plan unknown, every unknown seat counted as "
            f"{scenario.capitalize()} (scenario {scenario}; R17, never merged with the other "
            f"scenario); ")


def _regime_note(ents: Mapping[str, _Ent]) -> str:
    items = [f"{name} {e.pm.regime}" for name, e in sorted(ents.items())]
    if len(items) > _MAX_NOTE_ENTITIES:
        items = items[:_MAX_NOTE_ENTITIES] + [f"… {len(ents) - _MAX_NOTE_ENTITIES} more"]
    return "regime " + ", ".join(items) if items else "no pool entity"


def _reach_note(game: _Game, reach: Mapping[str | None, TeamReach]) -> str:
    """Credit-weighted reach of the Auto default at the point and the JetBrains interaction
    share (team names are never printed)."""
    known = [r for r in reach.values() if r.interactions > 0]
    inter = sum(r.interactions for r in known)
    parts = []
    if inter:
        jet = sum(r.jetbrains for r in known)
        parts.append(f"JetBrains {_pct(Fraction(jet, inter))} of interactions (managed model "
                     f"does not reach JetBrains; Visual Studio / Xcode / Eclipse excluded as "
                     f"unverified)")
    eligible = [u for u in game.units if u.auto]
    total = sum(u.value for u in eligible)
    if total:
        weighted = Fraction(0)
        unknown = 0
        for u in eligible:
            team = reach.get(u.team)
            r = Fraction(1) if u.cloud else (team.reach if team is not None else None)
            if r is None:
                unknown += u.value
            else:
                weighted += r * u.value
        parts.append(f"reach {_pct(weighted / total)} of eligible credits at the point")
        if unknown:
            parts.append(f"reach unknown (no activity data) for {_pct(Fraction(unknown, total))}"
                         f" of eligible credits: point 0, high 1")
    return "; ".join(parts) if parts else "no Auto-eligible credits"


def _lever_note(p: _Player, game: _Game, reach: Mapping[str | None, TeamReach],
                extra: Sequence[str], scen: str) -> str:
    parts: list[str] = []
    if p.kind == "auto":
        parts.append("Auto default: eligible direct-routed credits x (1 - 0.1 x reach); which "
                     "model Auto picks is not modeled (needs_eval)")
        parts.append(_reach_note(game, reach))
    elif p.kind == "remap":
        parts.append(f"price-only model-policy remap of {p.remap_model} on identical tokens"
                     + ("" if p.tokenizer_same else " (tokenizer band 1.00-1.35)")
                     + "; trade-off, needs_eval; reach 1 (server-side policy)")
    elif p.kind == "fast":
        parts.append("fast mode off: speed=fast cells re-priced at standard rates on identical "
                     "tokens; reach 1 (server-side policy)")
    elif p.kind == "runner":
        parts.append("larger-runner Actions lines re-priced at actions_linux: point assumes no "
                     "included minutes left, high = full net (included minutes cover standard "
                     "minutes)")
    parts.extend(p.notes)
    parts.extend(extra)
    return scen + "; ".join(parts)


def _plan(ctx: _Context) -> ActionPlan:
    pools = ctx.pools
    findings = ctx.findings
    auto_labels = any(c.routing == "auto" for c in ctx.cells)
    compliance = _pool.run_flags(ctx.config).get("compliance")
    pricing = _Pricing(ctx.pricer, compliance if compliance in ("data_residency", "fedramp")
                       else None)
    pairs = _remap_pairs()
    remap_targets = {src: (tgt, same) for src, tgt, same, _spec in pairs}
    units, n_cells, unconverted = _units(ctx, pricing, remap_targets)
    reach = team_reach(ctx.activity, ctx.config, month=ctx.month)
    reach_fr = {team: r.reach for team, r in reach.items()}
    auto_scale = {} if auto_labels else {
        team: 1 - share for team, share in _auto_shares(ctx.activity, ctx.month).items()}
    ents: dict[str, _Ent] = {}
    groups = _billing_groups(sorted(pools))
    forecast_used = False
    for name, pm in sorted(pools.items()):
        levels, used = _levels(pm, ctx.forecast)
        forecast_used = forecast_used or used
        cap = pm.pool_nano if pm.capped_policy is not None else None
        ents[name] = _Ent(pm, levels, groups[name], cap, pm.capped_policy)
    lines, no_minutes = _runner_lines(ctx)
    game = _Game(units, ents, lines, _auto_discount(), reach_fr, auto_scale)

    scen = _scenario_prefix(ctx.scenario)
    notes = list(ctx.notes)
    if not auto_labels and auto_scale:
        notes.append("no Auto labels in the report: Auto share per team from metrics model:auto "
                      "(ESTIMATED)")
    if any(pm.finality == "open" for pm in pools.values()):
        notes.append("month-end consumption from the pool forecast (p10-p90)" if forecast_used
                     else "open month: month-to-date consumption (no forecast)")
    for what, n in sorted(pricing.unpriced.items()):
        notes.append(f"{what}: {n} cells unpriced at the target, their saving not counted "
                     f"(unknown, not zero)")
    if no_minutes:
        notes.append(f"{no_minutes} larger-runner lines without minutes: low bound 0")

    linked = _linked_levers(findings)
    players: list[_Player] = []
    skipped: list[str] = []
    for lever in _catalog.COPILOT_LEVERS:
        fs = linked.get(lever.lever_id)
        if not fs:
            continue
        if lever.replay != "aggregate" or lever.lever_class == "behavioral":
            continue
        if lever.tradeoff and not ctx.include_tradeoffs:
            skipped.append(f"{lever.lever_id} (trade-off; include_tradeoffs off)")
            continue
        if lever.lever_id == _DOWNGRADE_ID and ctx.scenario is not None:
            skipped.append(f"{lever.lever_id} (plan unknown)")
            continue
        made = _candidates(ctx, lever, fs, game, pairs)
        if not made:
            skipped.append(f"{lever.lever_id} (linked, nothing to act on in the month's cells)")
        players.extend(made)
    if skipped:
        notes.append("not in the joint set: " + ", ".join(skipped))
    if ctx.excluded:
        notes.append("excluded (pool regime unknown; savings on their pooled credits are "
                     "unpriced, not zero; their direct org-metered rows are kept): "
                     + ", ".join(ctx.excluded))
    if not ents and unconverted:
        return _unpriced_plan(ctx, players, n_cells, scen + "; ".join(
            ["pool regime unknown for every entity: invoice savings unpriced", *notes]))
    if not players:
        return _empty_plan(ctx, n_cells, scen + "; ".join(
            ["no applicable aggregate Copilot lever", *notes]))
    for p in players:
        game.add(p)
    return _assemble(ctx, game, players, reach, n_cells, notes, scen)


def _unpriced_plan(ctx: _Context, players: Sequence[_Player], n_cells: int,
                   note: str) -> ActionPlan:
    fig = unpriced(note, Basis.LIST)
    head = unpriced(note, Basis.LIST_EQUIVALENT)
    levers = tuple(LeverResult(
        lever_id=p.lever.lever_id, lever_class=p.lever.lever_class, params=p.pid,
        basis=Basis.LIST, standalone=fig, shapley=fig, projected_monthly=fig,
        needs_eval=p.lever.needs_eval or p.needs_eval,
        upper_bound=p.lever.upper_bound or p.upper_bound, group=_GROUP,
        finding_ids=p.finding_ids) for p in players)
    ids = tuple(dict.fromkeys(p.lever.lever_id for p in players))
    return ActionPlan(joint_saving=fig, headline_monthly=fig, allowance_headroom_monthly=None,
                      levers=levers, groups=((_GROUP, ids),),
                      method="shapley-exact" if len(players) <= MAX_EXACT_PLAYERS
                      else "shapley-mc", shapley_se=(), sample=_sample(ctx, n_cells),
                      observed_rr=(), pool_headroom_monthly=head)


def _candidates(ctx: _Context, lever: _catalog.LeverDef, fs: Sequence[Finding], game: _Game,
                pairs: Sequence[tuple[str, str, bool, str]]) -> list[_Player]:
    """The chosen player(s) of one linked aggregate lever (empty when it has no effect)."""
    ids = tuple(sorted({f.finding_id for f in fs}))
    lid = lever.lever_id
    pools = ctx.pools
    if lid == _AUTO_ID:
        if not any(u.auto and u.value > 0 for u in game.units):
            return []
        spec = _catalog.AGGREGATE_GRIDS[lid][0]
        return [_Player(spec, lever, "auto", ids)]
    if lid == _POLICY_ID:
        models = {m for f in fs for k, m in f.scope.dims if k == "model"}
        restrict = bool(models) and all(any(k == "model" for k, _ in f.scope.dims) for f in fs)
        out = []
        for source, _target, same, spec in pairs:
            if restrict and source not in models:
                continue
            if any(u.remap_model == source and (u.d_remap or u.d_remap_hi)
                   for u in game.units):
                out.append(_Player(spec, lever, "remap", ids, remap_model=source,
                                   tokenizer_same=same))
        return out
    if lid == _FAST_ID:
        if not any(u.fast and u.d_fast for u in game.units):
            return []
        return [_Player(_catalog.AGGREGATE_GRIDS[lid][0], lever, "fast", ids)]
    if lid == _RUNNER_ID:
        if not any(hi for _lo, hi in game.runner_lines):
            return []
        return [_Player(_catalog.AGGREGATE_GRIDS[lid][0], lever, "runner", ids)]
    # seat levers
    specs = [_catalog.parse_aggregate_spec(s) for s in _catalog.AGGREGATE_GRIDS.get(lid, ())]
    if lid == _SEAT_POLICY_ID:
        orgs = sorted({dict(f.scope.dims).get("org") or "" for f in fs})
        if len(orgs) == 1 and orgs[0]:
            try:
                specs = [_catalog.AggregateSpec("seat_policy", "assign_selected",
                                                f"org:{orgs[0]}")]
            except UsageError:
                pass
    best: _Player | None = None
    best_key: tuple[int, int] | None = None
    candidates = [_seat_player(ctx, lever, s, fs, pools) for s in specs]
    effective = [c for c in candidates if any(c.seat_delta.values())]
    blocked_only = [c for c in candidates if c.notes and not any(c.seat_delta.values())
                    and any("renewal" in n for n in c.notes)]
    for cand in effective:
        game.add(cand)
        val, head = game.evaluate(frozenset({cand.pid}), _POINT)
        key = (val, head)
        if best_key is None or key > best_key:
            best, best_key = cand, key
    for cand in candidates:
        game.players.pop(cand.pid, None)
    if best is not None:
        return [best]
    if blocked_only:   # volume / azure only: shown with value 0 and the renewal note
        return [blocked_only[0]]
    return []


def _assemble(ctx: _Context, game: _Game, players: list[_Player],
              reach: Mapping[str | None, TeamReach], n_cells: int, notes: list[str],
              scen: str) -> ActionPlan:
    order = {lv.lever_id: i for i, lv in enumerate(_catalog.COPILOT_LEVERS)}
    players = sorted(players, key=lambda p: (order[p.lever.lever_id], p.pid))
    pids = [p.pid for p in players]
    states = _states(game, players, game.ents)
    phi: dict[_State, dict[str, int]] = {}
    phi_h: dict[_State, dict[str, int]] = {}
    se: dict[str, int] = {}
    for st in states:
        values, errors = _shapley(pids, lambda s, st=st: game.evaluate(s, st)[0])
        heads, _ = _shapley(pids, lambda s, st=st: game.evaluate(s, st)[1])
        phi[st] = values
        phi_h[st] = heads
        if st == _POINT:
            se = errors
    everyone = frozenset(pids)
    joint = _envelope({st: game.evaluate(everyone, st)[0] for st in states})
    joint_h = _envelope({st: game.evaluate(everyone, st)[1] for st in states})
    # headroom results exist whenever a credit lever plays (an explicit "headroom 0" in an
    # overage entity) or any coalition frees headroom; then every player has one (efficiency)
    headroom_game = any(p.kind in _CREDIT_KINDS for p in players) or any(
        game.evaluate(frozenset({p}), st)[1] for p in pids for st in states) or any(
        v for st in states for v in phi_h[st].values())
    regime = _regime_note(game.ents)
    base_note = "; ".join([f"pool rule (R11): invoice dollars = fees + overage + direct + "
                           f"Actions per billing entity; {regime}", *notes])
    results: list[LeverResult] = []
    head_results: list[LeverResult] = []
    sum_proj = [0, 0, 0]
    sum_proj_h = [0, 0, 0]
    upper_any = False
    for p in players:
        lv = p.lever
        upper = lv.upper_bound or p.upper_bound
        needs_eval = lv.needs_eval or p.needs_eval
        upper_any = upper_any or upper
        note = _lever_note(p, game, reach, [regime], scen)
        stand = _envelope({st: game.evaluate(frozenset({p.pid}), st)[0] for st in states})
        shap = _envelope({st: phi[st][p.pid] for st in states})
        proj = _project(shap, lv.lever_class)
        for i, v in enumerate((proj.point, proj.low, proj.high)):
            sum_proj[i] += v
        results.append(LeverResult(
            lever_id=lv.lever_id, lever_class=lv.lever_class, params=p.pid, basis=Basis.LIST,
            standalone=_fig(stand, Basis.LIST, note + "; standalone (never summed, R7)",
                            upper=upper),
            shapley=_fig(shap, Basis.LIST, note, upper=upper),
            projected_monthly=_fig(proj, Basis.LIST, note + _prior_note(lv.lever_class, proj),
                                   upper=upper),
            needs_eval=needs_eval, upper_bound=upper, group=_GROUP, finding_ids=p.finding_ids))
        if headroom_game:
            hnote = note + "; pool headroom: list-equivalent credits, not invoice dollars"
            stand_h = _envelope({st: game.evaluate(frozenset({p.pid}), st)[1] for st in states})
            shap_h = _envelope({st: phi_h[st][p.pid] for st in states})
            proj_h = _project(shap_h, lv.lever_class)
            for i, v in enumerate((proj_h.point, proj_h.low, proj_h.high)):
                sum_proj_h[i] += v
            head_results.append(LeverResult(
                lever_id=lv.lever_id, lever_class=lv.lever_class, params=p.pid,
                basis=Basis.LIST_EQUIVALENT,
                standalone=_fig(stand_h, Basis.LIST_EQUIVALENT,
                                hnote + "; standalone (never summed, R7)", upper=upper),
                shapley=_fig(shap_h, Basis.LIST_EQUIVALENT, hnote, upper=upper),
                projected_monthly=_fig(proj_h, Basis.LIST_EQUIVALENT,
                                       hnote + _prior_note(lv.lever_class, proj_h), upper=upper),
                needs_eval=needs_eval, upper_bound=upper, group=_HEADROOM_GROUP,
                finding_ids=p.finding_ids))
    lever_ids = tuple(dict.fromkeys(p.lever.lever_id for p in players))
    groups: list[tuple[str, tuple[str, ...]]] = [(_GROUP, lever_ids)]
    if head_results:
        groups.append((_HEADROOM_GROUP, lever_ids))
    headline = _Triple(sum_proj[0], sum_proj[1], sum_proj[2])
    head_total = _Triple(sum_proj_h[0], sum_proj_h[1], sum_proj_h[2])
    method = "shapley-exact" if len(pids) <= MAX_EXACT_PLAYERS else "shapley-mc"
    return ActionPlan(
        joint_saving=_fig(joint, Basis.LIST, scen + "joint cell replay of the selected levers "
                          "(monthly, invoice dollars); " + base_note, upper=upper_any),
        headline_monthly=_fig(headline, Basis.LIST, scen + "sum of Shapley credits x "
                              "realization priors (comonotone range; standalone values never "
                              "summed); " + base_note, upper=upper_any),
        allowance_headroom_monthly=None,
        levers=tuple(results + head_results),
        groups=tuple(groups),
        method=method,
        shapley_se=tuple(sorted(se.items())),
        sample=_sample(ctx, n_cells),
        observed_rr=(),
        pool_headroom_monthly=_fig(
            head_total if head_results else _Triple(joint_h.point, joint_h.low, joint_h.high),
            Basis.LIST_EQUIVALENT,
            scen + "pool headroom: list-equivalent credits the pool absorbs, not invoice "
            "dollars (never added to the headline); " + base_note, upper=upper_any),
    )


# ---------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------


def _context(cells: Iterable[Cell], pools: Iterable[PoolMonth], findings: Iterable[Finding],
             pricer: Pricer, *, lines: Iterable[CostLine], activity: Iterable[ActivityDay],
             config: Iterable[ConfigSnapshot], month: str, scenario: str | None,
             include_tradeoffs: bool, forecast: bool) -> _Context:
    month = _check_month(month)
    if scenario is not None and scenario not in _SCENARIOS:
        raise UsageError("plan_copilot: scenario must be None, 'business' or 'enterprise'")
    if not isinstance(pricer, Pricer):
        raise UsageError("plan_copilot: pricer must implement core.protocols.Pricer")
    cell_list = _as_list(cells, Cell, "cells")
    pool_list = _as_list(pools, PoolMonth, "pools")
    finding_list = _as_list(findings, Finding, "findings")
    line_list = _as_list(lines, CostLine, "lines")
    activity_list = _as_list(activity, ActivityDay, "activity")
    config_list = _as_list(config, ConfigSnapshot, "config")
    chosen: dict[str, PoolMonth] = {}
    for pm in pool_list:
        if pm.month != month:
            continue
        if pm.plan_scenario is not None and scenario is None:
            raise UsageError(f"plan_copilot: the plan of {pm.entity_id} is unknown in {month}; "
                             "use plan_copilot_scenarios (R17)")
        if pm.plan_scenario not in (None, scenario):
            continue
        if pm.entity_id in chosen:
            raise UsageError(f"plan_copilot: two pool months for {pm.entity_id} in {month}")
        chosen[pm.entity_id] = pm
    excluded = sorted(name for name, pm in chosen.items() if pm.regime == "unknown")
    included = {name: pm for name, pm in chosen.items() if pm.regime != "unknown"}
    month_cells = [c for c in cell_list if c.month == month]
    notes: list[str] = []
    orphan = sorted({c.entity_id for c in month_cells if c.entity_id not in chosen})
    if orphan:
        notes.append("cells without a pool month (not planned): " + ", ".join(orphan))
    return _Context(month=month, scenario=scenario, pools=included, excluded=excluded,
                    cells=month_cells,
                    findings=_scenario_findings(finding_list, scenario), lines=line_list,
                    activity=activity_list, config=config_list, pricer=pricer,
                    include_tradeoffs=bool(include_tradeoffs), forecast=bool(forecast),
                    notes=notes)


def plan_copilot(cells: Sequence[Cell], pools: Sequence[PoolMonth], findings: Sequence[Finding],
                 pricer: Pricer, *, lines: Sequence[CostLine], activity: Sequence[ActivityDay],
                 month: str, include_tradeoffs: bool = False, forecast: bool = False,
                 config: Sequence[ConfigSnapshot] = (),
                 scenario: str | None = None) -> ActionPlan:
    """The Copilot aggregate plan of *month* (addendum §9.2, §11.2) in invoice dollars.

    *cells*: ``core.pool.build_cells`` output (day or month grain; cells of other months are
    ignored); *pools*: ``core.pool.pool_months`` output (months other than *month* ignored);
    *findings*: Copilot findings (``product=copilot`` scope or a ``copilot.*`` detector) whose
    linked aggregate levers are the players; *pricer* prices remaps and fast mode through
    ``unit_rates``; *lines*: cost lines (Copilot-workload Actions lines feed the runner lever);
    *activity* / *config*: reach (``ActivityDay`` or ``activity_counts`` rows) and run flags
    (``compliance``, ``renewal_date.<entity>``). *scenario* (``business`` | ``enterprise``) plays
    that plan scenario's ``PoolMonth``s and findings; None requires every plan of the month to be
    known (``UsageError`` otherwise — use :func:`plan_copilot_scenarios`). ``forecast`` uses an
    open month's forecast consumption (p10–p90). Returns an ``ActionPlan`` with
    ``headline_monthly`` (invoice, ESTIMATED, LIST), ``pool_headroom_monthly``
    (LIST_EQUIVALENT), ``method`` ``shapley-exact`` (≤ 6 players) or ``shapley-mc`` and
    ``sample`` ``"copilot cells, <n> cells, month <m>"`` (+ ``", scenario <s>"``).
    """
    ctx = _context(cells, pools, findings, pricer, lines=lines, activity=activity, config=config,
                   month=month, scenario=scenario, include_tradeoffs=include_tradeoffs,
                   forecast=forecast)
    return _plan(ctx)


def plan_copilot_scenarios(cells: Sequence[Cell], pools: Sequence[PoolMonth],
                           findings: Sequence[Finding], pricer: Pricer, *,
                           lines: Sequence[CostLine], activity: Sequence[ActivityDay],
                           config: Sequence[ConfigSnapshot], month: str,
                           include_tradeoffs: bool = False,
                           forecast: bool = False) -> tuple[tuple[str, ActionPlan], ...]:
    """``(("known", plan),)`` when every entity's plan of *month* is known, else
    ``(("business", plan_b), ("enterprise", plan_e))`` — the same cells played on each
    scenario's ``PoolMonth``s and findings, never averaged or summed (R17, R-E22). Arguments as
    :func:`plan_copilot`."""
    month = _check_month(month)
    pool_list = _as_list(pools, PoolMonth, "pools")
    cell_list = _as_list(cells, Cell, "cells")
    finding_list = _as_list(findings, Finding, "findings")
    line_list = _as_list(lines, CostLine, "lines")
    activity_list = _as_list(activity, ActivityDay, "activity")
    config_list = _as_list(config, ConfigSnapshot, "config")
    unknown = any(pm.month == month and pm.plan_scenario is not None for pm in pool_list)
    scenarios: tuple[str | None, ...] = _SCENARIOS if unknown else (None,)
    out: list[tuple[str, ActionPlan]] = []
    for scenario in scenarios:
        plan = plan_copilot(cell_list, pool_list, finding_list, pricer, lines=line_list,
                            activity=activity_list, month=month,
                            include_tradeoffs=include_tradeoffs, forecast=forecast,
                            config=config_list, scenario=scenario)
        out.append((scenario if scenario is not None else SCENARIO_KNOWN, plan))
    return tuple(out)
