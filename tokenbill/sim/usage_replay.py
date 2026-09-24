"""Usage-level replay engine (SPEC §9.1–§9.5, D8, D9, D26, D28, D30; package REPLAY).

:class:`UsageReplayer` implements :class:`~tokenbill.core.protocols.Replayer` for every source,
content-free: it needs only billed usage buckets, timestamps, request parameters and lane events.

Semantics (SPEC §9.1): **minimal change** — a policy changes only the transitions it affects and
every other request keeps its priced ledger figure exactly (``changed=False``); savings are
``cost(observed) − cost(policy)`` **per request, then summed**, so model error on unaffected
traffic never leaks into them. ``replay(Policy.observed())`` returns ``cost == baseline`` to the
nano (points and bounds). One billing class per call (mixed input raises ``UsageError``).

Per request, the fixed application order is (1) rate transforms (model remap with the tokenizer
band, effort, ``fast=off``, ``geo=global``, ``regional=global``) → (2) context transforms
(compaction window, cold resume) → (3) cache-state transforms (two-way TTL flips, keepalive, the
``fast=off`` flips, repairs) → (4) TTL re-rating, the min-prefix gate, batch → (5) pricing.

Ranges: the point follows the documented rules; bounds come from two more deterministic passes
over the lanes that need them (the *low* pass takes the cheap end of every uncertain quantity:
ambiguous transitions alive, the tokenizer band's low factor, effort scale − 0.25, batch hit band
0.98; the *high* pass the other end) combined with the pricer's own line ranges, and every request's
bounds contain its point.

Pricing uses exact integer unit rates (SPEC §6.4) on the hot path and ``Pricer.price_usage`` for
range lines. A unit-rate key (pricing context × UTC day) is used only after a probe proved that
``price_usage`` prices the same buckets exactly at the same rates, and only below the total-input
level where the probe found a long-context band (binary search), so both paths agree to the nano.

Interpretations where the SPEC is silent are listed in ``tests/v2/sim/README.md``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

from tokenbill.core.cache_rules import RulesTable, effort_change_keeps_cache
from tokenbill.core.catalog import successor
from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.evidence import (
    BATCH_CACHE_HIT_BAND,
    COMPACTION_SUMMARY_TOKENS_DEFAULT,
    THINKING_SHARE_PRIOR,
    TOKENIZER_BAND,
)
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, zero
from tokenbill.core.policy import EFFORT_LEVELS, lane_matches, selector_terms
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import (
    Inference,
    InferenceKind,
    Lane,
    LaneEventKind,
    LaneKind,
    PricingContext,
    Request,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.transitions import AMBIGUITY_MS, classify_transitions
from tokenbill.core.types import (
    CalibrationReport,
    Policy,
    ReplayRequestOutcome,
    ReplayResult,
    Transition,
)

__all__ = [
    "BATCH_NO_CACHE_CHANNELS",
    "GAP_BANDS",
    "MODES",
    "UsageReplayer",
    "gap_band",
    "rho_from_report",
]

MODES = ("documented", "calibrated")

#: Gap bands of the model gate (§9.6 #4) and of calibrated replay (§9.4): (label, low ms, high ms).
GAP_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("0s-60s", 0, 60_000),
    ("60s-300s", 60_000, 300_000),
    ("300s-3600s", 300_000, 3_600_000),
    ("3600s+", 3_600_000, None),
)
#: Channels whose batch tier does no prompt caching at all (§9.3.5: "no caching in Bedrock batch").
BATCH_NO_CACHE_CHANNELS = frozenset({"bedrock"})
#: Workload classes eligible for the batch predicate (§9.3.5).
_BATCH_WORKLOADS = frozenset({WorkloadClass.CI, WorkloadClass.EVAL, WorkloadClass.SCHEDULED,
                              WorkloadClass.SERVICE})
#: Entrypoint fragments that identify Anthropic Managed Agents (no batch tier, §19.2).
_MANAGED_AGENT_MARKERS = ("managed_agent", "managed-agent", "managedagent")

_DAY_MS = 86_400_000
_FIVE_MIN_S = 300
_FIVE_MIN_MS = 300_000
_REPLAY_KINDS_DROP = frozenset({InferenceKind.MESSAGE, InferenceKind.FALLBACK})

# usage tuple layout (hot loops never build UsageBuckets)
_U, _R, _W5, _W1, _WO, _WOT, _WU, _O, _OR, _WS, _WF = range(11)
_PREF_5M = (_W5, None)
_PREF_1H = (_W1, None)
_PREF_UNKNOWN = (_WU, None)

_POINT, _LOW, _HIGH = 0, 1, 2
_FINAL = UsageSource.FINAL

_BAND_HI = Fraction(TOKENIZER_BAND.value[1])  # type: ignore[index]
_BAND_LO_POINT = Fraction(TOKENIZER_BAND.value[0])  # type: ignore[index]
#: (source family, target family) → (low factor, high factor); other differing pairs use the
#: symmetric band.
_TOKENIZER_BANDS: Mapping[tuple[str, str], tuple[Fraction, Fraction]] = {
    ("claude-legacy", "claude-4.7+"): (_BAND_LO_POINT, _BAND_HI),
    ("claude-4.7+", "claude-legacy"): (1 / _BAND_HI, _BAND_LO_POINT),
}
_BATCH_H_POINT = Fraction(64, 100)
_BATCH_H_LOW_COST = Fraction(BATCH_CACHE_HIT_BAND.value[1])  # type: ignore[index]
_BATCH_H_HIGH_COST = Fraction(BATCH_CACHE_HIT_BAND.value[0])  # type: ignore[index]
_THINKING_SHARE = Fraction(THINKING_SHARE_PRIOR.value)  # type: ignore[arg-type]
_EFFORT_BAND = Fraction(1, 4)
_EFFORT_RANK = {level: i for i, level in enumerate(EFFORT_LEVELS)}
_SUMMARY_DEFAULT: int = COMPACTION_SUMMARY_TOKENS_DEFAULT.value  # type: ignore[assignment]
_PROBE_HUGE = 2**50
_PROBE_SMALL = 5_000
_PROBE_EACH = 1_000


def gap_band(gap_ms: int) -> str:
    """The label of the gap band (:data:`GAP_BANDS`) containing *gap_ms*."""
    for label, _lo, hi in GAP_BANDS:
        if hi is None or gap_ms < hi:
            return label
    return GAP_BANDS[-1][0]  # pragma: no cover - the last band is open


def rho_from_report(report: CalibrationReport) -> dict[str, Fraction]:
    """ρ per gap band from a model-gate report (§9.4, §9.6 #4): ``hits / trials`` of the band,
    the pooled ρ for bands with fewer than 30 trials, 1 when nothing was observed."""
    hits = sum(h for _band, h, _t, _lo, _hi in report.rho)
    trials = sum(t for _band, _h, t, _lo, _hi in report.rho)
    pooled = Fraction(hits, trials) if trials else Fraction(1)
    out = {label: pooled for label, _lo, _hi in GAP_BANDS}
    for band, h, t, _lo, _hi in report.rho:
        if t >= 30:
            out[band] = Fraction(h, t)
    return out


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _tup(u: UsageBuckets) -> tuple:
    return (u.uncached_input, u.cache_read, u.cache_write_5m, u.cache_write_1h,
            u.cache_write_other, u.cache_write_other_ttl_s, u.cache_write_unknown, u.output,
            u.output_reasoning, u.web_search_requests, u.web_fetch_requests)


def _buckets(t: tuple) -> UsageBuckets:
    return UsageBuckets(uncached_input=t[_U], cache_read=t[_R], cache_write_5m=t[_W5],
                        cache_write_1h=t[_W1], cache_write_other=t[_WO],
                        cache_write_other_ttl_s=t[_WOT], cache_write_unknown=t[_WU],
                        output=t[_O], output_reasoning=t[_OR], web_search_requests=t[_WS],
                        web_fetch_requests=t[_WF])


def _writes(t: tuple) -> int:
    return t[_W5] + t[_W1] + t[_WO] + t[_WU]


def _total_input(t: tuple) -> int:
    return t[_U] + t[_R] + t[_W5] + t[_W1] + t[_WO] + t[_WU]


def _pref_of_ttl(ttl_s: int) -> tuple[int, int | None]:
    if ttl_s == 300:
        return _PREF_5M
    if ttl_s == 3600:
        return _PREF_1H
    return (_WO, ttl_s)


def _dominant(t: tuple) -> tuple[int, int | None] | None:
    """The write bucket holding most of *t*'s writes (ties: 5m, 1h, other, unknown)."""
    best: tuple[int, int | None] | None = None
    best_q = 0
    for idx in (_W5, _W1, _WO, _WU):
        q = t[idx]
        if q > best_q:
            best_q = q
            best = (idx, t[_WOT] if idx == _WO else None)
    return best


def _with_split(t: tuple, u: int, r: int, w: int, pref: tuple[int, int | None]) -> tuple:
    """*t* with uncached *u*, reads *r* and writes *w*; the observed write buckets are kept when
    the write total is unchanged, else every write goes to *pref*."""
    if w == _writes(t):
        return (u, r, t[_W5], t[_W1], t[_WO], t[_WOT], t[_WU], t[_O], t[_OR], t[_WS], t[_WF])
    idx, ttl = pref
    return (u, r,
            w if idx == _W5 else 0,
            w if idx == _W1 else 0,
            w if idx == _WO else 0,
            ttl if idx == _WO else t[_WOT],
            w if idx == _WU else 0,
            t[_O], t[_OR], t[_WS], t[_WF])


def _rerate(t: tuple, pref: tuple[int, int | None]) -> tuple:
    """Every write of *t* moved to the *pref* bucket (TTL re-rating, §9.3.1)."""
    w = _writes(t)
    idx, ttl = pref
    if w == 0 or t[idx] == w:
        return t
    return (t[_U], t[_R],
            w if idx == _W5 else 0,
            w if idx == _W1 else 0,
            w if idx == _WO else 0,
            ttl if idx == _WO else t[_WOT],
            w if idx == _WU else 0,
            t[_O], t[_OR], t[_WS], t[_WF])


def _scale_q(q: int | None, f: Fraction, up: bool) -> int | None:
    if not q:
        return q
    v, rem = divmod(q * f.numerator, f.denominator)
    if up and rem:
        v += 1
    return v


def _scale_t(t: tuple, f: Fraction, up: bool) -> tuple:
    """Every token quantity of *t* × *f* (outward rounding: floor below the point, ceil above)."""
    if f == 1:
        return t
    s = _scale_q
    return (s(t[_U], f, up), s(t[_R], f, up), s(t[_W5], f, up), s(t[_W1], f, up),
            s(t[_WO], f, up), t[_WOT], s(t[_WU], f, up), s(t[_O], f, up), s(t[_OR], f, up),
            t[_WS], t[_WF])


def _round_half_even(num: int, den: int) -> int:
    q, r = divmod(num, den)
    twice = 2 * r
    if twice > den or (twice == den and q % 2 == 1):
        q += 1
    return q


def _median(values: list[int]) -> int:
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2


def _attr(attrs: Iterable[tuple[str, Any]], key: str) -> Any:
    for k, v in attrs:
        if k == key:
            return v
    return None


def _differs(a: object, b: object) -> bool:
    return a is not None and b is not None and a != b


def _is_managed_agents(entrypoint: str | None) -> bool:
    text = (entrypoint or "").lower()
    return any(marker in text for marker in _MANAGED_AGENT_MARKERS)


# ---------------------------------------------------------------------------------------------
# pricing: exact unit rates on the hot path, price_usage for range lines
# ---------------------------------------------------------------------------------------------

#: priced amount: (point, low, high, ranged) or None when unpriced
_Priced = tuple[int, int, int, bool]


class _Unit:
    """Probe-verified integer unit rates of one (pricing context, UTC day)."""

    __slots__ = ("div", "limit", "mul", "n1", "n5", "no", "nout", "nr", "nu", "row", "scale",
                 "web")

    def __init__(self, nu: int, nr: int, n5: int, n1: int, no: int, nout: int, web: int,
                 scale: int, row: str) -> None:
        self.nu, self.nr, self.n5, self.n1, self.no, self.nout = nu, nr, n5, n1, no, nout
        self.web = web
        self.scale = scale
        self.mul = 10 ** (9 - scale) if scale <= 9 else 1
        self.div = 10 ** (scale - 9) if scale > 9 else 1
        self.row = row
        self.limit: int | None = None   # hot path valid iff total_input < limit (None: always)

    def nano(self, t: tuple) -> int:
        div = self.div
        if div == 1:
            return ((t[_U] * self.nu + t[_R] * self.nr + t[_W5] * self.n5 + t[_W1] * self.n1
                     + t[_WO] * self.no + t[_O] * self.nout) * self.mul + t[_WS] * self.web)
        total = t[_WS] * self.web
        for q, n in ((t[_U], self.nu), (t[_R], self.nr), (t[_W5], self.n5), (t[_W1], self.n1),
                     (t[_WO], self.no), (t[_O], self.nout)):
            if q:
                a, rem = divmod(q * n, div)
                twice = 2 * rem
                if twice > div or (twice == div and a % 2 == 1):
                    a += 1
                total += a
        return total


def _probe_tuple(total: int) -> tuple:
    e = _PROBE_EACH
    return (total - 4 * e, e, e, e, e, 1800, 0, e, None, 1, 0)


class _PriceBook:
    """Pricing with caches: unit-rate keys, min-cacheable, supports, tokenizer family."""

    def __init__(self, pricer: Pricer) -> None:
        self.pricer = pricer
        self._units: dict[tuple[PricingContext, int], _Unit | None] = {}
        self._min: dict[tuple[PricingContext, int], int] = {}
        self._supports: dict[tuple[PricingContext, int, str], bool] = {}
        self._family: dict[tuple[PricingContext, int], str | None] = {}
        self.rows: set[str] = set()              # the current provenance sink (see _Run)
        self.unpriced_reasons: set[str] = set()

    # ---------- probes ----------

    def _probe_ok(self, unit: _Unit, ctx: PricingContext, ts: int, total: int) -> bool:
        """True when ``price_usage`` prices the probe usage (total input *total*) with exact lines
        at exactly the unit rates, so the integer path reproduces it to the nano."""
        t = _probe_tuple(total)
        try:
            pi = self.pricer.price_usage(_buckets(t), ctx, ts_ms=ts)
        except (TokenbillError, ArithmeticError, ValueError, TypeError):
            return False
        fig = pi.figure
        if fig.nano is None or fig.low_nano is not None or fig.nano != unit.nano(t):
            return False
        rates = {"uncached_input": unit.nu, "cache_read": unit.nr, "cache_write_5m": unit.n5,
                 "cache_write_1h": unit.n1, "cache_write_other": unit.no, "output": unit.nout}
        for line in pi.lines:
            if not line.exact:
                return False
            try:
                rate = Decimal(line.unit_usd_per_mtok)
            except (ArithmeticError, ValueError, TypeError):
                return False
            if line.bucket == "web_search":
                if rate.scaleb(9) != unit.web:
                    return False
                continue
            num = rates.get(line.bucket)
            if num is None or rate != Decimal(num).scaleb(6 - unit.scale):
                return False
        return True

    def unit(self, ctx: PricingContext, ts: int) -> _Unit | None:
        key = (ctx, ts // _DAY_MS)
        try:
            return self._units[key]
        except KeyError:
            pass
        unit = self._build_unit(ctx, ts)
        self._units[key] = unit
        return unit

    def _build_unit(self, ctx: PricingContext, ts: int) -> _Unit | None:
        try:
            ur = self.pricer.unit_rates(ctx, ts_ms=ts)
        except (TokenbillError, ArithmeticError, ValueError):
            return None
        if ur is None:
            return None
        unit = _Unit(ur.uncached, ur.cache_read, ur.cache_write_5m, ur.cache_write_1h,
                     ur.cache_write_other, ur.output, ur.web_search_nano, ur.scale_exp, ur.row_id)
        if not self._probe_ok(unit, ctx, ts, _PROBE_SMALL):
            return None
        if self._probe_ok(unit, ctx, ts, _PROBE_HUGE):
            return unit
        lo, hi = _PROBE_SMALL, _PROBE_HUGE      # ok(lo), not ok(hi)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._probe_ok(unit, ctx, ts, mid):
                lo = mid
            else:
                hi = mid
        unit.limit = hi
        return unit

    # ---------- queries ----------

    def min_cacheable(self, ctx: PricingContext, ts: int) -> int:
        key = (ctx, ts // _DAY_MS)
        v = self._min.get(key)
        if v is None:
            got = self.pricer.min_cacheable_tokens(ctx, ts_ms=ts)
            v = got if got is not None else 0
            self._min[key] = v
        return v

    def supports(self, ctx: PricingContext, feature: str, ts: int) -> bool:
        key = (ctx, ts // _DAY_MS, feature)
        v = self._supports.get(key)
        if v is None:
            v = bool(self.pricer.supports(ctx, feature, ts_ms=ts))
            self._supports[key] = v
        return v

    def family(self, ctx: PricingContext, ts: int) -> str | None:
        key = (ctx, ts // _DAY_MS)
        if key not in self._family:
            self._family[key] = self.pricer.tokenizer_family(ctx, ts_ms=ts)
        return self._family[key]

    # ---------- pricing ----------

    def price(self, t: tuple, ctx: PricingContext, ts: int, billable: bool | None = True,
              source: UsageSource = _FINAL, output_upper: int | None = None) -> _Priced | None:
        """(point, low, high, ranged) of one inference, or None when unpriced. A non-billable
        inference costs exactly 0 (never priced)."""
        if billable is False:
            return (0, 0, 0, False)
        if billable is True and source is _FINAL and not t[_WU] and not t[_WF]:
            unit = self.unit(ctx, ts)
            if unit is not None and (unit.limit is None or _total_input(t) < unit.limit):
                self.rows.add(unit.row)
                v = unit.nano(t)
                return (v, v, v, False)
        pi = self.pricer.price_usage(_buckets(t), ctx, ts_ms=ts, billable=billable,
                                     usage_source=source, output_upper=output_upper)
        fig = pi.figure
        if fig.nano is None:
            self.unpriced_reasons.add(pi.unpriced_reason or "unpriced")
            return None
        self.rows.update(fig.provenance)
        if fig.low_nano is None or fig.high_nano is None:
            return (fig.nano, fig.nano, fig.nano, False)
        return (fig.nano, fig.low_nano, fig.high_nano, True)


def _add_priced(a: _Priced | None, b: _Priced | None) -> _Priced | None:
    if a is None or b is None:
        return None
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] or b[3])


def _blend(hit: _Priced | None, nohit: _Priced | None, rho: Fraction) -> _Priced | None:
    """ρ·hit + (1 − ρ)·nohit, rounded once (half-even) per bound (§9.4)."""
    if hit is None or nohit is None:
        return None
    num, den = rho.numerator, rho.denominator

    def mix(h: int, n: int) -> int:
        return _round_half_even(num * h + (den - num) * n, den)

    point, low, high = mix(hit[0], nohit[0]), mix(hit[1], nohit[1]), mix(hit[2], nohit[2])
    return (point, min(low, point), max(high, point), hit[3] or nohit[3])


# ---------------------------------------------------------------------------------------------
# compiled policy and per-lane configuration
# ---------------------------------------------------------------------------------------------


def _entries(items: Iterable[tuple], what: str) -> list[tuple]:
    """Selector-keyed entries sorted most specific first (more selector terms), then in policy
    order: the first entry whose selector matches a lane applies to it."""
    out = []
    for order, item in enumerate(items):
        if not isinstance(item, tuple) or not item or not isinstance(item[0], str):
            raise UsageError(f"policy: malformed {what} entry")
        out.append((-len(selector_terms(item[0])), order, item))
    out.sort(key=lambda e: (e[0], e[1]))
    return [item for _n, _o, item in out]


class _Compiled:
    """A validated view of a :class:`Policy` for the engine."""

    def __init__(self, policy: Policy) -> None:
        if not isinstance(policy, Policy):
            raise UsageError("replay: policy must be a Policy")
        self.policy = policy
        ttl = []
        for entry in _entries(policy.ttl, "ttl"):
            if len(entry) != 2 or entry[1] not in ("5m", "1h"):
                raise UsageError("policy: ttl must be 5m or 1h")
            ttl.append((entry[0], 300 if entry[1] == "5m" else 3600))
        self.ttl = ttl
        self.keepalive: tuple[str, int, int] | None = None
        if policy.keepalive is not None:
            ka = policy.keepalive
            if (not isinstance(ka, tuple) or len(ka) != 3 or type(ka[1]) is not int
                    or type(ka[2]) is not int or ka[1] <= 0 or ka[2] < 0):
                raise UsageError("policy: keepalive must be (selector, interval_s, max_idle_s)")
            selector_terms(ka[0])
            self.keepalive = (ka[0], ka[1] * 1000, ka[2] * 1000)
        self.cw: tuple[int, int | None] | None = None
        if policy.compaction_window is not None:
            cw = policy.compaction_window
            if (not isinstance(cw, tuple) or len(cw) != 2 or type(cw[0]) is not int
                    or (cw[1] is not None and type(cw[1]) is not int)):
                raise UsageError("policy: compaction_window must be (window, summary | None)")
            self.cw = (cw[0], cw[1])
        self.cr: tuple[str, int] | None = None
        if policy.cold_resume is not None:
            cr = policy.cold_resume
            if (not isinstance(cr, tuple) or len(cr) != 2 or cr[0] not in ("compact", "clear")
                    or type(cr[1]) is not int):
                raise UsageError("policy: cold_resume must be ('compact'|'clear', min_context)")
            self.cr = (cr[0], cr[1])
        self.remap = [(e[0], e[1]) for e in _entries(policy.model_remap, "model_remap")
                      if len(e) == 2 and isinstance(e[1], str) and e[1]]
        if len(self.remap) != len(policy.model_remap):
            raise UsageError("policy: model_remap entries must be (selector, model id)")
        effort = []
        for entry in _entries(policy.effort, "effort"):
            if len(entry) != 3 or entry[1] not in _EFFORT_RANK:
                raise UsageError("policy: unknown effort level")
            try:
                scale = Fraction(Decimal(entry[2]))
            except (ArithmeticError, TypeError, ValueError):
                raise UsageError("policy: effort scale must be a decimal string") from None
            if not 0 <= scale <= 1:
                raise UsageError("policy: effort scale must be in [0, 1]")
            effort.append((entry[0], _EFFORT_RANK[entry[1]], scale))
        self.effort = effort
        self.fast_off = bool(policy.fast_off)
        self.geo_global = bool(policy.geo_global)
        self.regional_to_global = bool(policy.regional_to_global)
        if policy.batch not in (None, "eligible"):
            raise UsageError("policy: batch must be 'eligible' or None")
        self.batch = policy.batch == "eligible"
        repairs = set()
        block = []
        for repair in policy.repairs:
            if not isinstance(repair, str):
                raise UsageError("policy: repairs must be strings")
            if repair.startswith("block:"):
                block.append(repair)
            elif repair in ("restore_caching", "stagger_fanout", "retry_backoff_cap",
                            "fallback_credit", "shared_ci_prefix"):
                repairs.add(repair)
            else:
                raise UsageError("policy: unknown repair")
        self.repairs = frozenset(repairs)
        if policy.breakpoint_policy not in (None, "observed"):
            block.append(f"breakpoints={policy.breakpoint_policy}")
        self.block = tuple(block)
        self.upper_bound = bool(self.cw or self.cr or self.remap or self.effort
                                or {"stagger_fanout", "shared_ci_prefix"} & self.repairs)
        self.needs_eval = bool(self.cw or self.cr or self.remap or self.effort)


@dataclasses.dataclass(slots=True)
class _Req:
    """One request of a lane with its observed pricing."""

    req: Request
    serving: Inference | None
    serving_ts: int
    ts: int
    obs_t: tuple | None
    others: list[tuple[int, Inference, int]]   # (attempt position, inference, attempt ts)
    base_serving: _Priced | None
    base_others: list[_Priced | None]
    base: _Priced | None
    pos: int = 0              # position in the lane
    # filled for steps (requests with a serving inference)
    step: int = -1
    reset: bool = False       # §3.15 rule 1 (reset events, edits, dropped thinking)
    reset_cw: bool = False    # §9.3.3 observed reset (COMPACTION/CLEAR event)


@dataclasses.dataclass(slots=True)
class _LaneCfg:
    """What the policy does to one lane (lane-level decisions)."""

    ttl_s: int | None = None
    ka: tuple[int, int] | None = None          # (κ ms, max idle ms)
    cw: tuple[int, int] | None = None          # (window, summary tokens)
    cr: tuple[str, int, int] | None = None     # (action, min ctx, summary tokens)
    remap: str | None = None
    effort: tuple[int, Fraction] | None = None
    batch: bool = False
    restore: bool = False
    rate_ctx: bool = False                     # any context-changing rate transform
    needs_trans: bool = False
    bounds: bool = False                       # set during the point pass
    skipped: list[str] = dataclasses.field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.ttl_s or self.ka or self.cw or self.cr or self.remap or self.effort
                    or self.batch or self.restore or self.rate_ctx or self.needs_trans)


@dataclasses.dataclass(slots=True)
class _State:
    """A request under the policy in one pass."""

    serving_t: tuple | None = None
    serving_ctx: PricingContext | None = None
    serving_upper: int | None = None
    alt_t: tuple | None = None                 # calibrated mode: the no-hit split
    band: str | None = None
    extras: list[tuple[InferenceKind, tuple, PricingContext, int, str]] = \
        dataclasses.field(default_factory=list)
    others: list[tuple[tuple, PricingContext, int] | None] | None = None  # None: unchanged
    changed: bool = False


# ---------------------------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------------------------


class UsageReplayer:
    """The usage-level :class:`~tokenbill.core.protocols.Replayer` (SPEC §9.1–§9.4).

    Stateless: every call of :meth:`replay` is independent and deterministic.
    """

    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult:
        """Replay *lanes* under *policy* (see the module docstring).

        *mode* ``"documented"`` applies the documented cache rules; ``"calibrated"`` blends every
        flip to a hit with ρ from a **passing** *calibration* report (§9.4) and otherwise falls
        back to the documented rules labeled UNCALIBRATED. Raises ``UsageError`` for an unknown
        mode, a malformed policy or lanes of more than one billing class.
        """
        return _Run(lanes, policy, mode=mode, pricer=pricer, rules=rules,
                    calibration=calibration, static_prefix_floor=static_prefix_floor,
                    keep_outcomes=keep_outcomes).result()


class _Run:
    """One replay call."""

    def __init__(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
                 rules: CacheRulesProvider | None, calibration: CalibrationReport | None,
                 static_prefix_floor: Mapping[tuple[str, str], int] | None,
                 keep_outcomes: bool) -> None:
        if mode not in MODES:
            raise UsageError("replay: mode must be 'documented' or 'calibrated'")
        lanes = list(lanes)
        for lane in lanes:
            if not isinstance(lane, Lane):
                raise UsageError("replay: lanes must be Lane records")
        classes = {lane.billing_class for lane in lanes if lane.requests}
        if len(classes) > 1:
            raise UsageError("replay: lanes of one billing class only (billed | allowance)")
        self.lanes = sorted(lanes, key=lambda lane: lane.lane_key)
        self.c = _Compiled(policy)
        self.policy = policy
        self.pricer = pricer
        self.rules: CacheRulesProvider = rules if rules is not None else RulesTable()
        self.floor = dict(static_prefix_floor) if static_prefix_floor else {}
        self.keep = bool(keep_outcomes)
        self.book = _PriceBook(pricer)
        self.base_rows: set[str] = set()
        self.policy_rows: set[str] = set()
        if classes == {"allowance"}:
            self.basis = Basis.LIST_EQUIVALENT
        else:
            self.basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) \
                else Basis.LIST
        self.observed = policy.is_observed()
        self.rho: dict[str, Fraction] | None = None
        self.calibration_label = Calibration.UNCALIBRATED
        self.mode = "documented"
        self.notes: list[str] = []
        passing = calibration is not None and calibration.status == "pass"
        if mode == "calibrated":
            if passing:
                assert calibration is not None
                self.rho = rho_from_report(calibration)
                self.mode = "calibrated"
                self.calibration_label = Calibration.CALIBRATED
            else:
                self.notes.append("calibrated mode unavailable: no passing calibration report "
                                  "(documented rules, UNCALIBRATED)")
        elif passing and calibration is not None and calibration.mode_used == "documented":
            self.calibration_label = Calibration.CALIBRATED
        # counters
        self.added_calls = 0
        self.pings = 0
        self.hindsight_pings = 0
        self.skipped: list[tuple[str, str]] = []
        self.summary_tokens, self.summary_source = self._summary_tokens()
        self.stagger = self._stagger_map() if "stagger_fanout" in self.c.repairs else {}
        self.ci_prefix = self._ci_map() if "shared_ci_prefix" in self.c.repairs else {}

    # ------------------------------------------------------------------ cross-lane pre-passes

    def _summary_tokens(self) -> tuple[int, str]:
        cw = self.c.cw
        if cw is not None and cw[1] is not None:
            return cw[1], "policy"
        if cw is None and self.c.cr is None:
            return _SUMMARY_DEFAULT, "default"
        posts = [v for lane in self.lanes for ev in lane.events
                 if ev.kind is LaneEventKind.COMPACTION
                 for v in (_attr(ev.attrs, "post_tokens"),) if type(v) is int and v > 0]
        if posts:
            return _median(posts), "median"
        return _SUMMARY_DEFAULT, "default"

    @staticmethod
    def _first_step(lane: Lane) -> tuple[Request, Inference] | None:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is not None:
                return req, inf
        return None

    def _stagger_map(self) -> dict[str, tuple[int, int]]:
        """request id → (shared prefix tokens, ms after the group's first member) for the
        non-first members of cold fan-out groups (§9.3.6 stagger_fanout)."""
        groups: dict[tuple, list[tuple[int, str, str, int]]] = {}
        for lane in self.lanes:
            first = self._first_step(lane)
            if first is None:
                continue
            req, inf = first
            u = inf.usage
            total, w = u.total_input, u.cache_write
            if total <= 0 or 5 * w < 4 * total:
                continue
            key = (lane.team or "", lane.kind.value, lane.cache_scope_key, req.model,
                   req.attribution.cwd_key or "")
            groups.setdefault(key, []).append((req.ts_start_ms, lane.lane_key, req.request_id, w))
        out: dict[str, tuple[int, int]] = {}
        for key in sorted(groups):
            members = sorted(groups[key])
            i = 0
            while i < len(members):
                anchor = members[i][0]
                j = i
                while j + 1 < len(members) and members[j + 1][0] - anchor <= AMBIGUITY_MS:
                    j += 1
                group = members[i:j + 1]
                if len(group) >= 2:
                    shared = min(m[3] for m in group)
                    for ts, _lk, rid, _w in group[1:]:
                        out[rid] = (shared, ts - anchor)
                i = j + 1
        return out

    def _lane_ttl(self, lane: Lane) -> int | None:
        for selector, seconds in self.c.ttl:
            if lane_matches(selector, lane):
                return seconds
        return None

    def _ci_map(self) -> dict[str, tuple[int, int]]:
        """request id → (S_ci, ms after the previous run) for the first request of every later
        CI run in a chain (§9.3.6 shared_ci_prefix)."""
        groups: dict[tuple, list[tuple[int, str, str, int, int]]] = {}
        for lane in self.lanes:
            first = self._first_step(lane)
            if first is None:
                continue
            req, inf = first
            if req.attribution.workload_class is not WorkloadClass.CI:
                continue
            u = inf.usage
            total, w = u.total_input, u.cache_write
            if total <= 0 or 5 * w < 4 * total:
                continue
            tau = self._lane_ttl(lane)
            if tau is None:
                pref = _dominant(_tup(u))
                tau = {_W5: 300, _W1: 3600}.get(pref[0], pref[1] or 300) if pref else 300
            key = (lane.team or "", lane.kind.value, lane.cache_scope_key, req.model)
            groups.setdefault(key, []).append((req.ts_start_ms, lane.lane_key, req.request_id,
                                               w, tau))
        out: dict[str, tuple[int, int]] = {}
        for key in sorted(groups):
            members = sorted(groups[key])
            chains: list[list[tuple[int, str, str, int, int]]] = []
            for m in members:
                if chains and m[0] - chains[-1][-1][0] <= chains[-1][-1][4] * 1000:
                    chains[-1].append(m)
                else:
                    chains.append([m])
            floor_s = self.floor.get((key[2], key[3]), 0)
            for chain in chains:
                if len(chain) < 2:
                    continue
                s_ci = floor_s if floor_s > 0 else (4 * min(m[3] for m in chain)) // 5
                for prev, m in zip(chain, chain[1:], strict=False):
                    out[m[2]] = (s_ci, m[0] - prev[0])
        return out

    # ------------------------------------------------------------------ per-lane preparation

    def _prepare(self, lane: Lane) -> tuple[list[_Req], list[_Req]]:
        """Every request with its observed pricing, and the steps (requests with a serving
        inference) with their reset flags."""
        book = self.book
        reqs: list[_Req] = []
        steps: list[_Req] = []
        for req in lane.requests:
            serving = req.serving_inference
            serving_ts = req.attempts[-1].ts_start_ms
            others: list[tuple[int, Inference, int]] = []
            for pos, att in enumerate(req.attempts):
                for inf in att.inferences:
                    if inf is serving or inf.billable is False:
                        continue
                    others.append((pos, inf, att.ts_start_ms))
            base_serving: _Priced | None = (0, 0, 0, False)
            obs_t = None
            if serving is not None:
                obs_t = _tup(serving.usage)
                base_serving = book.price(obs_t, serving.pricing, serving_ts, serving.billable,
                                          serving.usage_source, serving.output_upper)
            base_others = [book.price(_tup(inf.usage), inf.pricing, ts, inf.billable,
                                      inf.usage_source, inf.output_upper)
                           for _pos, inf, ts in others]
            base = base_serving
            for p in base_others:
                base = _add_priced(base, p)
            r = _Req(req=req, serving=serving, serving_ts=serving_ts, ts=req.ts_start_ms,
                     obs_t=obs_t, others=others, base_serving=base_serving,
                     base_others=base_others, base=base, pos=len(reqs))
            reqs.append(r)
            if serving is not None:
                r.step = len(steps)
                steps.append(r)
        events = lane.events
        if steps and events:
            k = 0
            prev_ts = None
            for st in steps:
                window = []
                while k < len(events) and events[k].ts_ms <= st.ts:
                    if prev_ts is not None and events[k].ts_ms > prev_ts:
                        window.append(events[k])
                    k += 1
                st.reset_cw = any(ev.kind in (LaneEventKind.COMPACTION, LaneEventKind.CLEAR)
                                  for ev in window)
                st.reset = st.reset_cw or any(ev.kind is LaneEventKind.CONTEXT_EDIT
                                              for ev in window)
                prev_ts = st.ts
        for st in steps:
            if not st.reset and any(att.applied_edits or att.thinking_dropped > 0
                                    for att in st.req.attempts):
                st.reset = True
        return reqs, steps

    def _lane_cfg(self, lane: Lane, steps: list[_Req]) -> _LaneCfg:
        c = self.c
        cfg = _LaneCfg()
        if not lane.requests:
            return cfg
        rules = self.rules
        # (1) rate transforms
        for selector, target in c.remap:
            if lane_matches(selector, lane):
                cfg.remap = target
                break
        for selector, rank, scale in c.effort:
            if lane_matches(selector, lane):
                cfg.effort = (rank, scale)
                break
        cfg.rate_ctx = bool(cfg.remap or c.fast_off or c.geo_global or c.regional_to_global)
        # TTL
        for selector, seconds in c.ttl:
            if lane_matches(selector, lane):
                bad = sorted({st.serving.pricing.channel for st in steps
                              if st.serving is not None and seconds not in rules.rules_for(
                                  st.serving.pricing.provider, st.serving.pricing.channel,
                                  st.serving.pricing.model).ttl_options_s})
                if bad:
                    cfg.skipped.append(f"ttl policy not applicable on channel {bad[0]}")
                else:
                    cfg.ttl_s = seconds
                break
        # keepalive
        if c.keepalive is not None and lane_matches(c.keepalive[0], lane):
            reason = self._keepalive_block(lane, steps)
            if reason is not None:
                cfg.skipped.append(reason)
            else:
                cfg.ka = (c.keepalive[1], c.keepalive[2])
        # context transforms (MAIN lanes)
        if lane.kind is LaneKind.MAIN and steps:
            st0 = steps[0]
            assert st0.serving is not None
            ctx0 = self._xctx(st0.serving.pricing, cfg, False)
            if c.cw is not None:
                if self.book.supports(ctx0, "1m_context", st0.serving_ts):
                    cfg.cw = (c.cw[0], self.summary_tokens)
                else:
                    cfg.skipped.append("compaction window: model without 1m context")
            if c.cr is not None:
                cfg.cr = (c.cr[0], c.cr[1], self.summary_tokens)
        # batch
        if c.batch and self._batch_eligible(lane):
            st0 = steps[0]
            assert st0.serving is not None
            if self.book.supports(self._xctx(st0.serving.pricing, cfg, False), "batch",
                                  st0.serving_ts):
                cfg.batch = True
            else:
                cfg.skipped.append("batch not supported for model")
        # repairs
        reps = c.repairs
        if "restore_caching" in reps and self._restore_eligible(steps):
            cfg.restore = True
        cross = False
        if steps and (steps[0].req.request_id in self.stagger
                      or steps[0].req.request_id in self.ci_prefix):
            cross = True
        cfg.needs_trans = bool(cfg.ttl_s or cfg.ka or cfg.cw or cfg.cr or c.fast_off or cross
                               or cfg.restore or {"fallback_credit", "retry_backoff_cap"} & reps)
        return cfg

    def _keepalive_block(self, lane: Lane, steps: list[_Req]) -> str | None:
        first = lane.requests[0]
        if first.attribution.agent_product == "claude_code":
            return "keepalive not allowed for claude_code"
        for req in lane.requests:
            p = req.params
            if p.output_format is not None:
                return "keepalive skipped: structured outputs"
            if p.tool_choice is not None and (p.tool_choice == "any"
                                              or p.tool_choice.startswith("tool:")):
                return "keepalive skipped: forced tool_choice"
            if p.thinking is not None and p.thinking.startswith("enabled"):
                return "keepalive skipped: thinking enabled"
            si = req.serving_inference
            if p.service_tier_requested == "batch" or (
                    si is not None and si.pricing.service_tier == "batch"):
                return "keepalive skipped: batch"
        for st in steps:
            assert st.serving is not None
            if not self.book.supports(st.serving.pricing, "keepalive", st.serving_ts):
                return "keepalive not supported for model"
        return None

    @staticmethod
    def _batch_eligible(lane: Lane) -> bool:
        if len(lane.requests) != 1:
            return False
        req = lane.requests[0]
        si = req.serving_inference
        if si is None or si.pricing.service_tier == "batch" or si.pricing.speed == "fast":
            return False
        if req.attribution.workload_class not in _BATCH_WORKLOADS:
            return False
        return not _is_managed_agents(req.attribution.entrypoint)

    def _restore_eligible(self, steps: list[_Req]) -> bool:
        if len(steps) < 5:
            return False
        totals = []
        for st in steps:
            t = st.obs_t
            assert t is not None
            if t[_R] or _writes(t):
                return False
            totals.append(_total_input(t))
        st0 = steps[0]
        assert st0.serving is not None
        mc = self.book.min_cacheable(st0.serving.pricing, st0.serving_ts)
        return _median(totals) >= max(mc, 4096)

    # ------------------------------------------------------------------ transforms

    def _xctx(self, ctx: PricingContext, cfg: _LaneCfg, batch: bool) -> PricingContext:
        c = self.c
        changes: dict[str, Any] = {}
        if cfg.remap is not None and ctx.model != cfg.remap:
            changes["model"] = cfg.remap
            changes["model_raw"] = cfg.remap
        if c.fast_off and ctx.speed != "standard":
            changes["speed"] = "standard"
        if c.geo_global and ctx.inference_geo is not None:
            changes["inference_geo"] = None
        if c.regional_to_global and ctx.endpoint_scope != "global":
            changes["endpoint_scope"] = "global"
        if batch and ctx.service_tier != "batch":
            changes["service_tier"] = "batch"
        if not changes:
            return ctx
        return dataclasses.replace(ctx, **changes)

    def _band(self, ctx: PricingContext, cfg: _LaneCfg,
              ts: int) -> tuple[Fraction, Fraction] | None:
        """The tokenizer band of remapping *ctx* to the lane's target (None: no band)."""
        target = cfg.remap
        if target is None or ctx.model == target or successor(ctx.model) == target:
            return None
        new_ctx = dataclasses.replace(ctx, model=target, model_raw=target)
        src, dst = self.book.family(ctx, ts), self.book.family(new_ctx, ts)
        if src is None or dst is None or src == dst:
            return None
        return _TOKENIZER_BANDS.get((src, dst), (1 / _BAND_HI, _BAND_HI))

    def _scaled(self, t: tuple, ctx: PricingContext, cfg: _LaneCfg, ts: int, side: int) -> tuple:
        band = self._band(ctx, cfg, ts) if cfg.remap is not None else None
        if band is None:
            return t
        cfg.bounds = True
        if side == _LOW:
            return _scale_t(t, band[0], False)
        if side == _HIGH:
            return _scale_t(t, band[1], True)
        return t

    def _param_change(self, cur: _Req, prev: _Req, ignore_speed: bool) -> bool:
        """§3.15 rule 4 (observed parameters; the served speed only when not repaired)."""
        assert cur.serving is not None and prev.serving is not None
        if not ignore_speed and cur.serving.pricing.speed != prev.serving.pricing.speed:
            return True
        p, pp = cur.req.params, prev.req.params
        if (_differs(p.effort, pp.effort) or _differs(p.thinking, pp.thinking)) and \
                not effort_change_keeps_cache(
                    agent_product=cur.req.attribution.agent_product,
                    model=cur.serving.pricing.model or cur.req.model,
                    channel=cur.serving.pricing.channel,
                    client_version=cur.req.attribution.client_version,
                    betas=p.betas):
            return True
        a, pa = cur.req.attribution, prev.req.attribution
        return _differs(a.client_version, pa.client_version) or _differs(a.cwd_key, pa.cwd_key)

    @staticmethod
    def _decide(gap: int, horizon: int, side: int, cfg: _LaneCfg) -> bool:
        """``gap ≤ horizon``; within ±10 s the point follows the rule and the bounds take both
        outcomes (alive in the low pass, dead in the high pass)."""
        if abs(gap - horizon) <= AMBIGUITY_MS:
            cfg.bounds = True
            if side == _LOW:
                return True
            if side == _HIGH:
                return False
        return gap <= horizon

    @staticmethod
    def _ka_pings(gap: int, kappa: int, max_idle: int) -> int:
        if gap <= kappa:
            return 0
        return min(-(-gap // kappa) - 1, max_idle // kappa)

    # ------------------------------------------------------------------ one pass over a lane

    def _scaled_int(self, q: int, ctx: PricingContext, cfg: _LaneCfg, ts: int, side: int) -> int:
        """A token count scaled like the lane's usage (tokenizer band of *ctx*)."""
        if cfg.remap is None or not q:
            return q
        return self._scaled((0, q, 0, 0, 0, None, 0, 0, None, 0, 0), ctx, cfg, ts, side)[_R]

    def _pass(self, lane: Lane, reqs: list[_Req], steps: list[_Req], cfg: _LaneCfg,
              trans: Mapping[int, Transition], side: int) -> dict[int, _State]:
        """The policy state of every changed request of *lane* in pass *side* (keyed by the
        request's position in the lane); requests absent from the result are unchanged."""
        c = self.c
        book = self.book
        states: dict[int, _State] = {}
        n = len(steps)
        t_new = [0] * n                     # T'_i (total input under the policy)
        u_new = [0] * n                     # U'_i
        model_new = [""] * n
        ctx_new: list[PricingContext | None] = [None] * n
        removed = 0
        prev_obs_total = 0
        first_total = 0
        ttl_pref = None if cfg.ttl_s is None else _pref_of_ttl(cfg.ttl_s)
        point = side == _POINT
        rate_ctx = cfg.rate_ctx or cfg.batch

        def alive(i: int) -> bool:
            """alive_π(i) (§9.2) in this pass."""
            st, pv = steps[i], steps[i - 1]
            if model_new[i] != model_new[i - 1] or st.reset:
                return False
            if self._param_change(st, pv, c.fast_off):
                return False
            gap = st.ts - pv.ts
            if cfg.ka is not None:
                n_p = self._ka_pings(gap, cfg.ka[0], cfg.ka[1])
                return self._decide(gap, n_p * cfg.ka[0] + _FIVE_MIN_MS, side, cfg)
            if cfg.ttl_s is not None:
                return self._decide(gap, cfg.ttl_s * 1000, side, cfg)
            ob = trans.get(i)
            if ob is None:
                return False
            if ob.ttl_s is None:
                return not ob.is_miss_event
            return self._decide(gap, ob.ttl_s * 1000, side, cfg)

        for i, st in enumerate(steps):
            serving = st.serving
            assert serving is not None and st.obs_t is not None
            obs_ctx = serving.pricing
            ctx = self._xctx(obs_ctx, cfg, cfg.batch) if rate_ctx else obs_ctx
            model_new[i] = ctx.model
            ctx_new[i] = ctx
            # (1) rate transforms: tokenizer band, effort
            t = st.obs_t
            upper = serving.output_upper
            if cfg.remap is not None:
                t = self._scaled(t, obs_ctx, cfg, st.serving_ts, side)
                if upper is not None:
                    upper = self._scaled_int(upper, obs_ctx, cfg, st.serving_ts, side)
            if cfg.effort is not None:
                rank = _EFFORT_RANK.get(st.req.params.effort or "")
                if rank is not None and rank > cfg.effort[0]:
                    t, cut = self._effort(t, cfg.effort[1], side, cfg)
                    if upper is not None and cut:
                        upper = max(t[_O], upper - cut)
            ob = trans.get(i) if i else None
            tau_obs = ob.ttl_s if ob is not None else None
            pref = ttl_pref or (_pref_of_ttl(tau_obs) if tau_obs else None) or _dominant(t) \
                or self._default_pref(st)
            state = _State()
            u, r, w = t[_U], t[_R], _writes(t)
            obs_total = u + r + w
            total = obs_total
            if i == 0:
                first_total = obs_total
            skip3 = False
            # (2) context transforms
            if cfg.cw is not None or cfg.cr is not None:
                if i >= 1 and (st.reset_cw or 2 * obs_total < prev_obs_total):
                    removed = 0
                t_eff = obs_total - removed
                new_i = max(0, obs_total - prev_obs_total) if i >= 1 else obs_total
                if cfg.cw is not None and t_eff > cfg.cw[0]:
                    read = min(t_new[i - 1] - u_new[i - 1], t_eff) if i >= 1 and alive(i) else 0
                    summary = cfg.cw[1]
                    ct = _with_split((0, 0, 0, 0, 0, None, 0, summary, None, 0, 0), 0, read,
                                     t_eff - read, pref)
                    state.extras.append((InferenceKind.COMPACTION, ct, ctx, st.serving_ts,
                                         stable_id("replay", "compaction", st.req.request_id)))
                    if point:
                        self.added_calls += 1
                    total = summary + new_i
                    r, u = 0, min(u, total)
                    w = total - u
                    removed = obs_total - total
                    skip3 = True
                elif cfg.cr is not None and i >= 1 and t_eff > cfg.cr[1] and not alive(i):
                    summary = cfg.cr[2]
                    if cfg.cr[0] == "compact":
                        ct = (t_eff, 0, 0, 0, 0, None, 0, summary, None, 0, 0)
                        state.extras.append((InferenceKind.OTHER, ct, ctx, st.serving_ts,
                                             stable_id("replay", "cold-resume",
                                                       st.req.request_id)))
                        if point:
                            self.added_calls += 1
                        total = summary + new_i
                    else:
                        total = first_total + new_i
                    r, u = 0, min(u, total)
                    w = total - u
                    removed = obs_total - total
                    skip3 = True
                elif removed > 0:
                    r_eff = max(0, r - removed)
                    w = w if r >= removed else max(0, w - (removed - r))
                    r = r_eff
                    total = r + w + u
            prev_obs_total = obs_total
            # keepalive pings: the daemon pings whatever the next request turns out to be
            if cfg.ka is not None and i >= 1:
                self._pings(state, steps, i, cfg, t_new, u_new, ctx_new, point)
            # (3) cache-state transforms
            flipped = False
            band_gap = 0
            pre = (u, r, w)
            if not skip3:
                expected = min(t_new[i - 1] - u_new[i - 1], total) if i >= 1 else 0
                if ob is not None:
                    gap = ob.gap_ms
                    hit_flip = False
                    if cfg.ttl_s is not None and tau_obs is not None and cfg.ttl_s != tau_obs:
                        if cfg.ttl_s > tau_obs:
                            hit_flip = ob.is_miss_event and ob.cause == "ttl-expiry" and \
                                alive(i)
                        elif not ob.is_miss_event and gap <= tau_obs * 1000 and \
                                not self._decide(gap, cfg.ttl_s * 1000, side, cfg):
                            floor_s = self._scaled_int(
                                self.floor.get((lane.cache_scope_key, st.req.model), 0),
                                obs_ctx, cfg, st.serving_ts, side)
                            r = min(floor_s, total - u)
                            w = total - u - r
                    if cfg.ka is not None and ob.is_miss_event and ob.cause == "ttl-expiry" \
                            and alive(i):
                        hit_flip = True
                        pref = ttl_pref or _PREF_5M
                    if c.fast_off and ob.is_miss_event and ob.cause == "param-change" and \
                            ob.sub_cause == "fast-toggle" and alive(i):
                        hit_flip = True
                    obs_w = _writes(st.obs_t)
                    if "fallback_credit" in c.repairs and ob.is_miss_event and \
                            ob.cause == "model-switch" and ob.sub_cause == "refusal-fallback" \
                            and 5 * obs_w >= 4 * ob.expected_reuse:
                        hit_flip = True
                    if "retry_backoff_cap" in c.repairs and self._retry_capped(st, ob):
                        hit_flip = True
                    if hit_flip:
                        r = min(expected, total - u)
                        w = total - u - r
                        flipped = True
                        band_gap = gap
                if cfg.restore:
                    pref = ttl_pref or _pref_of_ttl(self._restore_tau(st))
                    gap = st.ts - steps[i - 1].ts if i >= 1 else 0
                    if i >= 1 and gap <= (cfg.ttl_s or self._restore_tau(st)) * 1000:
                        r = min(t_new[i - 1], total)
                        flipped = True
                        band_gap = gap
                    else:
                        r = 0
                    u = 0
                    w = total - r
                if i == 0:
                    rid = st.req.request_id
                    fan = self.stagger.get(rid)
                    if fan is not None:
                        shared = self._scaled_int(fan[0], obs_ctx, cfg, st.serving_ts, side)
                        r = min(w, shared)
                        w = w - r
                        flipped = True
                        band_gap = fan[1]
                    ci = self.ci_prefix.get(rid)
                    if ci is not None:
                        s_ci = self._scaled_int(ci[0], obs_ctx, cfg, st.serving_ts, side)
                        r = min(s_ci, total - u)
                        w = total - u - r
                        flipped = True
                        band_gap = ci[1]
            total = u + r + w
            # (4) TTL re-rating, min-prefix gate, batch
            new_t = _with_split(t, u, r, w, pref)
            alt_t = None
            if flipped and self.rho is not None:
                alt_t = _with_split(t, pre[0], pre[1], pre[2], pref)
            if ttl_pref is not None:
                new_t = _rerate(new_t, ttl_pref)
                if alt_t is not None:
                    alt_t = _rerate(alt_t, ttl_pref)
            obs = st.obs_t
            if (ctx.model != obs_ctx.model or new_t[_U] != obs[_U] or new_t[_R] != obs[_R]
                    or _writes(new_t) != _writes(obs)):
                mc = book.min_cacheable(ctx, st.serving_ts)
                new_t = self._gate(new_t, mc, pref)
                if alt_t is not None:
                    alt_t = self._gate(alt_t, mc, pref)
            if cfg.batch:
                new_t = self._batch_split(new_t, ctx, side, cfg)
                if alt_t is not None:
                    alt_t = self._batch_split(alt_t, ctx, side, cfg)
            if alt_t is not None and alt_t == new_t:
                alt_t = None
            if ttl_pref is not None and state.extras:
                state.extras = [(k, _rerate(et, ttl_pref) if k is InferenceKind.COMPACTION
                                 else et, ectx, ets, eid)
                                for k, et, ectx, ets, eid in state.extras]
            state.serving_t = new_t
            state.serving_ctx = ctx
            state.serving_upper = upper
            state.alt_t = alt_t
            if alt_t is not None:
                state.band = gap_band(band_gap)
            t_new[i] = _total_input(new_t)
            u_new[i] = new_t[_U]
            state.changed = (new_t != obs or ctx != obs_ctx or bool(state.extras)
                             or alt_t is not None)
            if state.changed:
                states[st.pos] = state
        # passthrough inferences (and requests without a serving inference)
        drop_retries = "retry_backoff_cap" in c.repairs
        for k, rq in enumerate(reqs):
            if not rq.others:
                continue
            drop: frozenset[int] = frozenset()
            n_att = len(rq.req.attempts)
            if drop_retries and n_att > 3 and rq.step >= 1:
                ob = trans.get(rq.step)
                if ob is not None and self._retry_capped(rq, ob):
                    drop = frozenset(range(2, n_att - 1))
            if not rate_ctx and not drop and cfg.remap is None:
                continue
            others: list[tuple[tuple, PricingContext, int] | None] = []
            any_changed = False
            for pos, inf, ts in rq.others:
                if pos in drop:
                    others.append(None)
                    any_changed = True
                    continue
                octx = self._xctx(inf.pricing, cfg, cfg.batch) if rate_ctx else inf.pricing
                ot = _tup(inf.usage)
                nt = self._scaled(ot, inf.pricing, cfg, ts, side) if cfg.remap is not None \
                    else ot
                if octx != inf.pricing or nt != ot:
                    any_changed = True
                others.append((nt, octx, ts))
            if any_changed:
                state = states.get(k)
                if state is None:
                    state = _State()
                    states[k] = state
                state.others = others
                state.changed = True
        return states

    def _effort(self, t: tuple, scale: Fraction, side: int, cfg: _LaneCfg) -> tuple[tuple, int]:
        """§9.3.5 effort: ``O' = O − th·(1 − s)`` (reduction floored), ``th = output_reasoning``
        when known else ``floor(0.505·O)``; bounds use ``s ± 0.25`` clipped to [0, 1]."""
        cfg.bounds = True
        if side == _LOW:
            scale = max(Fraction(0), scale - _EFFORT_BAND)
        elif side == _HIGH:
            scale = min(Fraction(1), scale + _EFFORT_BAND)
        out = t[_O]
        th = t[_OR] if t[_OR] is not None else \
            (out * _THINKING_SHARE.numerator) // _THINKING_SHARE.denominator
        cut = (th * (scale.denominator - scale.numerator)) // scale.denominator
        if not cut:
            return t, 0
        reasoning = t[_OR] - cut if t[_OR] is not None else None
        return t[:_O] + (out - cut, reasoning) + t[_OR + 1:], cut

    def _pings(self, state: _State, steps: list[_Req], i: int, cfg: _LaneCfg, t_new: list[int],
               u_new: list[int], ctx_new: list[PricingContext | None], point: bool) -> None:
        """Keepalive pings before request *i* (§9.3.2, D9): ``n = min(ceil(gap/κ) − 1,
        floor(M/κ))`` KEEPALIVE reads of ``P'_{i−1}`` (+ ``U'_{i−1}`` uncached) at the lane's
        model, κ apart after request ``i − 1``."""
        assert cfg.ka is not None
        kappa, max_idle = cfg.ka
        st, pv = steps[i], steps[i - 1]
        gap = st.ts - pv.ts
        n_p = self._ka_pings(gap, kappa, max_idle)
        if n_p:
            pctx = ctx_new[i - 1]
            assert pctx is not None
            ping = (u_new[i - 1], t_new[i - 1] - u_new[i - 1], 0, 0, 0, None, 0, 0, None, 0, 0)
            for k in range(1, n_p + 1):
                state.extras.append((InferenceKind.KEEPALIVE, ping, pctx, pv.ts + k * kappa,
                                     stable_id("replay", "keepalive", st.req.request_id, k)))
            if point:
                self.pings += n_p
        if point and gap > _FIVE_MIN_MS:
            self.hindsight_pings += min(-(-(gap - _FIVE_MIN_MS) // kappa), max_idle // kappa)

    @staticmethod
    def _gate(t: tuple, min_cacheable: int, pref: tuple[int, int | None]) -> tuple:
        """The min-prefix gate (§9.3.7): below the model minimum nothing is cached."""
        total = _total_input(t)
        if total < min_cacheable and (t[_R] or _writes(t)):
            return _with_split(t, total, 0, 0, pref)
        return t

    def _retry_capped(self, rq: _Req, ob: Transition) -> bool:
        """§9.3.6 retry_backoff_cap applies: ≥ 2 attempts, the final one started more than
        ``τ_obs`` after the first, and it wrote ≥ 0.5·E."""
        tau = ob.ttl_s
        atts = rq.req.attempts
        if tau is None or rq.obs_t is None or len(atts) < 2:
            return False
        return (atts[-1].ts_start_ms - atts[0].ts_start_ms > tau * 1000
                and 2 * _writes(rq.obs_t) >= ob.expected_reuse)

    def _restore_tau(self, st: _Req) -> int:
        """The default TTL (s) of the request's channel (cache-rule table), else 300."""
        assert st.serving is not None
        ctx = st.serving.pricing
        options = self.rules.rules_for(ctx.provider, ctx.channel, ctx.model).ttl_options_s
        return options[0] if options else _FIVE_MIN_S

    def _default_pref(self, st: _Req) -> tuple[int, int | None]:
        """Where new writes go when neither a policy TTL, an observed TTL nor observed writes say:
        the channel's default TTL from the cache-rule table."""
        return _pref_of_ttl(self._restore_tau(st))

    def _batch_split(self, t: tuple, ctx: PricingContext, side: int, cfg: _LaneCfg) -> tuple:
        """§9.3.5 batch: reads retained at hit band h (``floor(h·R')``), the rest become 5m
        writes; no caching at all on :data:`BATCH_NO_CACHE_CHANNELS`."""
        cfg.bounds = True
        if ctx.channel in BATCH_NO_CACHE_CHANNELS:
            return _with_split(t, _total_input(t), 0, 0, _PREF_5M)
        h = _BATCH_H_POINT if side == _POINT else (
            _BATCH_H_LOW_COST if side == _LOW else _BATCH_H_HIGH_COST)
        reads = (t[_R] * h.numerator) // h.denominator
        moved = t[_R] - reads
        if not moved:
            return t
        return (t[_U], reads, t[_W5] + moved) + t[_W1:]

    # ------------------------------------------------------------------ pricing of a state

    def _price_state(self, rq: _Req, state: _State) -> _Priced | None:
        """Point and bounds of every billable inference of *rq* under *state*."""
        book = self.book
        if state.serving_t is not None:
            serving = rq.serving
            assert serving is not None and state.serving_ctx is not None
            if state.serving_t == rq.obs_t and state.serving_ctx == serving.pricing:
                total = rq.base_serving
            else:
                total = book.price(state.serving_t, state.serving_ctx, rq.serving_ts,
                                   serving.billable, serving.usage_source, state.serving_upper)
            if state.alt_t is not None and self.rho is not None:
                assert state.band is not None
                nohit = book.price(state.alt_t, state.serving_ctx, rq.serving_ts,
                                   serving.billable, serving.usage_source, state.serving_upper)
                total = _blend(total, nohit, self.rho[state.band])
        else:
            total = rq.base_serving
        if state.others is None:
            for p in rq.base_others:
                total = _add_priced(total, p)
        else:
            for j, other in enumerate(state.others):
                if other is None:
                    continue
                _pos, inf, _ts = rq.others[j]
                nt, octx, ts = other
                if octx == inf.pricing and nt == _tup(inf.usage):
                    p = rq.base_others[j]
                else:
                    upper = inf.output_upper
                    if upper is not None and nt[_O] > upper:
                        upper = nt[_O]
                    p = book.price(nt, octx, ts, inf.billable, inf.usage_source, upper)
                total = _add_priced(total, p)
        for _kind, et, ectx, ets, _eid in state.extras:
            total = _add_priced(total, book.price(et, ectx, ets))
        return total

    # ------------------------------------------------------------------ result

    def result(self) -> ReplayResult:
        c = self.c
        base_point = base_low = base_high = 0
        base_ranged = False
        base_unpriced = 0
        cost_point = cost_low = cost_high = 0
        cost_ranged = False
        cost_unpriced = 0
        sav_point = sav_low = sav_high = 0
        sav_ranged = False
        sav_excluded = 0
        n_changed = 0
        per_lane: list[tuple[str, int]] = []
        outcomes: list[ReplayRequestOutcome] = []
        n_requests = 0
        block_reason = ("block-level policy (" + ", ".join(c.block) + "): use the block-level "
                        "replayer") if c.block else None
        for lane in self.lanes:
            self.book.rows = self.base_rows
            reqs, steps = self._prepare(lane)
            self.book.rows = self.policy_rows
            n_requests += len(reqs)
            if block_reason is not None:
                self.skipped.append((lane.lane_key, block_reason))
            results: dict[int, tuple[_Priced | None, bool, _State | None]] = {}
            if not self.observed and lane.requests:
                cfg = self._lane_cfg(lane, steps)
                for reason in cfg.skipped:
                    self.skipped.append((lane.lane_key, reason))
                if cfg.active:
                    trans: dict[int, Transition] = {}
                    if cfg.needs_trans and len(steps) >= 2:
                        trans = {t.index: t for t in classify_transitions(
                            lane, pricer=self.pricer, rules=self.rules,
                            static_prefix_floor=self.floor)}
                    point = self._pass(lane, reqs, steps, cfg, trans, _POINT)
                    passes = [point]
                    if cfg.bounds:
                        passes.append(self._pass(lane, reqs, steps, cfg, trans, _LOW))
                        passes.append(self._pass(lane, reqs, steps, cfg, trans, _HIGH))
                    keys = set().union(*(p.keys() for p in passes))
                    for k in keys:
                        states = [p.get(k) for p in passes]
                        rq = reqs[k]
                        priced = [self._price_state(rq, s) if s is not None else rq.base
                                  for s in states]
                        results[k] = (self._combine(priced), True, states[0])
            lane_point = 0
            for k, rq in enumerate(reqs):
                base = rq.base
                got = results.get(k)
                changed = got is not None
                cost = got[0] if got is not None else base
                if base is None:
                    base_unpriced += 1
                else:
                    base_point += base[0]
                    base_low += base[1]
                    base_high += base[2]
                    base_ranged = base_ranged or base[3]
                if cost is None:
                    cost_unpriced += 1
                else:
                    cost_point += cost[0]
                    cost_low += cost[1]
                    cost_high += cost[2]
                    cost_ranged = cost_ranged or cost[3]
                    lane_point += cost[0]
                if changed:
                    n_changed += 1
                    if base is None or cost is None:
                        sav_excluded += 1
                    else:
                        sav_point += base[0] - cost[0]
                        sav_low += base[1] - cost[2]
                        sav_high += base[2] - cost[1]
                        sav_ranged = sav_ranged or base[3] or cost[3]
                if self.keep:
                    outcomes.append(self._outcome(rq, cost, changed,
                                                  got[2] if got is not None else None))
            per_lane.append((lane.lane_key, lane_point))
        basis = self.basis
        rows = tuple(sorted(self.base_rows))
        baseline = self._figure(base_point, base_low, base_high, base_ranged, base_unpriced,
                                rows, estimated_label=False)
        assumptions = self._assumptions(n_changed, sav_excluded)
        if self.observed or n_changed == 0:
            cost_fig = baseline
            saving = zero(basis)
        else:
            replay_id = stable_id("replay", self.policy_key(), self.mode)
            prov = tuple(sorted(self.base_rows | self.policy_rows)) + (replay_id,)
            cost_fig = self._figure(cost_point, cost_low, cost_high, cost_ranged, cost_unpriced,
                                    prov, estimated_label=True)
            if sav_excluded and sav_excluded == n_changed:
                saving = Figure(nano=None, evidence=Evidence.ESTIMATED, basis=basis,
                                calibration=self.calibration_label, upper_bound=c.upper_bound,
                                provenance=prov,
                                note="unpriced: every changed request is unpriced")
            else:
                note = "usage-level replay (documented rules)" if self.mode == "documented" \
                    else "usage-level replay (calibrated ρ)"
                if sav_excluded:
                    note += f"; excludes {sav_excluded} unpriced changed requests"
                saving = Figure(nano=sav_point, evidence=Evidence.ESTIMATED, basis=basis,
                                low_nano=min(sav_low, sav_point) if sav_ranged else None,
                                high_nano=max(sav_high, sav_point) if sav_ranged else None,
                                calibration=self.calibration_label, upper_bound=c.upper_bound,
                                provenance=prov, note=note)
        return ReplayResult(
            policy=self.policy,
            mode=self.mode,
            baseline=baseline,
            cost=cost_fig,
            saving=saving,
            per_lane=tuple(per_lane),
            outcomes=tuple(outcomes) if self.keep else None,
            assumptions=assumptions,
            calibration=self.calibration_label,
            added_calls=self.added_calls,
            keepalive_pings=self.pings,
            lanes_skipped=tuple(self.skipped),
            n_lanes=len(self.lanes),
            n_requests=n_requests,
        )

    def policy_key(self) -> str:
        try:
            return self.policy.spec()
        except TokenbillError:  # pragma: no cover - spec() of a valid policy never fails
            return self.policy.name

    @staticmethod
    def _combine(priced: list[_Priced | None]) -> _Priced | None:
        """Point of the point pass; bounds over every pass (each contains its point)."""
        if any(p is None for p in priced):
            return None
        first = priced[0]
        assert first is not None
        low = min(p[1] for p in priced if p is not None)
        high = max(p[2] for p in priced if p is not None)
        ranged = any(p[3] for p in priced if p is not None) or len(
            {(p[0], p[1], p[2]) for p in priced if p is not None}) > 1
        return (first[0], min(low, first[0]), max(high, first[0]), ranged)

    def _figure(self, point: int, low: int, high: int, ranged: bool, unpriced_n: int,
                provenance: tuple[str, ...], *, estimated_label: bool) -> Figure:
        basis = self.basis
        reasons = sorted(self.book.unpriced_reasons)
        if unpriced_n:
            note = f"unpriced: {unpriced_n} requests unpriced"
            if reasons:
                note += " (" + ", ".join(reasons) + ")"
            evidence = Evidence.ESTIMATED if (ranged or estimated_label) else Evidence.EXACT
            return Figure(nano=None, evidence=evidence, basis=basis,
                          calibration=self.calibration_label if estimated_label
                          else Calibration.NA,
                          provenance=provenance, note=note)
        if estimated_label:
            return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis,
                          low_nano=low if ranged else None, high_nano=high if ranged else None,
                          calibration=self.calibration_label, provenance=provenance,
                          note="policy cost (usage-level replay)")
        if ranged:
            return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis, low_nano=low,
                          high_nano=high, provenance=provenance, note="range lines")
        return Figure(nano=point, evidence=Evidence.EXACT, basis=basis, provenance=provenance)

    def _outcome(self, rq: _Req, cost: _Priced | None, changed: bool,
                 state: _State | None) -> ReplayRequestOutcome:
        if state is not None and state.serving_t is not None:
            usage = _buckets(state.serving_t)
        elif rq.serving is not None:
            usage = rq.serving.usage
        else:
            usage = UsageBuckets()
        extra: tuple[Inference, ...] = ()
        if state is not None and state.extras:
            extra = tuple(Inference(inference_id=eid, kind=kind, usage=_buckets(et),
                                    pricing=ectx) for kind, et, ectx, _ets, eid in state.extras)
        if cost is None:
            return ReplayRequestOutcome(request_id=rq.req.request_id, usage=usage, extra=extra,
                                        cost_nano=None, low_nano=None, high_nano=None,
                                        changed=changed)
        ranged = cost[3]
        return ReplayRequestOutcome(request_id=rq.req.request_id, usage=usage, extra=extra,
                                    cost_nano=cost[0], low_nano=cost[1] if ranged else None,
                                    high_nano=cost[2] if ranged else None, changed=changed)

    def _assumptions(self, n_changed: int, sav_excluded: int) -> tuple[str, ...]:
        c = self.c
        out = ["usage-level replay: documented cache rules (SPEC §9.2–§9.3); unaffected requests "
               "keep their billed usage"]
        if self.mode == "calibrated":
            out.append("calibrated mode: flips to a hit weighted by ρ per gap band from the "
                       "model gate (§9.4)")
        out.extend(self.notes)
        if c.ttl:
            out.append("ttl: two-way flips; gaps within ±10 s of the TTL are priced as ranges "
                       "spanning hit and miss")
            if not self.floor:
                out.append("static prefix floor not supplied: hit→miss flips read 0 tokens")
        if c.keepalive is not None:
            out.append(f"keepalive: κ = {c.keepalive[1] // 1000} s, max idle "
                       f"{c.keepalive[2] // 1000} s; {self.pings} pings (non-clairvoyant daemon); "
                       f"hindsight minimum {self.hindsight_pings} pings (lower bound only)")
        if c.cw is not None or c.cr is not None:
            source = {"policy": "from the policy", "median": "org median of compaction events",
                      "default": "COMPACTION_SUMMARY_TOKENS_DEFAULT"}[self.summary_source]
            out.append(f"summary tokens {self.summary_tokens} ({source})")
        if c.cw is not None:
            out.append(f"compaction window {c.cw[0]} tokens: ignores re-work and quality "
                       "(upper bound, trade-off, needs_eval)")
        if c.cr is not None:
            out.append(f"cold resume: {c.cr[0]} above {c.cr[1]} tokens (trajectory, needs_eval)")
        if c.remap:
            out.append("model remap: price-only; tokenizer band [1.00, 1.35] between families "
                       "(point 1.00), none for a same-tier successor (trade-off, needs_eval)")
        if c.effort:
            out.append("effort cap: thinking share 0.505 of output when output_reasoning is "
                       "unknown; range uses scale ± 0.25 (upper bound, needs_eval)")
        if c.fast_off or c.geo_global or c.regional_to_global:
            out.append("rate transforms: exact rate arithmetic on identical tokens")
        if c.batch:
            out.append("batch: eligible single-request lanes at the batch tier; cache hit band "
                       "h = 0.64 [0.30, 0.98]")
        if c.repairs:
            out.append("repairs: " + ", ".join(sorted(c.repairs)))
        if c.upper_bound:
            out.append("saving is an upper bound")
        if c.needs_eval:
            out.append("needs_eval: quality effects are not modeled")
        if sav_excluded:
            out.append(f"{sav_excluded} of {n_changed} changed requests unpriced: excluded "
                       "from the saving")
        return tuple(dict.fromkeys(out))
