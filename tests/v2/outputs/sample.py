"""A synthetic ``RunResult`` with every slot filled (OUT renderer tests; imported only within this
area). Hand-built from the ``core.types`` contracts; no real data. The policy pack's README and hook
text carry the content canary: renderers publish digests of them, never the text."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from tokenbill.core.builders import CANARY
from tokenbill.core.facts import load
from tokenbill.core.kanon import publish
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    estimated,
    exact,
    unpriced,
)
from tokenbill.core.records import ContentTier, UsageBuckets
from tokenbill.core.testing import published_for_tests
from tokenbill.core.types import (
    AbResult,
    ActionPlan,
    AggRow,
    BillSummary,
    CalibrationReport,
    ChannelVerdict,
    CheckResult,
    CheckViolation,
    ContractOverlay,
    DataQualityNote,
    Discrepancy,
    EvidenceItem,
    Finding,
    Fix,
    GuardResult,
    LeverResult,
    MeasurementResult,
    MeasurePlan,
    Policy,
    PolicyEntry,
    PolicyPack,
    PricedTotal,
    PricingReport,
    PrivacyInfo,
    RateCardInfo,
    RawAggregate,
    ReconciliationReport,
    ReconRow,
    ReplayResult,
    RunResult,
    Scope,
    SourceInfo,
)

USD = 10**9
DAY_MS = 86_400_000
T0 = 20_689 * DAY_MS          # 2026-08-24T00:00Z
T1 = 20_719 * DAY_MS          # 2026-09-23T00:00Z (exclusive)
SHA = "ab" * 32


def usd(amount: str) -> int:
    """Nano-USD of a decimal dollar string."""
    return int(Decimal(amount) * USD)


def priced(exact_usd: str, *, allowance: str | None = None, pool: str | None = None,
           est: tuple[str, str, str] | None = None, unpriced_n: int = 0) -> PricedTotal:
    return PricedTotal(
        exact=exact(usd(exact_usd), Basis.LIST, provenance=("anthropic/anthropic_api/x",)),
        estimated=None if est is None else estimated(
            usd(est[0]), Basis.LIST, low=usd(est[1]), high=usd(est[2]),
            note="unknown-TTL writes priced as a range"),
        allowance=None if allowance is None else exact(usd(allowance), Basis.LIST_EQUIVALENT),
        priced_inferences=100, unpriced_inferences=unpriced_n, unpriced_tokens=1200 * unpriced_n,
        coverage="0.998" if unpriced_n else "1",
        pool=None if pool is None else exact(usd(pool), Basis.LIST_EQUIVALENT))


def agg_row(dims: tuple[tuple[str, str | None], ...], users: int, total: str,
            **kw: object) -> AggRow:
    return AggRow(dims=dims, n_users=users, n_requests=users * 40,
                  usage=UsageBuckets(uncached_input=users * 1000, cache_read=users * 90_000,
                                     cache_write_5m=users * 4000, output=users * 3000),
                  priced=priced(total, **kw))  # type: ignore[arg-type]


def breakdowns() -> tuple[tuple[str, object], ...]:
    teams = RawAggregate(group_by=("team",), window=(T0, T1), rows=(
        agg_row((("team", "payments"),), 12, "18000.50", allowance="900"),
        agg_row((("team", "platform\x1b[31m"),), 8, "9000.25"),
        agg_row((("team", "tiny"),), 2, "15.00"),
        agg_row((("team", None),), 0, "3.00"),   # users unknown (R-E10)
    ))
    buckets = RawAggregate(group_by=("bucket",), window=(T0, T1), rows=(
        agg_row((("bucket", "cache_read"),), 20, "12000"),
        agg_row((("bucket", "output"),), 20, "15000.75"),
    ))
    return (("team", publish(teams, k=5)), ("bucket", published_for_tests(buckets)))


def bill() -> BillSummary:
    total = priced("27000.75", allowance="9880.12", pool="321.40",
                   est=("610", "290", "840"), unpriced_n=3)
    return BillSummary(total=total, esr="0.71", breakdowns=breakdowns(),  # type: ignore[arg-type]
                       naive_ratio="2.33",
                       footnotes=("trace@1 cache writes priced at the 5m rate (D12)",))


def recon() -> ReconciliationReport:
    rows = (
        ReconRow(key=(("channel", "anthropic_api"), ("date", "2026-09-01")), ledger_tokens=1000,
                 provider_tokens=1000, ledger_nano=usd("100"), priced_provider_nano=usd("100"),
                 invoice_nano=usd("99.5"), rate_card_error_pct="0.5", coverage_pct="100.5",
                 status="within_tolerance", residual_code=None),
        ReconRow(key=(("channel", "bedrock"), ("date", "2026-09-01")), ledger_tokens=500,
                 provider_tokens=None, ledger_nano=usd("50"), priced_provider_nano=None,
                 invoice_nano=None, rate_card_error_pct=None, coverage_pct=None,
                 status="unexplained", residual_code="unmapped_cost_type"),
    )
    contract = ContractOverlay(name="derived-2026-09", multiplier=Decimal("0.9"),
                               overrides=(("claude-opus-5-5", (("output", Decimal("45")),)),),
                               effective_from="2026-09-01", effective_to=None, derived=True,
                               assumed_fields=("cache_write_1h",), channels=("anthropic_api",),
                               sha256=SHA)
    return ReconciliationReport(
        window=("2026-08-24", "2026-09-23"), tolerance_pct="0.5", unexplained_tolerance_pct="1.0",
        rows=rows, token_coverage_pct="99.9", dollar_coverage_pct="100.2",
        rate_card_error=("0.1", "0.4", "0.9"), over_count_rows=0,
        effective_discount=(("anthropic_api:claude-opus-5-5:output", "0.1"),),
        residuals=(("seat_allowance_unmetered", usd("12")), ("cents_rounding", 3)),
        unexplained_nano=usd("0.42"),
        channels=(ChannelVerdict("anthropic_api", "reconciled", ("anthropic.cost_report",), True),
                  ChannelVerdict("bedrock", "not_reconciled", (), False)),
        verdict="not_reconciled", finality=Finality.FINAL, suggested_contract=contract,
        rerun_verdict="reconciled", decisions=(("convention:s_report", "excl"),))


def calibration() -> CalibrationReport:
    return CalibrationReport(
        granularity="day", n_periods=30, status="pass", mode_used="documented",
        nmbe_pct="1.2", cvrmse_pct="8.4", nmbe_pct_calibrated="0.8",
        cvrmse_pct_calibrated="7.1", thresholds=("10", "30"),
        rho=(("0-5m", 900, 1000, "0.88", "0.92"), ("5-60m", 40, 100, "0.31", "0.50")),
        diag_confusion=(("ttl-expiry", "previous_message_not_found", 12),),
        diag_precision_recall=(("ttl-expiry", "0.9", "0.8"),), unlabeled=3,
        no_comparison_labels=1, ttl_corroboration=(9, 10), notes=("12 periods minimum",))


def finding(fid: str, title: str, team: str | None, *, monthly: str = "6100",
            basis: Basis = Basis.LIST, kind: str = "ttl-expiry",
            category: str = "breaker", lever_ids: tuple[str, ...] = ("cc.prompt_cache_ttl.main",),
            headroom: Figure | None = None) -> Finding:
    dims = (("lane_kind", "main"),) + ((("team", team),) if team else ())
    return Finding(
        finding_id=fid, detector_id="cache.miss-by-cause", kind=kind, detector_version="1",
        category=category, lever_class="cache_transform", audience="org", title=title,
        summary="Cache re-writes after idle gaps longer than the 5-minute TTL.",
        scope=Scope(dims=tuple(sorted(dims))), n_events=42, n_lanes=7, n_users=9,
        first_seen_ms=T0 + 3600_000,
        cost_observed=exact(usd("812.40"), basis),
        recoverable=estimated(usd("420"), basis, low=usd("300"), high=usd("510"),
                              calibration=Calibration.CALIBRATED),
        recoverable_shapley=estimated(usd("390"), basis, calibration=Calibration.CALIBRATED),
        projected_monthly=estimated(usd(monthly), basis, low=usd(monthly) // 2,
                                    high=usd(monthly) * 3 // 2,
                                    calibration=Calibration.CALIBRATED),
        lever_ids=lever_ids,
        evidence=(EvidenceItem("transition", "rq_1", (("gap_ms", 420_000), ("cause", "ttl"))),),
        fix=Fix(text="Set promptCacheTtl to 1h for main lanes.",
                config_patch=(("promptCacheTtl", '"1h"'),), target="claude-code-managed-settings",
                doc_url="https://code.claude.com/docs/en/costs", gates=("claude-code>=2.1.267",)),
        confidence="high", validated_against="cache_miss_reason 12/12", needs_eval=False,
        references=("cc-cache-breakers",), headroom=headroom)


def findings() -> tuple[Finding, ...]:
    dq = dataclasses.replace(
        finding("f_dq", "Detectors skipped: missing capabilities", None, kind="dq.missing"),
        category="data-quality", recoverable=None, recoverable_shapley=None,
        projected_monthly=None, lever_ids=(), fix=None)
    return (
        finding("f_1", "TTL expiry re-writes on payments main lanes", "payments"),
        finding("f_2", "Fast mode premium on platform", "platform", monthly="1200",
                kind="fast-premium", lever_ids=("cc.fast_mode_opt_in",)),
        finding("f_3", "Allowance headroom: subscription TTL", "payments", monthly="300",
                basis=Basis.LIST_EQUIVALENT),
        dq,
    )


def plan() -> ActionPlan:
    lv = LeverResult(
        lever_id="cc.prompt_cache_ttl.main", lever_class="cache_transform",
        params="ttl=1h@lane_kind:main", basis=Basis.LIST,
        standalone=estimated(usd("420"), Basis.LIST, calibration=Calibration.CALIBRATED),
        shapley=estimated(usd("390"), Basis.LIST, calibration=Calibration.CALIBRATED),
        projected_monthly=estimated(usd("6100"), Basis.LIST, low=usd("2900"), high=usd("8400"),
                                    calibration=Calibration.CALIBRATED),
        needs_eval=False, upper_bound=False, group="g1", finding_ids=("f_1",))
    lv2 = dataclasses.replace(lv, lever_id="cc.autocompact_window", lever_class="trajectory",
                              needs_eval=True, group="g2", finding_ids=())
    return ActionPlan(
        joint_saving=estimated(usd("700"), Basis.LIST, calibration=Calibration.CALIBRATED),
        headline_monthly=estimated(usd("9800"), Basis.LIST, low=usd("4100"), high=usd("12000"),
                                   calibration=Calibration.CALIBRATED),
        allowance_headroom_monthly=estimated(usd("300"), Basis.LIST_EQUIVALENT,
                                             calibration=Calibration.UNCALIBRATED),
        levers=(lv, lv2), groups=(("g1", (lv.lever_id,)), ("g2", (lv2.lever_id,))),
        method="shapley-exact", shapley_se=((lv.lever_id, usd("3")),),
        sample="shapley on 2000/2000 lanes (seed 7)",
        observed_rr=(("cache_transform", "0.62", 3),),
        pool_headroom_monthly=estimated(usd("45"), Basis.LIST_EQUIVALENT,
                                        note="Copilot pool headroom"))


def packs() -> tuple[PolicyPack, ...]:
    entry = PolicyEntry(key="promptCacheTtl", value_json='"1h"',
                        projection=estimated(usd("6100"), Basis.LIST, low=usd("2900"),
                                             high=usd("8400"),
                                             calibration=Calibration.CALIBRATED),
                        lever_id="cc.prompt_cache_ttl.main", needs_eval=False, verified_key=True,
                        min_version="2.1.267", note="main lanes only")
    return (PolicyPack(target="claude-code", cohort="payments",
                       merge_patch_json='{"promptCacheTtl":"1h"}',
                       rollback_patch_json='{"promptCacheTtl":null}', entries=(entry,),
                       otel_resource_attributes="tokenbill.arm=cc.prompt_cache_ttl.main,"
                                                "tokenbill.wave=1",
                       readme_md=f"# Pack\nnever rendered {CANARY}\n",
                       hooks=(("hooks/tokenbill_session_start.py", f"# {CANARY}\n"),)),)


def replays() -> tuple[ReplayResult, ...]:
    return (ReplayResult(
        policy=Policy(name="ttl-1h", ttl=(("lane_kind:main", "1h"),)), mode="documented",
        baseline=exact(usd("1000"), Basis.LIST),
        cost=estimated(usd("880"), Basis.LIST, low=usd("850"), high=usd("910"),
                       calibration=Calibration.UNCALIBRATED),
        saving=estimated(usd("120"), Basis.LIST, low=usd("90"), high=usd("150"),
                         calibration=Calibration.UNCALIBRATED),
        per_lane=(("ln_a", usd("500")), ("ln_b", usd("380"))), outcomes=None,
        assumptions=("documented cache rules",), calibration=Calibration.UNCALIBRATED,
        added_calls=0, keepalive_pings=0, lanes_skipped=(("ln_c", "no_cache_data"),),
        n_lanes=3, n_requests=120),)


def measurement() -> MeasurementResult:
    return MeasurementResult(
        lever_id="cc.prompt_cache_ttl.main", design="stepped_wedge",
        unit="cost per active developer-day",
        estimate=Figure(nano=usd("2.10"), evidence=Evidence.VERIFIED, basis=Basis.LIST,
                        low_nano=usd("1.40"), high_nano=usd("2.80"), ci_level_pct=95),
        projected=estimated(usd("2.50"), Basis.LIST, calibration=Calibration.CALIBRATED),
        realization_rate=("0.84", "0.56", "1.12"),
        guards=(GuardResult("srm", True, "0.41", "0.01"),
                GuardResult("placebo", True, "0.62", "0.05")),
        scope=(("clusters", 12), ("units", 240), ("treated_unit_days", 3100)),
        scope_label="fleet:2026-08-24/2026-09-23",
        window=(("since", "2026-08-24"), ("until", "2026-09-23")),
        rate_card_sha256=SHA, assignment_log_sha256="cd" * 32, preregistration_sha256="ef" * 32,
        adjustments=("cuped",), rate_variance=exact(usd("-0.12"), Basis.LIST), signable=True)


def measure_plan() -> MeasurePlan:
    return MeasurePlan(
        lever_id="cc.prompt_cache_ttl.main", design="stepped_wedge", cluster_kind="team",
        waves=((1, ("payments", "search")), (2, ("platform",))), holdback=("mobile",),
        washout_hours=24, looks=("2026-10-01",), mde_nano=usd("0.80"),
        projection=estimated(usd("2.50"), Basis.LIST, calibration=Calibration.CALIBRATED),
        verification_design=True, clusters_needed=6, assignment_log_sha256="cd" * 32,
        preregistration_sha256="ef" * 32, preregistration_json='{"lever":"x"}',
        otel_tags=(("payments", "tokenbill.arm=x,tokenbill.wave=1"),),
        warnings=("two clusters below 5 developers merged",))


def ab() -> AbResult:
    m = dataclasses.replace(measurement(), design="ab", scope_label="lab:0123456789ab")
    return AbResult(
        verdict="costlier", scope_label="lab:0123456789ab", n_tasks=20, trials_per_arm=(5, 5),
        randomized_order=True,
        cost_per_success=(Figure(nano=usd("1.00"), evidence=Evidence.VERIFIED, basis=Basis.LIST,
                                 low_nano=usd("0.90"), high_nano=usd("1.10"), ci_level_pct=95),
                          Figure(nano=usd("1.07"), evidence=Evidence.VERIFIED, basis=Basis.LIST,
                                 low_nano=usd("0.97"), high_nano=usd("1.17"), ci_level_pct=95)),
        paired_difference=Figure(nano=usd("0.07"), evidence=Evidence.VERIFIED, basis=Basis.LIST,
                                 low_nano=usd("0.01"), high_nano=usd("0.13"), ci_level_pct=95),
        token_delta_pct="-38", turn_delta_pct="14", read_delta_pct="-52", success_delta_pct="0",
        measurement=m)


def check() -> CheckResult:
    return CheckResult(
        passed=False,
        violations=(CheckViolation("TB-CACHE-SHARE", "error", "cache-read share 0.41 < 0.80",
                                   "timestamp.jsonl#run=r1#call=3"),
                    CheckViolation("TB-NEW-BREAKER", "error", "new breaker volatile-system",
                                   "timestamp.jsonl#run=r1#call=1")),
        runs=3, cache_read_share="0.41", median_cost_nano=usd("0.12"),
        baseline_median_cost_nano=usd("0.11"), breaker_kinds=("volatile-system",),
        summary_md="| rule | result |\n|---|---|\n| TB-CACHE-SHARE | fail |\n")


def pricing() -> PricingReport:
    facts = load()
    return PricingReport(kind="verify", rows=facts.rate_rows[:3], modifiers=facts.modifiers[:2],
                         discrepancies=(Discrepancy(facts.rate_rows[0].row_id, "output", "25",
                                                    "24", "litellm", False),),
                         stale_rows=(), ok=True)


def source(name: str, adapter: str = "claude-code") -> SourceInfo:
    return SourceInfo(source_id=f"s_{name}", adapter=adapter, name_hmac=f"h_{name}",
                      sha256="12" * 32, bytes=4096, name_key_id="k_name000000",
                      principal_key_id="k_org0000000")


def full_result(**changes: object) -> RunResult:
    """A ``RunResult`` with every slot filled (``changes`` replace fields)."""
    result = RunResult(
        command="report", window=(T0, T1),
        inputs=((source("beta"), 43383, 2), (source("alpha", "otlp"), 120, 0)),
        privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id="k_org0000000",
                            identity_mode="central", k=5, suppressed_groups=1),
        rate_card=RateCardInfo(sha256=SHA, layers=("builtin@2026-09-23",), stale_rows=(),
                               contract=None, basis=Basis.LIST),
        bill=bill(),
        data_quality=(
            DataQualityNote("dq.naive_line_sum_ratio", "info", 103607,
                            "naive line sum is 2.33x the de-duplicated ledger", tokens=5_000_000),
            DataQualityNote("dq.provider_estimate", "info", 1, "OTel cost_usd total",
                            figure=Figure(nano=usd("27100"), evidence=Evidence.EXACT,
                                          basis=Basis.PROVIDER_ESTIMATE)),
        ),
        reconciliation=recon(), calibration=calibration(), findings=findings(),
        action_plan=plan(), policy_packs=packs(), replays=replays(), measure_plan=measure_plan(),
        measurements=(measurement(),), ab=ab(), check=check(), pricing=pricing(),
        receipts=("rcpt_b", "rcpt_a"), synthetic=True, notes=("demo fleet (seed 7)",))
    return dataclasses.replace(result, **changes) if changes else result


def empty_result(**changes: object) -> RunResult:
    """A ``RunResult`` with only the mandatory slots."""
    base = RunResult(command="bill", window=(T0, T1), inputs=(),
                     privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id=None,
                                         identity_mode="install", k=5, suppressed_groups=0),
                     rate_card=None)
    return dataclasses.replace(base, **changes) if changes else base


def unpriced_bill() -> BillSummary:
    total = PricedTotal(exact=unpriced("unknown model"), estimated=None, allowance=None,
                        priced_inferences=0, unpriced_inferences=3, unpriced_tokens=3600,
                        coverage="0")
    return BillSummary(total=total, esr=None, breakdowns=())
