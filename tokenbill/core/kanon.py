"""k-anonymity: the only implementation (SPEC §3.23, §8.4, §8.5; ruling R-E1).

* :func:`publish` turns a store :class:`~tokenbill.core.types.RawAggregate` into the only aggregate
  type renderers accept. Rows with fewer than *k* users merge into an ``"(other: <k users)"`` row
  inside their parent group (the group-by prefix without the last dimension); when that row is still
  below *k* it absorbs the smallest remaining row of the parent (complementary suppression), so no
  suppressed cell is recoverable by subtracting published rows from a published total. A parent
  group with no row of *k* or more users is resolved one level up; what cannot reach *k* even at the
  top is withheld (counted in ``suppressed_rows``/``suppressed_users``). Published totals equal raw
  totals whenever the whole table reaches *k*.
* A merged row's ``n_users`` is the **largest constituent count** — the only exact lower bound on
  the distinct users of a union when rows may share people (a user appears in several model or day
  rows). This mirrors :func:`rescope_findings` without ``count_users`` and can only suppress more.
* :func:`rescope_findings` re-scopes org findings below *k* to their parent scope, with exact
  distinct counts from the store (``count_users``) when given. Data-quality findings (kinds ``dq.*``
  and category ``data-quality``, e.g. the missing-capabilities finding) carry no personal data and
  are exempt (R-E1); so are provider-side ``aggregate`` findings whose scope names no person or API
  key (SPEC §8.5). A re-scoped finding's generated text (title, summary, fix text, evidence
  attributes, ``validated_against``) has the values of the dropped scope dimensions replaced by
  ``"(other)"``, so an org-level finding never names the small team it was merged away from.
* :func:`require_self_or_aggregate` refuses grouping by person without the self view;
  :func:`merge_small_groups` is the ingest-time team aggregation of SPEC §5.11.

GitHub Copilot (CORE-AMENDMENTS K-4; rulings R-E10, R-E16, R-E31, R-E37):

* :func:`publish` takes ``audience`` (``"org"`` default, ``"self"`` never suppresses) and keeps rows
  whose user count is unknown (``n_users == 0``: a source without a principal dimension) when the
  grouping has no person or person-proxy key (:data:`PERSON_PROXY_DIMS`); such rows carry the note
  :data:`USERS_UNKNOWN` (:func:`row_notes`).
* Findings whose scope has a ``product`` dim re-scope on the Copilot parent chain
  (:data:`COPILOT_RESCOPE_LEVELS`: team → bucket → plan → model → cost center → entity root; the
  root dims :data:`COPILOT_ROOT_DIMS` — ``product``, ``entity``, ``org``, ``plan_scenario`` — are
  never removed) and are exempt from k only when their count source
  (``core.catalog.COUNT_SOURCE``) is ``entity`` and their scope is entity-level (R-E16) — category
  ``aggregate`` does not exempt them. :func:`scope_counter` counts people per scope over the ledger
  or the extension record stores; :func:`rescope_findings` accepts it (two-argument
  ``count_users``). Merged findings sum ``headroom``; merged summaries are never cut mid-sentence
  (R-E31); merged priced totals keep ``PricedTotal.pool`` (R-E37).
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypeVar

from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.labels import Figure, add
from tokenbill.core.money import ratio
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import (
    _PUBLISH_TOKEN,
    AggRow,
    EvidenceItem,
    Finding,
    PricedTotal,
    PublishedAggregate,
    RawAggregate,
    Scope,
)

if TYPE_CHECKING:
    from tokenbill.core.protocols import ExtRecordStore, LedgerStore

__all__ = [
    "AUDIENCES",
    "COPILOT_RESCOPE_LEVELS",
    "COPILOT_ROOT_DIMS",
    "ENTITY_EXEMPT_DIMS",
    "PERSON_DIMS",
    "PERSON_PROXY_DIMS",
    "RESCOPE_LEVELS",
    "SCRUBBED",
    "USERS_UNKNOWN",
    "merge_small_groups",
    "other_label",
    "publish",
    "require_self_or_aggregate",
    "rescope_findings",
    "row_notes",
    "scope_counter",
]

T = TypeVar("T")

#: Dimensions that name a person (or a person's session); never grouped or listed for the org.
PERSON_DIMS = frozenset({"principal", "session", "session_key"})
#: Scope dimensions kept at each re-scoping level, finest first: team × lane kind → team →
#: cost center → org. ``billing_class`` is kept everywhere so allowance (list-equivalent) and billed
#: figures are never added (R10).
RESCOPE_LEVELS: tuple[frozenset[str], ...] = (
    frozenset({"team", "cost_center", "lane_kind", "billing_class"}),
    frozenset({"team", "cost_center", "billing_class"}),
    frozenset({"cost_center", "billing_class"}),
    frozenset({"billing_class"}),
)
_KEY_DIMS = frozenset({"api_key_id", "api_key"})
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

#: Grouping keys that name a person or stand in for one (ruling R-E10): with one of them in the
#: grouping, a row whose user count is unknown stays suppressed.
PERSON_PROXY_DIMS = frozenset({"principal", "session", "session_key", "api_key_id", "api_key",
                               "cwd_key"})
#: Note of published rows whose user count is unknown (``n_users == 0``, R-E10).
USERS_UNKNOWN = "users_unknown"
#: ``publish`` audiences: ``org`` (k-anonymous) and ``self`` (one principal's own data).
AUDIENCES = ("org", "self")

#: Scope dims of the Copilot entity root: never removed by re-scoping (so scenario findings are
#: never merged across scenarios; ``billing_class`` is kept too so pool and billed figures never
#: add).
COPILOT_ROOT_DIMS = frozenset({"product", "entity", "org", "plan_scenario", "billing_class"})
#: Copilot parent chain for scopes with a ``product`` dim (K-4): first every dim outside the chain
#: is dropped, then ``team``, ``bucket``, ``plan``, ``model`` and ``cost_center`` in that order; the
#: last level is the entity root.
COPILOT_RESCOPE_LEVELS: tuple[frozenset[str], ...] = (
    COPILOT_ROOT_DIMS | {"team", "bucket", "plan", "model", "cost_center"},
    COPILOT_ROOT_DIMS | {"bucket", "plan", "model", "cost_center"},
    COPILOT_ROOT_DIMS | {"plan", "model", "cost_center"},
    COPILOT_ROOT_DIMS | {"model", "cost_center"},
    COPILOT_ROOT_DIMS | {"cost_center"},
    COPILOT_ROOT_DIMS,
)
#: R-E16: a ``product``-scoped finding with count source ``entity`` is exempt from k iff its scope
#: dims are within this set (``billing_class`` names no people and is allowed as well).
ENTITY_EXEMPT_DIMS = frozenset({"product", "entity", "org", "model", "sku", "plan_scenario",
                                "billing_class"})
_P_RE = re.compile(r"p_[0-9a-f]{20}\Z")


def other_label(k: int) -> str:
    """The label of merged small groups: ``"(other: <5 users)"`` for ``k = 5``."""
    return f"(other: <{k} users)"


def _check_k(k: object) -> int:
    if type(k) is not int or k < 1:
        raise UsageError("k-anonymity threshold k must be an int >= 1")
    return k


# ---------------------------------------------------------------------------------------------
# value arithmetic for merged rows
# ---------------------------------------------------------------------------------------------


def _add_usage(a: UsageBuckets, b: UsageBuckets) -> UsageBuckets:
    """``a + b``; two different other-write TTLs keep the shorter one instead of raising (a
    published row mixing providers keeps the token total; the TTL is informational)."""
    ta, tb = a.cache_write_other_ttl_s, b.cache_write_other_ttl_s
    if ta is not None and tb is not None and ta != tb:
        ttl = min(ta, tb)
        a = dataclasses.replace(a, cache_write_other_ttl_s=ttl)
        b = dataclasses.replace(b, cache_write_other_ttl_s=ttl)
    return a + b


def _add_opt(a: Figure | None, b: Figure | None) -> Figure | None:
    if a is None:
        return b
    if b is None:
        return a
    return add(a, b)


def _tokens(usage: UsageBuckets) -> int:
    return usage.total_input + usage.output


def _coverage(priced_tokens: int, all_tokens: int) -> str:
    if all_tokens <= 0:
        return "1"
    value = ratio(max(priced_tokens, 0), all_tokens)
    assert value is not None
    text = format(value.normalize(), "f")
    return text


def _add_priced(a: PricedTotal, b: PricedTotal, usage_a: UsageBuckets,
                usage_b: UsageBuckets) -> PricedTotal:
    all_tokens = _tokens(usage_a) + _tokens(usage_b)
    unpriced = a.unpriced_tokens + b.unpriced_tokens
    return PricedTotal(
        exact=add(a.exact, b.exact),
        estimated=_add_opt(a.estimated, b.estimated),
        allowance=_add_opt(a.allowance, b.allowance),
        priced_inferences=a.priced_inferences + b.priced_inferences,
        unpriced_inferences=a.unpriced_inferences + b.unpriced_inferences,
        unpriced_tokens=unpriced,
        coverage=_coverage(all_tokens - unpriced, all_tokens),
        pool=_add_opt(a.pool, b.pool),  # R-E37: Copilot pooled credits are carried, never dropped
    )


@dataclasses.dataclass
class _Cell:
    dims: tuple[tuple[str, str | None], ...]
    n_users: int
    n_requests: int
    usage: UsageBuckets
    priced: PricedTotal
    members: tuple[int, ...]     # indexes of the raw rows inside this cell

    def row(self) -> AggRow:
        return AggRow(dims=self.dims, n_users=self.n_users, n_requests=self.n_requests,
                      usage=self.usage, priced=self.priced)


def _merge_dims(cells: Sequence[_Cell], label: str) -> tuple[tuple[str, str | None], ...]:
    first = cells[0].dims
    out = []
    for i, (name, value) in enumerate(first):
        same = all(len(c.dims) > i and c.dims[i] == (name, value) for c in cells)
        out.append((name, value if same else label))
    return tuple(out)


def _merge_cells(cells: Sequence[_Cell], label: str) -> _Cell:
    usage = cells[0].usage
    priced = cells[0].priced
    for c in cells[1:]:
        priced = _add_priced(priced, c.priced, usage, c.usage)
        usage = _add_usage(usage, c.usage)
    return _Cell(
        dims=_merge_dims(cells, label),
        n_users=max(c.n_users for c in cells),  # lower bound on distinct users of the union
        n_requests=sum(c.n_requests for c in cells),
        usage=usage,
        priced=priced,
        members=tuple(sorted(m for c in cells for m in c.members)),
    )


def _dims_key(dims: tuple[tuple[str, str | None], ...], label: str) -> tuple:
    return tuple(
        (2 if v == label else 1 if v is None else 0, v or "", name) for name, v in dims
    )


def _value_key(c: _Cell) -> tuple:
    exact = c.priced.exact.nano if c.priced.exact.nano is not None else -1
    return (c.n_users, exact, c.n_requests, _tokens(c.usage))


def _prefix(n: int) -> Callable[[tuple], tuple]:
    return lambda dims: tuple(dims[:n])


def _users_unknown_ok(group_by: Sequence[str]) -> bool:
    return not (set(group_by) & PERSON_PROXY_DIMS)


def row_notes(row: AggRow, *, group_by: Sequence[str] = ()) -> tuple[str, ...]:
    """Notes of a published row: ``(USERS_UNKNOWN,)`` when its user count is unknown
    (``n_users == 0`` and no person-proxy key in *group_by*, R-E10), else ``()``. Renderers print
    "users unknown" for such rows instead of a count."""
    if row.n_users == 0 and _users_unknown_ok(group_by):
        return (USERS_UNKNOWN,)
    return ()


def publish(raw: RawAggregate, *, k: int = 5,
            parent_of: Callable[[tuple], tuple] | None = None,
            audience: str = "org") -> PublishedAggregate:
    """Publish *raw* with k-anonymity and complementary suppression (SPEC §8.4).

    *parent_of* maps a row's ``dims`` to its parent key (default: the dims without the last
    dimension). Rows below *k* merge into ``other_label(k)`` rows; see the module docstring for the
    full rule. Output rows are sorted by dims (other rows last within their parent).

    R-E10: rows whose user count is unknown (``n_users == 0``: the source has no principal
    dimension, e.g. provider-side aggregates) are published unchanged — note
    :data:`USERS_UNKNOWN`, see :func:`row_notes` — when the grouping keys contain no person or
    person-proxy dimension (:data:`PERSON_PROXY_DIMS`); with such a key they are suppressed like any
    small row. ``audience="self"`` (a single principal's own data, ``--self``) never suppresses.
    """
    if not isinstance(raw, RawAggregate):
        raise ContractViolation("publish expects a RawAggregate")
    k = _check_k(k)
    if audience not in AUDIENCES:
        raise UsageError(f"publish audience must be one of {', '.join(AUDIENCES)}")
    if audience == "self":
        return PublishedAggregate(group_by=tuple(raw.group_by), rows=tuple(raw.rows),
                                  window=raw.window, k=k, suppressed_rows=0, suppressed_users=0,
                                  token=_PUBLISH_TOKEN)
    label = other_label(k)
    unknown_ok = _users_unknown_ok(raw.group_by)
    kept_unknown = [
        _Cell(dims=tuple(r.dims), n_users=r.n_users, n_requests=r.n_requests, usage=r.usage,
              priced=r.priced, members=(i,))
        for i, r in enumerate(raw.rows) if unknown_ok and r.n_users == 0
    ]
    cells = [
        _Cell(dims=tuple(r.dims), n_users=r.n_users, n_requests=r.n_requests, usage=r.usage,
              priced=r.priced, members=(i,))
        for i, r in enumerate(raw.rows) if not (unknown_ok and r.n_users == 0)
    ]
    depth = max((len(c.dims) for c in cells), default=len(raw.group_by))
    levels: list[Callable[[tuple], tuple]]
    if parent_of is not None:
        levels = [parent_of, _prefix(0)]
    else:
        levels = [_prefix(n) for n in range(depth - 1, -1, -1)]
    for parent in levels:
        if all(c.n_users >= k for c in cells):
            break
        groups: dict[tuple, list[_Cell]] = {}
        for c in cells:
            groups.setdefault(tuple(parent(c.dims)), []).append(c)
        next_cells: list[_Cell] = []
        for key in sorted(groups, key=lambda g: repr(g)):
            group = sorted(groups[key], key=lambda c: _dims_key(c.dims, label))
            small = [c for c in group if c.n_users < k]
            big = [c for c in group if c.n_users >= k]
            if not small:
                next_cells.extend(group)
                continue
            merged = _merge_cells(small, label)
            if merged.n_users < k and big:
                complement = min(big, key=lambda c: (_value_key(c), _dims_key(c.dims, label)))
                big = [c for c in big if c is not complement]
                merged = _merge_cells([merged, complement], label)
            next_cells.extend(big)
            next_cells.append(merged)
        cells = next_cells
    published = [c for c in cells if c.n_users >= k] + kept_unknown
    published.sort(key=lambda c: _dims_key(c.dims, label))
    shown = {c.members[0] for c in published if len(c.members) == 1}
    suppressed = [i for i in range(len(raw.rows)) if i not in shown]
    return PublishedAggregate(
        group_by=tuple(raw.group_by),
        rows=tuple(c.row() for c in published),
        window=raw.window,
        k=k,
        suppressed_rows=len(suppressed),
        suppressed_users=sum(raw.rows[i].n_users for i in suppressed),
        token=_PUBLISH_TOKEN,
    )


# ---------------------------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------------------------


def _count_source(f: Finding) -> str:
    """``core.catalog.COUNT_SOURCE`` of *f* (imported lazily; ``"requests"`` when unavailable)."""
    try:
        from tokenbill.core import catalog
    except ImportError:  # pragma: no cover - catalog is part of the core
        return "requests"
    fn = getattr(catalog, "count_source", None)
    return fn(f.detector_id, f.kind) if fn is not None else "requests"


def _is_product_scoped(f: Finding) -> bool:
    return any(name == "product" for name, _ in f.scope.dims)


def _exempt(f: Finding) -> bool:
    """R-E1: data-quality findings carry no personal data. Provider-side aggregate findings whose
    scope names no person and no API key describe workspaces/models, not people (§8.5, R-E9).
    R-E16: a finding whose scope has a ``product`` dim is exempt iff its count source is
    ``entity`` and its scope dims are within :data:`ENTITY_EXEMPT_DIMS` — its category never
    exempts it."""
    if f.category == "data-quality" or f.kind.startswith("dq."):
        return True
    names = {name for name, _ in f.scope.dims}
    if "product" in names:
        return _count_source(f) == "entity" and names <= ENTITY_EXEMPT_DIMS
    return f.category == "aggregate" and not (names & (PERSON_DIMS | _KEY_DIMS))


def _check_budget_privacy(f: Finding) -> None:
    """R-E16 / R14: per-user budget facts are published only as counts at team or entity level; a
    ``budget-*`` finding carrying a person dim or a ``p_`` value is a privacy defect."""
    if not f.kind.startswith("budget-"):
        return
    for name, value in f.scope.dims:
        if name in PERSON_DIMS or (isinstance(value, str) and _P_RE.match(value)):
            raise PrivacyError(f"{f.detector_id}/{f.kind}: budget findings never name a person")


def _needs_rescope(f: Finding, k: int) -> bool:
    names = {name for name, _ in f.scope.dims}
    return f.n_users < k or bool(names & PERSON_DIMS)


def _finding_id(detector_id: str, kind: str, scope: Scope) -> str:
    from tokenbill.core.registry import _finding_id as registry_finding_id

    return registry_finding_id(detector_id, kind, scope)


def _parent_scope(scope: Scope, keep: frozenset[str]) -> Scope:
    return Scope(dims=tuple(sorted((n, v) for n, v in scope.dims if n in keep)))


def _aggregate_parent(scope: Scope) -> Scope:
    return Scope(dims=tuple(sorted((n, v) for n, v in scope.dims
                                   if n not in PERSON_DIMS and n not in _KEY_DIMS)))


def _sum_opt(figs: Iterable[Figure | None]) -> Figure | None:
    out: Figure | None = None
    for f in figs:
        out = _add_opt(out, f)
    return out


#: What the values of scope dimensions dropped by re-scoping are replaced with in generated text.
SCRUBBED = "(other)"


def _scrubber(children: Sequence[Finding], scope: Scope) -> Callable[[str], str]:
    """A function removing, from generated text, the values of the scope dimensions that
    re-scoping dropped (a title "… in team mobile …" must not name the small child scope it was
    merged away from). Whole tokens only; values still present in *scope* are kept."""
    kept = {v for _, v in scope.dims}
    dropped = sorted({v for c in children for n, v in c.scope.dims
                      if (n, v) not in scope.dims and v and v not in kept},
                     key=lambda v: (-len(v), v))
    if not dropped:
        return lambda text: text
    pattern = re.compile(r"(?<![\w.\-])(?:" + "|".join(re.escape(v) for v in dropped)
                         + r")(?![\w\-]|\.\w)")
    return lambda text: pattern.sub(SCRUBBED, text)


def _scrub_evidence(item: EvidenceItem, scrub: Callable[[str], str]) -> EvidenceItem:
    attrs = tuple((name, scrub(value) if isinstance(value, str) else value)
                  for name, value in item.attrs)
    return item if attrs == item.attrs else dataclasses.replace(item, attrs=attrs)


#: Phrases of labelling statements that a merged summary must keep (R-E31).
_LABEL_MARKERS = ("list-equivalent", "not invoice", "estimated", "upper bound", "unpriced",
                  "provider estimate", "no mechanical fix", "if business", "if enterprise",
                  "scenario")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+")
_SUMMARY_MAX = 400


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT_RE.split(text.strip()) if part]


def _cut_words(text: str, limit: int) -> str:
    """*text* within *limit* chars, cut at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:max(limit - 1, 0)]
    if " " in head:
        head = head[:head.rindex(" ")]
    return head.rstrip(" ,;:") + "…"


_SHORT_PREFIXES = ("[re-scoped for k-anonymity] ", "[re-scoped] ")


def _fit_summary(prefix: str, summary: str, limit: int = _SUMMARY_MAX) -> str:
    """``prefix + summary`` within *limit* chars without cutting mid-summary (R-E31). The full
    summary is kept whenever it fits beside the re-scoping prefix (shortened if need be); otherwise
    whole sentences are dropped from the end — labelling statements (:data:`_LABEL_MARKERS`, e.g.
    the D26 "list-equivalent, not invoice dollars" sentence) never — and the text ends with "…";
    only when the labelling statements alone do not fit is the text cut at a word boundary."""
    prefixes = (prefix, *_SHORT_PREFIXES)
    for pre in prefixes:
        if len(pre) + len(summary) <= limit:
            return pre + summary
    pre = prefixes[-1]
    budget = limit - len(pre)
    parts = _sentences(summary)
    chosen = [any(m in part.lower() for m in _LABEL_MARKERS) for part in parts]

    def text(flags: list[bool]) -> str:
        body = " ".join(p for p, keep in zip(parts, flags, strict=True) if keep)
        return f"{body} …".strip() if not all(flags) else body

    if len(text(chosen)) > budget:
        return pre + _cut_words(text(chosen) or summary, budget)
    for i in range(len(parts)):
        if not chosen[i]:
            trial = [*chosen[:i], True, *chosen[i + 1:]]
            if len(text(trial)) <= budget:
                chosen = trial
    return pre + text(chosen)


def _merge_findings(children: Sequence[Finding], scope: Scope, n_users: int, k: int) -> Finding:
    first = children[0]
    scrub = _scrubber(children, scope)
    cost = children[0].cost_observed
    for c in children[1:]:
        cost = add(cost, c.cost_observed)
    evidence: list = []
    seen: set = set()
    for c in children:
        for item in c.evidence:
            item = _scrub_evidence(item, scrub)
            if item not in seen:
                seen.add(item)
                evidence.append(item)
    validated = {c.validated_against for c in children}
    fix = next((c.fix for c in children if c.fix is not None), None)
    if fix is not None:
        fix = dataclasses.replace(fix, text=scrub(fix.text))
    summary = scrub(first.summary)
    if len(children) > 1 or scope != first.scope:
        prefix = f"[re-scoped for k-anonymity (k={k}); {len(children)} finding(s) merged] "
        summary = _fit_summary(prefix, summary)
    only = validated.pop() if len(validated) == 1 else None
    return dataclasses.replace(
        first,
        finding_id=_finding_id(first.detector_id, first.kind, scope),
        scope=scope,
        n_events=sum(c.n_events for c in children),
        n_lanes=sum(c.n_lanes for c in children),
        n_users=n_users,
        first_seen_ms=min(c.first_seen_ms for c in children),
        title=scrub(first.title)[:120],
        summary=summary,
        cost_observed=cost,
        recoverable=_sum_opt(c.recoverable for c in children),
        recoverable_shapley=_sum_opt(c.recoverable_shapley for c in children),
        projected_monthly=_sum_opt(c.projected_monthly for c in children),
        headroom=_sum_opt(c.headroom for c in children),
        lever_ids=tuple(sorted({lv for c in children for lv in c.lever_ids})),
        evidence=tuple(evidence[:20]),
        fix=fix,
        confidence=min((c.confidence for c in children),
                       key=lambda v: _CONFIDENCE_RANK.get(v, 1)),
        validated_against=scrub(only) if only is not None else None,
        needs_eval=any(c.needs_eval for c in children),
        references=tuple(sorted({r for c in children for r in c.references})),
    )


def _smallness(f: Finding) -> tuple[int, int, str]:
    cost = f.cost_observed.nano if f.cost_observed.nano is not None else 0
    return (f.n_users, cost, f.finding_id)


def _sort_key(f: Finding) -> tuple[int, str, str]:
    p50 = f.recoverable.nano if f.recoverable is not None and f.recoverable.nano is not None else 0
    return (-p50, f.detector_id, f.finding_id)


CountUsers = Callable[[Scope], int] | Callable[[Finding, Scope], int]


def _counter_arity(count_users: Callable[..., int]) -> int:
    """1 for ``count_users(scope)``, 2 for ``count_users(finding, scope)`` (detected from the
    required positional parameters; a callable that cannot be inspected is taken as one-arg)."""
    try:
        params = inspect.signature(count_users).parameters.values()
    except (TypeError, ValueError):
        return 1
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                  and p.default is p.empty]
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return 2 if len(positional) >= 1 else 1
    return 2 if len(positional) >= 2 else 1


def _rescope_pending(pending: list[Finding], out: list[Finding], *, k: int,
                     levels: tuple[frozenset[str], ...], copilot: bool,
                     count: Callable[[Finding, Scope], int] | None) -> list[Finding]:
    """Walk *pending* up *levels*, merging per (detector, kind, parent) with complementary
    suppression against the published findings of the same chain; returns the new *out*."""
    for keep in levels:
        if not pending:
            break
        groups: dict[tuple, list[Finding]] = {}
        for f in pending:
            parent = _parent_scope(f.scope, keep)
            groups.setdefault((f.detector_id, f.kind, parent.dims), []).append(f)
        pending = []
        for key in sorted(groups):
            detector_id, kind, dims = key
            parent = Scope(dims=dims)
            children = sorted(groups[key], key=lambda f: f.finding_id)
            peers = [f for f in out if f.detector_id == detector_id and f.kind == kind
                     and f.audience == "org" and not _exempt(f)
                     and _is_product_scoped(f) is copilot
                     and _parent_scope(f.scope, keep) == parent]
            same_scope = [f for f in peers if f.scope == parent]
            # Complementary suppression: the re-scoped finding must not carry the small children
            # alone next to published siblings under the same parent (their data would be
            # isolated under the parent's larger user count), so it absorbs the finding at the
            # parent scope itself or else the smallest published sibling.
            absorbed = same_scope or sorted(peers, key=_smallness)[:1]
            if absorbed:
                out = [f for f in out if f not in absorbed]
                children = sorted(children + absorbed, key=lambda f: f.finding_id)
            if count is not None:
                n = int(count(children[0], parent))
            else:
                n = max(c.n_users for c in children)
            merged = _merge_findings(children, parent, n, k)
            if n >= k:
                out.append(merged)
            else:
                pending.append(merged)
    return out


def rescope_findings(findings: Sequence[Finding], *, k: int = 5,
                     count_users: CountUsers | None = None) -> list[Finding]:
    """Publish *findings* for the org audience with k-anonymity (SPEC §8.4).

    An org finding whose scope has fewer than *k* users — or names a person or session — is
    re-scoped to its parent (team × lane kind → team → cost center → org, :data:`RESCOPE_LEVELS`),
    merging the findings of the same detector and kind that land on the same parent. With
    complementary suppression: when findings of that detector and kind under the same parent are
    already published, the merge also absorbs the one at exactly the parent scope, else the
    smallest of them (fewest users, then lowest cost), so the small children's figures are never
    published alone beside their siblings. The merged ``n_users`` is
    ``count_users(parent_scope)`` — an exact distinct count from the store — or, without it, the
    largest child count (a lower bound, which can only suppress more). What stays below *k* at the
    org level is withheld. Generated text of a re-scoped finding has the values of the dropped
    dimensions replaced by :data:`SCRUBBED`. Exempt and passed through unchanged: data-quality
    findings (R-E1),
    provider-side aggregate findings without person or API-key dimensions, and ``self``-audience
    findings (published only to the self view by the caller). Output order: (−recoverable point or
    0, detector_id, finding_id).

    GitHub Copilot (K-4): *count_users* may also take ``(finding, scope)`` (e.g.
    :func:`scope_counter`; detected by arity). Findings whose scope has a ``product`` dim follow
    ruling R-E16 — exempt only with count source ``entity`` at entity level, category
    ``aggregate`` notwithstanding — and re-scope on :data:`COPILOT_RESCOPE_LEVELS` (the root dims,
    ``plan_scenario`` included, are never dropped, so scenarios are never merged); a ``budget-*``
    finding naming a person or a ``p_`` value raises ``PrivacyError``.
    """
    k = _check_k(k)
    count: Callable[[Finding, Scope], int] | None = None
    if count_users is not None:
        if _counter_arity(count_users) == 2:
            count = count_users  # type: ignore[assignment]
        else:
            one_arg = count_users

            def count(_f: Finding, scope: Scope) -> int:
                return one_arg(scope)  # type: ignore[call-arg]
    out: list[Finding] = []
    pending: list[Finding] = []
    pending_copilot: list[Finding] = []
    key_scoped: dict[tuple, list[Finding]] = {}
    for f in findings:
        if not isinstance(f, Finding):
            raise ContractViolation("rescope_findings expects Findings")
        product = _is_product_scoped(f)
        if product:
            _check_budget_privacy(f)
        if f.audience != "org" or _exempt(f):
            out.append(f)
        elif product:
            if _needs_rescope(f, k):
                pending_copilot.append(f)
            else:
                out.append(f)
        elif f.category == "aggregate" and _needs_rescope(f, k):
            # Provider-side finding on an API key below k: re-scope to the key's workspace level,
            # which names no person (SPEC §8.5).
            parent = _aggregate_parent(f.scope)
            key_scoped.setdefault((f.detector_id, f.kind, parent.dims), []).append(f)
        elif _needs_rescope(f, k):
            pending.append(f)
        else:
            out.append(f)
    for (detector_id, kind, dims), children in sorted(key_scoped.items()):
        parent = Scope(dims=dims)
        peers = [f for f in out if f.detector_id == detector_id and f.kind == kind
                 and f.audience == "org" and not _is_product_scoped(f)
                 and _aggregate_parent(f.scope) == parent]
        same_scope = [f for f in peers if f.scope == parent]
        # complementary suppression, as below: never the small keys alone beside published keys
        absorbed = same_scope or sorted(peers, key=_smallness)[:1]
        out = [f for f in out if f not in absorbed]
        group = sorted(children + absorbed, key=lambda f: f.finding_id)
        out.append(_merge_findings(group, parent, max(c.n_users for c in group), k))
    out = _rescope_pending(pending, out, k=k, levels=RESCOPE_LEVELS, copilot=False, count=count)
    out = _rescope_pending(pending_copilot, out, k=k, levels=COPILOT_RESCOPE_LEVELS, copilot=True,
                           count=count)
    out.sort(key=_sort_key)
    return out


# ---------------------------------------------------------------------------------------------
# counting people per scope (K-4, addendum CA-37)
# ---------------------------------------------------------------------------------------------

#: Scope dims → ``where`` key, per count source family. ``product``, ``entity`` and ``org`` are
#: mapped specially; ``plan_scenario`` is never a filter (both scenarios have the same people).
_LEDGER_REQUEST_KEYS = frozenset({"team", "cost_center", "lane_kind", "billing_class", "model",
                                  "workspace_id", "agent_product", "agent_type", "workload_class",
                                  "channel", "repo", "project", "billing_path", "provider"})
_LEDGER_COST_KEYS = frozenset({"team", "cost_center", "channel", "model", "sku", "workspace_id",
                               "cost_type"})
_RECORD_KEYS = frozenset({"team", "cost_center", "org", "plan", "bucket", "product"})
_COPILOT_PRODUCT = "copilot"


def _where_for(scope: Scope, source: str) -> dict[str, str] | None:
    """The ``where`` filter counting people of *scope* in *source*, or None when a scope dim has
    no filter there (the caller then counts 0: an unknown count can only suppress more)."""
    ledger = source in ("requests", "cost_lines")
    allowed = (_LEDGER_REQUEST_KEYS if source == "requests" else
               _LEDGER_COST_KEYS if source == "cost_lines" else _RECORD_KEYS)
    where: dict[str, str] = {}

    def put(key: str, value: str) -> bool:
        if key not in allowed or where.get(key, value) != value:
            return False
        where[key] = value
        return True

    for name, value in scope.dims:
        if name == "plan_scenario":
            continue
        if name == "product":
            if value != _COPILOT_PRODUCT:
                return None
            ok = put("channel", "github_copilot") if ledger else put("product", "github_copilot")
        elif name == "entity":
            if value == "enterprise":
                continue
            kind, _, rest = value.partition(":")
            if kind == "cc" and rest:
                ok = put("cost_center", rest)
            elif kind == "org" and rest:
                ok = put("workspace_id" if ledger else "org", rest)
            else:
                return None
        elif name == "org":
            ok = put("workspace_id" if ledger else "org", value)
        else:
            ok = put(name, value)
        if not ok:
            return None
    return where


def scope_counter(ledger: LedgerStore | None, record_stores: Sequence[ExtRecordStore] = (), *,
                  since_ms: int, until_ms: int,
                  source_of: Callable[[Finding], str] | None = None
                  ) -> Callable[[Finding, Scope], int]:
    """A two-argument ``count_users`` for :func:`rescope_findings` (K-4, addendum CA-37).

    ``count(finding, scope)`` counts the distinct people of *scope* over the finding's count
    source — *source_of* (default ``core.catalog.count_source(detector_id, kind)``):
    ``requests`` and ``cost_lines`` through ``ledger.count_users(…, source=…)``, ``licenses`` and
    ``activity`` through the record stores (the largest per-store count: stores never share
    people across key ids, so this is a lower bound), and ``entity`` — a scope that R-E16 does not
    exempt — through the cost lines. Scope → ``where``: ``team``/``cost_center``/``model`` as
    such, ``org`` → ``workspace_id`` (ledger) or ``org`` (records), ``entity`` ``cc:<n>`` →
    ``cost_center``, ``org:<o>`` → org, ``enterprise`` → no filter, ``product=copilot`` → channel
    ``github_copilot`` (ledger) or product ``github_copilot`` (records), ``plan``/``bucket`` →
    license filters; ``plan_scenario`` is ignored. A dim the source cannot filter, a missing store
    or an unknown source counts 0 (suppresses more, never less)."""

    def default_source(f: Finding) -> str:
        return _count_source(f)

    pick = source_of if source_of is not None else default_source

    def count(finding: Finding, scope: Scope) -> int:
        source = pick(finding)
        if source == "entity":
            source = "cost_lines"
        where = _where_for(scope, source)
        if where is None:
            return 0
        if source == "requests":
            if ledger is None:
                return 0
            return int(ledger.count_users(since_ms=since_ms, until_ms=until_ms, where=where))
        if source == "cost_lines":
            if ledger is None:
                return 0
            return int(ledger.count_users(since_ms=since_ms, until_ms=until_ms, where=where,
                                          source="cost_lines"))
        if source in ("licenses", "activity"):
            counts = [int(rs.count_users(since_ms=since_ms, until_ms=until_ms, where=where,
                                         source=source)) for rs in record_stores]
            return max(counts, default=0)
        return 0

    return count


def require_self_or_aggregate(group_by: Sequence[str], self_principal: str | None) -> None:
    """Refuse grouping by a person (``principal``, ``session``, ``session_key``) unless the caller
    is the self view (``self_principal`` given): no command lists or ranks people (SPEC §8.5)."""
    if isinstance(group_by, str):
        group_by = [part for part in group_by.split(",") if part]
    person = sorted({g.strip() for g in group_by} & PERSON_DIMS)
    if person and not self_principal:
        raise PrivacyError(
            f"grouping by {', '.join(person)} is only allowed in the self view (--self)"
        )


def _combine_payload(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, bool) or isinstance(b, bool):
        raise UsageError("merge_small_groups: boolean payloads cannot be combined")
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        return a + b
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        if len(a) != len(b):
            raise UsageError("merge_small_groups: payload sequences differ in length")
        return type(a)(_combine_payload(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return {key: _combine_payload(a.get(key), b.get(key)) for key in sorted({*a, *b})}
    if isinstance(a, UsageBuckets) and isinstance(b, UsageBuckets):
        return _add_usage(a, b)
    try:
        return a + b
    except TypeError:
        raise UsageError("merge_small_groups: payloads must support addition") from None


def merge_small_groups(rows: Sequence[tuple[str, int, T]], *, k: int,
                       other_label: str = "(other)") -> tuple[list[tuple[str, int, T]], int]:
    """Ingest-time aggregation of people into groups (SPEC §5.11): groups with fewer than *k* users
    merge into one *other_label* row when together they reach *k*; otherwise they are dropped.

    Precondition: the groups partition the users (each person maps to one team through
    ``IngestOptions.team_map``), so the merged count is the exact sum. Payloads are combined with
    ``+`` (numbers, ``UsageBuckets``, anything supporting ``+``), element-wise for tuples/lists and
    key-wise for mappings. Returns ``(rows, dropped)``: rows sorted by label with the other row
    last, and the number of input groups dropped entirely (for ``dq.outcomes_suppressed``).
    """
    k = _check_k(k)
    big: dict[str, tuple[str, int, Any]] = {}
    small: list[tuple[str, int, Any]] = []
    for row in rows:
        label, n, payload = row
        if type(n) is not int or n < 0:
            raise UsageError("merge_small_groups: user counts must be non-negative ints")
        if n >= k and label != other_label:
            if label in big:
                prev = big[label]
                big[label] = (label, prev[1] + n, _combine_payload(prev[2], payload))
            else:
                big[label] = (label, n, payload)
        else:
            small.append((label, n, payload))
    out: list[tuple[str, int, Any]] = [big[label] for label in sorted(big)]
    dropped = 0
    if small:
        total = sum(n for _, n, _ in small)
        if total >= k:
            payload = None
            for _, _, p in small:
                payload = _combine_payload(payload, p)
            out.append((other_label, total, payload))
        else:
            dropped = len(small)
    return out, dropped
