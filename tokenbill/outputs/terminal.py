"""Terminal renderer (SPEC §14.2, §8.7; package OUT).

:func:`render_terminal` prints every ``RunResult`` slot in at most *width* (default 100) columns.
Every money value is followed by its label chip (:func:`chip`): ``$41,203.17 exact·list``,
``$9,880.12 allowance·list-equivalent (not billed)``, ``~$6,100/mo est. (p10–p90 $2,900–$8,400)
calibrated``, ``unpriced (3 inferences)``. Billed lines accept only ``Figure.is_billed_eligible``
figures, allowance and Copilot-pool lines only list-equivalent ones (``ContractViolation``
otherwise, R3/R10); grouped data must be a ``PublishedAggregate``. Every string passes
``core.textsafe.sanitize`` (ANSI, C0/C1 controls, bidi overrides) and person pseudonyms
(``p_``/``c_`` + 20 hex) are replaced by ``(pseudonym)``. Rows whose user count is unknown print
"users unknown" (ruling R-E47, ``core.kanon.row_notes``). Channel-extension slots (``copilot``)
render through ``core.extensions.render_sections(result, "terminal")``.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality
from tokenbill.core.money import NANO_PER_USD, fmt_usd, nano_to_usd_str
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    ActionPlan,
    BillSummary,
    CalibrationReport,
    CheckResult,
    Finding,
    MeasurementResult,
    MeasurePlan,
    PricingReport,
    PublishedAggregate,
    ReconciliationReport,
    RunResult,
)
from tokenbill.outputs.result_json import (
    check_median,
    date_of,
    display_rows,
    extension_slots_present,
    policy_spec,
    require_allowance,
    require_billed,
    require_published,
)

__all__ = ["LEGEND", "chip", "pct", "render_terminal", "scrub", "usd"]

#: The legend line explaining the four labels (SPEC §14.2).
LEGEND = ("labels: exact = billed tokens × sourced rate · est. = modeled (range, calibration) · "
          "measured/verified = rollout estimate with CI · list-equivalent = seat allowance or "
          "Copilot credits, never billed")
_PSEUDONYM_RE = re.compile(r"\b[pc]_[0-9a-f]{20}\b")
_BASIS_LABEL = {
    Basis.LIST: "list", Basis.CONTRACT: "contract", Basis.INVOICE: "invoice",
    Basis.PROVIDER_ESTIMATE: "provider estimate (not billed)",
    Basis.LIST_EQUIVALENT: "list-equivalent (not billed)",
}
_CENT_NANO = 10_000_000
_WHOLE_NANO = 100 * NANO_PER_USD
_TOP_N = 10
_MIN_WIDTH = 40


def scrub(text: object, limit: int | None = None) -> str:
    """``sanitize`` plus person-pseudonym scrubbing (``p_``/``c_`` + 20 hex → ``(pseudonym)``)."""
    return _PSEUDONYM_RE.sub("(pseudonym)", sanitize("" if text is None else str(text), limit))


def usd(nano: int, *, whole: bool = False) -> str:
    """Display dollars: 2 places, the exact amount below one cent (``$0.0015``); whole dollars when
    *whole* (estimates of $100 or more)."""
    if whole:
        return fmt_usd(nano, 0)
    if nano != 0 and abs(nano) < _CENT_NANO:
        return ("-$" if nano < 0 else "$") + nano_to_usd_str(abs(nano))
    return fmt_usd(nano, 2)


def _whole(*values: int | None) -> bool:
    return max(abs(v) for v in values if v is not None) >= _WHOLE_NANO


def pct(ratio: str | None, places: int = 1) -> str:
    """A decimal ratio string as a percentage (``"0.998"`` → ``"99.8%"``); ``"n/a"`` for None or
    a malformed value."""
    if ratio is None:
        return "n/a"
    try:
        value = Decimal(ratio) * 100
    except (InvalidOperation, ValueError):
        return "n/a"
    if not value.is_finite():
        return "n/a"
    q = Decimal(1).scaleb(-places)
    return f"{value.quantize(q, rounding=ROUND_HALF_EVEN)}%"


def chip(fig: Figure, *, per: str = "", range_label: str = "range", kind: str | None = None,
         unpriced_count: int | None = None) -> str:
    """The money value of *fig* with its label chip (SPEC §14.2).

    *per* is a unit suffix (``"/mo"``); *range_label* names an estimate's range (``"p10–p90"``);
    *kind* replaces the evidence word of an EXACT figure (``"allowance"``); an unpriced figure
    prints ``unpriced`` (``unpriced (N inferences)`` with *unpriced_count*), never a number (R2).
    """
    if not isinstance(fig, Figure):
        raise ContractViolation("chip expects a Figure")
    if fig.nano is None:
        return f"unpriced ({unpriced_count} inferences)" if unpriced_count else "unpriced"
    basis = _BASIS_LABEL[fig.basis]
    ev = fig.evidence
    rng = fig.low_nano is not None and fig.high_nano is not None
    if fig.basis is Basis.PROVIDER_ESTIMATE and ev is Evidence.EXACT:
        text = f"{usd(fig.nano)}{per} {basis}"      # a reported number, never a bill (R4)
    elif ev is Evidence.EXACT:
        text = f"{usd(fig.nano)}{per} {kind or 'exact'}·{basis}"
    elif ev is Evidence.ESTIMATED:
        whole = _whole(fig.nano, fig.low_nano, fig.high_nano)
        text = f"~{usd(fig.nano, whole=whole)}{per} est."
        if fig.basis is not Basis.LIST:
            text += f"·{basis}"
        if rng:
            text += (f" ({range_label} {usd(fig.low_nano, whole=whole)}–"  # type: ignore[arg-type]
                     f"{usd(fig.high_nano, whole=whole)})")  # type: ignore[arg-type]
        if fig.calibration is not Calibration.NA:
            text += f" {fig.calibration.value}"
    else:
        text = f"{usd(fig.nano)}{per} {ev.value}·{basis}"
        if rng:
            text += (f" ({fig.ci_level_pct}% CI {usd(fig.low_nano)}–"  # type: ignore[arg-type]
                     f"{usd(fig.high_nano)})")  # type: ignore[arg-type]
    if fig.upper_bound:
        text += " upper bound"
    if fig.finality is Finality.PROVISIONAL:
        text += " provisional"
    return text


def _saving_key(f: Finding) -> tuple[int, int, str]:
    fig = f.recoverable_shapley or f.recoverable
    nano = fig.nano if fig is not None and fig.nano is not None else None
    return (0 if nano is not None else 1, -(nano or 0), f.finding_id)


def _is_headroom(f: Finding) -> bool:
    fig = f.recoverable_shapley or f.recoverable or f.headroom
    return f.headroom is not None or (fig is not None and fig.basis is Basis.LIST_EQUIVALENT)


class _Out:
    def __init__(self, width: int) -> None:
        self.width = width
        self.lines: list[str] = []

    def line(self, text: str = "", indent: int = 0) -> None:
        """One line; a line wider than the terminal wraps (continuation indented by 2)."""
        for part in scrub(text).split("\n"):
            if indent + len(part) <= self.width:
                self.lines.append(" " * indent + part)
            else:
                self.wrap(part, indent, cont=2)

    def _fit(self, text: str) -> None:
        self.lines.append(text if len(text) <= self.width else text[: self.width - 1] + "…")

    def wrap(self, text: str, indent: int = 0, first: str = "", cont: int = 0) -> None:
        body = scrub(text)
        wrapped = textwrap.wrap(body, width=self.width, initial_indent=" " * indent + first,
                                subsequent_indent=" " * (indent + len(first) + cont),
                                break_long_words=True, break_on_hyphens=False)
        for w in wrapped or [" " * indent + first]:
            self._fit(w)

    def section(self, title: str) -> None:
        self.lines.append("")
        self.line(title.upper())
        self.lines.append("-" * min(self.width, len(title) + 8))

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]], *,
              right: Sequence[int] = (), indent: int = 2, keep: Sequence[int] = ()) -> None:
        """A column table; columns in *keep* (money chips) are never truncated — when they do not
        fit, each row prints on its own line with the kept cells as indented ``header: value``
        lines."""
        body = [[scrub(c) for c in r] for r in rows]
        head = [scrub(h) for h in headers]
        widths = [max([len(head[i])] + [len(r[i]) for r in body]) for i in range(len(head))]
        room = self.width - indent - 2 * (len(widths) - 1)
        while sum(widths) > room:
            shrink = [i for i in range(len(widths)) if i not in keep and widths[i] > 8]
            if not shrink:
                break
            widths[max(shrink, key=lambda i: (widths[i], -i))] -= 1
        if sum(widths) > room:
            free = [i for i in range(len(widths)) if i not in keep]
            for r in body:
                self.line("  ".join(r[i] for i in free), indent)
                for i in keep:
                    if r[i]:
                        self.line(f"{head[i]}: {r[i]}", indent + 2)
            return

        def fmt(cells: Sequence[str]) -> str:
            out = []
            for i, cell in enumerate(cells):
                cell = cell if len(cell) <= widths[i] else cell[: max(widths[i] - 1, 0)] + "…"
                out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
            return (" " * indent + "  ".join(out)).rstrip()

        self._fit(fmt(head))
        for r in body:
            self._fit(fmt(r))


# ---------------------------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------------------------


def _header(out: _Out, r: RunResult) -> None:
    out.line(f"TOKEN BILL · {r.command} · {date_of(r.window[0])} → {date_of(r.window[1])} "
             "(end exclusive)")
    if r.synthetic:
        out.line("*** SYNTHETIC DEMO DATA — not a real bill ***")
    records = sum(n for _s, n, _q in r.inputs)
    quarantined = sum(q for _s, _n, q in r.inputs)
    out.line(f"sources: {len(r.inputs)} · records: {records:,} · quarantined: {quarantined:,}")
    for src, n, q in r.inputs[:_TOP_N]:
        out.line(f"{src.adapter}  {src.source_id}  records {n:,}  quarantined {q:,}", indent=2)
    if len(r.inputs) > _TOP_N:
        out.line(f"(+{len(r.inputs) - _TOP_N} more sources; see --format json)", indent=2)
    if r.bill is not None:
        t = r.bill.total
        out.line(f"inferences: {t.priced_inferences + t.unpriced_inferences:,} "
                 f"({t.priced_inferences:,} priced, {t.unpriced_inferences:,} unpriced)")
    p = r.privacy
    out.line(f"privacy: content tier {p.content_tier.value} · identity {p.identity_mode} · "
             f"k={p.k} · {p.suppressed_groups} small groups merged or withheld")


def _badges(rep: ReconciliationReport | None) -> list[str]:
    if rep is None:
        return ["reconciliation: not run (the bill is our ledger at the rate card)"]
    marks = {"reconciled": "[ok]", "not_reconciled": "[!!]", "insufficient_data": "[??]"}
    return [f"{marks.get(c.verdict, '[??]')} {c.channel}: {c.verdict.replace('_', ' ')}"
            for c in rep.channels]


def _breakdown(out: _Out, key: str, agg: PublishedAggregate) -> None:
    require_published(agg, f"bill breakdown {key}")
    out.line(f"by {key} (k={agg.k}; {agg.suppressed_rows} rows merged or withheld)", indent=2)
    rows = []
    shown = display_rows(agg)
    for row in shown[:_TOP_N * 2]:
        require_billed(row.priced.exact, f"breakdown {key} exact")
        dims = " ".join(f"{v}" if v is not None else "(none)" for _k, v in row.dims)
        users = "users unknown" if USERS_UNKNOWN in row_notes(row, group_by=agg.group_by) \
            else f"{row.n_users:,}"
        extra = ""
        if row.priced.allowance is not None:
            extra = chip(require_allowance(row.priced.allowance, "breakdown allowance"),
                         kind="allowance")
        rows.append([dims or "(all)", users, f"{row.n_requests:,}",
                     chip(row.priced.exact, unpriced_count=row.priced.unpriced_inferences),
                     extra])
    out.table([key, "users", "requests", "bill", "allowance"], rows, right=(1, 2), indent=4,
              keep=(3, 4))
    if len(shown) > _TOP_N * 2:
        out.line(f"(+{len(shown) - _TOP_N * 2} more rows; see --format json)", indent=4)


def _bill(out: _Out, r: RunResult, bill: BillSummary) -> None:
    out.section("bill")
    t = bill.total
    exact = require_billed(t.exact, "bill exact")
    out.line(f"exact      {chip(exact, unpriced_count=t.unpriced_inferences)}", indent=2)
    if r.rate_card is not None:
        rc = r.rate_card
        contract = f" · contract {rc.contract}" if rc.contract else ""
        out.line(f"basis {rc.basis.value} · rate card {rc.sha256[:12]} "
                 f"({', '.join(rc.layers)}){contract}", indent=2)
        if rc.stale_rows:
            out.line(f"stale rate rows: {len(rc.stale_rows)}", indent=2)
    out.line(f"coverage   {pct(t.coverage)} of billable tokens priced", indent=2)
    if t.unpriced_inferences:
        out.line(f"not priced unpriced ({t.unpriced_inferences:,} inferences) · "
                 f"{t.unpriced_tokens:,} tokens — unknown is not zero", indent=2)
    if t.estimated is not None:
        if t.estimated.basis is Basis.LIST_EQUIVALENT:
            raise ContractViolation("bill estimated: list-equivalent figures are never billed")
        out.line(f"estimated  {chip(t.estimated)} (beside the bill, never in it)", indent=2)
    if t.allowance is not None:
        allowance = require_allowance(t.allowance, "bill allowance")
        out.line(f"allowance  {chip(allowance, kind='allowance')}", indent=2)
    if t.pool is not None:
        pool = require_allowance(t.pool, "bill pool")
        out.line(f"pool       {chip(pool, kind='Copilot credits')} seen by collectors", indent=2)
    if bill.esr is not None:
        out.line(f"ESR        {pct(bill.esr)} effective token savings rate (exact)", indent=2)
    if bill.naive_ratio is not None:
        out.line(f"naive line-sum ratio {bill.naive_ratio}× (de-duplicated bill is priced)",
                 indent=2)
    for badge in _badges(r.reconciliation):
        out.line(badge, indent=2)
    for key, agg in bill.breakdowns:
        _breakdown(out, key, agg)
    for note in bill.footnotes:
        out.wrap(note, indent=2, first="note: ")


def _data_quality(out: _Out, r: RunResult) -> None:
    out.section("data quality")
    for n in r.data_quality:
        extra = f" · {n.tokens:,} tokens" if n.tokens is not None else ""
        fig = f" · {chip(n.figure)}" if n.figure is not None else ""
        out.wrap(f"{n.code} ×{n.count:,}{extra}{fig} — {n.detail}", indent=2,
                 first=f"[{n.severity}] ")


def _calibration(out: _Out, c: CalibrationReport) -> None:
    out.section("calibration (model gate)")
    out.line(f"status {c.status} · mode {c.mode_used or 'n/a'} · {c.n_periods} {c.granularity} "
             f"periods · label {c.calibration().value}", indent=2)
    out.line(f"NMBE {c.nmbe_pct or 'n/a'}% · CV(RMSE) {c.cvrmse_pct or 'n/a'}% "
             f"(thresholds ±{c.thresholds[0]}% / {c.thresholds[1]}%)", indent=2)
    if c.nmbe_pct_calibrated is not None or c.cvrmse_pct_calibrated is not None:
        out.line(f"calibrated: NMBE {c.nmbe_pct_calibrated or 'n/a'}% · CV(RMSE) "
                 f"{c.cvrmse_pct_calibrated or 'n/a'}%", indent=2)
    if c.rho:
        out.table(["gap band", "hits", "trials", "ρ 95% Wilson"],
                  [[b, f"{h:,}", f"{t:,}", f"[{lo}, {hi}]"] for b, h, t, lo, hi in c.rho],
                  right=(1, 2), indent=4)
    if c.diag_confusion:
        out.table(["predicted", "server reason", "n"],
                  [[p, s, f"{n:,}"] for p, s, n in c.diag_confusion[:_TOP_N]], right=(2,),
                  indent=4)
    out.line(f"unlabeled {c.unlabeled:,} · no comparison label {c.no_comparison_labels:,} · "
             f"TTL corroboration {c.ttl_corroboration[0]}/{c.ttl_corroboration[1]}", indent=2)
    for n in c.notes:
        out.wrap(n, indent=2, first="note: ")


def _reconciliation(out: _Out, rep: ReconciliationReport) -> None:
    out.section("reconciliation (ledger gate, per channel)")
    out.wrap(f"verdict {rep.verdict} · finality {rep.finality.value} · window {rep.window[0]} → "
             f"{rep.window[1]} · tolerance {rep.tolerance_pct}% (unexplained "
             f"{rep.unexplained_tolerance_pct}%)", indent=2)
    for c in rep.channels:
        src = ", ".join(c.invoice_sources) or "no invoice source"
        mapped = "" if c.mapping_verified else " · mapping unverified"
        out.line(f"{_badges_one(c.verdict)} {c.channel}: {c.verdict} ({src}){mapped}", indent=2)
    out.line(f"token coverage {rep.token_coverage_pct or 'n/a'}% · dollar coverage "
             f"{rep.dollar_coverage_pct or 'n/a'}% · over-count rows {rep.over_count_rows}",
             indent=2)
    if rep.rate_card_error is not None:
        p50, p95, mx = rep.rate_card_error
        out.line(f"rate-card error |%| p50 {p50} · p95 {p95} · max {mx}", indent=2)
    out.line(f"unexplained residual {usd(rep.unexplained_nano)} (estimated)", indent=2)
    for code, n in rep.residuals:
        out.line(f"residual {code}: {usd(n)} (estimated)", indent=4)
    for key, disc in rep.effective_discount[:_TOP_N]:
        out.line(f"effective discount {key}: {pct(disc)}", indent=4)
    if rep.suggested_contract is not None:
        out.line(f"suggested contract {rep.suggested_contract.name} → re-run verdict "
                 f"{rep.rerun_verdict or 'n/a'}", indent=2)
    for key, value in rep.decisions:
        out.line(f"decision {key} = {value}", indent=4)


def _badges_one(verdict: str) -> str:
    return {"reconciled": "[ok]", "not_reconciled": "[!!]"}.get(verdict, "[??]")


def _tags(f: Finding) -> str:
    tags = []
    figs = [f.recoverable, f.recoverable_shapley, f.projected_monthly]
    if any(x is not None and x.upper_bound for x in figs):
        tags.append("upper-bound")
    if f.needs_eval:
        tags.append("needs-eval")
    if any(_tradeoff(lid) for lid in f.lever_ids):
        tags.append("trade-off")
    return f" [{', '.join(tags)}]" if tags else ""


def _tradeoff(lever_id: str) -> bool:
    from tokenbill.core import catalog

    try:
        return bool(catalog.lever(lever_id).tradeoff)
    except Exception:  # noqa: BLE001 - an unknown lever id carries no trade-off tag
        return False


def _finding_lines(out: _Out, i: int, f: Finding) -> None:
    out.wrap(f"{f.title}{_tags(f)}", indent=2, first=f"{i}. ")
    parts = []
    if f.projected_monthly is not None:
        parts.append("monthly " + chip(f.projected_monthly, per="/mo", range_label="p10–p90"))
    elif f.recoverable_shapley is not None:
        parts.append("shapley " + chip(f.recoverable_shapley))
    elif f.recoverable is not None:
        parts.append("recoverable " + chip(f.recoverable))
    if f.headroom is not None:
        parts.append("headroom " + chip(require_allowance(f.headroom, "finding headroom")))
    parts.append("observed " + chip(f.cost_observed))
    scope = " ".join(f"{k}={v}" for k, v in f.scope.dims) or "org"
    parts.append(f"scope {scope}")
    out.wrap(" · ".join(parts), indent=5)
    if f.fix is not None:
        out.wrap(f.fix.text, indent=5, first="fix: ")


def _findings(out: _Out, findings: Sequence[Finding]) -> None:
    dq = [f for f in findings if f.category == "data-quality" or f.kind.startswith("dq.")]
    rest = [f for f in findings if f not in dq]
    billed = sorted((f for f in rest if not _is_headroom(f)), key=_saving_key)
    headroom = sorted((f for f in rest if _is_headroom(f)), key=_saving_key)
    out.section("top recoverable (Shapley-ranked; standalone ceilings are never summed)")
    if not billed:
        out.line("no billed-basis findings", indent=2)
    for i, f in enumerate(billed[:_TOP_N], 1):
        _finding_lines(out, i, f)
    if len(billed) > _TOP_N:
        out.line(f"(+{len(billed) - _TOP_N} more findings; see --format json)", indent=2)
    if headroom:
        out.section("allowance / pool headroom (list-equivalent, not invoice dollars)")
        for i, f in enumerate(headroom[:_TOP_N], 1):
            _finding_lines(out, i, f)
    if dq:
        out.section("data-quality findings")
        for f in dq[:_TOP_N]:
            out.wrap(f"{f.title} — {f.summary}", indent=2, first="- ")


def _plan(out: _Out, p: ActionPlan) -> None:
    out.section(f"action plan ({p.method})")
    out.line("headline     " + chip(p.headline_monthly, per="/mo", range_label="p10–p90")
             + " (billed-basis levers)", indent=2)
    out.line("joint saving " + chip(p.joint_saving) + " in the window (full-scope joint replay)",
             indent=2)
    if p.sample:
        out.wrap(p.sample, indent=2, first="sample: ")
    for lv in p.levers:
        tags = [t for t, on in (("needs-eval", lv.needs_eval), ("upper-bound", lv.upper_bound),
                                ("trade-off", _tradeoff(lv.lever_id))) if on]
        tag = f" [{', '.join(tags)}]" if tags else ""
        out.line(f"{lv.lever_id} ({lv.lever_class}, group {lv.group}){tag}", indent=2)
        out.wrap(f"shapley {chip(lv.shapley)} · monthly "
                 f"{chip(lv.projected_monthly, per='/mo', range_label='p10–p90')}", indent=4)
    if p.allowance_headroom_monthly is not None:
        out.line("allowance headroom " + chip(require_allowance(
            p.allowance_headroom_monthly, "plan allowance headroom"), per="/mo",
            range_label="p10–p90") + " (not invoice dollars)", indent=2)
    if p.pool_headroom_monthly is not None:
        out.line("Copilot pool headroom " + chip(require_allowance(
            p.pool_headroom_monthly, "plan pool headroom"), per="/mo", range_label="p10–p90"),
            indent=2)
    for cls, rr, n in p.observed_rr:
        out.line(f"observed realization {cls}: mean RR {rr} (n={n})", indent=2)


def _packs(out: _Out, r: RunResult) -> None:
    out.section("policy packs")
    for pack in r.policy_packs:
        out.line(f"{pack.target} · cohort {pack.cohort} · {len(pack.entries)} settings", indent=2)
        for e in pack.entries:
            flag = "" if e.verified_key else " (VERIFY key: comment only)"
            proj = f" · {chip(e.projection, per='/mo', range_label='p10–p90')}" if (
                e.projection is not None) else ""
            out.wrap(f"{e.key} = {e.value_json}{flag}{proj}", indent=4, first="- ")
        if pack.otel_resource_attributes:
            out.line(f"OTEL_RESOURCE_ATTRIBUTES={pack.otel_resource_attributes}", indent=4)


def _replays(out: _Out, r: RunResult) -> None:
    out.section("whatif (counterfactual replays)")
    for rep in r.replays:
        out.wrap(f"{policy_spec(rep.policy)} ({rep.mode})", indent=2, first="policy ")
        out.wrap(f"baseline {chip(rep.baseline)} · cost {chip(rep.cost)} · saving "
                 f"{chip(rep.saving)}", indent=4)
        skipped = f" · skipped {len(rep.lanes_skipped)} lanes" if rep.lanes_skipped else ""
        out.line(f"lanes {rep.n_lanes:,} · requests {rep.n_requests:,} · added calls "
                 f"{rep.added_calls:,} · keepalive pings {rep.keepalive_pings:,}{skipped}",
                 indent=4)


def _measure_plan(out: _Out, m: MeasurePlan) -> None:
    out.section("measure plan")
    out.line(f"lever {m.lever_id} · design {m.design} · clusters by {m.cluster_kind}", indent=2)
    for wave, clusters in m.waves:
        out.wrap(", ".join(clusters) or "(none)", indent=2, first=f"wave {wave}: ")
    out.wrap(", ".join(m.holdback) or "(none)", indent=2, first="holdback: ")
    out.line(f"washout {m.washout_hours} h · looks {', '.join(m.looks) or 'none'}", indent=2)
    mde = "n/a" if m.mde_nano is None else usd(m.mde_nano)
    proj = "n/a" if m.projection is None else chip(m.projection, per="/mo",
                                                  range_label="p10–p90")
    out.line(f"MDE {mde} (estimated) · projection {proj}", indent=2)
    design = "yes" if m.verification_design else "no (cannot verify; at most MEASURED)"
    needed = f" · clusters needed {m.clusters_needed}" if m.clusters_needed is not None else ""
    out.line(f"verification design: {design}{needed}", indent=2)
    out.line(f"pre-registration {m.preregistration_sha256[:16]} · assignment log "
             f"{m.assignment_log_sha256[:16]}", indent=2)
    for w in m.warnings:
        out.wrap(w, indent=2, first="warning: ")


def _measurement(out: _Out, m: MeasurementResult, indent: int = 2) -> None:
    out.wrap(f"{m.lever_id} · {m.design} · {m.unit} · {m.scope_label}", indent=indent)
    out.wrap("estimate " + chip(m.estimate), indent=indent + 2)
    if m.projected is not None:
        out.wrap("projected " + chip(m.projected), indent=indent + 2)
    if m.realization_rate is not None:
        p, lo, hi = m.realization_rate
        out.line(f"realization rate {p} [{lo}, {hi}]", indent=indent + 2)
    guards = ", ".join(f"{g.name} {'pass' if g.passed else 'FAIL'} ({g.value} vs {g.threshold})"
                       for g in m.guards)
    if guards:
        out.wrap(guards, indent=indent + 2, first="guards: ")
    if m.rate_variance is not None:
        out.wrap("rate variance " + chip(m.rate_variance) + " (price effect, reported apart)",
                 indent=indent + 2)
    out.line(f"signable: {'yes' if m.signable else 'no'}", indent=indent + 2)


def _ab(out: _Out, r: RunResult) -> None:
    ab = r.ab
    assert ab is not None
    out.section("ab (paired lab comparison)")
    out.line(f"verdict {ab.verdict} · {ab.scope_label} · {ab.n_tasks} tasks · trials "
             f"{ab.trials_per_arm[0]}/{ab.trials_per_arm[1]} · randomized "
             f"{'yes' if ab.randomized_order else 'no'}", indent=2)
    out.wrap(f"cost per success baseline {chip(ab.cost_per_success[0])} · candidate "
             f"{chip(ab.cost_per_success[1])}", indent=2)
    out.wrap("paired difference " + chip(ab.paired_difference), indent=2)
    out.line(f"Δ tokens {ab.token_delta_pct}% · Δ turns {ab.turn_delta_pct}% · Δ reads "
             f"{ab.read_delta_pct}% · Δ success {ab.success_delta_pct}%", indent=2)
    _measurement(out, ab.measurement, indent=2)


def _check(out: _Out, c: CheckResult, basis: Basis) -> None:
    out.section(f"check ({'passed' if c.passed else 'FAILED'})")
    share = pct(c.cache_read_share) if c.cache_read_share is not None else "n/a"
    med = check_median(c.median_cost_nano, basis, "median cost per run")
    base = check_median(c.baseline_median_cost_nano, basis, "baseline median cost per run")
    out.line(f"runs {c.runs} · cache-read share {share}", indent=2)
    out.line(f"median cost per run {chip(med) if med else 'n/a'} · baseline "
             f"{chip(base) if base else 'n/a'}", indent=2)
    if c.breaker_kinds:
        out.wrap(", ".join(c.breaker_kinds), indent=2, first="breaker kinds: ")
    for v in c.violations:
        out.wrap(f"{v.rule_id} {v.message} @ {v.location}", indent=2, first=f"[{v.level}] ")


def _pricing(out: _Out, p: PricingReport) -> None:
    out.section(f"pricing ({p.kind}; {'ok' if p.ok else 'NOT ok'})")
    rows = [[r.row_id, str(r.input_usd_per_mtok), str(r.output_usd_per_mtok),
             "yes" if r.enabled else "no (VERIFY)", r.verified_on] for r in p.rows[:_TOP_N * 3]]
    if rows:
        out.table(["rate row", "in $/MTok", "out $/MTok", "enabled", "verified"], rows,
                  right=(1, 2))
    if len(p.rows) > _TOP_N * 3:
        out.line(f"(+{len(p.rows) - _TOP_N * 3} more rows; see --format json)", indent=2)
    out.line(f"modifiers {len(p.modifiers)} · stale rows {len(p.stale_rows)}", indent=2)
    for d in p.discrepancies:
        kind = "discrepancy" if d.authoritative else "warning"
        out.wrap(f"{d.row_id} {d.field}: ours {d.ours} vs {d.theirs} ({d.source})", indent=2,
                 first=f"{kind}: ")


def render_terminal(result: RunResult, *, width: int = 100) -> str:
    """Render every ``RunResult`` slot for a terminal in at most *width* columns (SPEC §14.2).

    Raises ``UsageError`` for a width below 40 and ``ContractViolation`` when a billed line gets a
    figure that is not billed-eligible, an allowance line a billed figure, or a breakdown is not a
    ``PublishedAggregate``.
    """
    if not isinstance(result, RunResult):
        raise ContractViolation("render_terminal expects a RunResult")
    if type(width) is not int or width < _MIN_WIDTH:
        raise UsageError(f"terminal width must be an int >= {_MIN_WIDTH}")
    out = _Out(width)
    basis = result.rate_card.basis if result.rate_card is not None else Basis.LIST
    _header(out, result)
    if result.bill is not None:
        _bill(out, result, result.bill)
    if result.data_quality:
        _data_quality(out, result)
    if result.calibration is not None:
        _calibration(out, result.calibration)
    if result.reconciliation is not None:
        _reconciliation(out, result.reconciliation)
    if result.findings:
        _findings(out, result.findings)
    if result.action_plan is not None:
        _plan(out, result.action_plan)
    if result.policy_packs:
        _packs(out, result)
    if result.replays:
        _replays(out, result)
    if result.measure_plan is not None:
        _measure_plan(out, result.measure_plan)
    if result.measurements:
        out.section("measurements")
        for m in result.measurements:
            _measurement(out, m)
    if result.ab is not None:
        _ab(out, result)
    if result.check is not None:
        _check(out, result.check, basis)
    if result.pricing is not None:
        _pricing(out, result.pricing)
    if result.receipts:
        out.section("receipts")
        for rid in result.receipts:
            out.line(rid, indent=2)
    if result.notes:
        out.section("notes")
        for n in result.notes:
            out.wrap(n, indent=2, first="- ")
    if extension_slots_present(result):
        for text in extensions.render_sections(result, "terminal", width=width):
            out.lines.append("")
            for line in str(text).split("\n"):
                out.line(line)
    out.lines.append("")
    out.wrap(LEGEND)
    return "\n".join(out.lines) + "\n"
