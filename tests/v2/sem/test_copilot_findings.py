"""CORE-AMENDMENTS S-1 … S-3 and ruling R-E20: product families, Copilot scopes, the per-field
basis domains, pool-cohort labels, Copilot fix substitution and ``min_usd_gate``."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import catalog, registry
from tokenbill.core.builders import FlatRates, make_copilot_ctx, make_inference
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import ContractViolation
from tokenbill.core.findings import (
    COPILOT_SUMMARY_PHRASE,
    COPILOT_TITLE_PREFIX,
    MAX_SUMMARY,
    MAX_TITLE,
    NO_COPILOT_SETTING,
    build_finding,
    cohort_key,
    finding_id,
    make_scope,
    min_usd_gate,
    product_family,
)
from tokenbill.core.labels import Basis, Evidence, Figure, estimated, exact, unpriced
from tokenbill.core.records import Attribution, InferenceKind, LaneKind
from tokenbill.core.types import AnalysisContext, Finding, Fix, Scope

from .helpers import lane, req, usage

PRICER = FlatRates()
RULES = RulesTable()
LE, LIST, INV = Basis.LIST_EQUIVALENT, Basis.LIST, Basis.INVOICE
CLAUDE_FIX = Fix(
    text="Keep the model fixed mid-session; switch with /model only at a task boundary.",
    config_patch=(("env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
                  ("model", '"claude-opus-5-5"')),
    target="claude-code-managed-settings",
    doc_url="https://code.claude.com/docs/en/prompt-caching",
    gates=("claude-code>=2.1.267",),
)
COPILOT_FIX = Fix(
    text="Auto switches models only at cache boundaries; switch models at /new.",
    config_patch=(("copilot.model", '"auto"'),),
    target="github-copilot",
    doc_url="https://docs.github.com/en/copilot/tutorials/optimize-ai-usage",
)


def ctx(**thresholds: str) -> AnalysisContext:
    return AnalysisContext(pricer=PRICER, rules=RULES, replayer=None, calibration=None,
                           window=(0, 10**12), capabilities=frozenset(), thresholds=thresholds)


def pe(nano: int = 10) -> Figure:
    return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.PROVIDER_ESTIMATE)


def contract(nano: int = 10) -> Figure:
    return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.CONTRACT)


COPILOT = make_scope(product="copilot", entity="enterprise", team="payments")
POOL = make_scope(team="payments", lane_kind="main", billing_class="pool")


def fields(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        detector_id="copilot.org-scan", kind="auto-adoption", detector_version="1.0",
        category="aggregate", lever_class="rate", audience="org",
        title="Auto model selection is rarely used", summary="Most requests pick a model.",
        scope=COPILOT, n_events=3, n_lanes=0, n_users=12, first_seen_ms=0,
        cost_observed=exact(2_000_000_000, LE),
        recoverable=estimated(600_000_000, LIST, note="pool realization"),
        headroom=estimated(400_000_000, LE, note="credits"),
        references=["copilot-cost-levers"])
    base.update(over)
    return base


def pool_fields(**over: Any) -> dict[str, Any]:
    """A DETECT-CACHE-shaped generic finding on a pool lane cohort (every figure LE)."""
    base = fields(detector_id="cache.miss-by-cause", kind="model-switch", category="breaker",
                  lever_class="behavioral", title="Model switches rewrite the cache in payments",
                  summary="12 model switches rebuilt 1.2M cached tokens.", scope=POOL,
                  cost_observed=exact(900_000_000, LE),
                  recoverable=estimated(450_000_000, LE, upper_bound=True), headroom=None)
    base.update(over)
    return base


# ---------------------------------------------------------------------------------------------
# S-1: product families and cohorts
# ---------------------------------------------------------------------------------------------


def copilot_lane(lane_key: str = "C", team: str | None = "payments", **ctx_kw: Any):
    ctx_kw.setdefault("billing_path", "copilot_pool")
    attribution = Attribution(team=team, billing_path=ctx_kw["billing_path"])
    return lane([req(lane_key, 0, 0, usage(w5=10_000), provider="github",
                     channel="github_copilot", attribution=attribution, **ctx_kw)],
                lane_key=lane_key)


def test_claude_code_and_copilot_lanes_never_share_a_cohort() -> None:
    claude = lane([req("A", 0, 0, usage(w5=10_000), billing_path="api_key",
                       attribution=Attribution(team="payments", billing_path="api_key",
                                               agent_product="claude_code"))], lane_key="A")
    copilot = copilot_lane()
    assert cohort_key(claude) == ("payments", "main", "billed")
    assert cohort_key(copilot) == ("payments", "main", "pool")
    assert cohort_key(claude) != cohort_key(copilot)
    assert len(cohort_key(copilot)) == 3     # the pinned 3-tuple is unchanged
    assert product_family(claude) == "default"
    assert product_family(copilot) == "copilot"
    direct = copilot_lane("D", billing_path="copilot_direct")
    assert cohort_key(direct) == ("payments", "main", "pool")
    assert product_family(direct) == "copilot"


def test_product_family_by_channel_and_first_request() -> None:
    # a github_copilot channel marks a Copilot lane even when the path is not a pool path
    unknown_path = lane([req("U", 0, 0, usage(w5=1), provider="github",
                             channel="github_copilot")], lane_key="U")
    assert unknown_path.billing_class == "billed"
    assert product_family(unknown_path) == "copilot"
    # the first request decides: a Claude first request makes a default lane
    mixed = lane([req("M", 0, 0, usage(w5=1)),
                  req("M", 1, 10, usage(r=1), provider="github", channel="github_copilot")],
                 lane_key="M")
    assert product_family(mixed) == "default"
    assert product_family(lane([])) == "default"
    subscription = lane([req("S", 0, 0, usage(w5=1), billing_path="subscription")],
                        lane_key="S")
    assert product_family(subscription) == "default"


def test_product_family_without_a_serving_inference() -> None:
    residual = make_inference(usage(o=10), kind=InferenceKind.OUTPUT_RESIDUAL,
                              ctx=make_copilot_ctx(billing_path="unknown"))
    assert residual.pricing.channel == "github_copilot"
    first = req("R", 0, 0, usage(o=10), kind="output_residual", provider="github",
                channel="github_copilot")
    assert first.serving_inference is None
    assert product_family(lane([first], lane_key="R")) == "copilot"
    claude_residual = req("Q", 0, 0, usage(o=10), kind="output_residual")
    assert product_family(lane([claude_residual], lane_key="Q")) == "default"


def test_make_scope_adds_the_copilot_product_to_pool_cohorts() -> None:
    assert make_scope(billing_class="pool", team="t") == Scope(dims=(
        ("billing_class", "pool"), ("product", "copilot"), ("team", "t")))
    assert dict(make_scope(billing_class="pool", team="t").dims)["product"] == "copilot"
    assert dict(make_scope(billing_class="pool", product=None).dims)["product"] == "copilot"
    assert dict(make_scope(billing_class="pool", product="other").dims)["product"] == "other"
    assert make_scope(billing_class="allowance", team="t") == Scope(dims=(
        ("billing_class", "allowance"), ("team", "t")))
    assert make_scope(team="t", lane_kind=LaneKind.MAIN) == Scope(dims=(
        ("lane_kind", "main"), ("team", "t")))
    # the product dim is part of the finding id (pool findings never collide with billed ones)
    assert finding_id("d", "k", make_scope(team="t", billing_class="pool")) == \
        finding_id("d", "k", Scope(dims=(("billing_class", "pool"), ("product", "copilot"),
                                         ("team", "t"))))


def test_registry_filters_lanes_with_product_family() -> None:
    assert registry._product_family_fn() is product_family

    class CopilotOnly:
        id = "semc.copilot-only"
        version = "1"
        requires: frozenset[str] = frozenset()
        kinds = ("seen",)
        families = frozenset({"copilot"})

        def __init__(self) -> None:
            self.seen: list[str] = []

        def detect(self, lanes, ctx):  # noqa: ANN001, ANN202
            self.seen.extend(ln.lane_key for ln in lanes)
            return []

    det = CopilotOnly()
    claude = lane([req("A", 0, 0, usage(w5=1))], lane_key="A")
    lanes = [claude, copilot_lane("C")]
    families = registry._Families(lanes)
    assert families.of_lanes() == ["default", "copilot"]
    assert [ln.lane_key for ln in registry._lanes_for(det, lanes, families, {})] == ["C"]


# ---------------------------------------------------------------------------------------------
# S-2 / R-E20: basis domains
# ---------------------------------------------------------------------------------------------


def test_r_e20_valid_copilot_finding() -> None:
    f = build_finding(**fields())
    assert f.cost_observed.basis is LE and f.recoverable is not None
    assert f.recoverable.basis is LIST and f.headroom is not None and f.headroom.basis is LE
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.finding_id == finding_id("copilot.org-scan", "auto-adoption", COPILOT)
    assert f.title == "Auto model selection is rarely used"   # not a pool cohort: no prefix


@pytest.mark.parametrize("over", [
    {"headroom": estimated(400_000_000, LIST)},                    # headroom on LIST
    {"headroom": exact(1, INV)},
    {"headroom": contract()},
    {"cost_observed": contract()},                                 # CONTRACT never
    {"recoverable": contract()},
    {"recoverable_shapley": contract()},
    {"projected_monthly": contract()},
    {"recoverable": exact(1, INV)},                                # dollar fields: LIST / LE
    {"recoverable_shapley": exact(1, INV)},
    {"projected_monthly": exact(1, INV)},
    {"cost_observed": pe()},                                       # R4 outside data-quality
    {"recoverable": pe()},
    {"headroom": pe()},
    {"headroom": 5},
    {"recoverable": 5},
])
def test_r_e20_rejects(over: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        build_finding(**fields(**over))


def test_r_e20_accepted_shapes() -> None:
    # pool-regime: cost LIST_EQUIVALENT, nothing recoverable
    build_finding(**fields(kind="pool-regime", recoverable=None, headroom=None))
    # idle-seat: cost LIST ESTIMATED, recoverable LIST
    build_finding(**fields(detector_id="copilot.seats-budgets", kind="idle-seat",
                           cost_observed=estimated(390_000_000_000, LIST, note="seats"),
                           recoverable=estimated(78_000_000_000, LIST), headroom=None))
    # INVOICE cost_observed on an entity scope (no billing_class dim)
    entity = make_scope(product="copilot", entity="enterprise")
    f = build_finding(**fields(scope=entity, cost_observed=exact(12_000_000_000, INV),
                               projected_monthly=estimated(1, LE),
                               recoverable_shapley=estimated(2, LIST)))
    assert f.cost_observed.basis is INV and "billing_class" not in dict(f.scope.dims)
    # LIST_EQUIVALENT recoverable beside headroom: per-field domains only (S-2 wins over the
    # addendum's "one basis B" text)
    build_finding(**fields(recoverable=estimated(1, LE), headroom=estimated(2, LE)))
    # every dollar field on its own basis
    build_finding(**fields(recoverable=estimated(1, LIST), recoverable_shapley=estimated(1, LE),
                           projected_monthly=estimated(1, LIST), cost_observed=exact(3, LIST)))


def test_provider_estimates_in_copilot_data_quality_findings() -> None:
    dq = build_finding(**fields(kind="dq.cost-state-drift", category="data-quality",
                                lever_class="none", cost_observed=pe(), recoverable=pe(),
                                headroom=None))
    assert dq.cost_observed.basis is Basis.PROVIDER_ESTIMATE
    mixed = build_finding(**fields(kind="dq.cost-state-drift", category="data-quality",
                                   lever_class="none", cost_observed=pe(),
                                   recoverable=estimated(1, LIST), headroom=estimated(1, LE)))
    assert mixed.headroom is not None
    with pytest.raises(ContractViolation):    # headroom is LIST_EQUIVALENT only, even in dq
        build_finding(**fields(kind="dq.x", category="data-quality", lever_class="none",
                               headroom=pe()))
    with pytest.raises(ContractViolation):    # CONTRACT never, even in dq
        build_finding(**fields(kind="dq.x", category="data-quality", lever_class="none",
                               cost_observed=contract()))


def test_billing_class_pool_scope_alone_is_a_copilot_scope() -> None:
    manual = Scope(dims=(("billing_class", "pool"), ("team", "payments")))   # no product dim
    f = build_finding(**pool_fields(scope=manual, headroom=estimated(1, LE)))
    assert f.headroom is not None and f.title.startswith(COPILOT_TITLE_PREFIX)
    with pytest.raises(ContractViolation):
        build_finding(**pool_fields(scope=manual, cost_observed=contract()))


@pytest.mark.parametrize("scope", [
    make_scope(team="payments", lane_kind="main"),
    make_scope(team="payments", lane_kind="main", billing_class="allowance"),
    make_scope(product="claude-code", team="payments"),
])
def test_headroom_on_claude_findings_is_a_contract_violation(scope: Scope) -> None:
    allowance = ("billing_class", "allowance") in scope.dims
    basis = LE if allowance else LIST
    base = dict(scope=scope, detector_id="cache.miss-by-cause", kind="ttl-expiry",
                category="breaker", title="Allowance headroom: x" if allowance else "x",
                cost_observed=exact(10, basis), recoverable=estimated(5, basis),
                headroom=None)
    ok = build_finding(**fields(**base))
    assert ok.headroom is None
    with pytest.raises(ContractViolation, match="R-E20"):
        build_finding(**fields(**{**base, "headroom": estimated(5, LE)}))


def test_claude_findings_keep_r_e8_exactly() -> None:
    billed = make_scope(team="payments", lane_kind="main")
    with pytest.raises(ContractViolation):      # one basis per finding
        build_finding(**fields(scope=billed, headroom=None, cost_observed=exact(1, LIST),
                               recoverable=estimated(1, INV)))
    with pytest.raises(ContractViolation):      # LE only in allowance cohorts (D26)
        build_finding(**fields(scope=billed, headroom=None, recoverable=None,
                               cost_observed=exact(1, LE)))
    with pytest.raises(ContractViolation):      # INVOICE cost is not a Copilot privilege here
        build_finding(**fields(scope=billed, headroom=None, recoverable=estimated(1, LIST),
                               cost_observed=exact(1, INV)))


_BASES = st.sampled_from(list(Basis))
_OPT_BASES = st.one_of(st.none(), _BASES)


@settings(max_examples=400, deadline=None)
@given(cost=_BASES, rec=_OPT_BASES, shap=_OPT_BASES, proj=_OPT_BASES, head=_OPT_BASES,
       dq=st.booleans(), copilot=st.booleans())
def test_basis_rule_property(cost: Basis, rec: Basis | None, shap: Basis | None,
                             proj: Basis | None, head: Basis | None, dq: bool,
                             copilot: bool) -> None:
    """build_finding accepts exactly the R-E20 domains on Copilot scopes and exactly R-E8 (plus
    ``headroom is None``) elsewhere."""
    def fig(b: Basis | None) -> Figure | None:
        return None if b is None else Figure(nano=1, evidence=Evidence.EXACT, basis=b)

    scope = COPILOT if copilot else make_scope(team="payments", lane_kind="main")
    over = dict(scope=scope, cost_observed=fig(cost), recoverable=fig(rec),
                recoverable_shapley=fig(shap), projected_monthly=fig(proj), headroom=fig(head))
    if dq:
        over.update(category="data-quality", lever_class="none", kind="dq.probe")
    present = {"cost_observed": cost, "recoverable": rec, "recoverable_shapley": shap,
               "projected_monthly": proj, "headroom": head}
    present = {k: v for k, v in present.items() if v is not None}
    if copilot:
        domains = {"cost_observed": {LE, LIST, INV}, "recoverable": {LIST, LE},
                   "recoverable_shapley": {LIST, LE}, "projected_monthly": {LIST, LE},
                   "headroom": {LE}}
        valid = all(
            b in domains[k] or (dq and b is Basis.PROVIDER_ESTIMATE and k != "headroom")
            for k, b in present.items())
    else:
        bases = {b for k, b in present.items() if k != "headroom"}
        valid = head is None and len(bases) == 1 and (
            (bases == {Basis.PROVIDER_ESTIMATE} and dq) or bases == {LIST}
            or bases == {Basis.CONTRACT} or bases == {INV})
    if valid:
        assert isinstance(build_finding(**fields(**over)), Finding)
    else:
        with pytest.raises(ContractViolation):
            build_finding(**fields(**over))


# ---------------------------------------------------------------------------------------------
# S-2: pool-cohort labels
# ---------------------------------------------------------------------------------------------


def test_pool_cohort_titles_and_summaries_are_labelled() -> None:
    f = build_finding(**pool_fields())
    assert f.title == COPILOT_TITLE_PREFIX + "Model switches rewrite the cache in payments"
    assert f.summary == "12 model switches rebuilt 1.2M cached tokens. " \
                        "(list-equivalent AI-credit value)"
    assert f.finding_id == finding_id("cache.miss-by-cause", "model-switch", POOL)
    # idempotent: rebuilding from the normalized fields changes nothing
    again = build_finding(**{fld.name: getattr(f, fld.name) for fld in dataclasses.fields(f)})
    assert again == f
    # present labels are kept as they are
    same = build_finding(**pool_fields(title=COPILOT_TITLE_PREFIX + "x",
                                       summary=f"Values are {COPILOT_SUMMARY_PHRASE}."))
    assert same.title == COPILOT_TITLE_PREFIX + "x"
    assert same.summary == f"Values are {COPILOT_SUMMARY_PHRASE}."
    empty = build_finding(**pool_fields(summary=""))
    assert empty.summary == f"({COPILOT_SUMMARY_PHRASE})"


def test_pool_labels_fit_the_limits_and_always_survive() -> None:
    long_title = "Model switches in team payments-" + "x" * 20 + " " + "word " * 30
    f = build_finding(**pool_fields(title=long_title.strip(), summary="word " * 80 + "end"))
    assert len(f.title) <= MAX_TITLE and f.title.startswith(COPILOT_TITLE_PREFIX)
    assert f.title.endswith("…")
    assert len(f.summary) <= MAX_SUMMARY
    assert f.summary.endswith(f"({COPILOT_SUMMARY_PHRASE})")
    # a 120-char title (valid before labelling) is cut at a word boundary, never mid-token
    title = ("team-" + "a" * 50 + " ") * 2 + "b" * 8
    assert len(title) == 120
    cut = build_finding(**pool_fields(title=title)).title
    assert len(cut) <= MAX_TITLE and ("team-" + "a" * 50) in cut and "b" not in cut
    # a single over-long token is still cut to the limit
    token = build_finding(**pool_fields(title="z" * 120)).title
    assert len(token) == MAX_TITLE and token.endswith("…")
    # an already-prefixed title is cut behind its prefix, never rejected
    pre = build_finding(**pool_fields(title=COPILOT_TITLE_PREFIX + "word " * 40)).title
    assert len(pre) <= MAX_TITLE and pre.startswith(COPILOT_TITLE_PREFIX)
    assert pre.count(COPILOT_TITLE_PREFIX) == 1
    # a 400-char summary keeps the phrase
    full = build_finding(**pool_fields(summary="s" * MAX_SUMMARY)).summary
    assert len(full) <= MAX_SUMMARY and COPILOT_SUMMARY_PHRASE in full


def test_labels_only_on_pool_cohorts_with_list_equivalent_figures() -> None:
    # a Copilot aggregate finding (product=copilot, no pool cohort) keeps its title
    f = build_finding(**fields())
    assert not f.title.startswith(COPILOT_TITLE_PREFIX)
    assert COPILOT_SUMMARY_PHRASE not in f.summary
    # a pool data-quality finding priced as a provider estimate is not a credit value
    dq = build_finding(**pool_fields(kind="dq.cost-state-drift", category="data-quality",
                                     lever_class="none", cost_observed=pe(), recoverable=None))
    assert not dq.title.startswith(COPILOT_TITLE_PREFIX)
    assert COPILOT_SUMMARY_PHRASE not in dq.summary
    # a pool finding with a LIST recoverable beside a LIST_EQUIVALENT cost is labelled
    lab = build_finding(**pool_fields(recoverable=estimated(1, LIST)))
    assert lab.title.startswith(COPILOT_TITLE_PREFIX)
    # Claude cohorts are never touched
    claude = build_finding(**fields(scope=make_scope(team="p", lane_kind="main"),
                                    cost_observed=exact(1, LIST),
                                    recoverable=estimated(1, LIST), headroom=None))
    assert claude.title == "Auto model selection is rarely used"


@pytest.mark.parametrize("over", [
    {"title": ""}, {"title": 7}, {"title": None}, {"summary": None}, {"summary": 3},
])
def test_invalid_titles_and_summaries_still_raise_on_pool_cohorts(over: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        build_finding(**pool_fields(**over))


@pytest.mark.parametrize("over", [{"title": "x" * 121}, {"summary": "y" * 401}])
def test_only_pool_cohorts_are_cut_to_fit(over: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        build_finding(**fields(**over))              # product=copilot aggregate: validated
    f = build_finding(**pool_fields(**over))         # pool cohort: normalized, never rejected
    assert len(f.title) <= MAX_TITLE and len(f.summary) <= MAX_SUMMARY


# ---------------------------------------------------------------------------------------------
# S-2: Copilot fixes
# ---------------------------------------------------------------------------------------------


class Calls:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.args: list[tuple[str, str | None, str]] = []

    def __call__(self, detector_id: str, kind: str | None, family: str) -> Any:
        self.args.append((detector_id, kind, family))
        return self.answer


def _no_claude_keys(fix: Fix | None) -> bool:
    return fix is None or not any("CLAUDE_CODE" in k for k, _ in (fix.config_patch or ()))


def test_copilot_fix_from_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = Calls(COPILOT_FIX)
    monkeypatch.setattr(catalog, "fix_for", stub, raising=False)
    f = build_finding(**pool_fields(fix=CLAUDE_FIX))
    assert f.fix == COPILOT_FIX and _no_claude_keys(f.fix)
    assert stub.args == [("cache.miss-by-cause", "model-switch", "copilot")]
    # an aggregate Copilot scope (product=copilot) is substituted too
    g = build_finding(**fields(fix=CLAUDE_FIX))
    assert g.fix == COPILOT_FIX


def test_copilot_fix_without_a_catalog_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "fix_for", Calls(None), raising=False)
    f = build_finding(**pool_fields(fix=CLAUDE_FIX))
    assert f.fix is not None
    assert f.fix.text == CLAUDE_FIX.text + NO_COPILOT_SETTING
    assert f.fix.text.endswith("(no Copilot setting known)")
    assert f.fix.config_patch is None and f.fix.target is None and f.fix.gates == ()
    assert f.fix.doc_url == CLAUDE_FIX.doc_url
    assert _no_claude_keys(f.fix)
    # idempotent: the suffix is not appended twice
    again = build_finding(**{fld.name: getattr(f, fld.name) for fld in dataclasses.fields(f)})
    assert again.fix == f.fix


def test_missing_fix_for_accessor_means_no_copilot_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(catalog, "fix_for", raising=False)
    f = build_finding(**pool_fields(fix=CLAUDE_FIX))
    assert f.fix is not None and f.fix.text.endswith(NO_COPILOT_SETTING)
    assert f.fix.config_patch is None and f.fix.target is None


def test_catalog_answers_that_are_not_copilot_fixes_are_ignored(
        monkeypatch: pytest.MonkeyPatch) -> None:
    for answer in (CLAUDE_FIX, "not a fix", dataclasses.replace(COPILOT_FIX, target=None)):
        monkeypatch.setattr(catalog, "fix_for", Calls(answer), raising=False)
        f = build_finding(**pool_fields(fix=CLAUDE_FIX))
        assert f.fix is not None and f.fix.target is None and _no_claude_keys(f.fix)
        assert f.fix.text.endswith(NO_COPILOT_SETTING)


def test_copilot_fixes_and_missing_fixes_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = Calls(dataclasses.replace(COPILOT_FIX, text="other"))
    monkeypatch.setattr(catalog, "fix_for", stub, raising=False)
    own = build_finding(**fields(fix=COPILOT_FIX))
    assert own.fix == COPILOT_FIX
    assert build_finding(**pool_fields(fix=None)).fix is None
    assert stub.args == []                  # never consulted for these


def test_claude_findings_keep_their_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = Calls(COPILOT_FIX)
    monkeypatch.setattr(catalog, "fix_for", stub, raising=False)
    f = build_finding(**fields(scope=make_scope(team="p", lane_kind="main"), headroom=None,
                               cost_observed=exact(1, LIST), recoverable=estimated(1, LIST),
                               fix=CLAUDE_FIX))
    assert f.fix == CLAUDE_FIX and stub.args == []


def test_fix_for_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(detector_id: str, kind: str, family: str) -> Fix:
        raise ContractViolation("catalog bug")

    monkeypatch.setattr(catalog, "fix_for", broken, raising=False)
    with pytest.raises(ContractViolation, match="catalog bug"):
        build_finding(**pool_fields(fix=CLAUDE_FIX))


@pytest.mark.gate
def test_every_catalog_copilot_fix_passes_build_finding() -> None:
    """Gate F' companion (F-KIT-C's ``fix_for``): a catalog Copilot fix survives build_finding."""
    fix_for = getattr(catalog, "fix_for", None)
    if fix_for is None:
        pytest.skip("core.catalog.fix_for not merged yet (F-KIT-C)")
    for det in registry.all_detectors():          # the importable ones, with their kinds
        for kind in det.kinds:
            fix = fix_for(det.id, kind, "copilot")
            if fix is None:
                continue
            assert fix.target == "github-copilot" and _no_claude_keys(fix)
            f = build_finding(**pool_fields(detector_id=det.id, kind=kind, fix=CLAUDE_FIX))
            assert f.fix == fix


# ---------------------------------------------------------------------------------------------
# S-3: min_usd_gate
# ---------------------------------------------------------------------------------------------


def _finding(**over: Any) -> Finding:
    return build_finding(**fields(**over))


def _claude(**over: Any) -> Finding:
    base = dict(scope=make_scope(team="p", lane_kind="main"), headroom=None,
                cost_observed=exact(5_000_000_000, LIST),
                recoverable=estimated(2_000_000_000, LIST))
    base.update(over)
    return build_finding(**fields(**base))


def test_min_usd_gate_on_claude_findings() -> None:
    assert min_usd_gate(_claude(), ctx())
    assert not min_usd_gate(_claude(recoverable=estimated(999_999_999, LIST)), ctx())
    assert min_usd_gate(_claude(recoverable=estimated(1_000_000_000, LIST)), ctx())
    assert min_usd_gate(_claude(recoverable=estimated(100_000_000, LIST)), ctx(min_usd="0.10"))
    # no recoverable, or an unpriced one: the cost_observed point decides (triage / info kinds)
    assert min_usd_gate(_claude(recoverable=None), ctx())
    assert not min_usd_gate(_claude(recoverable=None, cost_observed=exact(1, LIST)), ctx())
    assert min_usd_gate(_claude(recoverable=unpriced("model")), ctx())
    assert not min_usd_gate(_claude(recoverable=None, cost_observed=unpriced("model")), ctx())


def test_min_usd_gate_on_copilot_findings_uses_headroom_too() -> None:
    small = estimated(100_000_000, LIST)
    assert min_usd_gate(_finding(recoverable=small, headroom=estimated(1_500_000_000, LE)),
                        ctx())
    assert not min_usd_gate(_finding(recoverable=small, headroom=estimated(200_000_000, LE)),
                            ctx())
    assert min_usd_gate(_finding(recoverable=None, headroom=estimated(1_000_000_000, LE)),
                        ctx())
    assert min_usd_gate(_finding(recoverable=estimated(3_000_000_000, LIST), headroom=None),
                        ctx())
    # unpriced recoverable, priced headroom: headroom decides; neither: cost_observed decides
    assert not min_usd_gate(_finding(recoverable=unpriced("x"), headroom=estimated(5, LE)),
                            ctx())
    assert min_usd_gate(_finding(recoverable=unpriced("x"),
                                 headroom=estimated(1_000_000_000, LE)), ctx())
    assert min_usd_gate(_finding(recoverable=None, headroom=None), ctx())
    assert not min_usd_gate(_finding(recoverable=None, headroom=None,
                                     cost_observed=exact(10, LE)), ctx())
    # a pool lane finding (billing_class=pool) is a Copilot scope for the gate
    lane_f = build_finding(**pool_fields(recoverable=estimated(10, LE),
                                         headroom=estimated(2_000_000_000, LE)))
    assert min_usd_gate(lane_f, ctx())
