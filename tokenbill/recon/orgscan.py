"""Aggregate-level org scan (SPEC §10.3, §12.6, D32): findings from Admin / Analytics / billing data
alone, before any collector is rolled out.

:class:`OrgScan` is the detector registered as ``aggregate.org-scan``. It reads ``ctx.aggregates``
and ``ctx.cost_lines`` only (lanes are ignored; ``aggregate = True``: it runs once per run, ruling
R-E17) and reuses the reconciliation's source selection, pricing and effective-discount arithmetic
(:mod:`tokenbill.recon.reconcile`). Scopes are ``channel``, ``workspace_id`` (``api_key_id`` only
with ``ctx.thresholds["aggregate.org-scan.scope"] == "api_key"`` and break-glass, §8.5) and
``model``; never a principal. Figures that are rate arithmetic on provider-reported usage are EXACT;
triage figures are ESTIMATED upper bounds. Channels owned by extensions are skipped.

Kinds: ``cache-read-share`` (read share below 0.80 against the published 0.84 median and 0.94 top
decile, with their sources), ``write-read-thrash``, ``ttl-mix`` (1h-only writes carrying ≥ 20% of
spend), ``fast-premium`` / ``geo-premium`` / ``priority-share`` (cost − cost at standard speed,
global geo, standard tier on identical tokens), ``batch-share`` and ``effective-discount``
(``1 − invoice / list``). Money is int nano; no floats (SPEC §2.4).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from decimal import Decimal

from tokenbill.core import catalog, extensions
from tokenbill.core import findings as fh
from tokenbill.core.errors import ContractViolation, PricingError
from tokenbill.core.evidence import (
    CACHE_READ_SHARE_INVESTIGATE_BELOW,
    CACHE_READ_SHARE_MEDIAN,
    CACHE_READ_SHARE_TOP_DECILE,
    EvidenceConstant,
)
from tokenbill.core.labels import Basis, Evidence, Figure, estimated, exact, sub
from tokenbill.core.money import RATIO_CTX
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import Lane, PricingContext, UsageAggregate
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix
from tokenbill.recon import reconcile as rc
from tokenbill.recon.costmap import PRIORITY_TIERS

__all__ = ["KINDS", "OrgScan"]

KINDS = ("cache-read-share", "write-read-thrash", "ttl-mix", "fast-premium", "geo-premium",
         "priority-share", "batch-share", "effective-discount")
_DETECTOR = "aggregate.org-scan"
_WRITE_BUCKETS = ("cache_write_5m", "cache_write_1h", "cache_write_other", "cache_write_unknown")
_TTL_MIX_SPEND_SHARE = Decimal("0.20")
_DEFAULT_WS = "(default)"
_ORG_SCAN_REF = "anth-admin-apis-org-scan"
_PREMIUM_REFS = ("anth-modifiers-geo-fast-priority", "premium-modifiers")
_ALTERNATIVES = {  # premium kind → (context field, premium values, standard value)
    "fast-premium": ("speed", frozenset({"fast"}), "standard"),
    "geo-premium": ("inference_geo", frozenset({"us"}), "global"),
    "priority-share": ("service_tier", PRIORITY_TIERS, "standard"),
}
_FIXES = {
    "cache-read-share": "Install the collectors or the recorder for this scope to find the cause "
                        "of the cold cache; check gateways that strip cache_control.",
    "write-read-thrash": "Install the collectors or the recorder for this scope; review TTLs and "
                         "breakpoint placement (writes that are never read).",
    "ttl-mix": "Collect usage for this scope and run the TTL advisor before keeping 1h-only "
               "cache writes.",
    "fast-premium": "Turn fast mode off where latency allows (fastModePerSessionOptIn, "
                    "CLAUDE_CODE_DISABLE_FAST_MODE).",
    "geo-premium": "Use global inference where residency policy allows.",
    "priority-share": "Review whether the Priority Tier commitment is needed for this scope.",
    "batch-share": "Review batch eligibility: Message Batches halve token prices for work that "
                   "can wait.",
    "effective-discount": "Run `tokenbill reconcile --suggest-contract` to derive the contract "
                          "overlay and emit Claude Code modelPricing from it.",
}


def _pct_text(value: Decimal) -> str:
    return rc._dec_str(value * 100, Decimal("0.1")) + "%"


def _share_text(part: int, whole: int) -> str:
    return _pct_text(RATIO_CTX.divide(Decimal(part), Decimal(whole)))


def _const_text(c: EvidenceConstant) -> str:
    return str(c.value)


@dataclasses.dataclass
class _Scope:
    """Everything the org scan sums for one ``(channel, workspace/api key, model)`` scope."""

    channel: str
    tokens: dict[str, int] = dataclasses.field(default_factory=dict)
    costs: list[Figure] = dataclasses.field(default_factory=list)
    write_costs: list[Figure] = dataclasses.field(default_factory=list)
    batch_costs: list[Figure] = dataclasses.field(default_factory=list)
    premiums: dict[str, list[Figure]] = dataclasses.field(default_factory=dict)
    ctx: PricingContext | None = None
    last_ts: int = 0
    first_ms: int = 0
    n: int = 0
    unpriced: bool = False


def _sum(figs: Sequence[Figure], basis: Basis) -> Figure:
    return fh.sum_figures(figs, basis)


def _line_figure(amount: int, exact_line: bool, basis: Basis, row_id: str) -> Figure:
    if exact_line:
        return exact(amount, basis, provenance=(row_id,))
    return estimated(amount, basis, low=amount, high=amount, note="range line (unknown TTL/scope)",
                     provenance=(row_id,))


class OrgScan:
    """``aggregate.org-scan``: org findings from provider-side aggregates (SPEC §10.3)."""

    id = _DETECTOR
    version = "1"
    kinds = KINDS
    requires = frozenset({"aggregates"})
    aggregate = True

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Findings over ``ctx.aggregates`` / ``ctx.cost_lines`` inside ``ctx.window`` (lanes are
        ignored)."""
        start, end = ctx.window
        aggs = [a for a in ctx.aggregates if start <= a.bucket_start_ms < end]
        lines = [c for c in ctx.cost_lines
                 if start <= (rc._ordinal(c.date_utc, "date") - rc._EPOCH_ORDINAL) * rc._DAY_MS
                 < end]
        delegated = extensions.delegated_channels()
        channels = rc._plan(aggs, lines, delegated, lambda _d: True)
        by_key = (ctx.thresholds.get(f"{_DETECTOR}.scope") == "api_key"
                  and ctx.break_glass is not None)
        out: list[Finding] = []
        for name in sorted(channels):
            ch = channels[name]
            scopes = self._scopes(ch.aggs, ctx.pricer, name, by_key)
            for key in sorted(scopes, key=lambda k: tuple(v or "" for v in k)):
                out.extend(self._scope_findings(key, scopes[key], ctx, by_key))
            out.extend(self._discount_findings(ch, ctx))
        return sorted((f for f in out if fh.min_usd_gate(f, ctx)), key=lambda f: f.finding_id)

    # ----- accumulation ---------------------------------------------------------------------

    @staticmethod
    def _scopes(aggs: Sequence[UsageAggregate], pricer: Pricer, channel: str, by_key: bool
                ) -> dict[tuple[str, str | None, str | None], _Scope]:
        scopes: dict[tuple[str, str | None, str | None], _Scope] = {}
        for agg in aggs:
            dims = dict(agg.dims)
            where = dims.get("api_key_id") if by_key else (dims.get("workspace_id") or _DEFAULT_WS)
            model = dims.get("model") or None
            s = scopes.setdefault((channel, where, model), _Scope(channel))
            s.n += 1
            s.first_ms = agg.bucket_start_ms if s.n == 1 else min(s.first_ms, agg.bucket_start_ms)
            for bucket in ("uncached_input", "cache_read", *_WRITE_BUCKETS, "output"):
                s.tokens[bucket] = s.tokens.get(bucket, 0) + getattr(agg.usage, bucket)
            try:
                actx = rc._provider_ctx(channel, dims)
            except ContractViolation:
                s.unpriced = True
                continue
            priced = pricer.price_usage(agg.usage, actx, ts_ms=agg.bucket_start_ms)
            if priced.unpriced_reason is not None or priced.figure.nano is None:
                s.unpriced = True
                continue
            if agg.bucket_start_ms >= s.last_ts:
                s.last_ts = agg.bucket_start_ms
                s.ctx = dataclasses.replace(actx, service_tier="standard", speed="standard",
                                            inference_geo=None)
            basis = priced.figure.basis
            s.costs.append(priced.figure)
            for line in priced.lines:
                if line.bucket in _WRITE_BUCKETS:
                    s.write_costs.append(_line_figure(line.amount_nano, line.exact, basis,
                                                      line.rate_row_id))
            if actx.service_tier == "batch":
                s.batch_costs.append(priced.figure)
            for kind, (fname, values, standard) in _ALTERNATIVES.items():
                if getattr(actx, fname) not in values:
                    continue
                alt = pricer.price_usage(agg.usage, dataclasses.replace(actx, **{fname: standard}),
                                         ts_ms=agg.bucket_start_ms)
                if alt.unpriced_reason is None and alt.figure.nano is not None:
                    s.premiums.setdefault(kind, []).append(sub(priced.figure, alt.figure))
        return scopes

    # ----- findings -------------------------------------------------------------------------

    @staticmethod
    def _finding(kind: str, scope_dims: dict[str, str | None], *, title: str, summary: str,
                 cost: Figure, recoverable: Figure | None, evidence: Sequence[EvidenceItem],
                 n_events: int, first_ms: int, lever_class: str, references: tuple[str, ...],
                 confidence: str, lever_ids: tuple[str, ...] = ()) -> Finding:
        scope = fh.make_scope(**scope_dims)
        return fh.build_finding(
            detector_id=_DETECTOR, kind=kind, detector_version=OrgScan.version,
            category="aggregate", lever_class=lever_class, audience="org", title=title,
            summary=summary, scope=scope, n_events=n_events, n_lanes=0, n_users=0,
            first_seen_ms=first_ms, cost_observed=cost, recoverable=recoverable,
            lever_ids=lever_ids, evidence=fh.top_evidence(evidence),
            fix=Fix(text=_FIXES[kind], config_patch=None, target=None, doc_url=None),
            confidence=confidence, references=references)

    def _scope_findings(self, key: tuple[str, str | None, str | None], s: _Scope,
                        ctx: AnalysisContext, by_key: bool) -> list[Finding]:
        if s.unpriced or not s.costs or s.ctx is None:
            return []
        channel, where, model = key
        dims: dict[str, str | None] = {"channel": channel, "model": model,
                                       ("api_key_id" if by_key else "workspace_id"): where}
        label = " ".join(v for v in (channel, where, model) if v)
        basis = s.costs[0].basis
        spend = _sum(s.costs, basis)
        t = s.tokens
        total_in = t["uncached_input"] + t["cache_read"] + sum(t[b] for b in _WRITE_BUCKETS)
        reads = t["cache_read"]
        writes = sum(t[b] for b in _WRITE_BUCKETS)
        ref = f"scope:{label}"
        out: list[Finding] = []
        threshold = fh.threshold(ctx, f"{_DETECTOR}.read_share_below",
                                 _const_text(CACHE_READ_SHARE_INVESTIGATE_BELOW))
        share = RATIO_CTX.divide(Decimal(reads), Decimal(total_in)) if total_in else None
        common = {"n_events": s.n, "first_ms": s.first_ms}
        if share is not None and share < threshold:
            out.append(self._cache_read(dims, label, ref, s, ctx, spend, share, total_in, reads,
                                        common))
        if share is not None and writes > reads and share < threshold:
            recoverable = self._premium_over_read(ctx.pricer, s)
            out.append(self._finding(
                "write-read-thrash", dims,
                title=f"Cache writes exceed reads on {label}"[:120],
                summary=(f"{writes} cache-write vs {reads} cache-read tokens (read share "
                         f"{_pct_text(share)}): writes that are not read back cost the write "
                         "premium. Triage upper bound: write premium over reads."),
                cost=_sum(s.write_costs, basis), recoverable=recoverable,
                evidence=[EvidenceItem("aggregate", ref, (("writes", writes), ("reads", reads)))],
                lever_class="none", references=(_ORG_SCAN_REF, "write-without-read"),
                confidence="medium", n_events=s.n, first_ms=s.first_ms))
        w5, w1 = t["cache_write_5m"], t["cache_write_1h"]
        write_spend = _sum(s.write_costs, basis)
        if w1 > 0 and w5 == 0 and spend.nano and write_spend.nano is not None and (
                write_spend.nano >= _TTL_MIX_SPEND_SHARE * spend.nano):
            write_share = _share_text(write_spend.nano, spend.nano)
            out.append(self._finding(
                "ttl-mix", dims, title=f"1h-only cache writes on {label}"[:120],
                summary=(f"Every cache write on this scope uses the 1h TTL and writes carry "
                         f"{write_share} of spend. No mechanical fix from aggregates alone."),
                cost=write_spend, recoverable=None,
                evidence=[EvidenceItem("aggregate", ref, (("write_1h_tokens", w1),
                                                          ("write_5m_tokens", w5)))],
                lever_class="none", references=(_ORG_SCAN_REF, "cc-ttl-advisor"),
                confidence="medium", n_events=s.n, first_ms=s.first_ms))
        for kind, figs in sorted(s.premiums.items()):
            premium = _sum(figs, basis)
            if not premium.nano:
                continue
            extra = ""
            if kind == "priority-share" and spend.nano:
                extra = f" ({_share_text(premium.nano, spend.nano)} of spend)"
            out.append(self._finding(
                kind, dims, title=f"{kind.replace('-', ' ').capitalize()} on {label}"[:120],
                summary=(f"Premium over identical tokens at standard rates{extra}: rate "
                         "arithmetic on provider-reported usage."),
                cost=premium, recoverable=premium,
                evidence=[EvidenceItem("aggregate", ref, (("nano", premium.nano),))],
                lever_class="rate", references=_PREMIUM_REFS, confidence="high",
                lever_ids=tuple(lv.lever_id for lv in catalog.levers_for_kind(kind)),
                n_events=s.n, first_ms=s.first_ms))
        if s.batch_costs:
            batch = _sum(s.batch_costs, basis)
            if batch.nano and spend.nano:
                share_b = RATIO_CTX.divide(Decimal(batch.nano), Decimal(spend.nano))
                out.append(self._finding(
                    "batch-share", dims, title=f"Batch share of spend on {label}"[:120],
                    summary=(f"{_pct_text(share_b)} of this scope's spend runs in the batch tier. "
                             "No mechanical fix from aggregates alone."),
                    cost=batch, recoverable=None,
                    evidence=[EvidenceItem("aggregate", ref, (("batch_share",
                                                              rc._dec_str(share_b,
                                                                          Decimal("0.0001"))),))],
                    lever_class="none", references=("anth-batch-stacking",), confidence="high",
                    lever_ids=tuple(lv.lever_id for lv in catalog.levers_for_kind("batch-share")),
                    n_events=s.n, first_ms=s.first_ms))
        return out

    def _cache_read(self, dims: dict[str, str | None], label: str, ref: str, s: _Scope,
                    ctx: AnalysisContext, spend: Figure, share: Decimal, total_in: int,
                    reads: int, common: dict[str, int]) -> Finding:
        median = CACHE_READ_SHARE_MEDIAN
        top = CACHE_READ_SHARE_TOP_DECILE
        below = CACHE_READ_SHARE_INVESTIGATE_BELOW
        target = RATIO_CTX.multiply(Decimal(total_in), Decimal(_const_text(median)))
        gap_tokens = int(target.to_integral_value()) - reads
        recoverable = None
        if gap_tokens > 0:
            assert s.ctx is not None
            try:
                nano = (fh.rate_nano(ctx.pricer, s.ctx, s.last_ts, "cache_write_5m", gap_tokens)
                        - fh.rate_nano(ctx.pricer, s.ctx, s.last_ts, "cache_read", gap_tokens))
            except PricingError:
                nano = None
            if nano is not None:
                recoverable = estimated(
                    max(nano, 0), spend.basis, upper_bound=True,
                    note=(f"triage to the median benchmark {_const_text(median)} "
                          f"({median.source_url})"))
        attrs: tuple[tuple[str, str | int], ...] = (
            ("read_share", rc._dec_str(share, Decimal("0.0001"))),
            ("benchmark_median", _const_text(median)),
            ("benchmark_top_decile", _const_text(top)),
            ("investigate_below", _const_text(below)),
            ("source", median.source_url), ("source_top_decile", top.source_url),
            ("checked_on", median.checked_on), ("tokens", total_in))
        return self._finding(
            "cache-read-share", dims,
            title=f"Low cache-read share ({_pct_text(share)}) on {label}"[:120],
            summary=(f"Cache reads are {_pct_text(share)} of input tokens, below "
                     f"{_const_text(below)}; published median {_const_text(median)}, top decile "
                     f"{_const_text(top)} ({median.source_url}, checked {median.checked_on}). "
                     "Triage upper bound to the median."),
            cost=spend, recoverable=recoverable,
            evidence=[EvidenceItem("aggregate", ref, attrs)], lever_class="none",
            references=(_ORG_SCAN_REF, median.finding_id), confidence="medium",
            n_events=common["n_events"], first_ms=common["first_ms"])

    @staticmethod
    def _premium_over_read(pricer: Pricer, s: _Scope) -> Figure | None:
        """``Σ W_b · (w_b − r)`` over the write buckets, ESTIMATED upper bound."""
        assert s.ctx is not None
        total = 0
        try:
            for bucket in _WRITE_BUCKETS:
                qty = s.tokens[bucket]
                if qty:
                    total += (fh.rate_nano(pricer, s.ctx, s.last_ts, bucket, qty)
                              - fh.rate_nano(pricer, s.ctx, s.last_ts, "cache_read", qty))
        except PricingError:
            return None
        return estimated(max(total, 0), s.costs[0].basis, upper_bound=True,
                         note="write premium over reads (triage upper bound)")

    def _discount_findings(self, ch: rc._Channel, ctx: AnalysisContext) -> list[Finding]:
        """``effective-discount`` (info) per (channel, model): ``1 − invoice / list``."""
        prov = rc._price_provider(ch, ctx.pricer)
        inv = rc._invoice(ch)
        mults = rc._multipliers(ch, prov, inv, lambda _d: True)
        out: list[Finding] = []
        for model, (m, amount) in sorted(mults.items()):
            if m == 1:
                continue
            discount = rc._dec_str(1 - m, Decimal("0.0001"))
            model_dim = None if model == "*" else model
            label = " ".join(v for v in (ch.channel, model_dim) if v)
            evidence = self._discount_days(ch.channel, model, inv, prov)
            n_lines = sum(1 for line, mp in ch.lines if model == "*" or mp.model == model)
            first = min((line.date_utc for line, mp in ch.lines
                         if model == "*" or mp.model == model), default="")
            first_ms = (rc._ordinal(first, "date") - rc._EPOCH_ORDINAL) * rc._DAY_MS if first else 0
            out.append(self._finding(
                "effective-discount", {"channel": ch.channel, "model": model_dim},
                title=f"Effective discount {discount} on {label}"[:120],
                summary=(f"Invoice vs list implies an effective discount of {discount} "
                         "(1 − invoice/list; EXACT arithmetic on invoice data). "
                         "No mechanical fix: record it as a contract overlay."),
                cost=Figure(nano=amount, evidence=Evidence.EXACT, basis=Basis.INVOICE),
                recoverable=None, evidence=evidence, lever_class="none",
                references=("cc-anthropic-discount-visibility",), confidence="high",
                n_events=n_lines, first_ms=first_ms))
        return out

    @staticmethod
    def _discount_days(channel: str, model: str, inv: rc._Invoice, prov: rc._Provider
                       ) -> list[EvidenceItem]:
        """Per model-day discounts (list = the lines' list amounts, else our price)."""
        days: dict[str, list[int]] = {}
        if model == "*":
            for date, (amount, listed, _n, missing) in inv.totals.items():
                if not missing and listed:
                    days[date] = [amount, listed]
        else:
            priced: dict[str, int] = {}
            for (date, _ws, m, bucket, tier), value in prov.cells.items():
                if m == model and not tier and bucket != "web_search" and not value[2]:
                    priced[date] = priced.get(date, 0) + value[1]
            for (date, _ws, m, _bucket), (amount, listed, _n, missing) in inv.cells.items():
                if m != model:
                    continue
                cur = days.setdefault(date, [0, 0, 0])
                cur[0] += amount
                cur[1] += listed
                cur[2] += missing
            for date, cur in list(days.items()):
                base = cur[1] if not cur[2] else priced.get(date, 0)
                days[date] = [cur[0], base]
        items = []
        for date, (amount, base) in sorted(days.items()):
            if base:
                d = rc._dec_str(1 - RATIO_CTX.divide(Decimal(amount), Decimal(base)),
                                Decimal("0.0001"))
                items.append(EvidenceItem("aggregate", f"{channel}:{model}:{date}",
                                          (("discount", d), ("nano", amount))))
        return items
