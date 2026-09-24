"""Cache-TTL detectors (SPEC §10.2 ``cache.cold-resume`` and ``cache.ttl-advisor``, D8, D9, D11,
D26; package DETECT-CACHE).

* :class:`ColdResume` — a main conversation resumed after the cache expired, with at least
  100,000 tokens of context rewritten: the rewrite billed is EXACT, its premium over a warm read
  ESTIMATED (D11).
* :class:`TtlAdvisor` — per cohort (team, lane kind, billing path) the observed policy, ``ttl=5m``,
  ``ttl=1h`` and — for SDK/API agent lanes only (never Claude Code, R-E11) — a 240 s keepalive are
  replayed through ``ctx.replayer``; the cheapest policy **different from the observed TTL** is
  recommended when it saves at least ``max(min_usd, 2% of cohort spend)``. When fewer than 60% of
  the cohort's principals (at least ``k`` of them) are individually cheaper under a TTL
  recommendation, ``ttl-heterogeneous`` recommends per-cohort (MDM group) delivery instead
  (counts only). Evidence: the gap histogram, Anthropic's 1-in-20 rule verdict and its agreement,
  the keepalive break-even ``κ(w/r − 1)``.

See ``detect.cache_miss`` for the shared conventions (cohorts, allowance labeling, ``min_usd``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from tokenbill.core.errors import TokenbillError
from tokenbill.core.evidence import (
    COLD_RESUME_MIN_CONTEXT,
    KEEPALIVE_INTERVAL_S,
    KEEPALIVE_MAX_IDLE_S,
    TTL_RULE_GAP_SHARE_5_60MIN,
    keepalive_break_even_s,
)
from tokenbill.core.findings import min_usd_nano, threshold
from tokenbill.core.labels import Figure
from tokenbill.core.policy import to_spec
from tokenbill.core.records import Lane, LaneKind, PricingContext
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix, Policy, ReplayResult
from tokenbill.detect.cache_miss import (
    API_CACHE_DOC,
    DETECTOR_VERSION,
    Cohort,
    Emit,
    Prices,
    Tally,
    applicable_levers,
    cc_gates,
    cohorts,
    combine,
    decimal_str,
    emit,
    evidence_item,
    gateway_of,
    is_claude_code,
    patch,
    replay,
    saving_figure,
    settings_doc,
    sort_findings,
    transitions,
)

__all__ = [
    "GAP_BANDS",
    "ColdResume",
    "TtlAdvisor",
    "keepalive_spec",
    "ttl_spec",
]

# =============================================================================================
# cache.cold-resume
# =============================================================================================

_COLD_RESUME_REFS = ("cc-cold-resume", "compaction-timing", "channel-code-review-hooks")
_HOOK_VALUE = ('[{"hooks":[{"command":"python3 hooks/tokenbill_session_start.py",'
               '"type":"command"}],"matcher":"resume"}]')


class ColdResume:
    """``cache.cold-resume`` (SPEC §10.2, Appendix A.5): on MAIN lanes, a ``ttl-expiry`` miss with
    ``W_i ≥ 0.5·P_{i−1}`` and ``P_{i−1} ≥ 100,000`` (``COLD_RESUME_MIN_CONTEXT``; threshold
    ``cache.cold-resume.min_context``).

    ``cost_observed`` = ``min(W_i, P_{i−1})·w_billed`` — the rewrite billed (EXACT);
    ``recoverable`` = ``min(W_i, P_{i−1})·(w − r)`` — the premium over a warm read (ESTIMATED,
    D11). Levers: the SessionStart hook (behavioral, never projected), compact-on-resume
    (trajectory) and the main TTL.
    """

    id = "cache.cold-resume"
    version = DETECTOR_VERSION
    kinds = ("cold-resume",)
    requires = frozenset({"usage_sequence", "timing", "ttl_split"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One ``cold-resume`` finding per MAIN cohort at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        min_ctx = int(threshold(ctx, f"{self.id}.min_context",
                                str(COLD_RESUME_MIN_CONTEXT.value)))
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            if cohort.lane_kind != LaneKind.MAIN.value:
                continue
            tally = Tally()
            for lane in cohort.lanes:
                reqs = {r.request_id: r for r in lane.requests}
                for t in transitions(lane, ctx):
                    if not t.is_miss_event or t.cause != "ttl-expiry":
                        continue
                    prefix = t.prev_prefix
                    req = reqs[t.request_id]
                    inf = req.serving_inference
                    assert inf is not None
                    writes = inf.usage.cache_write
                    if prefix < min_ctx or 2 * writes < prefix:
                        continue
                    tokens = min(writes, prefix)
                    ts = req.ts_start_ms
                    tally.hit(lane, ts)
                    billed = prices.written(inf.pricing, ts, inf.usage, tokens)
                    premium = combine((1, billed),
                                      (-1, prices.line(inf.pricing, ts, "cache_read", tokens)))
                    if billed is None or premium is None:
                        tally.unpriced += 1
                        continue
                    tally.cost.add_money(billed)
                    tally.rec.add_money(premium)
                    tally.items.append(evidence_item(
                        "transition", t.request_id, gap_ms=t.gap_ms, context=prefix,
                        tokens=tokens, nano=billed.point))
            if tally.events == tally.unpriced:
                continue
            found = self._finding(ctx, cohort, tally, min_ctx)
            if found is not None:
                out.append(found)
        return sort_findings(out)

    def _finding(self, ctx: AnalysisContext, cohort: Cohort, tally: Tally,
                 min_ctx: int) -> Finding | None:
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        cost = tally.cost.billed(basis)
        recoverable = tally.rec.estimate(basis, "premium of the rewrite over a warm read")
        text = ("Before stepping away, /compact to continue the same task or /clear to start a "
                "new one; a SessionStart hook warns on resume when the cache has likely expired "
                "and states the rebuild cost.")
        config = None
        target = "code"
        gates: tuple[str, ...] = ()
        doc = API_CACHE_DOC
        if any(is_claude_code(lane) for lane in lanes):
            config = patch(("hooks.SessionStart", _HOOK_VALUE))
            target = "claude-code-managed-settings"
            gates = cc_gates("hooks.SessionStart")
            doc = settings_doc("hooks.SessionStart") or doc
        spec = Emit(
            kind="cold-resume", category="breaker", lever_class="behavioral",
            title=f"Cold resumes of large conversations in {cohort.label()} lanes",
            summary=(f"{tally.events} resumes after the cache expired rewrote at least "
                     f"{min_ctx:,} tokens of context each in "
                     f"{cohort.label()} lanes; the rewrite billed is exact, the premium over a "
                     f"warm read is estimated."),
            references=_COLD_RESUME_REFS, lever_ids=applicable_levers("cold-resume", lanes),
            fix=Fix(text=text, config_patch=config, target=target, doc_url=doc, gates=gates),
            confidence="high")
        return emit(self, ctx, cohort, tally, spec, cost, recoverable)


# =============================================================================================
# cache.ttl-advisor
# =============================================================================================

#: Gap histogram bands (label, low s inclusive, high s exclusive or None).
GAP_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("gaps_0_1m", 0, 60), ("gaps_1_5m", 60, 300), ("gaps_5_10m", 300, 600),
    ("gaps_10_30m", 600, 1800), ("gaps_30_60m", 1800, 3600), ("gaps_60m_plus", 3600, None))
_TTL_KINDS = ("ttl-1h-recommended", "ttl-5m-recommended", "keepalive-recommended",
              "ttl-heterogeneous")
_ADVISED_KINDS = frozenset({LaneKind.MAIN.value, LaneKind.SUBAGENT.value,
                            LaneKind.WORKFLOW_AGENT.value, LaneKind.API_RUN.value})
_TTL_REFS = ("cc-ttl-advisor", "cc-ttl-policy", "anth-ttl-choice-keepalive",
             "keepalive-economics", "cc-apps-gateway-routing-tax")
_GATEWAY_NOTE = " Behind a gateway: forward anthropic-beta; the Claude apps gateway cannot use 1h."
_SAVING_KEYS = {"ttl-1h-recommended": "saving_ttl_1h_nano",
                "ttl-5m-recommended": "saving_ttl_5m_nano",
                "keepalive-recommended": "saving_keepalive_nano"}
_SPEND_SHARE_DEFAULT = "0.02"
_HETEROGENEITY_DEFAULT = "0.60"


def ttl_spec(lane_kind: str, ttl: str) -> str:
    """The canonical policy spec the advisor replays for a TTL: ``ttl=<ttl>@lane_kind:<kind>``."""
    return to_spec(Policy(name="", ttl=((f"lane_kind:{lane_kind}", ttl),)))


def keepalive_spec(lane_kind: str) -> str:
    """The canonical keepalive spec the advisor replays: 240 s pings, 3,600 s maximum idle."""
    kappa = KEEPALIVE_INTERVAL_S.value
    max_idle = KEEPALIVE_MAX_IDLE_S.value
    assert isinstance(kappa, int) and isinstance(max_idle, int)
    return to_spec(Policy(name="", keepalive=(f"lane_kind:{lane_kind}", kappa, max_idle)))


def _billing_path(lane: Lane) -> str:
    first = lane.requests[0]
    if first.attribution.billing_path:
        return first.attribution.billing_path
    si = first.serving_inference
    return si.pricing.billing_path if si is not None else "unknown"


def _observed_ttl(lanes: Sequence[Lane]) -> str:
    """``5m`` / ``1h`` / ``mixed`` / ``unknown`` from the cohort's billed write tokens (unknown-TTL
    writes count by their hint)."""
    five = hour = 0
    for lane in lanes:
        for req in lane.requests:
            for inf in req.billable_inferences:
                u = inf.usage
                five += u.cache_write_5m
                hour += u.cache_write_1h
                if u.cache_write_unknown and inf.pricing.write_ttl_hint == "1h":
                    hour += u.cache_write_unknown
                elif u.cache_write_unknown and inf.pricing.write_ttl_hint == "5m":
                    five += u.cache_write_unknown
    if five and hour:
        return "mixed"
    if hour:
        return "1h"
    if five:
        return "5m"
    return "unknown"


class _Sub:
    """An advisor cohort: a (team, lane kind, billing class) cohort narrowed to a billing path."""

    def __init__(self, cohort: Cohort, billing_path: str, lanes: list[Lane],
                 split: bool) -> None:
        self.cohort = cohort
        self.billing_path = billing_path
        self.lanes = lanes
        self.split = split

    def scope_extra(self) -> dict[str, str | None]:
        return {"billing_path": self.billing_path} if self.split else {}


class TtlAdvisor:
    """``cache.ttl-advisor`` (SPEC §10.2, D8, D9, R-E11). See the module docstring.

    Replays (through ``ctx.replayer``, on the cohort's lanes only): ``observed``,
    :func:`ttl_spec` for 5m and 1h, and :func:`keepalive_spec` for API_RUN cohorts with SDK/API
    lanes. ``cost_observed`` is the cohort spend (EXACT, LIST_EQUIVALENT for allowance cohorts);
    ``recoverable`` the recommended policy's saving (ESTIMATED; calibration from the replay).
    Thresholds: ``min_usd``, ``cache.ttl-advisor.spend_share`` (0.02) and
    ``cache.ttl-advisor.heterogeneity_share`` (0.60). No findings without a replayer.
    """

    id = "cache.ttl-advisor"
    version = DETECTOR_VERSION
    kinds = _TTL_KINDS
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """At most one recommendation per advisor cohort."""
        if ctx.replayer is None:
            return []
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            if cohort.lane_kind not in _ADVISED_KINDS:
                continue
            by_path: dict[str, list[Lane]] = {}
            for lane in cohort.lanes:
                by_path.setdefault(_billing_path(lane), []).append(lane)
            split = len(by_path) > 1
            for path in sorted(by_path):
                found = self._advise(ctx, prices, _Sub(cohort, path, by_path[path], split))
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    def _advise(self, ctx: AnalysisContext, prices: Prices, sub: _Sub) -> Finding | None:
        cohort, lanes = sub.cohort, sub.lanes
        basis = cohort.basis(ctx.pricer)
        observed = replay(ctx, lanes, "observed")
        if observed is None or observed.baseline.nano is None:
            return None
        spend = observed.baseline
        if spend.basis is not basis:
            return None
        current = _observed_ttl(lanes)
        candidates: list[tuple[str, str]] = []   # (kind, spec)
        if current != "1h":
            candidates.append(("ttl-1h-recommended", ttl_spec(cohort.lane_kind, "1h")))
        if current != "5m":
            candidates.append(("ttl-5m-recommended", ttl_spec(cohort.lane_kind, "5m")))
        if cohort.lane_kind == LaneKind.API_RUN.value and \
                any(not is_claude_code(lane) for lane in lanes):
            candidates.append(("keepalive-recommended", keepalive_spec(cohort.lane_kind)))
        results: dict[str, ReplayResult] = {}
        for kind, spec in candidates:
            result = replay(ctx, lanes, spec)
            if result is not None and result.saving.nano is not None and \
                    result.saving.basis is basis:
                results[kind] = result
        if not results:
            return None
        best_kind = max(results, key=lambda k: (results[k].saving.nano or 0,
                                                -_TTL_KINDS.index(k)))
        best = results[best_kind]
        saving = best.saving.nano or 0
        share = threshold(ctx, f"{self.id}.spend_share", _SPEND_SHARE_DEFAULT)
        if saving <= 0 or saving < min_usd_nano(ctx) or Decimal(saving) < share * spend.nano:
            return None
        return self._finding(ctx, prices, sub, current, best_kind, observed, best, results)

    # ---------- the finding ----------

    def _finding(self, ctx: AnalysisContext, prices: Prices, sub: _Sub, current: str, kind: str,
                 observed: ReplayResult, best: ReplayResult,
                 results: dict[str, ReplayResult]) -> Finding | None:
        cohort, lanes = sub.cohort, sub.lanes
        basis = cohort.basis(ctx.pricer)
        tally = Tally()
        gaps: list[int] = []
        for lane in lanes:
            ts = lane.requests[0].ts_start_ms
            tally.hit(lane, ts)
            gaps.extend(t.gap_ms for t in transitions(lane, ctx))
        tally.events = len(gaps)
        ttl = {"ttl-1h-recommended": "1h", "ttl-5m-recommended": "5m"}.get(kind)
        hetero = None
        if ttl is not None:
            hetero = self._heterogeneity(ctx, lanes, observed, best)
            if hetero is not None:
                kind = "ttl-heterogeneous"
        evidence = self._evidence(ctx, prices, lanes, gaps, ttl, results, current, hetero)
        rule = _rule_verdict(gaps)
        recoverable = saving_figure(best, basis)
        if recoverable is None:  # pragma: no cover - guarded by _advise
            return None
        fix, levers = self._fix(kind, ttl, cohort, lanes, hetero)
        agree = rule == ttl if ttl is not None else None
        label = cohort.label()
        if kind == "keepalive-recommended":
            title = f"Keepalive pings would keep {label} caches warm"
            what = "a 240 s keepalive (SDK)"
        elif kind == "ttl-heterogeneous":
            title = f"TTL {ttl} pays for only some {label} principals: deliver per cohort"
            what = f"ttl={ttl} for the whole cohort"
        else:
            title = f"Switch {label} lanes to a {ttl} cache TTL"
            what = f"ttl={ttl}"
        summary = (f"Replaying {len(lanes)} {label} lanes (observed TTL {current}) under {what} "
                   f"is cheaper than the observed policy and every other candidate (estimated "
                   f"saving). Rule of thumb (1h when more than 1 in 20 gaps fall in 5-60 min): "
                   f"{rule}.")
        if hetero is not None:
            summary += (f" Only {hetero[1]} of {hetero[0]} principals are individually cheaper, "
                        f"so deliver the setting per MDM group instead of org-wide.")
        spec = Emit(
            kind=kind, category="lever", lever_class="cache_transform", title=title,
            summary=summary, references=_TTL_REFS, lever_ids=levers, fix=fix,
            confidence="high" if agree else "medium",
            scope_extra=sub.scope_extra())
        cost: Figure = observed.baseline
        return emit(self, ctx, cohort, tally, spec, cost, recoverable, evidence)

    def _heterogeneity(self, ctx: AnalysisContext, lanes: Sequence[Lane],
                       observed: ReplayResult, best: ReplayResult) -> tuple[int, int] | None:
        """``(principals, principals cheaper)`` when fewer than 60% of at least ``k`` principals
        are individually cheaper under the recommendation, else None."""
        base = dict(observed.per_lane)
        policy = dict(best.per_lane)
        per_principal: dict[str, int] = {}
        for lane in lanes:
            principal = lane.requests[0].attribution.principal
            if principal is None or lane.lane_key not in base or lane.lane_key not in policy:
                continue
            delta = base[lane.lane_key] - policy[lane.lane_key]
            per_principal[principal] = per_principal.get(principal, 0) + delta
        n = len(per_principal)
        if n < ctx.k_anonymity:
            return None
        cheaper = sum(1 for v in per_principal.values() if v > 0)
        share = threshold(ctx, f"{self.id}.heterogeneity_share", _HETEROGENEITY_DEFAULT)
        if Decimal(cheaper) >= share * n:
            return None
        return (n, cheaper)

    def _evidence(self, ctx: AnalysisContext, prices: Prices, lanes: Sequence[Lane],
                  gaps: Sequence[int], ttl: str | None, results: dict[str, ReplayResult],
                  current: str, hetero: tuple[int, int] | None) -> list[EvidenceItem]:
        hist = Counter[str]()
        for gap in gaps:
            for label, lo, hi in GAP_BANDS:
                if gap >= lo * 1000 and (hi is None or gap < hi * 1000):
                    hist[label] += 1
                    break
        rule = _rule_verdict(gaps)
        n_mid = sum(1 for g in gaps if 300_000 < g <= 3_600_000)
        items = [
            evidence_item("aggregate", "ttl:gap-histogram", transitions=len(gaps),
                          **{label: hist.get(label, 0) for label, _, _ in GAP_BANDS}),
            evidence_item("aggregate", "ttl:rule-1-in-20", gaps_5_60m=n_mid,
                          share_5_60m_pct=decimal_str(Decimal(100 * n_mid) / len(gaps))
                          if gaps else "0.0",
                          verdict=rule, agrees="n/a" if ttl is None else
                          ("yes" if rule == ttl else "no")),
            evidence_item("aggregate", "ttl:replays", observed_ttl=current,
                          **{_SAVING_KEYS[k]: r.saving.nano for k, r in sorted(results.items())}),
        ]
        breakeven = _break_even(ctx, prices, lanes)
        if breakeven is not None:
            items.append(evidence_item("aggregate", "ttl:keepalive-break-even",
                                       seconds=breakeven[0], minutes=breakeven[1]))
        if hetero is not None:
            items.append(evidence_item("aggregate", "ttl:heterogeneity", principals=hetero[0],
                                       principals_cheaper=hetero[1]))
        return items

    def _fix(self, kind: str, ttl: str | None, cohort: Cohort, lanes: Sequence[Lane],
             hetero: tuple[int, int] | None) -> tuple[Fix, tuple[str, ...]]:
        gateway = any(gateway_of(lane) for lane in lanes)
        if kind == "keepalive-recommended":
            levers = applicable_levers("keepalive-recommended", lanes) or ("sdk.keepalive",)
            text = ("SDK keepalive: while an agent idles, re-send the cached prefix with "
                    "max_tokens 0 every 240 s for up to 3,600 s (not with stream, structured "
                    "outputs, a forced tool_choice or thinking enabled); pings bill reads only.")
            return Fix(text=text, config_patch=None, target="sdk", doc_url=API_CACHE_DOC), levers
        assert ttl is not None
        levers = applicable_levers(kind, lanes)
        claude_code = [lane for lane in lanes if is_claude_code(lane)]
        per_cohort = (" Deliver it per cohort (MDM group or Claude apps gateway IdP group), not "
                      "org-wide.") if hetero is not None else ""
        note = _GATEWAY_NOTE if gateway and ttl == "1h" else ""
        if claude_code and cohort.lane_kind in (LaneKind.MAIN.value, LaneKind.SUBAGENT.value,
                                                LaneKind.WORKFLOW_AGENT.value):
            main = cohort.lane_kind == LaneKind.MAIN.value
            key = "promptCacheTtl" if main else "subagentPromptCacheTtl"
            env = "CLAUDE_CODE_PROMPT_CACHE_TTL" if main else \
                "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"
            text = (f"Set {key} to \"{ttl}\" in managed settings (env {env} for gateway "
                    f"fleets).{per_cohort}{note}")
            if len(claude_code) < len(lanes):
                text += (" For the SDK lanes of this cohort set cache_control ttl "
                         f"\"{ttl}\" on the breakpoints.")
            fix = Fix(text=text, config_patch=patch((key, f'"{ttl}"')),
                      target="claude-code-managed-settings", doc_url=settings_doc(key),
                      gates=cc_gates(key))
            return fix, levers
        text = (f"Set cache_control {{\"type\": \"ephemeral\", \"ttl\": \"{ttl}\"}} on the "
                f"prompt-cache breakpoints of these agents.{per_cohort}{note}")
        return Fix(text=text, config_patch=None, target="sdk", doc_url=API_CACHE_DOC), levers


def _rule_verdict(gaps: Sequence[int]) -> str:
    """Anthropic's rule of thumb: 1h pays when more than 1 in 20 gaps fall in (5, 60] min."""
    if not gaps:
        return "5m"
    share = TTL_RULE_GAP_SHARE_5_60MIN.value
    assert isinstance(share, Decimal)
    n_mid = sum(1 for g in gaps if 300_000 < g <= 3_600_000)
    return "1h" if Decimal(n_mid) > share * len(gaps) else "5m"


def _break_even(ctx: AnalysisContext, prices: Prices,
                lanes: Sequence[Lane]) -> tuple[int, str] | None:
    """Keepalive break-even idle ``κ(w/r − 1)`` (seconds, minutes) at the cohort's dominant
    serving model (write at 5m, read)."""
    counts: Counter[tuple[str, str]] = Counter()
    sample: dict[tuple[str, str], tuple[PricingContext, int]] = {}
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None:
                continue
            key = (inf.pricing.model, inf.pricing.channel)
            counts[key] += 1
            sample.setdefault(key, (inf.pricing, req.ts_start_ms))
    if not counts:
        return None
    key = min(counts, key=lambda k: (-counts[k], k))
    pricing, ts = sample[key]
    try:
        rates = ctx.pricer.resolve(pricing, ts_ms=ts)
    except TokenbillError:
        return None
    if rates is None or not rates.cache_read or not rates.cache_write_5m:
        return None
    seconds = keepalive_break_even_s(rates.cache_write_5m, rates.cache_read)
    whole = int(seconds.to_integral_value())
    return whole, decimal_str(seconds / 60)

