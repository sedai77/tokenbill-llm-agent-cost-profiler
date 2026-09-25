"""The Copilot summary: bill lines per entity × month and k-anonymous team figures (CP-OUT).

:func:`assemble_summary` turns GitHub's own billing lines, the pool months of ``core.pool`` and the
plan evidence into a :class:`~tokenbill.core.types.CopilotSummary` — the ``RunResult.copilot`` slot
that :class:`tokenbill.copilot.render.CopilotSection`, the showback and CP-WIRE read.

**Bill lines (addendum §14.1, R16).** Per entity × month (the entity-months of the pools, plus any
entity-month with adjacent lines only):

* ``seats.<plan>`` — the seat SKU lines' net: INVOICE when the month is closed, every line final and
  ``github_copilot`` reconciled, else EXACT LIST noting ``unreconciled`` / ``provisional``; without
  seat lines the pool's seat count × list price, ESTIMATED LIST (:data:`SEAT_NOTE`).
* ``ai_credits.gross`` — Σ report gross (LIST_EQUIVALENT, EXACT): the credit valuation of usage.
* ``ai_credits.discount_pool`` (classified pool-included, LIST_EQUIVALENT), ``discount_other`` and
  ``discount_unclassified`` (EXACT LIST; never labelled pool-included before DC22 classifies it).
* ``ai_credits.overage`` / ``ai_credits.direct_org`` — Σ net of pooled / direct rows, labelled by
  R16; an open month is observed-so-far (LIST, provisional) and its forecast lives in the pool.
* ``code_quality.licenses``, ``actions.*``, ``sandbox`` — Σ net with their own channel's verdict;
  ``code_quality.ai_credits`` is a breakout of credits already inside overage / direct-org and is
  never added to the total.
* ``total.invoice`` — ``core.labels.combine_weakest`` over the dollar lines only (never a
  LIST_EQUIVALENT line), ``components`` naming them and the note naming each non-invoice one.

**Unknown plan (R17, ruling R-E22).** When an entity-month's pools carry ``plan_scenario``, the
plan-independent lines (gross, discounts when both scenarios classify alike, the observed pooled
net, direct-org, Actions, sandbox, Code Quality) are emitted once; the seat lines (known seats plus
``seats.unknown_plan`` priced at the scenario's list price, ESTIMATED), the scenario overage
(consumption − the scenario's pool, ESTIMATED) and ``total.invoice`` are emitted once per scenario
with ``scenario`` set. Nothing is summed or chosen across scenarios.

**People (R14, DC7).** ``teams`` (credits and GitHub's net per entity × month × team × workload),
``seat_counts`` (seats per team × ``plan:bucket``) and ``editor_split`` (interactions and credits
per team × editor family) go through ``core.kanon.publish`` — the only k-anonymity implementation;
rows below *k* users are merged, with complementary suppression.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from tokenbill.core import catalog
from tokenbill.core import facts as _facts
from tokenbill.core.errors import UsageError
from tokenbill.core.kanon import other_label, publish
from tokenbill.core.labels import (
    Basis,
    Evidence,
    Figure,
    Finality,
    combine_weakest,
    estimated,
    exact,
)
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, nano_to_credits_str
from tokenbill.core.pool import DIRECT_COST_TYPES, POOLED_COST_TYPES, build_cells, entity_of
from tokenbill.core.records import (
    COPILOT_CHANNELS,
    ActivityDay,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import (
    ActionPlan,
    AdminAction,
    AggRow,
    ChannelVerdict,
    CopilotBillLine,
    CopilotSummary,
    PlanEvidence,
    PoolMonth,
    PricedTotal,
    PublishedAggregate,
    RawAggregate,
)

__all__ = [
    "DOLLAR_LINES",
    "EDITOR_GROUP_BY",
    "EDITOR_SPLIT_FAMILIES",
    "INTERACTIVE",
    "KNOWN",
    "PLAN_HINT",
    "SCENARIOS",
    "SEAT_GROUP_BY",
    "SEAT_NOTE",
    "TEAM_GROUP_BY",
    "apportion",
    "assemble_summary",
    "entity_mode_of",
    "capped_of",
    "is_reconciled",
    "r16_figure",
    "with_note",
]

#: Plan scenarios while a plan is unknown (R17), in display order.
SCENARIOS = ("business", "enterprise")
#: CP-PLAN's ``plan_copilot_scenarios`` key of the single plan when every plan is known.
KNOWN = "known"
RECONCILED = "reconciled"
_CREDITS = "github_copilot"
_ACTIONS = "github_actions"
_SANDBOX = "github_sandbox"
#: The lines ``total.invoice`` may sum (dollar lines; never LIST_EQUIVALENT, never a breakout).
DOLLAR_LINES = ("seats.business", "seats.enterprise", "seats.unknown_plan", "ai_credits.overage",
                "ai_credits.direct_org", "code_quality.licenses", "actions.code_review",
                "actions.cloud_agent", "actions.agentic_workflow", "sandbox")
#: Note of seat fees computed as count × list price (addendum §14.1).
SEAT_NOTE = ("list price × seats; proration, upfront charges, volume/EA pricing not modeled")
#: How to find out an unknown plan (addendum §14.2; ``PlanEvidence`` has no hint field, C-13).
PLAN_HINT = ("how to find out: the seats API plan_type (copilot pull --sources seats), the "
             "detailed usage report's seat SKU copilot_for_business / copilot_enterprise, "
             "Enterprise settings → Licensing, or the admin's answers.json "
             "(--plan ENTITY=business|enterprise)")
_ACTIONS_LINES = {"copilot_code_review": "actions.code_review",
                  "copilot_cloud_agent": "actions.cloud_agent",
                  "agentic_workflow": "actions.agentic_workflow"}
#: ``teams`` grouping: credits (pool, LIST_EQUIVALENT) and GitHub's per-row net (exact, LIST) of
#: the pooled report rows.
TEAM_GROUP_BY = ("entity", "month", "team", "workload")
#: ``workload`` value of pooled rows without a Copilot workload (IDE, chat, CLI, agent use).
INTERACTIVE = "interactive"
#: ``seat_counts`` is published from this grouping (count = ``AggRow.n_requests``).
SEAT_GROUP_BY = ("plan_bucket", "team")
#: ``editor_split`` grouping (owner answer 3: VS Code vs JetBrains; other editors merged).
EDITOR_GROUP_BY = ("team", "editor_family")
EDITOR_SPLIT_FAMILIES = ("vscode", "jetbrains", "other")
_OTHER_TEAM = "(other)"
_UNATTRIBUTED = "(unattributed)"
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_EDITOR_NOTE = ("report credits apportioned by the team's ide interaction share (ESTIMATED "
                "allocation)")


# ---------------------------------------------------------------------------------------------
# small shared helpers (also used by render, showback and focus)
# ---------------------------------------------------------------------------------------------


def apportion(total: int, weights: Sequence[int]) -> list[int]:
    """Split *total* nano over *weights* by the largest-remainder rule: the parts sum to *total*
    exactly (ties go to the earlier weight); all-zero weights give all-zero parts."""
    if total < 0:
        return [-p for p in apportion(-total, weights)]
    w = [max(0, int(x)) for x in weights]
    den = sum(w)
    if den == 0:
        return [0] * len(w)
    parts = [total * x // den for x in w]
    rest = total - sum(parts)
    order = sorted(range(len(w)), key=lambda i: (-(total * w[i] % den), i))
    for i in order[:rest]:
        parts[i] += 1
    return parts


def capped_of(pools: Iterable[PoolMonth]) -> dict[str, Decimal]:
    """Capped cost centers (name → cap credits) as the pools name them (``cc:<name>`` entities)."""
    out: dict[str, Decimal] = {}
    for pm in pools:
        if pm.entity_id.startswith("cc:"):
            out[pm.entity_id[3:]] = Decimal(pm.pool_credits)
    return dict(sorted(out.items()))


def entity_mode_of(pools: Iterable[PoolMonth]) -> str:
    """``org`` when the pools are per organization (``org:<login>`` entities), else
    ``enterprise``."""
    return "org" if any(pm.entity_id.startswith("org:") for pm in pools) else "enterprise"


def is_reconciled(verdicts: Mapping[str, str], channel: str) -> bool:
    """True iff *channel*'s reconciliation verdict is ``reconciled``."""
    return verdicts.get(channel) == RECONCILED


def _verdict_pairs(channel_verdicts: object) -> tuple[tuple[str, str], ...]:
    if channel_verdicts is None:
        return ()
    items = channel_verdicts.items() if isinstance(channel_verdicts, Mapping) else channel_verdicts
    out: dict[str, str] = {}
    for item in items:  # type: ignore[union-attr]
        if isinstance(item, ChannelVerdict):
            out[item.channel] = item.verdict
        elif (isinstance(item, (tuple, list)) and len(item) == 2
              and all(isinstance(x, str) for x in item)):
            out[item[0]] = item[1]
        else:
            raise UsageError("channel_verdicts: expected channel → verdict pairs")
    return tuple(sorted(out.items()))


def _window_ms(window: tuple[str, str]) -> tuple[int, int]:
    try:
        lo = (_dt.date.fromisoformat(window[0]) - _EPOCH).days * _DAY_MS
        hi = ((_dt.date.fromisoformat(window[1]) - _EPOCH).days + 1) * _DAY_MS
    except (TypeError, ValueError, IndexError):
        return (0, 0)
    return (lo, max(lo, hi))


def _seat_price(plan: str) -> Decimal:
    fact = _facts.copilot_plans().get(plan)
    if fact is None:
        raise UsageError("unknown Copilot plan")
    return fact.seat_usd_per_month


def _list_fee(count: Decimal, plan: str) -> int:
    return decimal_to_nano(EXACT_CTX.multiply(count, _seat_price(plan)))


def _dec(value: object) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _dec_str(d: Decimal) -> str:
    if not d:
        return "0"
    return format(d.normalize(), "f")


# ---------------------------------------------------------------------------------------------
# bill lines
# ---------------------------------------------------------------------------------------------


@dataclass
class _Acc:
    """Σ of cost lines of one bill line: quantity, net, gross, all-final."""

    quantity: Decimal = Decimal(0)
    net: int = 0
    gross: int = 0
    final: bool = True
    n: int = 0
    unit: str | None = None

    def add(self, line: CostLine) -> None:
        self.quantity = EXACT_CTX.add(self.quantity, _dec(line.quantity or "0"))
        self.net += line.amount_nano
        self.gross += line.list_amount_nano if line.list_amount_nano is not None else (
            line.amount_nano)
        self.final = self.final and line.finality == "final"
        self.n += 1
        self.unit = self.unit or line.unit


@dataclass
class _EntityMonth:
    """Everything the bill of one entity × month is made of."""

    seats: dict[str | None, _Acc] = field(default_factory=dict)      # plan (None: unmapped SKU)
    adjacent: dict[str, _Acc] = field(default_factory=dict)          # bill line → lines
    channel_of: dict[str, str] = field(default_factory=dict)         # bill line → channel
    cq_credits: _Acc = field(default_factory=_Acc)                   # code_quality.ai_credits
    gross: int = 0
    credits: Decimal = Decimal(0)
    pooled_net: int = 0
    pooled_credits: Decimal = Decimal(0)
    direct_net: int = 0
    direct_credits: Decimal = Decimal(0)
    ai_cells: int = 0
    direct_cells: int = 0


def r16_figure(nano: int, *, closed: bool, final: bool, reconciled: bool, open_note: str = "",
         provenance: Sequence[str] = ()) -> Figure:
    """An invoice-side amount labelled by R16: INVOICE iff closed month × final rows × reconciled
    channel; else EXACT LIST naming why (``unreconciled`` / ``provisional``)."""
    if closed and final and reconciled:
        return exact(nano, Basis.INVOICE, finality=Finality.FINAL, provenance=provenance)
    notes = []
    if not (closed and final):
        notes.append(open_note or "provisional: month open or rows not final")
    if not reconciled:
        notes.append("unreconciled")
    return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.LIST,
                  finality=Finality.FINAL if closed and final else Finality.PROVISIONAL,
                  provenance=tuple(provenance), note="; ".join(notes))


def _collect(cost_lines: Sequence[CostLine], aggregates: Sequence[UsageAggregate],
             capped: Mapping[str, Decimal], mode: str,
             notes: list[str]) -> dict[tuple[str, str], _EntityMonth]:
    out: dict[tuple[str, str], _EntityMonth] = defaultdict(_EntityMonth)
    skipped = False
    for line in cost_lines:
        if line.channel not in COPILOT_CHANNELS:
            continue
        key = (entity_of(line.cost_center, line.workspace_id, capped=capped, entity_mode=mode),
               line.date_utc[:7])
        ct = line.cost_type
        if ct == "seat" and line.channel == _CREDITS:
            em = out[key]
            em.seats.setdefault(catalog.copilot_seat_plan(line.sku), _Acc()).add(line)
        elif ct == "code_quality.license":
            out[key].adjacent.setdefault("code_quality.licenses", _Acc()).add(line)
            out[key].channel_of["code_quality.licenses"] = line.channel
        elif ct in (*POOLED_COST_TYPES, *DIRECT_COST_TYPES) and line.workload == "code_quality":
            out[key].cq_credits.add(line)
        elif ct == "actions":
            name = _ACTIONS_LINES.get(line.workload or "")
            if name is None:
                skipped = True
                continue
            out[key].adjacent.setdefault(name, _Acc()).add(line)
            out[key].channel_of[name] = line.channel
        elif ct == "sandbox":
            out[key].adjacent.setdefault("sandbox", _Acc()).add(line)
            out[key].channel_of["sandbox"] = line.channel
    if skipped:
        notes.append("Actions lines without a Copilot workload (code review, cloud agent, "
                     "agentic workflow) are not part of the Copilot bill and were left out")
    cells, _ = build_cells(aggregates, cost_lines, grain="month", capped=capped, entity_mode=mode)
    for c in cells:
        em = out[(c.entity_id, c.month)]
        credits = _dec(c.credits)
        if c.cost_type in POOLED_COST_TYPES:
            em.pooled_net += c.net_nano
            em.pooled_credits = EXACT_CTX.add(em.pooled_credits, credits)
        elif c.cost_type in DIRECT_COST_TYPES:
            em.direct_net += c.net_nano
            em.direct_credits = EXACT_CTX.add(em.direct_credits, credits)
            em.direct_cells += 1
        else:
            continue
        em.gross += c.gross_nano
        em.credits = EXACT_CTX.add(em.credits, credits)
        em.ai_cells += 1
    return out


class _Bill:
    """Builds the lines of one entity × month."""

    def __init__(self, entity: str, month: str, em: _EntityMonth, pools: Sequence[PoolMonth],
                 verdicts: Mapping[str, str]) -> None:
        self.entity, self.month, self.em = entity, month, em
        self.pools = sorted(pools, key=lambda p: p.plan_scenario or "")
        self.verdicts = verdicts
        self.closed = bool(self.pools) and all(p.finality == "closed" for p in self.pools)
        self.out: list[CopilotBillLine] = []

    def line(self, name: str, amount: Figure, *, quantity: str | None = None,
             unit: str | None = None, scenario: str | None = None,
             components: Sequence[str] = ()) -> CopilotBillLine:
        bl = CopilotBillLine(month=self.month, entity_id=self.entity, line=name,
                             quantity=quantity, unit=unit, amount=amount,
                             components=tuple(components), scenario=scenario)
        self.out.append(bl)
        return bl

    def _data_line(self, name: str, acc: _Acc, channel: str, *,
                   scenario: str | None = None, note: str = "") -> CopilotBillLine:
        fig = r16_figure(acc.net, closed=self.closed, final=acc.final,
                   reconciled=is_reconciled(self.verdicts, channel))
        if note:
            fig = with_note(fig, note)
        return self.line(name, fig, quantity=_dec_str(acc.quantity), unit=acc.unit,
                         scenario=scenario)

    def seats(self, pm: PoolMonth | None, scenario: str | None) -> list[CopilotBillLine]:
        em = self.em
        counts = {plan: _dec(n) for plan, n in pm.seats} if pm is not None else {}
        lines: list[CopilotBillLine] = []
        lower = pm is not None and pm.seats_source == "report_users"
        for plan in SCENARIOS:
            acc = em.seats.get(plan)
            if acc is not None:
                lines.append(self._data_line(f"seats.{plan}", acc, _CREDITS, scenario=scenario))
            elif not em.seats and counts.get(plan, Decimal(0)) > 0:
                note = SEAT_NOTE + ("; seats lower bound (report users)" if lower else "")
                fee = estimated(_list_fee(counts[plan], plan), Basis.LIST, note=note)
                lines.append(self.line(f"seats.{plan}", fee, quantity=_dec_str(counts[plan]),
                                       unit="seats", scenario=scenario))
        unknown = counts.get("unknown", Decimal(0))
        unmapped = em.seats.get(None)
        if unmapped is not None:
            lines.append(self._data_line("seats.unknown_plan", unmapped, _CREDITS,
                                         scenario=scenario,
                                         note="seat SKU not mapped to a plan; amount from the "
                                              "seat lines"))
        elif scenario is not None and unknown > 0:
            note = (f"plan unknown: priced as {scenario}; both scenarios shown; {SEAT_NOTE}"
                    + ("; seats lower bound (report users)" if lower else ""))
            lines.append(self.line("seats.unknown_plan",
                                   estimated(_list_fee(unknown, scenario), Basis.LIST, note=note),
                                   quantity=_dec_str(unknown), unit="seats", scenario=scenario))
        return lines

    def build(self) -> list[CopilotBillLine]:
        em, pools = self.em, self.pools
        scenarios = [p for p in pools if p.plan_scenario is not None]
        known = next((p for p in pools if p.plan_scenario is None), None)
        credits_ok = is_reconciled(self.verdicts, _CREDITS)
        shared: list[CopilotBillLine] = []
        if known is not None or not scenarios:
            shared.extend(self.seats(known, None))
        if em.ai_cells:
            fin = Finality.FINAL if self.closed else Finality.PROVISIONAL
            gross = Figure(nano=em.gross, evidence=Evidence.EXACT, basis=Basis.LIST_EQUIVALENT,
                           finality=fin, note="credit valuation of usage (list-equivalent), not "
                                              "invoice dollars")
            self.line("ai_credits.gross", gross, quantity=_dec_str(em.credits),
                      unit="ai-credits")
        self._discounts(pools, scenarios)
        open_note = "observed so far (provisional); the month-end forecast is in the pool bar"
        if known is not None:
            shared.append(self.line(
                "ai_credits.overage", r16_figure(known.overage_observed_nano, closed=self.closed,
                                           final=True, reconciled=credits_ok,
                                           open_note=open_note),
                quantity=nano_to_credits_str(known.overage_observed_nano), unit="ai-credits"))
        elif scenarios and em.ai_cells:
            fig = with_note(r16_figure(em.pooled_net, closed=self.closed, final=True,
                                  reconciled=credits_ok, open_note=open_note),
                             "observed net of pooled report rows (GitHub's per-row net); the "
                             "plan-dependent overage is shown per scenario")
            self.line("ai_credits.overage", fig, quantity=nano_to_credits_str(em.pooled_net),
                      unit="ai-credits")
        if em.direct_cells:
            direct = pools[0].direct_net_nano if pools else em.direct_net
            shared.append(self.line(
                "ai_credits.direct_org", r16_figure(direct, closed=self.closed, final=True,
                                              reconciled=credits_ok, open_note=open_note),
                quantity=_dec_str(em.direct_credits), unit="ai-credits"))
        if em.cq_credits.n:
            self._data_line("code_quality.ai_credits", em.cq_credits, _CREDITS,
                            note="breakout: already inside ai_credits.overage / direct_org; not "
                                 "added to the total")
        for name in ("code_quality.licenses", "actions.code_review", "actions.cloud_agent",
                     "actions.agentic_workflow", "sandbox"):
            acc = em.adjacent.get(name)
            if acc is not None:
                shared.append(self._data_line(name, acc, em.channel_of.get(name, _CREDITS)))
        if not scenarios:
            self._total(shared, None)
            return self.out
        for pm in scenarios:
            s = pm.plan_scenario
            own = self.seats(pm, s)
            fig = estimated(pm.overage_observed_nano, Basis.LIST,
                            note=f"plan unknown: scenario {s}: consumption − the scenario's pool "
                                 f"at $0.01 per credit"
                                 + ("; observed so far" if not self.closed else ""))
            own.append(self.line("ai_credits.overage", fig,
                                 quantity=nano_to_credits_str(pm.overage_observed_nano),
                                 unit="ai-credits", scenario=s))
            self._total(own + [bl for bl in shared if bl.line != "ai_credits.overage"], s)
        return self.out

    def _discounts(self, pools: Sequence[PoolMonth], scenarios: Sequence[PoolMonth]) -> None:
        triples = {(p.pool_draw_nano, p.discount_other_nano, p.discount_unclassified_nano)
                   for p in pools}
        per_scenario = len(triples) > 1
        for pm in (scenarios if per_scenario else pools[:1]):
            s = pm.plan_scenario if per_scenario else None
            if pm.pool_draw_nano is not None:
                self.line("ai_credits.discount_pool",
                          exact(pm.pool_draw_nano, Basis.LIST_EQUIVALENT),
                          quantity=nano_to_credits_str(pm.pool_draw_nano), unit="ai-credits",
                          scenario=s)
            if pm.discount_other_nano:
                self.line("ai_credits.discount_other",
                          with_note(exact(pm.discount_other_nano, Basis.LIST),
                                     "classified non-pool discount (e.g. Auto model selection)"),
                          quantity=nano_to_credits_str(pm.discount_other_nano),
                          unit="ai-credits", scenario=s)
            if pm.discount_unclassified_nano:
                self.line("ai_credits.discount_unclassified",
                          with_note(exact(pm.discount_unclassified_nano, Basis.LIST),
                                     "includes included usage and other discounts"),
                          quantity=nano_to_credits_str(pm.discount_unclassified_nano),
                          unit="ai-credits", scenario=s)

    def _total(self, parts: Sequence[CopilotBillLine], scenario: str | None) -> None:
        parts = [p for p in parts if p.line in DOLLAR_LINES]
        if not parts:
            return
        names = [p.line for p in parts]
        weak = [f"{p.line} ({p.amount.evidence.value} {p.amount.basis.value})" for p in parts
                if p.amount.basis is not Basis.INVOICE]
        note = "total of " + ", ".join(names)
        if weak:
            note += "; non-invoice components: " + ", ".join(weak)
        if scenario is not None:
            note += (f"; plan unknown: scenario {scenario}, never combined with the other "
                     "scenario")
        fig = combine_weakest([p.amount for p in parts], note=note)
        # the core note also names non-invoice inputs by position; the line names say it better
        kept = [p for p in fig.note.split("; ") if not p.startswith("non-invoice: input ")]
        fig = _renote(fig, "; ".join(kept))
        self.line("total.invoice", fig, scenario=scenario, components=names)


def _renote(fig: Figure, note: str) -> Figure:
    return Figure(nano=fig.nano, evidence=fig.evidence, basis=fig.basis, finality=fig.finality,
                  low_nano=fig.low_nano, high_nano=fig.high_nano, ci_level_pct=fig.ci_level_pct,
                  calibration=fig.calibration, upper_bound=fig.upper_bound,
                  provenance=fig.provenance, note=note)


def with_note(fig: Figure, note: str) -> Figure:
    """*fig* with *note* appended to its note."""
    return _renote(fig, note if not fig.note else f"{fig.note}; {note}")


def bill_lines(cost_lines: Sequence[CostLine], aggregates: Sequence[UsageAggregate],
               pools: Sequence[PoolMonth], verdicts: Mapping[str, str],
               notes: list[str] | None = None) -> list[CopilotBillLine]:
    """The addendum §14.1 bill lines of every entity × month (see the module docstring), sorted
    by entity, month, scenario (shared lines first) and ``BILL_LINES`` order."""
    capped = capped_of(pools)
    mode = entity_mode_of(pools)
    collected = _collect(cost_lines, aggregates, capped, mode, notes if notes is not None else [])
    by_key: dict[tuple[str, str], list[PoolMonth]] = defaultdict(list)
    for pm in pools:
        by_key[(pm.entity_id, pm.month)].append(pm)
    out: list[CopilotBillLine] = []
    for key in sorted(set(by_key) | set(collected)):
        em = collected.get(key, _EntityMonth())
        out.extend(_Bill(key[0], key[1], em, by_key.get(key, []), verdicts).build())
    return out


# ---------------------------------------------------------------------------------------------
# k-anonymous team figures
# ---------------------------------------------------------------------------------------------


def _priced(net: Figure, pool: Figure | None) -> PricedTotal:
    return PricedTotal(exact=net, estimated=None, allowance=None, priced_inferences=0,
                       unpriced_inferences=0, unpriced_tokens=0, coverage="1", pool=pool)


def _small(people: Mapping[object, set[str]], k: int) -> set[object]:
    """Groups with fewer than *k* distinct people: merged into one small-teams group before
    ``publish``, so one small team never forces the complementary suppression of a large one."""
    return {key for key, members in people.items() if len(members) < k}


def _gross(line: CostLine) -> int:
    return line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano


def _teams(cost_lines: Sequence[CostLine], capped: Mapping[str, Decimal], mode: str,
           window: tuple[int, int], k: int) -> PublishedAggregate | None:
    keyed = []
    people: dict[object, set[str]] = defaultdict(set)
    for line in cost_lines:
        if line.channel != _CREDITS or line.cost_type not in POOLED_COST_TYPES:
            continue
        em = (entity_of(line.cost_center, line.workspace_id, capped=capped, entity_mode=mode),
              line.date_utc[:7], line.team)
        keyed.append((em, line))
        if line.principal is not None:
            people[em].add(line.principal)
        else:
            people.setdefault(em, set())
    if not keyed:
        return None
    small = _small(people, k)
    acc: dict[tuple[str, str, str | None, str], list] = {}
    for (e, m, t), line in keyed:
        team = other_label(k) if (e, m, t) in small else t
        a = acc.setdefault((e, m, team, line.workload or INTERACTIVE), [set(), 0, 0, 0])
        if line.principal is not None:
            a[0].add(line.principal)
        a[1] += _gross(line)
        a[2] += line.amount_nano
        a[3] += 1
    rows = tuple(
        AggRow(dims=(("entity", e), ("month", m), ("team", t), ("workload", w)),
               n_users=len(users), n_requests=n, usage=UsageBuckets(),
               priced=_priced(exact(net, Basis.LIST), exact(gross, Basis.LIST_EQUIVALENT)))
        for (e, m, t, w), (users, gross, net, n) in sorted(
            acc.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "", kv[0][3])))
    return publish(RawAggregate(group_by=TEAM_GROUP_BY, rows=rows, window=window), k=k)


def _seat_counts(licenses: Sequence[LicenseSnapshot], window: tuple[int, int],
                 k: int) -> tuple[tuple[str, str, int], ...]:
    latest: dict[tuple[str, str | None], LicenseSnapshot] = {}
    for lic in licenses:
        key = (lic.principal, lic.org)
        cur = latest.get(key)
        if cur is None or (lic.snapshot_date, lic.fetched_ms) > (cur.snapshot_date,
                                                                   cur.fetched_ms):
            latest[key] = lic
    if not latest:
        return ()
    people: dict[object, set[str]] = defaultdict(set)
    for lic in latest.values():
        people[lic.team].add(lic.principal)
    small = _small(people, k)
    counts: dict[tuple[str, str | None], int] = defaultdict(int)
    for lic in latest.values():
        team = other_label(k) if lic.team in small else lic.team
        counts[(f"{lic.plan}:{lic.last_activity_bucket}", team)] += 1
    rows = tuple(AggRow(dims=(("plan_bucket", pb), ("team", t)), n_users=n, n_requests=n,
                        usage=UsageBuckets(), priced=_priced(exact(0, Basis.LIST), None))
                 for (pb, t), n in sorted(counts.items(), key=lambda kv: (kv[0][0],
                                                                          kv[0][1] or "")))
    pub = publish(RawAggregate(group_by=SEAT_GROUP_BY, rows=rows, window=window), k=k)
    out = []
    for row in pub.rows:
        dims = dict(row.dims)
        pb, team = dims["plan_bucket"], dims["team"]
        merged_pb = pb is not None and pb.startswith("(other")
        merged_team = team is not None and team.startswith("(other")
        out.append((_OTHER_TEAM if merged_team else (team or _UNATTRIBUTED),
                    "(other)" if merged_pb else str(pb), row.n_requests))
    return tuple(sorted(out, key=lambda r: (r[0] == _OTHER_TEAM, r[0], r[1])))


def _family(key: str) -> str:
    fam = catalog.editor_family(key)
    return fam if fam in ("vscode", "jetbrains") else "other"


def _editor_split(activity: Sequence[ActivityDay], cost_lines: Sequence[CostLine],
                  window: tuple[int, int], k: int) -> PublishedAggregate | None:
    people: dict[object, set[str]] = defaultdict(set)
    for day in activity:
        if any(key.startswith("ide:") and n > 0 for key, n in day.counts):
            people[day.team].add(day.principal)
    if not people:
        return None
    small = _small(people, k)

    def label_of(team: str | None) -> str | None:
        return other_label(k) if team in small else team

    inter: dict[tuple[str | None, str], int] = defaultdict(int)
    users: dict[tuple[str | None, str], set[str]] = defaultdict(set)
    for day in activity:
        for key, n in day.counts:
            if key.startswith("ide:") and n > 0:
                cell = (label_of(day.team), _family(key))
                inter[cell] += n
                users[cell].add(day.principal)
    credits: dict[str | None, int] = defaultdict(int)
    for line in cost_lines:
        if line.channel == _CREDITS and line.cost_type in POOLED_COST_TYPES:
            credits[label_of(line.team)] += _gross(line)
    rows: list[AggRow] = []
    for team in sorted({t for t, _ in inter}, key=lambda t: t or ""):
        fams = [f for f in EDITOR_SPLIT_FAMILIES if (team, f) in inter]
        parts = apportion(credits.get(team, 0), [inter[(team, f)] for f in fams])
        for fam, nano in zip(fams, parts, strict=True):
            rows.append(AggRow(dims=(("team", team), ("editor_family", fam)),
                               n_users=len(users[(team, fam)]), n_requests=inter[(team, fam)],
                               usage=UsageBuckets(),
                               priced=_priced(exact(0, Basis.LIST),
                                              estimated(nano, Basis.LIST_EQUIVALENT,
                                                        note=_EDITOR_NOTE))))
    return publish(RawAggregate(group_by=EDITOR_GROUP_BY, rows=tuple(rows), window=window), k=k)


# ---------------------------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------------------------


def _plans(plans_by_scenario: object) -> tuple[ActionPlan | None, dict[str, ActionPlan]]:
    if plans_by_scenario is None:
        return None, {}
    items = (plans_by_scenario.items() if isinstance(plans_by_scenario, Mapping)
             else plans_by_scenario)
    known: ActionPlan | None = None
    scen: dict[str, ActionPlan] = {}
    for item in items:  # type: ignore[union-attr]
        if not (isinstance(item, (tuple, list)) and len(item) == 2
                and isinstance(item[1], ActionPlan)):
            raise UsageError("plans_by_scenario: expected (scenario, ActionPlan) pairs")
        key, plan = item
        if key in SCENARIOS:
            scen[key] = plan
        elif key in (KNOWN, None):
            known = plan
        else:
            raise UsageError("plans_by_scenario: keys are known | business | enterprise")
    return known, scen


def _plan_status(plans: Iterable[PlanEvidence]) -> tuple[PlanEvidence, ...]:
    latest: dict[str, PlanEvidence] = {}
    for pe in plans:
        if not isinstance(pe, PlanEvidence):
            raise UsageError("plans: expected PlanEvidence records")
        cur = latest.get(pe.entity_id)
        if cur is None or pe.month > cur.month:
            latest[pe.entity_id] = pe
    return tuple(latest[e] for e in sorted(latest))


def _action_key(a: AdminAction) -> tuple:
    return (a.deadline is None, a.deadline or "", a.action_id)


def assemble_summary(*, cost_lines: Iterable[CostLine], aggregates: Iterable[UsageAggregate],
                     licenses: Iterable[LicenseSnapshot], activity: Iterable[ActivityDay],
                     pools: Iterable[PoolMonth], plans: Iterable[PlanEvidence] = (),
                     plans_by_scenario: object = (), actions: Iterable[AdminAction] = (),
                     channel_verdicts: object = (), window: tuple[str, str],
                     k: int = 5, pricer: object = None) -> CopilotSummary:
    """The ``RunResult.copilot`` summary (addendum §14; see the module docstring).

    *plans* are the ``PlanEvidence`` records (``plan_status`` keeps the latest month per entity);
    *plans_by_scenario* is CP-PLAN's ``plan_copilot_scenarios`` result (``(("known", plan),)`` or
    one plan per scenario; a mapping works too); *channel_verdicts* is channel → verdict (pairs, a
    mapping or ``ChannelVerdict`` objects). While any pool carries a ``plan_scenario`` the summary's
    ``plan`` is None and ``plans_by_scenario`` holds one plan per scenario. *pricer* is accepted for
    the hook signature and unused: every amount is GitHub's own line or a ``core.pool`` figure.
    """
    del pricer
    if type(k) is not int or k < 1:
        raise UsageError("k must be an int >= 1")
    lines = [c for c in cost_lines if isinstance(c, CostLine)]
    aggs = [a for a in aggregates if isinstance(a, UsageAggregate)]
    lics = [x for x in licenses if isinstance(x, LicenseSnapshot)]
    acts = [x for x in activity if isinstance(x, ActivityDay)]
    pool_list = list(pools)
    if not all(isinstance(p, PoolMonth) for p in pool_list):
        raise UsageError("pools: expected PoolMonth records")
    pool_list.sort(key=lambda p: (p.entity_id, p.month, p.plan_scenario or ""))
    verdicts = _verdict_pairs(channel_verdicts)
    notes: list[str] = []
    bill = bill_lines(lines, aggs, pool_list, dict(verdicts), notes)
    known, scen = _plans(plans_by_scenario)
    unknown = any(p.plan_scenario is not None for p in pool_list)
    if unknown:
        notes.append("plan unknown for at least one entity: pool-dependent figures are shown as "
                     "Business and as Enterprise, each ESTIMATED, never combined")
    wms = _window_ms(window)
    capped, mode = capped_of(pool_list), entity_mode_of(pool_list)
    return CopilotSummary(
        window=(str(window[0]), str(window[1])),
        lines=tuple(bill),
        pools=tuple(pool_list),
        teams=_teams(lines, capped, mode, wms, k),
        seat_counts=_seat_counts(lics, wms, k),
        plan=None if unknown else known,
        actions=tuple(sorted(actions, key=_action_key)),
        channel_verdicts=verdicts,
        notes=tuple(notes),
        plan_status=_plan_status(plans),
        plans_by_scenario=tuple(sorted(scen.items())) if unknown else (),
        editor_split=_editor_split(acts, lines, wms, k),
    )
