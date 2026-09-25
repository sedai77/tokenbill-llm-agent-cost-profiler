"""The Copilot output section: terminal, HTML and result@2 JSON (addendum §14.2, CP-OUT).

:class:`CopilotSection` implements ``core.protocols.SectionRenderer`` for ``RunResult.copilot``;
OUT's renderers include it through ``core.extensions.render_sections``.

* **Terminal** (≤ *width* columns, every string through ``textsafe.sanitize``): a PLAN block first
  (per entity the detected plan and its source, the conflict when any, or "unknown — shown as
  Business and as Enterprise" with the how-to-find-out hint) — before any number; then per entity
  × month "COPILOT BILL" with label chips, the pool bar (pool, consumed, forecast p10–p90, cap
  policy) with regime, billing mode and direct-draw status, and the verdict badges; "COPILOT PLAN"
  (headline, pool headroom on its own line, levers with Shapley credit, reach and tags — never a
  standalone sum); "WHAT TO CHANGE IN GITHUB" (deadlines and auth notes). An entity whose plan is
  unknown renders its scenario block **once per scenario** with the same block renderer, as two
  columns "if Business" | "if Enterprise" (stacked below 80 columns).
* **HTML**: one escaped ``<section id="copilot">``; no scripts, no external resources, no URL
  schemes (doc links print as text); inline SVG pool bars each paired with a ``<table>`` with a
  ``<caption>``; labels spelled out (never color alone); colors from OUT's page variables.
* **JSON**: ``{"pools", "lines", "teams", "seat_counts", "plan", "actions", "channel_verdicts",
  "evidence", "plan_status", "editor_split", "window", "notes"}`` plus ``"scenarios"`` while a plan
  is unknown (the top-level ``lines`` / ``pools`` then hold only scenario-free items); MONEY via
  ``core.labels.figure_json``, no floats, integer objects carry ``evidence``, list-equivalent never
  under a billed key.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tokenbill.copilot.summary import PLAN_HINT, SCENARIOS, is_reconciled, r16_figure
from tokenbill.core import catalog
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
from tokenbill.core.labels import Basis, Evidence, Figure, Finality, estimated, exact, figure_json
from tokenbill.core.money import fmt_usd, nano_to_credits_str
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    ActionPlan,
    AdminAction,
    CopilotBillLine,
    CopilotSummary,
    PlanEvidence,
    PoolMonth,
    PublishedAggregate,
    RunResult,
)

__all__ = [
    "CopilotSection",
    "PlanInfo",
    "block_keys",
    "chip",
    "clean",
    "credits_text",
    "esc",
    "label",
    "money",
    "no_scheme",
    "plan_info",
    "published_json",
]

_PSEUDONYM_RE = re.compile(r"\b[pcr]_[0-9a-f]{20}\b")
_SCHEME_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
_BASIS_WORD = {Basis.LIST: "list", Basis.CONTRACT: "contract", Basis.INVOICE: "invoice",
               Basis.PROVIDER_ESTIMATE: "provider estimate (not billed)",
               Basis.LIST_EQUIVALENT: "list-equivalent (not billed)"}
_PLAN_WORD = {"business": "Business", "enterprise": "Enterprise",
              "mixed": "Mixed (Business and Enterprise)", "unknown": "unknown"}
_SOURCE_WORD = {"seat_lines": "seat SKU lines", "seats_api": "the seats API plan_type",
                "org_settings": "the org billing plan_type",
                "report_quota": "the report quota (experimental)",
                "admin_statement": "an admin statement (not data)", "none": "no evidence"}
_CONFLICT_PREFIX = "dq.copilot_plan_conflict:"
_MIN_COLUMNS = 80
_SEP = " | "


# ---------------------------------------------------------------------------------------------
# text helpers (shared with the showback)
# ---------------------------------------------------------------------------------------------


def clean(text: object, limit: int | None = None) -> str:
    """*text* sanitized (``textsafe.sanitize``) with person pseudonyms replaced by ``(person)``."""
    return _PSEUDONYM_RE.sub("(person)", sanitize(str(text), limit))


def esc(text: object) -> str:
    """*text* cleaned and HTML-escaped (quotes too)."""
    return html_lib.escape(clean(text), quote=True)


def no_scheme(url: str | None) -> str:
    """A URL printed as text without its scheme (HTML pages carry no external URL)."""
    return _SCHEME_RE.sub("", url or "")


def money(fig: Figure | None) -> str:
    """The value of *fig*: ``$1,234.50``, ``~$1,234.50`` for estimates, ``unpriced`` (R2)."""
    if fig is None:
        return "—"
    if fig.nano is None:
        return "unpriced"
    prefix = "~" if fig.evidence is not Evidence.EXACT else ""
    return prefix + fmt_usd(fig.nano)


def label(fig: Figure | None) -> str:
    """The label chip of *fig*: ``invoice``, ``exact·list``, ``est.·list (p10–p90 …)``,
    ``exact·list-equivalent (not billed)``, plus ``provisional`` / ``unreconciled``."""
    if fig is None:
        return ""
    basis = _BASIS_WORD[fig.basis]
    if fig.evidence is Evidence.EXACT:
        text = "invoice" if fig.basis is Basis.INVOICE else f"exact·{basis}"
    else:
        text = f"{'est.' if fig.evidence is Evidence.ESTIMATED else fig.evidence.value}·{basis}"
        if (fig.low_nano is not None and fig.high_nano is not None
                and fig.low_nano != fig.high_nano):
            text += f" ({fmt_usd(fig.low_nano)}–{fmt_usd(fig.high_nano)})"
    if fig.finality is Finality.PROVISIONAL:
        text += " provisional"
    if "unreconciled" in fig.note:
        text += " unreconciled"
    return text


def chip(fig: Figure | None) -> str:
    """``money label`` in one string."""
    return f"{money(fig)} {label(fig)}".strip()


def _group(digits: str) -> str:
    sign = "-" if digits.startswith("-") else ""
    whole, _, frac = digits.lstrip("-").partition(".")
    return sign + f"{int(whole):,}" + (f".{frac}" if frac else "")


def credits_text(nano: int | None) -> str:
    """Nano-USD as AI credits: ``2,680,000 cr``."""
    if nano is None:
        return "unknown"
    return _group(nano_to_credits_str(nano)) + " cr"


def _qty(bl: CopilotBillLine) -> str:
    if bl.quantity is None:
        return ""
    unit = {"ai-credits": "cr", "seat-months": "seats"}.get(bl.unit or "", bl.unit or "")
    return f"{_group(bl.quantity)} {unit}".strip()


# ---------------------------------------------------------------------------------------------
# plan information per entity × month
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanInfo:
    """What the output says about one entity-month's plan."""

    entity_id: str
    month: str
    plan: str
    source: str
    conflict: bool
    unknown: bool
    evidence: tuple[str, ...]

    def text(self) -> str:
        """The PLAN line body."""
        if self.unknown:
            return (f"{self.entity_id} ({self.month}): unknown — shown as Business and as "
                    f"Enterprise, each ESTIMATED, never combined; {PLAN_HINT}")
        if self.plan == "unknown":
            return (f"{self.entity_id} ({self.month}): no seats found — plan and pool unknown; "
                    f"{PLAN_HINT}")
        text = (f"{self.entity_id} ({self.month}): {_PLAN_WORD.get(self.plan, self.plan)} — from "
                f"{_SOURCE_WORD.get(self.source, self.source)}")
        if self.conflict:
            detail = next((e[len(_CONFLICT_PREFIX):].strip() for e in self.evidence
                           if e.startswith(_CONFLICT_PREFIX)),
                          "a lower-precedence source disagreed")
            text += (f"; conflict: {detail} ({_SOURCE_WORD.get(self.source, self.source)} win; "
                     "data beats a statement)")
        return text


def plan_info(entity: str, month: str, pools: Sequence[PoolMonth],
              status: Sequence[PlanEvidence]) -> PlanInfo:
    """The plan of *entity* × *month* from its pools and the matching ``PlanEvidence``."""
    own = [p for p in pools if p.entity_id == entity and p.month == month]
    ev = next((p for p in status if p.entity_id == entity and p.month == month), None)
    unknown = any(p.plan_scenario is not None for p in own)
    if ev is not None:
        return PlanInfo(entity, month, "unknown" if unknown else ev.plan, ev.source,
                        ev.conflict, unknown, ev.evidence)
    known = sorted({plan for p in own for plan, n in p.seats
                    if plan in SCENARIOS and n not in ("0", "")})
    plan = "unknown" if unknown or not known else ("mixed" if len(known) > 1 else known[0])
    first = own[0] if own else None
    return PlanInfo(entity, month, plan, first.plan_source if first else "none",
                    bool(first and first.plan_conflict), unknown, ())


def block_keys(s: CopilotSummary) -> list[tuple[str, str]]:
    """Entity × month keys of the bill, sorted."""
    return sorted({(bl.entity_id, bl.month) for bl in s.lines}
                  | {(p.entity_id, p.month) for p in s.pools})


def _latest_infos(s: CopilotSummary) -> list[PlanInfo]:
    latest: dict[str, str] = {}
    for entity, month in block_keys(s):
        latest[entity] = max(month, latest.get(entity, month))
    for pe in s.plan_status:
        latest[pe.entity_id] = max(pe.month, latest.get(pe.entity_id, pe.month))
    return [plan_info(e, m, s.pools, s.plan_status) for e, m in sorted(latest.items())]


def _verdicts(s: CopilotSummary) -> dict[str, str]:
    return dict(s.channel_verdicts)


def _pool_overage(pm: PoolMonth, verdicts: Mapping[str, str]) -> Figure:
    if pm.plan_scenario is not None:
        return estimated(pm.overage_observed_nano, Basis.LIST,
                         note=f"plan unknown: scenario {pm.plan_scenario}")
    return r16_figure(pm.overage_observed_nano, closed=pm.finality == "closed", final=True,
                      reconciled=is_reconciled(verdicts, "github_copilot"))


def _reach(actions: Sequence[AdminAction], lever_id: str) -> str | None:
    return next((a.reach for a in actions if a.lever_id == lever_id and a.reach is not None),
                None)


def _tradeoff(lever_id: str) -> bool:
    try:
        return catalog.lever(lever_id).tradeoff
    except UsageError:
        return False


def _tags(lever: object, tradeoff: bool) -> list[str]:
    tags = []
    if tradeoff:
        tags.append("trade-off")
    if getattr(lever, "needs_eval", False):
        tags.append("needs eval")
    if getattr(lever, "upper_bound", False):
        tags.append("upper bound")
    return tags


def _pct(reach: str | None) -> str:
    if reach is None:
        return "reach n/a"
    return f"reach {(Decimal(reach) * 100).quantize(Decimal(1))}%"


# ---------------------------------------------------------------------------------------------
# terminal
# ---------------------------------------------------------------------------------------------


class _Text:
    """Width-limited, sanitized terminal lines."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.lines: list[str] = []

    def add(self, text: str = "", indent: int = 0, cont: int = 2) -> None:
        body = clean(text)
        room = self.width - indent
        if room < 8:
            self.lines.append(clean(" " * indent + body, self.width))
            return
        first = True
        while True:
            pad = indent if first else indent + cont
            space = self.width - pad
            if len(body) <= space:
                self.lines.append(" " * pad + body)
                return
            cut = body.rfind(" ", 0, space + 1)
            if cut <= space // 3:
                cut = space
            self.lines.append(" " * pad + body[:cut].rstrip())
            body = body[cut:].lstrip()
            first = False

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.lines.append(line if len(line) <= self.width else line[: self.width - 1] + "…")


def _bar(pm: PoolMonth, length: int) -> str:
    """ASCII pool bar: ``#`` consumed within the pool, ``!`` over it, ``.`` pool left, ``~`` the
    forecast p10–p90 beyond consumption (never color alone)."""
    consumed = pm.consumed_report_nano
    high = pm.forecast.high_nano if pm.forecast is not None and pm.forecast.high_nano else 0
    low = pm.forecast.low_nano if pm.forecast is not None and pm.forecast.low_nano else consumed
    top = max(pm.pool_nano, consumed, high, 1)

    def pos(n: int) -> int:
        return min(length, (max(0, n) * length + top // 2) // top)

    p, c, lo, hi = pos(pm.pool_nano), pos(consumed), pos(low), pos(high)
    out = []
    for i in range(length):
        if i < min(c, p):
            out.append("#")
        elif i < c:
            out.append("!")
        elif lo <= i < hi or (c <= i < hi):
            out.append("~")
        elif i < p:
            out.append(".")
        else:
            out.append(" ")
    return "[" + "".join(out).rstrip() + "]"


def _pool_lines(pm: PoolMonth, verdicts: Mapping[str, str], width: int) -> list[str]:
    t = _Text(width)
    head = f"POOL {credits_text(pm.pool_nano)}"
    if pm.promo:
        head += f" (promo {pm.promo})"
    t.add(f"{head} · consumed {credits_text(pm.consumed_report_nano)} · regime {pm.regime}",
          indent=2)
    t.add(_bar(pm, max(10, min(40, width - 6))), indent=4)
    if pm.forecast is not None:
        t.add(f"forecast {chip(pm.forecast)} (month-end consumption, p10–p90)", indent=4)
    if pm.overage_forecast is not None:
        t.add(f"overage forecast {chip(pm.overage_forecast)}", indent=4)
    extra = [f"billing {pm.billing_mode}", f"direct draws pool: {pm.direct_draws_pool}",
             f"{pm.finality}, {pm.days_final}/{pm.days_in_month} days final"]
    if pm.capped_policy is not None:
        extra.append(f"cap policy {pm.capped_policy}")
    if pm.pool_draw_nano is None:
        extra.append("discounts unclassified")
    t.add(" · ".join(extra), indent=4)
    t.add(f"overage {chip(_pool_overage(pm, verdicts))}", indent=4)
    return t.lines


def _line_rows(lines: Sequence[CopilotBillLine], width: int) -> list[str]:
    """The bill table: name, quantity, amount and label (label wraps under narrow widths)."""
    t = _Text(width)
    wide = width >= 90
    for bl in lines:
        qty = _qty(bl)
        if wide:
            row = f"{bl.line:<32}{qty:>18}{money(bl.amount):>18}  {label(bl.amount)}"
            t.add(row, indent=2, cont=52)
        else:
            t.add(f"{bl.line} {qty}".strip(), indent=2)
            t.add(f"{money(bl.amount)} {label(bl.amount)}", indent=6)
    return t.lines


def _notes_of(lines: Sequence[CopilotBillLine], width: int) -> list[str]:
    t = _Text(width)
    for bl in lines:
        if bl.amount.note and (bl.amount.basis is not Basis.INVOICE or bl.line == "total.invoice"):
            t.add(f"- {bl.line}: {bl.amount.note}", indent=2, cont=4)
    return t.lines


def _block(title: str, lines: Sequence[CopilotBillLine], pool: PoolMonth | None,
           verdicts: Mapping[str, str], width: int) -> list[str]:
    """One bill block (the same renderer for a known-plan entity and for each scenario column)."""
    t = _Text(width)
    t.add(title)
    t.extend(_line_rows(lines, width))
    if pool is not None:
        t.extend(_pool_lines(pool, verdicts, width))
    t.extend(_notes_of(lines, width))
    return t.lines


def _columns(left: Sequence[str], right: Sequence[str], width: int) -> list[str]:
    col = (width - len(_SEP)) // 2
    out = []
    for i in range(max(len(left), len(right))):
        a = left[i] if i < len(left) else ""
        b = right[i] if i < len(right) else ""
        out.append((a.ljust(col) + _SEP + b).rstrip())
    return out


def _plan_block(plan: ActionPlan | None, actions: Sequence[AdminAction], width: int) -> list[str]:
    t = _Text(width)
    if plan is None:
        t.add("no aggregate plan", indent=2)
        return t.lines
    t.add(f"headline {chip(plan.headline_monthly)} /month (invoice dollars)", indent=2)
    if plan.pool_headroom_monthly is not None:
        t.add(f"pool headroom {chip(plan.pool_headroom_monthly)} /month — list-equivalent, not "
              "invoice dollars; never added to the headline", indent=2)
    for lever in plan.levers:
        tags = _tags(lever, _tradeoff(lever.lever_id))
        t.add(f"- {lever.lever_id}: Shapley {chip(lever.shapley)}; projected "
              f"{chip(lever.projected_monthly)} /month; {_pct(_reach(actions, lever.lever_id))}"
              + (f"; {', '.join(tags)}" if tags else ""), indent=2, cont=4)
    t.add("Lever figures are Shapley shares of the joint plan; standalone values are never "
          "summed.", indent=2)
    return t.lines


def _terminal(s: CopilotSummary, width: int) -> str:
    verdicts = _verdicts(s)
    t = _Text(width)
    t.add("COPILOT")
    t.add("-" * min(width, 24))
    infos = _latest_infos(s)
    if any(i.unknown for i in infos):
        t.add("Plan unknown for at least one entity: every pool-dependent figure is shown for "
              "Business and for Enterprise, side by side, each ESTIMATED; no scenario is chosen.")
    t.add("PLAN")
    for info in infos:
        t.add(f"- {info.text()}", indent=2, cont=2)
    t.add(f"window {s.window[0]} … {s.window[1]}")
    for entity, month in block_keys(s):
        info = plan_info(entity, month, s.pools, s.plan_status)
        pools = [p for p in s.pools if p.entity_id == entity and p.month == month]
        lines = [bl for bl in s.lines if bl.entity_id == entity and bl.month == month]
        t.lines.append("")
        known = next((p for p in pools if p.plan_scenario is None), None)
        state = f"{known.finality}; billing {known.billing_mode}" if known else (
            f"{pools[0].finality}; billing {pools[0].billing_mode}" if pools else "no pool month")
        t.add(f"COPILOT BILL — {entity} · {month} · {state}")
        t.add(f"PLAN {info.text()}", indent=2, cont=2)
        shared = [bl for bl in lines if bl.scenario is None]
        t.extend(_block("lines:", shared, known, verdicts, width))
        scen = [p for p in pools if p.plan_scenario is not None]
        if scen:
            t.add("SCENARIOS (plan unknown; each ESTIMATED; never combined)", indent=2)
            col = (width - len(_SEP)) // 2 if width >= _MIN_COLUMNS else width
            blocks = [_block(f"if {_PLAN_WORD[p.plan_scenario or '']}",
                             [bl for bl in lines if bl.scenario == p.plan_scenario], p, verdicts,
                             col) for p in scen]
            if width >= _MIN_COLUMNS and len(blocks) == 2:
                t.extend(_columns(blocks[0], blocks[1], width))
            else:
                for b in blocks:
                    t.extend(b)
        t.add("VERDICTS " + (" · ".join(f"{c}: {v}" for c, v in s.channel_verdicts)
                             or "none (every channel unreconciled)"), indent=2, cont=4)
    t.lines.append("")
    t.add("COPILOT PLAN")
    if s.plans_by_scenario:
        blocks = [[f"if {_PLAN_WORD[k]}"] + _plan_block(p, s.actions, (width - len(_SEP)) // 2
                                                        if width >= _MIN_COLUMNS else width)
                  for k, p in s.plans_by_scenario]
        if width >= _MIN_COLUMNS and len(blocks) == 2:
            t.extend(_columns(blocks[0], blocks[1], width))
        else:
            for b in blocks:
                t.extend(b)
    else:
        t.extend(_plan_block(s.plan, s.actions, width))
    if s.actions:
        t.lines.append("")
        t.add("WHAT TO CHANGE IN GITHUB")
        for a in s.actions:
            parts = [f"[{a.where}] {a.what}"]
            if a.deadline:
                parts.append(f"deadline {a.deadline}")
            if a.auth_note:
                parts.append(f"auth: {a.auth_note}")
            if a.projection is not None:
                parts.append(f"projection {chip(a.projection)}")
            if a.rest_file:
                parts.append(f"request file {a.rest_file} (not executed)")
            parts.append(f"doc {no_scheme(a.doc_url)}")
            t.add("- " + "; ".join(parts), indent=2, cont=2)
    if s.notes:
        t.lines.append("")
        t.add("COPILOT NOTES")
        for n in s.notes:
            t.add(f"- {n}", indent=2, cont=2)
    return "\n".join(line.rstrip() for line in t.lines)


# ---------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------


def _table(caption: str, head: Sequence[str], rows: Iterable[Sequence[str]], *,
           row_header: bool = False) -> str:
    """A ``<table>`` with a ``<caption>``; cells are escaped here."""
    out = [f"<table><caption>{esc(caption)}</caption><thead><tr>"]
    out.extend(f'<th scope="col">{esc(h)}</th>' for h in head)
    out.append("</tr></thead><tbody>")
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            if row_header and i == 0:
                cells.append(f'<th scope="row">{esc(cell)}</th>')
            else:
                cells.append(f"<td>{esc(cell)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _svg_bar(pm: PoolMonth, title: str) -> str:
    """An inline SVG pool bar (pool, consumed, forecast range) with a text label; its table twin
    follows it in the same container."""
    consumed = pm.consumed_report_nano
    high = pm.forecast.high_nano if pm.forecast is not None and pm.forecast.high_nano else 0
    low = pm.forecast.low_nano if pm.forecast is not None and pm.forecast.low_nano else consumed
    top = max(pm.pool_nano, consumed, high, 1)

    def x(n: int) -> int:
        return (max(0, n) * 400 + top // 2) // top

    parts = [f'<svg role="img" aria-label="{esc(title)}" viewBox="0 0 420 44" width="420" '
             'height="44">',
             f'<rect x="10" y="8" width="{x(pm.pool_nano)}" height="14" '
             'style="fill:none;stroke:var(--line,#6b6f76);stroke-width:2"/>',
             f'<rect x="10" y="11" width="{x(consumed)}" height="8" '
             'style="fill:var(--bar-le,#9a5a00)"/>']
    if high:
        parts.append(f'<line x1="{10 + x(low)}" y1="30" x2="{10 + x(high)}" y2="30" '
                     'style="stroke:var(--bar,#2563a8);stroke-width:3"/>')
    parts.append(f'<text x="10" y="42" style="fill:var(--muted,#4a4d52);font-size:10px">'
                 f'{esc("outline = pool; filled = consumed; line = forecast p10–p90")}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pool_html(pm: PoolMonth, verdicts: Mapping[str, str]) -> str:
    title = f"Pool {pm.entity_id} {pm.month}" + (
        f" — if {_PLAN_WORD[pm.plan_scenario]}" if pm.plan_scenario else "")
    rows = [("pool", credits_text(pm.pool_nano) + (f" (promo {pm.promo})" if pm.promo else "")),
            ("consumed (report)", credits_text(pm.consumed_report_nano)),
            ("forecast p10–p90", chip(pm.forecast) if pm.forecast else "none (closed month)"),
            ("overage", chip(_pool_overage(pm, verdicts))),
            ("overage forecast", chip(pm.overage_forecast) if pm.overage_forecast else "none"),
            ("regime", pm.regime), ("billing mode", pm.billing_mode),
            ("direct draws pool", pm.direct_draws_pool),
            ("cap policy", pm.capped_policy or "n/a"), ("finality", pm.finality)]
    return ('<div class="copilot-chart">' + _svg_bar(pm, title)
            + _table(title, ["item", "value"], rows, row_header=True) + "</div>")


def _lines_rows(lines: Sequence[CopilotBillLine]) -> list[tuple[str, str, str, str, str]]:
    return [(bl.line, _qty(bl), money(bl.amount), label(bl.amount), bl.amount.note)
            for bl in lines]


def _scenario_table(lines: Sequence[CopilotBillLine]) -> str:
    names = [n for n in dict.fromkeys(bl.line for bl in lines if bl.scenario is not None)]
    rows = []
    for name in names:
        row = [name]
        for s in SCENARIOS:
            bl = next((b for b in lines if b.line == name and b.scenario == s), None)
            row.append(f"{_qty(bl)} {chip(bl.amount)}".strip() if bl else "—")
        rows.append(row)
    return _table("Plan unknown — if Business | if Enterprise (each ESTIMATED, never combined)",
                  ["line", "if Business", "if Enterprise"], rows, row_header=True)


def _plan_html(plan: ActionPlan | None, actions: Sequence[AdminAction], caption: str) -> str:
    if plan is None:
        return f"<p>{esc(caption)}: no aggregate plan.</p>"
    rows = [("headline (invoice dollars)", chip(plan.headline_monthly), "", "")]
    if plan.pool_headroom_monthly is not None:
        rows.append(("pool headroom (list-equivalent, not invoice dollars)",
                     chip(plan.pool_headroom_monthly), "", ""))
    for lever in plan.levers:
        rows.append((lever.lever_id, f"Shapley {chip(lever.shapley)}; projected "
                                     f"{chip(lever.projected_monthly)} /month",
                     _pct(_reach(actions, lever.lever_id)),
                     ", ".join(_tags(lever, _tradeoff(lever.lever_id)))))
    return _table(caption + " (Shapley shares; standalone values never summed)",
                  ["item", "figure", "reach", "tags"], rows, row_header=True)


def _html(s: CopilotSummary) -> str:
    verdicts = _verdicts(s)
    out = ['<section id="copilot"><h2>GitHub Copilot</h2>']
    infos = _latest_infos(s)
    if any(i.unknown for i in infos):
        out.append("<p><strong>Plan unknown</strong> for at least one entity: every "
                   "pool-dependent figure is shown for Business and for Enterprise, side by side, "
                   "each ESTIMATED; no scenario is chosen.</p>")
    out.append(_table("Copilot plan per billing entity", ["entity", "plan"],
                      [(i.entity_id, i.text()) for i in infos], row_header=True))
    for entity, month in block_keys(s):
        info = plan_info(entity, month, s.pools, s.plan_status)
        pools = [p for p in s.pools if p.entity_id == entity and p.month == month]
        lines = [bl for bl in s.lines if bl.entity_id == entity and bl.month == month]
        out.append(f"<h3>{esc(f'Copilot bill — {entity} · {month}')}</h3>")
        out.append(f"<p>{esc('PLAN ' + info.text())}</p>")
        out.append(_table(f"Bill lines {entity} {month}",
                          ["line", "quantity", "amount", "label", "note"],
                          _lines_rows([bl for bl in lines if bl.scenario is None]),
                          row_header=True))
        if any(bl.scenario is not None for bl in lines):
            out.append(_scenario_table(lines))
        for pm in pools:
            out.append(_pool_html(pm, verdicts))
    out.append("<h3>Reconciliation verdicts</h3><ul>" + "".join(
        f"<li>{esc(c)}: <strong>{esc(v)}</strong></li>" for c, v in s.channel_verdicts)
        + "</ul>" if s.channel_verdicts else "<p>No channel verdicts: every channel unreconciled."
                                             "</p>")
    out.append("<h3>Copilot plan</h3>")
    if s.plans_by_scenario:
        out.extend(_plan_html(p, s.actions, f"If {_PLAN_WORD[k]}") for k, p in s.plans_by_scenario)
    else:
        out.append(_plan_html(s.plan, s.actions, "Copilot aggregate plan"))
    if s.actions:
        out.append("<h3>What to change in GitHub</h3>")
        out.append(_table("Admin actions (never executed by Token Bill)",
                          ["where", "what", "deadline", "auth", "doc"],
                          [(a.where, a.what, a.deadline or "", a.auth_note or "",
                            no_scheme(a.doc_url)) for a in s.actions]))
    if s.notes:
        out.append("<ul>" + "".join(f"<li>{esc(n)}</li>" for n in s.notes) + "</ul>")
    out.append("</section>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------


def _line_json(bl: CopilotBillLine) -> dict[str, object]:
    return {"month": bl.month, "entity_id": bl.entity_id, "line": bl.line,
            "quantity": bl.quantity, "unit": bl.unit, "amount": figure_json(bl.amount),
            "components": list(bl.components), "scenario": bl.scenario}


def _pool_json(pm: PoolMonth, verdicts: Mapping[str, str]) -> dict[str, object]:
    fin = Finality.FINAL if pm.finality == "closed" else Finality.PROVISIONAL
    closed = pm.finality == "closed"
    reconciled = is_reconciled(verdicts, "github_copilot")
    return {
        "entity_id": pm.entity_id, "month": pm.month, "billing_mode": pm.billing_mode,
        "seats": [{"plan": p, "seats": n} for p, n in pm.seats],
        "seats_source": pm.seats_source, "pool_credits": pm.pool_credits,
        "pool": figure_json(exact(pm.pool_nano, Basis.LIST_EQUIVALENT)), "promo": pm.promo,
        "consumed": figure_json(exact(pm.consumed_report_nano, Basis.LIST_EQUIVALENT,
                                      finality=fin)),
        "consumed_estimate": figure_json(exact(pm.consumed_estimate_nano,
                                               Basis.PROVIDER_ESTIMATE)),
        "pool_draw": None if pm.pool_draw_nano is None else figure_json(
            exact(pm.pool_draw_nano, Basis.LIST_EQUIVALENT)),
        "discount_other": figure_json(exact(pm.discount_other_nano, Basis.LIST)),
        "discount_unclassified": figure_json(exact(pm.discount_unclassified_nano, Basis.LIST)),
        "overage": figure_json(_pool_overage(pm, verdicts)),
        "direct_net": figure_json(r16_figure(pm.direct_net_nano, closed=closed, final=True,
                                             reconciled=reconciled)),
        "direct_draws_pool": pm.direct_draws_pool, "capped_policy": pm.capped_policy,
        "days_final": pm.days_final, "days_provisional": pm.days_provisional,
        "days_in_month": pm.days_in_month, "evidence": Evidence.EXACT.value,
        "finality": pm.finality,
        "forecast": None if pm.forecast is None else figure_json(pm.forecast),
        "overage_forecast": None if pm.overage_forecast is None else figure_json(
            pm.overage_forecast),
        "regime": pm.regime, "notes": [clean(n) for n in pm.notes],
        "plan_source": pm.plan_source, "plan_scenario": pm.plan_scenario,
        "plan_conflict": pm.plan_conflict,
    }


def published_json(agg: PublishedAggregate | None, *, count_key: str = "requests"
                   ) -> dict[str, object] | None:
    """A published aggregate as JSON: rows with dims, users (or ``users unknown``, R-E47), the
    credits (``priced.pool``) and GitHub's net (``priced.exact``) as MONEY."""
    if agg is None:
        return None
    if not isinstance(agg, PublishedAggregate):
        raise ContractViolation("published_json expects a PublishedAggregate")
    rows = []
    for row in agg.rows:
        unknown = USERS_UNKNOWN in row_notes(row, group_by=agg.group_by)
        rows.append({"dims": {k: v for k, v in row.dims}, "users": None if unknown else
                     row.n_users, "users_unknown": unknown, count_key: row.n_requests,
                     "credits": None if row.priced.pool is None else figure_json(row.priced.pool),
                     "net": figure_json(row.priced.exact), "evidence": Evidence.EXACT.value})
    return {"group_by": list(agg.group_by), "k": agg.k, "suppressed_rows": agg.suppressed_rows,
            "suppressed_users": agg.suppressed_users, "evidence": Evidence.EXACT.value,
            "rows": rows}


def _plan_json(plan: ActionPlan | None, actions: Sequence[AdminAction]) -> dict[str, object] | None:
    if plan is None:
        return None

    def opt(fig: Figure | None) -> dict[str, object] | None:
        return None if fig is None else figure_json(fig)

    return {
        "joint_saving": figure_json(plan.joint_saving),
        "headline_monthly": figure_json(plan.headline_monthly),
        "pool_headroom_monthly": opt(plan.pool_headroom_monthly),
        "allowance_headroom_monthly": opt(plan.allowance_headroom_monthly),
        "levers": [{"lever_id": lv.lever_id, "lever_class": lv.lever_class, "params": lv.params,
                    "basis": lv.basis.value, "standalone": figure_json(lv.standalone),
                    "shapley": figure_json(lv.shapley),
                    "projected_monthly": figure_json(lv.projected_monthly),
                    "needs_eval": lv.needs_eval, "upper_bound": lv.upper_bound,
                    "tradeoff": _tradeoff(lv.lever_id), "reach": _reach(actions, lv.lever_id),
                    "group": lv.group, "finding_ids": list(lv.finding_ids)}
                   for lv in plan.levers],
        "groups": [{"group": g, "levers": list(ids)} for g, ids in plan.groups],
        "method": plan.method, "sample": clean(plan.sample),
        "observed_rr": [{"lever_class": c, "mean_rr": rr, "receipts": n,
                         "evidence": Evidence.EXACT.value} for c, rr, n in plan.observed_rr],
    }


def _action_json(a: AdminAction) -> dict[str, object]:
    return {"action_id": a.action_id, "lever_id": a.lever_id, "admin_action": a.admin_action,
            "where": a.where, "what": clean(a.what), "doc_url": a.doc_url,
            "rest_file": a.rest_file, "auth_note": a.auth_note, "reach": a.reach,
            "projection": None if a.projection is None else figure_json(a.projection),
            "deadline": a.deadline, "needs_eval": a.needs_eval, "tradeoff": a.tradeoff}


def _status_json(s: CopilotSummary) -> list[dict[str, object]]:
    out = []
    for entity, month in block_keys(s):
        info = plan_info(entity, month, s.pools, s.plan_status)
        out.append({"entity_id": entity, "month": month, "plan": info.plan,
                    "source": info.source, "conflict": info.conflict,
                    "evidence": [clean(e) for e in info.evidence],
                    "scenarios": list(SCENARIOS) if info.unknown else [],
                    "hint": PLAN_HINT if info.unknown or info.plan == "unknown" else None})
    return out


def _json(s: CopilotSummary) -> dict[str, object]:
    verdicts = _verdicts(s)
    doc: dict[str, object] = {
        "window": list(s.window),
        "pools": [_pool_json(p, verdicts) for p in s.pools if p.plan_scenario is None],
        "lines": [_line_json(bl) for bl in s.lines if bl.scenario is None],
        "teams": published_json(s.teams),
        "seat_counts": [{"team": t, "plan_bucket": pb, "seats": n,
                         "evidence": Evidence.EXACT.value} for t, pb, n in s.seat_counts],
        "plan": _plan_json(s.plan, s.actions),
        "actions": [_action_json(a) for a in s.actions],
        "channel_verdicts": [{"channel": c, "verdict": v} for c, v in s.channel_verdicts],
        "evidence": Evidence.EXACT.value,
        "plan_status": _status_json(s),
        "editor_split": published_json(s.editor_split, count_key="interactions"),
        "notes": [clean(n) for n in s.notes],
    }
    if any(p.plan_scenario is not None for p in s.pools) or s.plans_by_scenario:
        plans = dict(s.plans_by_scenario)
        doc["scenarios"] = {
            sc: {"lines": [_line_json(bl) for bl in s.lines if bl.scenario == sc],
                 "pools": [_pool_json(p, verdicts) for p in s.pools if p.plan_scenario == sc],
                 "plan": _plan_json(plans.get(sc), s.actions)}
            for sc in SCENARIOS}
    return doc


# ---------------------------------------------------------------------------------------------
# the renderer
# ---------------------------------------------------------------------------------------------


def _summary(result: RunResult) -> CopilotSummary | None:
    if not isinstance(result, RunResult):
        raise ContractViolation("CopilotSection expects a RunResult")
    s = result.copilot
    if s is not None and not isinstance(s, CopilotSummary):
        raise ContractViolation("RunResult.copilot must be a CopilotSummary")
    return s


class CopilotSection:
    """``SectionRenderer`` of ``RunResult.copilot`` (addendum §14.2)."""

    name = "copilot"

    def terminal(self, result: RunResult, *, width: int = 100) -> str:
        """The terminal section (≤ *width* columns, sanitized); ``""`` without Copilot data."""
        s = _summary(result)
        if s is None:
            return ""
        if type(width) is not int or width < 40:
            raise UsageError("terminal width must be an int >= 40")
        return _terminal(s, width)

    def html(self, result: RunResult) -> str:
        """One escaped ``<section id="copilot">`` (no scripts, no external URL); ``""`` without
        Copilot data."""
        s = _summary(result)
        return "" if s is None else _html(s)

    def json(self, result: RunResult) -> dict | None:
        """The ``copilot`` object of result@2 (MONEY via ``figure_json``), or None."""
        s = _summary(result)
        return None if s is None else _json(s)
