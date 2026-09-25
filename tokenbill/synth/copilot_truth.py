"""Closed-form truths of the synthetic Copilot enterprise (addendum §18, Appendix C; CP-SYNTH).

Every figure here is computed from the generator's own facts — the rows it wrote, the seats it
assigned and the lanes it built — with closed forms taken from the binding worked examples:
token prices from the ``core.facts`` Copilot rate rows and modifiers (tokens × USD per MTok,
rounded once per bucket), the pool rule of Appendix C.P1–P15 (Σ seats × included credits, promo
allowances, caps), the month-end forecast of Appendix C.P9 (nearest-rank p10 / p50 / p90 of the
last 10 business and 4 weekend days) and the seat-change conversion of C.P2–P4 / P10–P13b. Nothing
here calls ``core.pool``, ``core.testing.FakePricer``, a detector or the plan: gate tests compare
these truths against what the real implementations recover.

All money is integer nano-USD (1 AI credit = 10,000,000 nano); no floats (SPEC §2.4).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from tokenbill.core import facts as _facts
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, token_nano
from tokenbill.core.records import InferenceKind, LaneEventKind, Session, UsageBuckets
from tokenbill.core.types import RateRow

__all__ = [
    "NANO_PER_CREDIT",
    "CopilotTruth",
    "LaneTruth",
    "RowFact",
    "SeatFact",
    "TruthPoolMonth",
    "allowance",
    "forecast",
    "k_dated_changes",
    "lane_truth",
    "nearest_rank",
    "pool_month_truths",
    "price",
    "rate_row",
    "rates",
    "regime",
    "seat_change_saving",
]

#: nano-USD per AI credit ($0.01).
NANO_PER_CREDIT = 10_000_000
_NANO_PER_USD = 10**9
_DAY = _dt.timedelta(days=1)
_FROM_K = re.compile(r"(?:^|;\s*)date_source=K")
_TO_K = re.compile(r"effective_to date_source=K")
_LISTED_K = re.compile(r"listed (\d{4}-\d{2}-\d{2}), K")
_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
            "cache_write_unknown", "output")


# ---------------------------------------------------------------------------------------------
# generator facts handed to the closed forms
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowFact:
    """One AI usage report row as the generator wrote it (the analyst sees it as a CostLine plus
    its share of a token aggregate)."""

    date: str
    principal: str | None
    team: str | None
    org: str
    cost_center: str | None
    cost_type: str           # ai_credit.user | ai_credit.direct
    model: str               # canonical id; "" for pseudo rows
    routing: str
    speed: str
    pseudo: str | None
    repo: str | None
    usage: UsageBuckets
    gross: int
    discount: int

    @property
    def net(self) -> int:
        """Net amount (gross − pool discount)."""
        return self.gross - self.discount


@dataclass(frozen=True, slots=True)
class SeatFact:
    """One seated user of the world (the truth of the seats API, whatever a view shows)."""

    principal: str
    team: str
    org: str
    cost_center: str | None
    plan: str                # business | enterprise
    via_team: bool
    idle: bool
    plan_mix: bool
    zero_budget: bool
    surface: str             # editor family of the seat's last activity


@dataclass(frozen=True, slots=True)
class TruthPoolMonth:
    """The closed-form state of one pool entity × month (× plan scenario)."""

    entity_id: str
    month: str
    plan_scenario: str | None
    seats: tuple[tuple[str, str], ...]     # plan → seat-months (decimal string); ``unknown`` kept
    pool_credits: str
    pool_nano: int
    promo: bool
    consumed_report_nano: int
    overage_observed_nano: int
    direct_net_nano: int
    finality: str                          # closed | open
    regime: str
    forecast: tuple[int, int, int] | None  # (point, low, high) month-end consumption, open months
    overage_forecast: tuple[int, int, int] | None   # (point, low, high)


@dataclass(frozen=True)
class LaneTruth:
    """Closed forms of the collector lanes (VS Code, CLI, OTel, gh-aw)."""

    requests_unique: int
    requests_duplicated: int                    # request records seen in two sources
    band_premium_exact_nano: int                # hypothesis A alone (context tier unknown)
    band_premium_ranges: tuple[tuple[int, int, int], ...]   # (point A, low, high) per request
    band_requests: int
    compaction_spend_nano: int
    compaction_forced_nano: int                 # context_limit_retry + memory_pressure
    compactions_by_trigger: tuple[tuple[str, int], ...]
    static_overhead_nano: int
    static_tool_definition_tokens: int
    model_switches: int
    ci_uncapped_sessions: int
    ci_session_spend_nano: tuple[int, ...]      # sorted spend of the uncapped CI sessions
    requests_by_source: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CopilotTruth:
    """Every planted cause of the synthetic enterprise and its closed-form dollar truth."""

    today: str
    variants: tuple[str, ...]
    team_sizes: tuple[tuple[str, int], ...]
    control_team: str
    tiny_team: str
    plan_view: str                                  # known | unknown | quota | conflict
    plans: tuple[tuple[str, str, str, str, bool], ...]   # (entity, month, plan, source, conflict)
    pool_months: tuple[TruthPoolMonth, ...]
    idle_seats: Mapping[str, Mapping[str, int]]     # team → assignment → n
    idle_seat_saving_nano: int | None               # platform, removable seats (known plan)
    seat_auto_assign_nano: int | None               # ops (org B, assign_all)
    idle_seat_saving_by_scenario: Mapping[tuple[str, str | None], int | None]
    plan_mix_seats: Mapping[str, int]
    zero_user_budgets: Mapping[str, int]
    fast_premium_nano: int                          # mobile
    premium_remap_saving_by_team: Mapping[str, int]
    premium_share_by_team: Mapping[str, Fraction]
    auto_reach: Mapping[str, Fraction]
    auto_saving_by_team: Mapping[str, int]
    editor_share: Mapping[str, Mapping[str, Fraction]]
    forced_migration_delta: Mapping[tuple[str, str], int]
    direct_org_net_nano: int
    larger_runner: tuple[int, int, int]             # (net, low, high)
    agentic_run_prices: tuple[int, ...]
    agentic_run_p50_nano: int
    agentic_run_p90_nano: int
    promo_cliff_nano: Mapping[str, int]
    cap_overage_range: Mapping[str, tuple[int, int]]   # month → (low, high)
    lanes: LaneTruth
    report_convention: Mapping[str, str]            # report set → excl | incl | undecidable
    notes: tuple[str, ...] = ()
    extra: Mapping[str, object] = field(default_factory=dict)

    @property
    def direct_net_nano(self) -> int:
        """Alias of :attr:`direct_org_net_nano`."""
        return self.direct_org_net_nano

    @property
    def fast_mode_premium_nano(self) -> int:
        """Alias of :attr:`fast_premium_nano`."""
        return self.fast_premium_nano

    @property
    def seat_reclaim_nano(self) -> int | None:
        """Alias of :attr:`idle_seat_saving_nano`."""
        return self.idle_seat_saving_nano

    @property
    def reach_by_team(self) -> Mapping[str, Fraction]:
        """Alias of :attr:`auto_reach`."""
        return self.auto_reach


# ---------------------------------------------------------------------------------------------
# prices (closed form over the facts rows)
# ---------------------------------------------------------------------------------------------


def rate_row(model: str, date: str) -> RateRow | None:
    """The ``core.facts`` Copilot rate row of *model* in force on *date* (None: unpriced)."""
    for row in _facts.copilot_rates():
        if row.model == model and row.effective_from <= date and (
                row.effective_to is None or date < row.effective_to):
            return row if row.enabled else None
    return None


def _modifier(modifier_id: str) -> object | None:
    for m in _facts.copilot_modifiers():
        if m.modifier_id == modifier_id:
            return m
    return None


def rates(model: str, date: str, *, routing: str = "direct", speed: str = "standard",
          band: bool = False) -> dict[str, Decimal | None] | None:
    """USD per MTok per bucket (``input``, ``output``, ``cache_read``, ``cache_write_5m``,
    ``cache_write_1h``; None when the row prices no write) of *model* on *date*: fast mode
    replaces the base, multipliers follow the (replaced) input rate, the long-context band
    overrides its buckets, Auto multiplies every bucket by its factor. None when unpriced."""
    row = rate_row(model, date)
    if row is None:
        return None
    inp, out = row.input_usd_per_mtok, row.output_usd_per_mtok
    if speed == "fast":
        for m in _facts.copilot_modifiers():
            when = dict(m.when)
            if (m.kind == "replace_base" and when.get("speed") == "fast"
                    and model in str(when.get("model_in", "")).split(",")):
                base = dict(m.base_usd_per_mtok)
                inp, out = base.get("input", inp), base.get("output", out)

    def times(mult: Decimal | None) -> Decimal | None:
        return None if mult is None else EXACT_CTX.multiply(inp, mult)

    table: dict[str, Decimal | None] = {
        "input": inp, "output": out, "cache_read": times(row.cache_read_mult),
        "cache_write_5m": times(row.cache_write_5m_mult),
        "cache_write_1h": times(row.cache_write_1h_mult)}
    if band:
        for bucket, value in row.long_context_usd_per_mtok:
            table[bucket] = value
    if routing == "auto":
        auto = _modifier("github.auto")
        factor = getattr(auto, "factor", None) or Decimal("0.9")
        table = {k: (None if v is None else EXACT_CTX.multiply(v, factor))
                 for k, v in table.items()}
    return table


def price(usage: UsageBuckets, model: str, date: str, *, routing: str = "direct",
          speed: str = "standard", band: bool = False, write: str = "5m") -> int | None:
    """Nano-USD of *usage* on *model* (rounded once per bucket). Unknown-TTL writes at the 5m
    (point, low) or 1h (*write*="1h", high) price; a row without a write price folds writes into
    input. None when unpriced."""
    table = rates(model, date, routing=routing, speed=speed, band=band)
    if table is None:
        return None
    inp = table["input"]
    assert inp is not None
    w5 = table["cache_write_5m"] if table["cache_write_5m"] is not None else inp
    w1 = table["cache_write_1h"] if table["cache_write_1h"] is not None else w5
    read = table["cache_read"] if table["cache_read"] is not None else inp
    out = table["output"]
    assert out is not None
    per = {"uncached_input": inp, "cache_read": read, "cache_write_5m": w5,
           "cache_write_1h": w1, "cache_write_unknown": w1 if write == "1h" else w5,
           "output": out}
    return sum(token_nano(getattr(usage, b), per[b]) for b in _BUCKETS if getattr(usage, b))


def band_threshold(model: str, date: str) -> int | None:
    """The long-context threshold of the row in force (None: no band)."""
    row = rate_row(model, date)
    return row.long_context_threshold if row is not None else None


def k_dated_changes() -> dict[str, frozenset[str]]:
    """Model → dates of its K-dated rate changes (``date_source=K`` on a row's
    ``effective_from`` / ``effective_to``, and ``listed <date>, K`` modifier listings such as the
    Opus 4.8 fast mode) in ``core.facts.copilot_rates()``."""
    out: dict[str, set[str]] = defaultdict(set)
    for row in _facts.copilot_rates():
        if _FROM_K.search(row.notes):
            out[row.model].add(row.effective_from)
        if _TO_K.search(row.notes) and row.effective_to is not None:
            out[row.model].add(row.effective_to)
        for listed in _LISTED_K.findall(row.notes):
            out[row.model].add(listed)
    return {k: frozenset(v) for k, v in sorted(out.items())}


# ---------------------------------------------------------------------------------------------
# pool closed forms (Appendix C.P1-P15)
# ---------------------------------------------------------------------------------------------


def allowance(plan: str, month: str, *, promo_eligible: bool = True) -> tuple[Decimal, bool]:
    """Included credits per seat-month of *plan* in *month* and whether the promo applied."""
    fact = _facts.copilot_plans()[plan]
    first = f"{month}-01"
    if (promo_eligible and fact.promo_credits is not None and fact.promo_from is not None
            and fact.promo_to is not None and fact.promo_from <= first < fact.promo_to):
        return Decimal(fact.promo_credits), True
    return Decimal(fact.included_credits), False


def seat_fee_nano(plan: str) -> int:
    """List price of one seat-month (nano)."""
    return decimal_to_nano(_facts.copilot_plans()[plan].seat_usd_per_month)


def _credits_nano(credits: Decimal) -> int:
    return decimal_to_nano(EXACT_CTX.multiply(credits, Decimal("0.01")))


def regime(low: int, high: int, pool: int) -> str:
    """``slack`` / ``overage`` / ``straddling`` of a consumption range against a pool."""
    if high <= pool:
        return "slack"
    if low > pool:
        return "overage"
    return "straddling"


def nearest_rank(values: Sequence[int], num: int, den: int = 10) -> int:
    """Nearest-rank percentile ``num/den``: index ``ceil(num·n/den) − 1`` of the sorted values."""
    ordered = sorted(values)
    idx = max(0, -((-num * len(ordered)) // den) - 1)
    return ordered[min(idx, len(ordered) - 1)]


def forecast(daily: Mapping[str, int], month: str, today: str, lag: int
             ) -> tuple[int, int, int, int] | None:
    """(observed, point, low, high) month-end consumption (Appendix C.P9): observed days ≤ today −
    lag; each remaining business day adds p50 / p10 / p90 of the last 10 observed business days,
    each remaining weekend day those of the last 4 observed weekend days."""
    first = _dt.date.fromisoformat(f"{month}-01")
    nxt = (first.replace(day=28) + 4 * _DAY).replace(day=1)
    days = [first + i * _DAY for i in range((nxt - first).days)]
    cutoff = _dt.date.fromisoformat(today) - lag * _DAY
    observed = [d for d in days if d <= cutoff]
    if not observed:
        return None
    total = sum(daily.get(d.isoformat(), 0) for d in observed)
    business = [daily.get(d.isoformat(), 0) for d in observed if d.weekday() < 5][-10:]
    weekend = [daily.get(d.isoformat(), 0) for d in observed if d.weekday() >= 5][-4:]
    stats_b = [nearest_rank(business or weekend, q) for q in (1, 5, 9)]
    stats_w = [nearest_rank(weekend or business, q) for q in (1, 5, 9)]
    add = [0, 0, 0]
    for d in days:
        if d <= cutoff:
            continue
        stats = stats_b if d.weekday() < 5 else stats_w
        for i in range(3):
            add[i] += stats[i]
    return total, total + add[1], total + add[0], total + add[2]


def _last_day(month: str) -> _dt.date:
    first = _dt.date.fromisoformat(f"{month}-01")
    return (first.replace(day=28) + 4 * _DAY).replace(day=1) - _DAY


def pool_month_truths(rows: Iterable[tuple[str, RowFact]],
                      seats: Mapping[tuple[str, str], Mapping[str, int]],
                      caps: Mapping[str, Decimal], *, today: str, lag: int,
                      promo_eligible: bool = True,
                      cap_policy: str = "unknown") -> tuple[TruthPoolMonth, ...]:
    """The pool months of a view: *rows* as ``(entity, row)`` pairs (the entity the view's caps
    give), *seats* per ``(entity, month)`` → plan → seats (``unknown`` seats split into the two
    scenarios, R17), *caps* cost-center name → credits (a capped entity's pool is its cap) and
    *cap_policy* the stated policy at the cap (``unknown``: a capped entity's overage forecast
    spans block (0) to continue, Appendix C.P7)."""
    by_em: dict[tuple[str, str], list[RowFact]] = defaultdict(list)
    for entity, row in rows:
        by_em[(entity, row.date[:7])].append(row)
    keys = sorted(set(by_em) | set(seats))
    today_d = _dt.date.fromisoformat(today)
    commons: dict[tuple[str, str], dict[str, object]] = {}
    for key in keys:
        items = by_em.get(key, [])
        pooled = [r for r in items if r.cost_type == "ai_credit.user"]
        daily: dict[str, int] = defaultdict(int)
        for r in pooled:
            daily[r.date] += r.gross
        month = key[1]
        closed = _last_day(month) <= today_d - lag * _DAY and all(
            r.date[:7] < today[:7] for r in items)
        fc = None if closed else forecast(daily, month, today, lag)
        consumed = sum(r.gross for r in pooled)
        levels = (consumed, consumed, consumed) if fc is None else (fc[2], fc[1], fc[3])
        commons[key] = {"consumed": consumed, "net": sum(r.net for r in pooled),
                        "direct": sum(r.net for r in items if r.cost_type == "ai_credit.direct"),
                        "closed": closed, "fc": fc, "levels": levels}
    out: list[TruthPoolMonth] = []
    for entity, month in keys:
        cm = commons[(entity, month)]
        plan_seats = dict(seats.get((entity, month), {}))
        is_cc = entity.startswith("cc:") and entity[3:] in caps
        scenarios: tuple[str | None, ...] = (("business", "enterprise")
                                              if plan_seats.get("unknown", 0) > 0 else (None,))
        unused = [0, 0, 0]
        if entity == "enterprise":
            for (e, m), other in commons.items():
                if m == month and e.startswith("cc:") and e[3:] in caps:
                    cap = _credits_nano(caps[e[3:]])
                    for i, level in enumerate(other["levels"]):  # type: ignore[arg-type]
                        unused[i] += max(0, cap - level)
        for scen in scenarios:
            resolved: dict[str, int] = defaultdict(int)
            for plan, n in plan_seats.items():
                target = scen if plan == "unknown" else plan
                if target is not None and n > 0:
                    resolved[target] += n
            credits = Decimal(0)
            promo = False
            for plan, n in sorted(resolved.items()):
                per, applied = allowance(plan, month, promo_eligible=promo_eligible)
                credits = EXACT_CTX.add(credits, EXACT_CTX.multiply(Decimal(n), per))
                promo = promo or applied
            pool_c = caps[entity[3:]] if is_cc else credits
            pool = _credits_nano(pool_c)
            known_pool = is_cc or bool(resolved)
            consumed = int(cm["consumed"])  # type: ignore[call-overload]
            if scen is None or is_cc:
                overage = int(cm["net"])  # type: ignore[call-overload]
            else:
                overage = max(0, consumed - (pool + unused[1]))
            fc = cm["fc"]
            if not known_pool:
                reg = "unknown"
            elif cm["closed"]:
                reg = regime(consumed, consumed, pool + unused[1])
            elif fc is None:
                reg = "unknown"
            else:
                reg = regime(fc[2] - unused[0], fc[3] - unused[2], pool)  # type: ignore[index]
            over_fc = None
            if fc is not None and known_pool:
                over_fc = tuple(max(0, fc[i] - (pool + unused[j]))  # type: ignore[index]
                                for i, j in ((1, 1), (2, 0), (3, 2)))
                if is_cc and cap_policy == "block":
                    over_fc = (0, 0, 0)
                elif is_cc and cap_policy != "continue":
                    over_fc = (over_fc[0], 0, over_fc[2])
            out.append(TruthPoolMonth(
                entity_id=entity, month=month, plan_scenario=scen,
                seats=tuple(sorted((p, str(n)) for p, n in plan_seats.items() if n > 0)),
                pool_credits=f"{pool_c.normalize():f}", pool_nano=pool, promo=promo,
                consumed_report_nano=consumed, overage_observed_nano=overage,
                direct_net_nano=int(cm["direct"]),  # type: ignore[call-overload]
                finality="closed" if cm["closed"] else "open", regime=reg,
                forecast=(fc[1], fc[2], fc[3]) if fc is not None else None,  # type: ignore[index]
                overage_forecast=over_fc))  # type: ignore[arg-type]
    return tuple(out)


def seat_change_saving(pm: TruthPoolMonth, delta: Mapping[str, int], *,
                       billing_mode: str = "metered") -> tuple[int, int, int] | None:
    """(point, low, high) monthly invoice saving of a seat change in *pm* (Appendix C.P2–P4,
    P10–P13b): −Δfees − Δoverage at the month's consumption (an open month's forecast point, p10
    and p90). *delta*: plan → seats (negative = removal; ``unknown`` under the scenario's plan).
    None for volume / azure billing (savings only at renewal, C.P10)."""
    if billing_mode in ("volume", "azure"):
        return None
    fees = 0
    dpool = Decimal(0)
    for plan, n in sorted(delta.items()):
        effective = pm.plan_scenario if plan == "unknown" else plan
        assert effective is not None
        fees += n * seat_fee_nano(effective)
        per, _ = allowance(effective, pm.month, promo_eligible=pm.promo)
        dpool = EXACT_CTX.add(dpool, EXACT_CTX.multiply(Decimal(n), per))
    pool0 = pm.pool_nano
    pool1 = max(0, pool0 + _credits_nano(dpool))
    if pm.forecast is not None:
        levels = (pm.forecast[1], pm.forecast[0], pm.forecast[2])
    else:
        c = pm.consumed_report_nano
        levels = (c, c, c)
    values = [-fees - (max(0, c - pool1) - max(0, c - pool0)) for c in levels]
    return values[1], min(values), max(values)


# ---------------------------------------------------------------------------------------------
# collector lanes
# ---------------------------------------------------------------------------------------------

_FORCED = ("context_limit_retry", "memory_pressure")


def _date_of_ms(ms: int) -> str:
    return (_dt.date(1970, 1, 1) + _dt.timedelta(milliseconds=ms)).isoformat()


def lane_truth(sessions: Sequence[Session]) -> LaneTruth:
    """Closed forms over the collector sessions: unique requests (a request id seen in two sources
    counts once), long-context band premiums (hypothesis A exact when the context tier is unknown;
    the A/B range when a known tier disagrees), compaction spend by trigger, static overhead
    (``system_tokens + tool_definitions_tokens`` of the lane's COMPACTION events: read price per
    warm request, 5m write price per miss), model switches and uncapped CI sessions."""
    seen: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    band_exact = 0
    band_ranges: list[tuple[int, int, int]] = []
    compaction = forced = static = static_tools = switches = 0
    by_trigger: dict[str, int] = defaultdict(int)
    ci_spend: list[int] = []
    counted: set[str] = set()
    for sess in sorted(sessions, key=lambda s: (s.session_key, s.source_kind)):
        tier = None
        cap = None
        for lane in sess.lanes:
            for ev in lane.events:
                attrs = dict(ev.attrs)
                if ev.kind is LaneEventKind.SESSION_META:
                    tier = attrs.get("context_tier", tier)
                    cap = attrs.get("credit_limit_nano", cap)
        spend = 0
        for lane in sess.lanes:
            triggers = [dict(ev.attrs) for ev in lane.events
                        if ev.kind is LaneEventKind.COMPACTION]
            switches += sum(1 for ev in lane.events
                            if ev.kind is LaneEventKind.MODEL_SWITCH_USER)
            s_tokens = max((int(a.get("system_tokens") or 0)
                            + int(a.get("tool_definitions_tokens") or 0) for a in triggers),
                           default=0)
            tools = max((int(a.get("tool_definitions_tokens") or 0) for a in triggers), default=0)
            ti = iter(t.get("copilot_trigger") or t.get("trigger") for t in triggers)
            for req in lane.requests:
                seen[req.request_id] += 1
                by_source[sess.source_kind] += 1
                first = req.request_id not in counted
                counted.add(req.request_id)
                date = _date_of_ms(req.ts_start_ms)
                for inf in req.billable_inferences:
                    ctx = inf.pricing
                    point = price(inf.usage, ctx.model, date, routing=ctx.routing,
                                  speed=ctx.speed) or 0
                    thr = band_threshold(ctx.model, date)
                    a = thr is not None and inf.usage.total_input > thr
                    b = ctx.context_tier == "long_context" if ctx.context_tier else None
                    band_a = price(inf.usage, ctx.model, date, routing=ctx.routing,
                                   speed=ctx.speed, band=True) if thr is not None else None
                    if a:
                        point = band_a or point
                    spend += point
                    if not first:
                        continue
                    if inf.kind is InferenceKind.COMPACTION:
                        trig = str(next(ti, "auto"))
                        by_trigger[trig] += point
                        compaction += point
                        if trig in _FORCED:
                            forced += point
                    if thr is not None and band_a is not None and (a or b):
                        base = price(inf.usage, ctx.model, date, routing=ctx.routing,
                                     speed=ctx.speed) or 0
                        premium = band_a - base
                        if b is None or b == a:
                            band_exact += premium if a else 0
                            if a:
                                band_ranges.append((premium, premium, premium))
                        else:
                            band_ranges.append((premium if a else 0, 0, premium))
                if first and s_tokens and req.serving_inference is not None:
                    inf = req.serving_inference
                    table = rates(inf.pricing.model, date, routing=inf.pricing.routing,
                                  speed=inf.pricing.speed)
                    if table is not None:
                        warm = inf.usage.cache_read >= s_tokens
                        rate = table["cache_read"] if warm else (
                            table["cache_write_5m"] or table["input"])
                        static += token_nano(s_tokens, rate)  # type: ignore[arg-type]
                        static_tools = max(static_tools, tools)
        if sess.attribution.workload_class.value == "ci" and cap is None and (
                sess.attribution.agent_product == "copilot_cli"):
            ci_spend.append(spend)
    return LaneTruth(
        requests_unique=len(seen), requests_duplicated=sum(1 for v in seen.values() if v > 1),
        band_premium_exact_nano=band_exact, band_premium_ranges=tuple(sorted(band_ranges)),
        band_requests=len(band_ranges), compaction_spend_nano=compaction,
        compaction_forced_nano=forced, compactions_by_trigger=tuple(sorted(by_trigger.items())),
        static_overhead_nano=static, static_tool_definition_tokens=static_tools,
        model_switches=switches, ci_uncapped_sessions=len(ci_spend),
        ci_session_spend_nano=tuple(sorted(ci_spend)),
        requests_by_source=tuple(sorted(by_source.items())))
