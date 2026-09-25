"""``tokenbill/result@2`` JSON (SPEC §14.1, §14.7; package OUT).

:func:`to_result_json` turns a :class:`~tokenbill.core.types.RunResult` into the result document,
:func:`dumps_result` serializes it canonically and :func:`validate_result_json` checks a document
against the schema rule of SPEC §14.1:

* no JSON float anywhere (money is an exact decimal string plus int nano; ratios are decimal
  strings);
* every JSON object that holds an integer leaf — directly or inside its arrays — also holds an
  ``"evidence"`` key (count objects say ``"exact"``). Two places are the documented exceptions: the
  ``range`` object inside a ``MONEY`` object (its ``MONEY`` parent carries the label) and the
  root's wall-clock ``generated_ms`` (metadata, dropped by ``deterministic=True``, shown label-free
  in the SPEC example);
* money only as ``MONEY`` objects (``core.labels.figure_json``): a key ``usd``/``nano`` or ending in
  ``_usd``/``_nano`` anywhere else is a violation;
* ``basis: "list_equivalent"`` never under ``exact`` or any other billed key (:data:`BILLED_KEYS`).

The renderer-side rules (R3, R10) are enforced while building: a billed slot fed a figure that is
not ``Figure.is_billed_eligible`` raises ``ContractViolation`` (:func:`require_billed`); allowance
and pool slots accept only ``list_equivalent`` figures (:func:`require_allowance`); grouped data
must be a ``PublishedAggregate``. Channel extensions contribute their ``RunResult`` slot (e.g.
``copilot``) through ``core.extensions.render_sections(result, "json")``.

The helpers :func:`require_billed`, :func:`require_allowance`, :func:`require_published`,
:func:`date_of` and :func:`rule_violations` are shared by the other OUT renderers.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation
from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
from tokenbill.core.labels import Basis, Evidence, Figure, add, estimated, exact, figure_json
from tokenbill.core.money import nano_to_usd_str, ratio
from tokenbill.core.records import UsageBuckets
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    AbResult,
    ActionPlan,
    AggRow,
    BillSummary,
    CalibrationReport,
    CheckResult,
    DataQualityNote,
    Finding,
    LeverResult,
    MeasurementResult,
    MeasurePlan,
    PolicyPack,
    PricedTotal,
    PricingReport,
    PublishedAggregate,
    ReconciliationReport,
    ReplayResult,
    RunResult,
)

__all__ = [
    "BILLED_KEYS",
    "MONEY_KEYS",
    "SCHEMA",
    "TOOL_NAME",
    "canonical_dumps",
    "date_of",
    "display_rows",
    "dumps_result",
    "extension_slots_present",
    "money_json",
    "require_allowance",
    "require_billed",
    "require_published",
    "rule_violations",
    "to_result_json",
    "validate_result_json",
]

SCHEMA = "tokenbill/result@2"
TOOL_NAME = "tokenbill"
#: The keys of a ``MONEY`` object (``core.labels.figure_json``).
MONEY_KEYS = frozenset({"usd", "nano", "evidence", "basis", "finality", "range", "ci_level_pct",
                        "calibration", "upper_bound", "provenance", "note"})
_RANGE_KEYS = frozenset({"low_usd", "low_nano", "high_usd", "high_nano"})
#: Keys whose ``MONEY`` value is billed (or a savings total on billed bases): never
#: ``list_equivalent`` (R3, R10, D26).
BILLED_KEYS = frozenset({"exact", "billed", "spend", "invoice", "joint_saving",
                         "headline_monthly"})
_EVIDENCE_VALUES = frozenset(e.value for e in Evidence)
_BASIS_VALUES = frozenset(b.value for b in Basis)
_MONEY_KEY_RE = re.compile(r"(?:usd|nano|.+_usd|.+_nano)\Z")
_EXACT = "exact"
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_TEXT_MAX = 400
_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other",
            "cache_write_unknown", "output", "web_search_requests", "web_fetch_requests")


# ---------------------------------------------------------------------------------------------
# shared label helpers (R3, R10, SPEC §8.4)
# ---------------------------------------------------------------------------------------------


def require_billed(fig: Figure, column: str) -> Figure:
    """*fig* when it may appear in a billed column (``Figure.is_billed_eligible``: EXACT on list,
    contract or invoice); ``ContractViolation`` otherwise (R3: estimates, list-equivalent allowance
    and provider estimates never render as billed)."""
    if not isinstance(fig, Figure):
        raise ContractViolation(f"{column}: expects a Figure")
    if not fig.is_billed_eligible:
        raise ContractViolation(
            f"{column}: billed columns accept only exact list/contract/invoice figures "
            f"(got {fig.evidence.value}, {fig.basis.value})")
    return fig


def require_allowance(fig: Figure, column: str) -> Figure:
    """*fig* when it is list-equivalent (seat allowance or Copilot pool credits, R10);
    ``ContractViolation`` otherwise."""
    if not isinstance(fig, Figure):
        raise ContractViolation(f"{column}: expects a Figure")
    if fig.basis is not Basis.LIST_EQUIVALENT:
        raise ContractViolation(f"{column}: allowance columns accept only list-equivalent figures")
    return fig


def _require_not_allowance(fig: Figure, column: str) -> Figure:
    if not isinstance(fig, Figure):
        raise ContractViolation(f"{column}: expects a Figure")
    if fig.basis is Basis.LIST_EQUIVALENT:
        raise ContractViolation(f"{column}: list-equivalent figures never render as billed (R10)")
    return fig


def require_published(agg: object, what: str) -> PublishedAggregate:
    """*agg* when it is a ``PublishedAggregate`` (the only grouped type renderers accept, SPEC
    §8.4); ``ContractViolation`` otherwise."""
    if not isinstance(agg, PublishedAggregate):
        raise ContractViolation(f"{what}: renderers accept only PublishedAggregate "
                                "(core.kanon.publish)")
    return agg


def _add_opt(a: Figure | None, b: Figure | None) -> Figure | None:
    if a is None:
        return b
    if b is None:
        return a
    return add(a, b)


def _add_priced(a: PricedTotal, b: PricedTotal, tokens_a: int, tokens_b: int) -> PricedTotal:
    unpriced = a.unpriced_tokens + b.unpriced_tokens
    total = tokens_a + tokens_b
    cov = ratio(max(total - unpriced, 0), total) if total > 0 else None
    return PricedTotal(
        exact=add(a.exact, b.exact), estimated=_add_opt(a.estimated, b.estimated),
        allowance=_add_opt(a.allowance, b.allowance),
        priced_inferences=a.priced_inferences + b.priced_inferences,
        unpriced_inferences=a.unpriced_inferences + b.unpriced_inferences,
        unpriced_tokens=unpriced,
        coverage="1" if cov is None else format(cov.normalize(), "f"),
        pool=_add_opt(a.pool, b.pool))


def display_rows(agg: PublishedAggregate) -> tuple[AggRow, ...]:
    """The rows of *agg* as renderers show them (ruling R-E30): cells whose user count is unknown
    (``users_unknown``, e.g. from sources without per-cell user counts) are shown at team level
    only — in a grouping finer than ``team`` they are folded into one row per team, the other dims
    printed as ``"(all)"``. Every other row is shown as published."""
    require_published(agg, "aggregate")
    group_by = agg.group_by
    if "team" not in group_by or len(group_by) == 1:
        return agg.rows
    ti = group_by.index("team")
    kept: list[AggRow] = []
    folded: dict[str | None, AggRow] = {}
    for row in agg.rows:
        if USERS_UNKNOWN not in row_notes(row, group_by=group_by):
            kept.append(row)
            continue
        team = row.dims[ti][1]
        dims = tuple((k, v if k == "team" else "(all)") for k, v in row.dims)
        prev = folded.get(team)
        if prev is None:
            folded[team] = AggRow(dims=dims, n_users=0, n_requests=row.n_requests,
                                  usage=row.usage, priced=row.priced)
            continue
        tokens = (prev.usage.total_input + prev.usage.output, row.usage.total_input
                  + row.usage.output)
        try:
            usage = prev.usage + row.usage
        except ContractViolation:   # two different other-write TTLs: keep the first, informational
            usage = prev.usage + dataclasses.replace(
                row.usage, cache_write_other_ttl_s=prev.usage.cache_write_other_ttl_s)
        folded[team] = AggRow(dims=dims, n_users=0, n_requests=prev.n_requests + row.n_requests,
                              usage=usage, priced=_add_priced(prev.priced, row.priced, *tokens))
    order = sorted(folded, key=lambda t: (t is None, t or ""))
    return tuple(kept) + tuple(folded[t] for t in order)


def extension_slots_present(result: RunResult) -> bool:
    """True when *result* fills the slot of a registered channel extension (``RunResult.copilot``
    for the ``copilot`` extension); renderers then add the extensions' sections
    (``core.extensions.render_sections``), otherwise they skip the hook entirely."""
    return any(getattr(result, spec.name, None) is not None for spec in extensions.extensions())


def date_of(ms: int) -> str:
    """The UTC date (``YYYY-MM-DD``) of epoch milliseconds *ms*."""
    return (_EPOCH + _dt.timedelta(days=ms // _DAY_MS)).isoformat()


def money_json(fig: Figure) -> dict[str, object]:
    """The canonical ``MONEY`` object of *fig* (delegates to ``core.labels.figure_json``, A-8)."""
    return figure_json(fig)


def _opt_money(fig: Figure | None) -> dict[str, object] | None:
    return None if fig is None else figure_json(fig)


def _text(value: object, limit: int = _TEXT_MAX) -> str:
    return sanitize("" if value is None else str(value), limit)


def _opt_text(value: object, limit: int = _TEXT_MAX) -> str | None:
    return None if value is None else _text(value, limit)


def _pairs_obj(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    return {_text(k, 128): v for k, v in pairs}


# ---------------------------------------------------------------------------------------------
# slot encoders
# ---------------------------------------------------------------------------------------------


def _tokens(usage: UsageBuckets) -> dict[str, object]:
    out: dict[str, object] = {name: getattr(usage, name) for name in _BUCKETS}
    out["evidence"] = _EXACT
    return out


def _priced(total: PricedTotal, where: str) -> dict[str, object]:
    est = total.estimated
    if est is not None:
        _require_not_allowance(est, f"{where}.estimated")
    return {
        "exact": figure_json(require_billed(total.exact, f"{where}.exact")),
        "estimated": _opt_money(est),
        "allowance": None if total.allowance is None else figure_json(
            require_allowance(total.allowance, f"{where}.allowance")),
        "pool": None if total.pool is None else figure_json(
            require_allowance(total.pool, f"{where}.pool")),
    }


def _coverage(total: PricedTotal) -> dict[str, object]:
    return {"priced_inferences": total.priced_inferences,
            "unpriced_inferences": total.unpriced_inferences,
            "unpriced_tokens": total.unpriced_tokens, "coverage": total.coverage,
            "evidence": _EXACT}


def _agg_row(row: AggRow, group_by: Sequence[str], where: str) -> dict[str, object]:
    notes = list(row_notes(row, group_by=group_by))
    doc: dict[str, object] = {
        "dims": {_text(k, 128): _opt_text(v, 128) for k, v in row.dims},
        "n_users": None if notes else row.n_users,
        "n_requests": row.n_requests,
        "tokens": _tokens(row.usage),
        **_priced(row.priced, where),
        "coverage": row.priced.coverage,
        "notes": notes,
        "evidence": _EXACT,
    }
    return doc


def _published(key: str, agg: PublishedAggregate) -> dict[str, object]:
    require_published(agg, f"bill.breakdowns[{key}]")
    return {
        "dims": _text(key, 128),
        "group_by": [_text(g, 64) for g in agg.group_by],
        "k": agg.k,
        "suppressed_rows": agg.suppressed_rows,
        "suppressed_users": agg.suppressed_users,
        "rows": [_agg_row(r, agg.group_by, f"bill.breakdowns[{key}]")
                 for r in display_rows(agg)],
        "evidence": _EXACT,
    }


def _bill(bill: BillSummary) -> dict[str, object]:
    total = bill.total
    return {
        **_priced(total, "bill"),
        "coverage": _coverage(total),
        "esr": None if bill.esr is None else {"ratio": bill.esr, "evidence": _EXACT},
        "naive_ratio": None if bill.naive_ratio is None else {"ratio": bill.naive_ratio,
                                                              "evidence": _EXACT},
        "breakdowns": [_published(k, agg) for k, agg in bill.breakdowns],
        "footnotes": [_text(f) for f in bill.footnotes],
    }


def _dq(note: DataQualityNote) -> dict[str, object]:
    return {"code": _text(note.code, 128), "severity": _text(note.severity, 16),
            "count": note.count, "detail": _text(note.detail, 256), "tokens": note.tokens,
            "figure": _opt_money(note.figure), "evidence": _EXACT}


def _recon(rep: ReconciliationReport, basis: Basis) -> dict[str, object]:
    def ledger(n: int | None) -> dict[str, object] | None:
        if n is None:
            return None
        return figure_json(estimated(n, basis, note="ledger at the rate card (exact and "
                                                    "estimated points)"))

    def priced_provider(n: int | None) -> dict[str, object] | None:
        return None if n is None else figure_json(exact(n, basis))

    def invoice(n: int | None) -> dict[str, object] | None:
        if n is None:
            return None
        return figure_json(require_billed(exact(n, Basis.INVOICE, finality=rep.finality),
                                          "reconciliation.invoice"))

    rows = [{
        "key": _pairs_obj((k, _text(v, 128)) for k, v in r.key),
        "ledger_tokens": r.ledger_tokens, "provider_tokens": r.provider_tokens,
        "ledger": ledger(r.ledger_nano), "priced_provider": priced_provider(r.priced_provider_nano),
        "invoice": invoice(r.invoice_nano), "rate_card_error_pct": r.rate_card_error_pct,
        "coverage_pct": r.coverage_pct, "status": _text(r.status, 32),
        "residual_code": _opt_text(r.residual_code, 64), "evidence": _EXACT,
    } for r in rep.rows]
    contract = rep.suggested_contract
    return {
        "window": {"since": rep.window[0], "until": rep.window[1]},
        "tolerance_pct": rep.tolerance_pct,
        "unexplained_tolerance_pct": rep.unexplained_tolerance_pct,
        "token_coverage_pct": rep.token_coverage_pct,
        "dollar_coverage_pct": rep.dollar_coverage_pct,
        "rate_card_error": None if rep.rate_card_error is None else dict(
            zip(("p50", "p95", "max"), rep.rate_card_error, strict=True)),
        "over_count_rows": rep.over_count_rows,
        "effective_discount": [{"key": _text(k, 128), "discount": v}
                               for k, v in rep.effective_discount],
        "residuals": [{"code": _text(code, 64), "amount": figure_json(estimated(
            n, basis, note=f"reconciliation residual {_text(code, 64)}"))}
            for code, n in rep.residuals],
        "unexplained": figure_json(estimated(rep.unexplained_nano, basis,
                                             note="unexplained reconciliation residual")),
        "channels": [{"channel": _text(c.channel, 64), "verdict": _text(c.verdict, 32),
                      "invoice_sources": [_text(s, 64) for s in c.invoice_sources],
                      "mapping_verified": c.mapping_verified} for c in rep.channels],
        "verdict": _text(rep.verdict, 32),
        "finality": rep.finality.value,
        "rows": rows,
        "suggested_contract": None if contract is None else {
            "name": _text(contract.name, 128),
            "multiplier": None if contract.multiplier is None else str(contract.multiplier),
            "overrides": [{"model": _text(m, 128),
                           "rates": {_text(b, 32): str(v) for b, v in rates}}
                          for m, rates in contract.overrides],
            "effective_from": contract.effective_from, "effective_to": contract.effective_to,
            "derived": contract.derived,
            "assumed_fields": [_text(a, 64) for a in contract.assumed_fields],
            "channels": [_text(c, 64) for c in contract.channels], "sha256": contract.sha256},
        "rerun_verdict": _opt_text(rep.rerun_verdict, 32),
        "decisions": _pairs_obj(rep.decisions),
        "evidence": _EXACT,
    }


def _calibration(cal: CalibrationReport) -> dict[str, object]:
    return {
        "granularity": cal.granularity, "n_periods": cal.n_periods, "status": cal.status,
        "mode_used": cal.mode_used, "calibration": cal.calibration().value,
        "nmbe_pct": cal.nmbe_pct, "cvrmse_pct": cal.cvrmse_pct,
        "nmbe_pct_calibrated": cal.nmbe_pct_calibrated,
        "cvrmse_pct_calibrated": cal.cvrmse_pct_calibrated,
        "thresholds": {"nmbe_pct": cal.thresholds[0], "cvrmse_pct": cal.thresholds[1]},
        "rho": [{"gap_band": _text(b, 32), "hits": h, "trials": t, "wilson_low": lo,
                 "wilson_high": hi, "evidence": _EXACT} for b, h, t, lo, hi in cal.rho],
        "diag_confusion": [{"predicted": _text(p, 64), "server_reason": _text(s, 64), "count": n,
                            "evidence": _EXACT} for p, s, n in cal.diag_confusion],
        "diag_precision_recall": [{"class": _text(c, 64), "precision": p, "recall": r}
                                  for c, p, r in cal.diag_precision_recall],
        "unlabeled": cal.unlabeled, "no_comparison_labels": cal.no_comparison_labels,
        "ttl_corroboration": list(cal.ttl_corroboration),
        "notes": [_text(n) for n in cal.notes],
        "evidence": _EXACT,
    }


def _finding(f: Finding) -> dict[str, object]:
    fix = f.fix
    return {
        "finding_id": f.finding_id, "detector_id": f.detector_id, "kind": f.kind,
        "detector_version": f.detector_version, "category": f.category,
        "lever_class": f.lever_class, "audience": f.audience, "title": _text(f.title, 120),
        "summary": _text(f.summary),
        "scope": _pairs_obj((k, _text(v, 128)) for k, v in f.scope.dims),
        "counts": {"events": f.n_events, "lanes": f.n_lanes, "users": f.n_users,
                   "first_seen_ms": f.first_seen_ms, "evidence": _EXACT},
        "cost_observed": figure_json(f.cost_observed),
        "recoverable": _opt_money(f.recoverable),
        "recoverable_shapley": _opt_money(f.recoverable_shapley),
        "projected_monthly": _opt_money(f.projected_monthly),
        "headroom": None if f.headroom is None else figure_json(
            require_allowance(f.headroom, "finding.headroom")),
        "lever_ids": list(f.lever_ids),
        "evidence_items": [{"kind": _text(e.kind, 32), "ref": _text(e.ref, 128),
                            "attrs": [[_text(k, 64), v if isinstance(v, int) else _text(v, 128)]
                                      for k, v in e.attrs],
                            "evidence": _EXACT} for e in f.evidence],
        "fix": None if fix is None else {
            "text": _text(fix.text), "config_patch": None if fix.config_patch is None
            else _pairs_obj(fix.config_patch), "target": fix.target, "doc_url": fix.doc_url,
            "gates": list(fix.gates)},
        "confidence": f.confidence, "validated_against": _opt_text(f.validated_against),
        "needs_eval": f.needs_eval, "references": list(f.references),
    }


def _lever(lv: LeverResult) -> dict[str, object]:
    return {"lever_id": lv.lever_id, "lever_class": lv.lever_class, "params": _text(lv.params),
            "basis": lv.basis.value, "standalone": figure_json(lv.standalone),
            "shapley": figure_json(lv.shapley),
            "projected_monthly": figure_json(lv.projected_monthly),
            "needs_eval": lv.needs_eval, "upper_bound": lv.upper_bound, "group": lv.group,
            "finding_ids": list(lv.finding_ids)}


def _plan(plan: ActionPlan) -> dict[str, object]:
    basis_of = {lv.lever_id: lv.basis for lv in plan.levers}
    return {
        "joint_saving": figure_json(_require_not_allowance(plan.joint_saving,
                                                           "action_plan.joint_saving")),
        "headline_monthly": figure_json(_require_not_allowance(plan.headline_monthly,
                                                               "action_plan.headline_monthly")),
        "allowance_headroom_monthly": None if plan.allowance_headroom_monthly is None else
        figure_json(require_allowance(plan.allowance_headroom_monthly,
                                      "action_plan.allowance_headroom_monthly")),
        "pool_headroom_monthly": None if plan.pool_headroom_monthly is None else figure_json(
            require_allowance(plan.pool_headroom_monthly, "action_plan.pool_headroom_monthly")),
        "levers": [_lever(lv) for lv in plan.levers],
        "groups": {g: list(ids) for g, ids in plan.groups},
        "method": plan.method,
        "shapley_se": [{"lever_id": lid, "se": figure_json(estimated(
            n, basis_of.get(lid, Basis.LIST), note="Monte Carlo standard error of the Shapley "
                                                   "credit"))} for lid, n in plan.shapley_se],
        "sample": _text(plan.sample),
        "observed_rr": [{"lever_class": c, "mean_rr": rr, "n_receipts": n, "evidence": _EXACT}
                        for c, rr, n in plan.observed_rr],
    }


def _pack(pack: PolicyPack) -> dict[str, object]:
    return {
        "target": pack.target, "cohort": _text(pack.cohort, 128),
        "merge_patch_json": pack.merge_patch_json, "rollback_patch_json": pack.rollback_patch_json,
        "entries": [{"key": e.key, "value_json": e.value_json,
                     "projection": _opt_money(e.projection), "lever_id": e.lever_id,
                     "needs_eval": e.needs_eval, "verified_key": e.verified_key,
                     "min_version": e.min_version, "note": _text(e.note)} for e in pack.entries],
        "otel_resource_attributes": pack.otel_resource_attributes,
        # the README and hook files are written by `policy -o DIR`; the result carries digests
        "readme_sha256": hashlib.sha256(pack.readme_md.encode("utf-8")).hexdigest(),
        "hooks": [{"path": _text(p, 256), "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest()}
                  for p, t in pack.hooks],
    }


def policy_spec(policy: Any) -> str:
    """The canonical spec of a ``Policy`` (``core.policy.to_spec``), or its name when the policy
    cannot be written in the grammar."""
    try:
        return str(policy.spec())
    except Exception:  # noqa: BLE001 - a display fallback; the name is always printable
        return str(policy.name)


def _replay(rep: ReplayResult) -> dict[str, object]:
    reasons: dict[str, int] = {}
    for _lane, reason in rep.lanes_skipped:
        key = _text(reason, 64)
        reasons[key] = reasons.get(key, 0) + 1
    skipped = [{"reason": r, "lanes": n, "evidence": _EXACT} for r, n in sorted(reasons.items())]
    return {
        "policy": _text(policy_spec(rep.policy)), "policy_name": _text(rep.policy.name, 128),
        "mode": rep.mode, "baseline": figure_json(rep.baseline), "cost": figure_json(rep.cost),
        "saving": figure_json(rep.saving), "assumptions": [_text(a) for a in rep.assumptions],
        "calibration": rep.calibration.value,
        "counts": {"added_calls": rep.added_calls, "keepalive_pings": rep.keepalive_pings,
                   "n_lanes": rep.n_lanes, "n_requests": rep.n_requests,
                   "lanes_priced": len(rep.per_lane), "evidence": _EXACT},
        "lanes_skipped": skipped,
    }


def _measure_plan(mp: MeasurePlan) -> dict[str, object]:
    basis = mp.projection.basis if mp.projection is not None else Basis.LIST
    return {
        "lever_id": mp.lever_id, "design": mp.design, "cluster_kind": mp.cluster_kind,
        "waves": [{"wave": w, "clusters": [_text(c, 128) for c in cs], "evidence": _EXACT}
                  for w, cs in mp.waves],
        "holdback": [_text(c, 128) for c in mp.holdback], "washout_hours": mp.washout_hours,
        "looks": list(mp.looks),
        "mde": None if mp.mde_nano is None else figure_json(estimated(
            mp.mde_nano, basis, note="minimum detectable effect of the design")),
        "projection": _opt_money(mp.projection),
        "verification_design": mp.verification_design, "clusters_needed": mp.clusters_needed,
        "assignment_log_sha256": mp.assignment_log_sha256,
        "preregistration_sha256": mp.preregistration_sha256,
        "preregistration_json": mp.preregistration_json,
        "otel_tags": _pairs_obj(mp.otel_tags),
        "warnings": [_text(w) for w in mp.warnings],
        "evidence": _EXACT,
    }


def _measurement(m: MeasurementResult) -> dict[str, object]:
    rr = m.realization_rate
    return {
        "lever_id": m.lever_id, "design": m.design, "unit": m.unit,
        "estimate": figure_json(m.estimate), "projected": _opt_money(m.projected),
        "realization_rate": None if rr is None else dict(zip(("point", "low", "high"), rr,
                                                             strict=True)),
        "guards": [{"name": g.name, "passed": g.passed, "value": g.value,
                    "threshold": g.threshold} for g in m.guards],
        "scope": {**_pairs_obj(m.scope), "evidence": _EXACT},
        "scope_label": _text(m.scope_label, 128), "window": _pairs_obj(m.window),
        "rate_card_sha256": m.rate_card_sha256, "assignment_log_sha256": m.assignment_log_sha256,
        "preregistration_sha256": m.preregistration_sha256,
        "adjustments": [_text(a) for a in m.adjustments],
        "rate_variance": _opt_money(m.rate_variance), "signable": m.signable,
    }


def _ab(ab: AbResult) -> dict[str, object]:
    return {
        "verdict": ab.verdict, "scope_label": _text(ab.scope_label, 128), "n_tasks": ab.n_tasks,
        "trials_per_arm": list(ab.trials_per_arm), "randomized_order": ab.randomized_order,
        "cost_per_success": {"baseline": figure_json(ab.cost_per_success[0]),
                             "candidate": figure_json(ab.cost_per_success[1])},
        "paired_difference": figure_json(ab.paired_difference),
        "token_delta_pct": ab.token_delta_pct, "turn_delta_pct": ab.turn_delta_pct,
        "read_delta_pct": ab.read_delta_pct, "success_delta_pct": ab.success_delta_pct,
        "measurement": _measurement(ab.measurement),
        "evidence": _EXACT,
    }


def check_median(n: int | None, basis: Basis, what: str) -> Figure | None:
    """The EXACT figure of a ``CheckResult`` median cost per run (billed tokens × the rate card)."""
    if n is None:
        return None
    return Figure(nano=n, evidence=Evidence.EXACT, basis=basis, note=what)


def _check(c: CheckResult, basis: Basis) -> dict[str, object]:
    return {
        "passed": c.passed, "runs": c.runs, "cache_read_share": c.cache_read_share,
        "median_cost": _opt_money(check_median(c.median_cost_nano, basis, "median cost per run")),
        "baseline_median_cost": _opt_money(check_median(
            c.baseline_median_cost_nano, basis, "baseline median cost per run")),
        "breaker_kinds": list(c.breaker_kinds),
        "violations": [{"rule_id": v.rule_id, "level": v.level, "message": _text(v.message),
                        "location": _text(v.location, 256)} for v in c.violations],
        "summary_md": _text(c.summary_md, 20_000),
        "evidence": _EXACT,
    }


def _dec(value: object) -> str | None:
    return None if value is None else str(value)


def _pricing(p: PricingReport) -> dict[str, object]:
    rows = [{
        "row_id": r.row_id, "provider": r.provider, "channel": r.channel, "model": r.model,
        "aliases": list(r.aliases), "generation": r.generation,
        "effective_from": r.effective_from, "effective_to": r.effective_to,
        "input_usd_per_mtok": _dec(r.input_usd_per_mtok),
        "output_usd_per_mtok": _dec(r.output_usd_per_mtok),
        "cache_read_mult": _dec(r.cache_read_mult),
        "cache_write_5m_mult": _dec(r.cache_write_5m_mult),
        "cache_write_1h_mult": _dec(r.cache_write_1h_mult),
        "cache_write_other_mult": _dec(r.cache_write_other_mult),
        "cache_write_other_ttl_s": r.cache_write_other_ttl_s,
        "min_cacheable_tokens": r.min_cacheable_tokens,
        "long_context_threshold": r.long_context_threshold,
        "supports": list(r.supports), "enabled": r.enabled, "verified_on": r.verified_on,
        "sources": [{"url": s.url, "retrieved": s.retrieved, "finding": s.finding}
                    for s in r.sources],
        "promotion": r.promotion, "notes": _text(r.notes), "evidence": _EXACT,
    } for r in p.rows]
    mods = [{"modifier_id": m.modifier_id, "kind": m.kind, "factor": _dec(m.factor),
             "base_usd_per_mtok": {b: str(v) for b, v in m.base_usd_per_mtok},
             "applies_to": list(m.applies_to), "when": _pairs_obj(m.when), "stacking": m.stacking}
            for m in p.modifiers]
    return {"kind": p.kind, "ok": p.ok, "rows": rows, "modifiers": mods,
            "discrepancies": [{"row_id": d.row_id, "field": d.field, "ours": d.ours,
                               "theirs": d.theirs, "source": d.source,
                               "authoritative": d.authoritative} for d in p.discrepancies],
            "stale_rows": list(p.stale_rows)}


# ---------------------------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------------------------


def _tool_version() -> str:
    import tokenbill

    return str(getattr(tokenbill, "__version__", "0"))


def _sort(items: list[dict[str, object]], *keys: str) -> list[dict[str, object]]:
    return sorted(items, key=lambda d: tuple(json.dumps(d.get(k), sort_keys=True) for k in keys)
                  + (json.dumps(d, sort_keys=True),))


def _deterministic(doc: dict[str, object]) -> None:
    """Order every record array by stable ids (SPEC §14.1 ``--deterministic``)."""
    doc["inputs"] = _sort(doc["inputs"], "source_id")  # type: ignore[arg-type]
    doc["data_quality"] = _sort(doc["data_quality"], "code", "detail")  # type: ignore[arg-type]
    doc["findings"] = _sort(doc["findings"], "finding_id")  # type: ignore[arg-type]
    for f in doc["findings"]:  # type: ignore[union-attr]
        f["evidence_items"] = _sort(f["evidence_items"], "kind", "ref")  # type: ignore[index]
    doc["policy_packs"] = _sort(doc["policy_packs"], "target", "cohort")  # type: ignore[arg-type]
    doc["replays"] = _sort(doc["replays"], "policy", "mode")  # type: ignore[arg-type]
    doc["measurements"] = _sort(doc["measurements"], "lever_id", "design")  # type: ignore[arg-type]
    doc["receipts"] = sorted(doc["receipts"])  # type: ignore[type-var,arg-type]
    bill = doc.get("bill")
    if isinstance(bill, dict):
        bill["breakdowns"] = _sort(bill["breakdowns"], "dims")
        for b in bill["breakdowns"]:
            b["rows"] = _sort(b["rows"], "dims")
    plan = doc.get("action_plan")
    if isinstance(plan, dict):
        plan["levers"] = _sort(plan["levers"], "lever_id")  # type: ignore[arg-type]
        plan["shapley_se"] = _sort(plan["shapley_se"], "lever_id")  # type: ignore[arg-type]
    recon = doc.get("reconciliation")
    if isinstance(recon, dict):
        recon["channels"] = _sort(recon["channels"], "channel")  # type: ignore[arg-type]
        recon["rows"] = _sort(recon["rows"], "key")  # type: ignore[arg-type]
    check = doc.get("check")
    if isinstance(check, dict):
        check["violations"] = _sort(check["violations"], "rule_id", "location")  # type: ignore
    pricing = doc.get("pricing")
    if isinstance(pricing, dict):
        pricing["rows"] = _sort(pricing["rows"], "row_id")  # type: ignore[arg-type]
        pricing["modifiers"] = _sort(pricing["modifiers"], "modifier_id")  # type: ignore
        pricing["discrepancies"] = _sort(pricing["discrepancies"], "row_id", "field")  # type: ignore


def to_result_json(result: RunResult, *, deterministic: bool = False) -> dict[str, object]:
    """The ``tokenbill/result@2`` document of *result* (SPEC §14.1).

    Every ``RunResult`` slot has a key; extension slots (``copilot``) come from
    ``core.extensions.render_sections(result, "json")`` (an unavailable extension renderer adds a
    ``dq.extension_unavailable`` entry to ``data_quality``). ``deterministic=True`` drops the
    wall-clock ``generated_ms`` and orders record arrays by stable ids. Raises
    ``ContractViolation`` when a billed slot holds a non-billed-eligible figure, an allowance slot a
    billed one, or a breakdown is not a ``PublishedAggregate``.
    """
    if not isinstance(result, RunResult):
        raise ContractViolation("to_result_json expects a RunResult")
    basis = result.rate_card.basis if result.rate_card is not None else Basis.LIST
    rc = result.rate_card
    doc: dict[str, object] = {
        "schema": SCHEMA,
        "tool": {"name": TOOL_NAME, "version": _tool_version()},
        "command": _text(result.command, 64),
        "window": {"since": date_of(result.window[0]), "until": date_of(result.window[1])},
        "inputs": [{"source_id": s.source_id, "adapter": _text(s.adapter, 64), "records": n,
                    "quarantined": q, "evidence": _EXACT} for s, n, q in result.inputs],
        "privacy": {"content_tier": result.privacy.content_tier.value,
                    "key_id": result.privacy.key_id, "identity_mode": result.privacy.identity_mode,
                    "k": result.privacy.k, "suppressed_groups": result.privacy.suppressed_groups,
                    "evidence": _EXACT},
        "rate_card": None if rc is None else {
            "sha256": rc.sha256, "layers": [_text(x, 128) for x in rc.layers],
            "stale_rows": list(rc.stale_rows), "contract": _opt_text(rc.contract, 128),
            "basis": rc.basis.value},
        "bill": None if result.bill is None else _bill(result.bill),
        "data_quality": [_dq(n) for n in result.data_quality],
        "reconciliation": None if result.reconciliation is None else _recon(
            result.reconciliation, basis),
        "calibration": None if result.calibration is None else _calibration(result.calibration),
        "findings": [_finding(f) for f in result.findings],
        "action_plan": None if result.action_plan is None else _plan(result.action_plan),
        "policy_packs": [_pack(p) for p in result.policy_packs],
        "replays": [_replay(r) for r in result.replays],
        "measure_plan": None if result.measure_plan is None else _measure_plan(result.measure_plan),
        "measurements": [_measurement(m) for m in result.measurements],
        "ab": None if result.ab is None else _ab(result.ab),
        "check": None if result.check is None else _check(result.check, basis),
        "pricing": None if result.pricing is None else _pricing(result.pricing),
        "receipts": list(result.receipts),
        "synthetic": result.synthetic,
        "notes": [_text(n) for n in result.notes],
    }
    if not deterministic:
        doc["generated_ms"] = time.time_ns() // 1_000_000
    notes: list[DataQualityNote] = []
    sections = extensions.render_sections(result, "json", notes=notes) if (
        extension_slots_present(result)) else []
    for section in sections:
        for name, obj in section.items():  # type: ignore[union-attr]
            if name in doc:
                raise ContractViolation(f"extension section {name!r} collides with a result key")
            doc[name] = obj
    if notes:
        doc["data_quality"] = [*doc["data_quality"], *(_dq(n) for n in notes)]  # type: ignore[misc]
    if deterministic:
        _deterministic(doc)
    return doc


def _find_float(obj: object, path: str) -> str | None:
    if isinstance(obj, float):
        return path or "/"
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            hit = _find_float(v, f"{path}/{k}")
            if hit is not None:
                return hit
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hit = _find_float(v, f"{path}/{i}")
            if hit is not None:
                return hit
    return None


def canonical_dumps(doc: Mapping[str, object]) -> str:
    """Canonical JSON (sorted keys, compact separators, UTF-8 text); ``ContractViolation`` on a
    float anywhere in *doc* (money never becomes a float, R1)."""
    hit = _find_float(doc, "")
    if hit is not None:
        raise ContractViolation(f"float in a JSON document at {hit}")
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def dumps_result(result: RunResult, *, deterministic: bool = False) -> str:
    """``canonical_dumps(to_result_json(result, deterministic=...))``: byte-identical for identical
    inputs when *deterministic*."""
    return canonical_dumps(to_result_json(result, deterministic=deterministic))


# ---------------------------------------------------------------------------------------------
# validation (SPEC §14.1 schema rule)
# ---------------------------------------------------------------------------------------------


def _is_label(value: object, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _money_errors(obj: Mapping[str, object], path: str) -> list[str]:
    errs: list[str] = []
    if set(obj) != MONEY_KEYS:
        return [f"{path}: money not in MONEY form (keys {sorted(set(obj) ^ MONEY_KEYS)})"]
    nano, usd = obj["nano"], obj["usd"]
    if nano is None:
        if usd is not None:
            errs.append(f"{path}: MONEY usd must be null when nano is null")
    elif not _is_int(nano):
        errs.append(f"{path}: MONEY nano must be an int")
    elif not isinstance(usd, str) or usd != nano_to_usd_str(nano):
        errs.append(f"{path}: MONEY usd must be the exact decimal string of nano")
    if not _is_label(obj["evidence"], _EVIDENCE_VALUES):
        errs.append(f"{path}: MONEY evidence not an Evidence value")
    if not _is_label(obj["basis"], _BASIS_VALUES):
        errs.append(f"{path}: MONEY basis not a Basis value")
    rng = obj["range"]
    if rng is not None:
        if not isinstance(rng, Mapping) or set(rng) != _RANGE_KEYS:
            errs.append(f"{path}/range: MONEY range must hold low/high usd and nano")
        else:
            for side in ("low", "high"):
                n, u = rng[f"{side}_nano"], rng[f"{side}_usd"]
                if not _is_int(n) or u != nano_to_usd_str(n):  # type: ignore[arg-type]
                    errs.append(f"{path}/range: {side} must be an int nano with its usd string")
    ci = obj["ci_level_pct"]
    if ci is not None and not _is_int(ci):
        errs.append(f"{path}: MONEY ci_level_pct must be an int or null")
    if not isinstance(obj["upper_bound"], bool):
        errs.append(f"{path}: MONEY upper_bound must be a bool")
    if not isinstance(obj["provenance"], list) or not all(
            isinstance(p, str) for p in obj["provenance"]):  # type: ignore[union-attr]
        errs.append(f"{path}: MONEY provenance must be a list of strings")
    if not isinstance(obj["note"], str):
        errs.append(f"{path}: MONEY note must be a string")
    return errs


def _is_money_candidate(obj: Mapping[str, object]) -> bool:
    return "usd" in obj or "nano" in obj


class _Walker:
    def __init__(self, exempt_root: frozenset[str]) -> None:
        self.errors: list[str] = []
        self.exempt_root = exempt_root

    def value(self, v: object, path: str, key: str | None, owner: dict[str, bool]) -> None:
        if isinstance(v, float):
            self.errors.append(f"{path}: float in the document (money and ratios are strings)")
        elif _is_int(v):
            owner["int"] = True
        elif isinstance(v, Mapping):
            self.obj(v, path, key)
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                self.value(item, f"{path}/{i}", None, owner)

    def obj(self, obj: Mapping[str, object], path: str, key: str | None, *,
            root: bool = False) -> None:
        if not all(isinstance(k, str) for k in obj):
            self.errors.append(f"{path or '/'}: object keys must be strings")
            return
        if _is_money_candidate(obj):
            self.errors.extend(_money_errors(obj, path or "/"))
            if key in BILLED_KEYS and obj.get("basis") == Basis.LIST_EQUIVALENT.value:
                self.errors.append(f"{path}: list_equivalent figure under billed key {key!r}")
            hit = _find_float(obj, path)
            if hit is not None:
                self.errors.append(f"{hit}: float in the document (money and ratios are strings)")
            return
        owner = {"int": False}
        for k, v in obj.items():
            child = f"{path}/{k}"
            if _MONEY_KEY_RE.match(k) and v is not None:
                self.errors.append(f"{child}: money not in MONEY form")
                if isinstance(v, float):
                    self.errors.append(f"{child}: float in the document")
                continue
            if root and k in self.exempt_root and _is_int(v):
                continue
            self.value(v, child, k, owner)
        if owner["int"]:
            ev = obj.get("evidence")
            if ev is None:
                self.errors.append(f"{path or '/'}: object with integer values has no evidence key")
            elif not _is_label(ev, _EVIDENCE_VALUES):
                self.errors.append(f"{path or '/'}: evidence is not an Evidence value")


def rule_violations(doc: object, *, exempt_root: Iterable[str] = ("generated_ms",)) -> list[str]:
    """The SPEC §14.1 schema-rule violations of any JSON-like document (floats, integer objects
    without ``evidence``, money outside ``MONEY`` objects, malformed ``MONEY``, list-equivalent
    under billed keys); ``[]`` when it conforms. Used for result@2 and OUT's other JSON outputs."""
    walker = _Walker(frozenset(exempt_root))
    if isinstance(doc, Mapping):
        walker.obj(doc, "", None, root=True)
    else:
        walker.errors.append("/: document is not an object")
    return walker.errors


def validate_result_json(doc: Mapping[str, object]) -> list[str]:
    """Schema-rule violations of a result@2 document (``[]`` = ok): the §14.1 rule
    (:func:`rule_violations`) plus the ``schema`` tag and the presence of every top-level key."""
    errors = rule_violations(doc)
    if not isinstance(doc, Mapping):
        return errors
    if doc.get("schema") != SCHEMA:
        errors.insert(0, f"/schema: must be {SCHEMA!r}")
    missing = [k for k in _TOP_KEYS if k not in doc]
    if missing:
        errors.append(f"/: missing top-level keys {missing}")
    return errors


_TOP_KEYS = ("schema", "tool", "command", "window", "inputs", "privacy", "rate_card", "bill",
             "data_quality", "reconciliation", "calibration", "findings", "action_plan",
             "policy_packs", "replays", "measure_plan", "measurements", "ab", "check", "pricing",
             "receipts")
