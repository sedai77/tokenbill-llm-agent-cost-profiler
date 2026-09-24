"""Edge cases of the aggregate plan: pricing failures, month and entity handling, linked levers
with nothing to act on, and the small helpers behind the notes."""

from __future__ import annotations

from dataclasses import replace

from tokenbill.copilot import plan as cp
from tokenbill.core import builders as b
from tokenbill.core.errors import PricingError
from tokenbill.core.records import UsageBuckets

from .worlds import (
    PRICER,
    USD,
    World,
    finding,
    has_lever,
    idle,
    lever,
    p1_world,
    plan_for,
    seat_finding,
)

SEAT = "copilot.seat_reclaim"
FAST = "copilot.fast_mode_off"
POLICY = "copilot.seat_policy_selected"


class _RaisingPricer:
    """A Pricer whose unit rates fail for one model (a rate registry error)."""

    def __init__(self, bad: str) -> None:
        self.bad = bad
        self.rate_card_sha256 = PRICER.rate_card_sha256
        self.basis = PRICER.basis

    def unit_rates(self, ctx, *, ts_ms):  # noqa: ANN001, ANN201
        if ctx.model == self.bad:
            raise PricingError("rate row unreadable")
        return PRICER.unit_rates(ctx, ts_ms=ts_ms)

    def resolve(self, ctx, *, ts_ms):  # noqa: ANN001, ANN201
        return PRICER.resolve(ctx, ts_ms=ts_ms)

    def price_inference(self, inf, *, ts_ms):  # noqa: ANN001, ANN201
        return PRICER.price_inference(inf, ts_ms=ts_ms)

    def price_usage(self, usage, ctx, **kw):  # noqa: ANN001, ANN003, ANN201
        return PRICER.price_usage(usage, ctx, **kw)

    def min_cacheable_tokens(self, ctx, *, ts_ms):  # noqa: ANN001, ANN201
        return PRICER.min_cacheable_tokens(ctx, ts_ms=ts_ms)

    def supports(self, ctx, feature, *, ts_ms):  # noqa: ANN001, ANN201
        return PRICER.supports(ctx, feature, ts_ms=ts_ms)

    def tokenizer_family(self, ctx, *, ts_ms):  # noqa: ANN001, ANN201
        return PRICER.tokenizer_family(ctx, ts_ms=ts_ms)


def test_a_pricer_error_leaves_the_cell_unpriced_and_noted() -> None:
    w = p1_world(3_100_000).usage(29, model="Claude Opus 4.8 (fast mode)", output_tokens=5_800)
    plan = cp.plan_copilot(w.cells(), w.pools(), [finding("fast-mode", team="t1")],
                           _RaisingPricer("claude-opus-4-8"), lines=w.lines, activity=[],
                           month=w.month)
    assert not has_lever(plan, FAST)
    assert "fast claude-opus-4-8: 1 cells unpriced at the target" in plan.headline_monthly.note
    assert "copilot.fast_mode_off (linked, nothing to act on" in plan.headline_monthly.note


def test_fast_cell_of_a_model_without_a_rate_on_its_day_is_unpriced() -> None:
    w = p1_world(3_100_000).usage(100, model="Claude Opus 5.5 (fast mode)",
                                  output_tokens=50_000, date="2026-09-10")
    plan = plan_for(w, [finding("fast-mode", team="t1")], grain="day")
    assert "fast claude-opus-5-5: 1 cells unpriced" in plan.headline_monthly.note


def test_december_month_grain_cells_price_on_the_last_day() -> None:
    w = World(month="2026-12").seats("business", 1000).seats("enterprise", 200)
    w.usage(3_100_000, users=2).usage(29, model="Claude Opus 4.8 (fast mode)",
                                      input_tokens=10_000, cache_read=90_000, output_tokens=2_000)
    fast = lever(plan_for(w, [finding("fast-mode", team="t1")], today="2027-01-20"), FAST)
    assert fast.shapley.nano == 145_000_000


def test_linked_levers_with_nothing_to_act_on_are_named() -> None:
    w = p1_world(2_000_000)
    fs = [finding("fast-mode", team="t1"), finding("larger-runner", entity="enterprise")]
    plan = plan_for(w, fs, include_tradeoffs=True)
    assert plan.levers == ()
    note = plan.headline_monthly.note
    assert "copilot.fast_mode_off (linked, nothing to act on" in note
    assert "copilot.agent_runner_standard (linked, nothing to act on" in note


def test_seat_levers_linked_by_id_skip_findings_of_the_wrong_kind() -> None:
    fs = [finding("plan-status", detector="copilot.seats-budgets", entity="enterprise",
                  lever_ids=(SEAT, POLICY, "copilot.seat_downgrade")), idle(10)]
    plan = plan_for(p1_world(2_000_000), fs, include_tradeoffs=True)
    seat = lever(plan, SEAT)
    assert seat.shapley.nano == 190 * USD
    assert set(seat.finding_ids) == {f.finding_id for f in fs}
    assert not has_lever(plan, POLICY) and not has_lever(plan, "copilot.seat_downgrade")


def test_seat_policy_org_name_outside_the_grammar_falls_back_to_all() -> None:
    f = seat_finding("seat-auto-assign", {"auto_assigned": 2}, entity="enterprise",
                     org="org,b", plan="business")
    pol = lever(plan_for(p1_world(2_000_000), [f], include_tradeoffs=True), POLICY)
    assert pol.params == "copilot:seat_policy=assign_selected@all"


def test_org_mode_seat_finding_resolves_its_entity_from_the_org_dim() -> None:
    w = World(entity_mode="org")
    w.seats("business", 100, org="org-a").usage(100_000, org="org-a")
    w.seats("business", 100, org="org-b").usage(100_000, org="org-b")
    f = seat_finding("idle-seat", {"removable": 10}, org="org-b", plan="business",
                     bucket="none_90d", team="t1")
    seat = lever(plan_for(w, [f]), SEAT)
    assert "org:org-b business -10" in seat.shapley.note
    # a single pool entity takes a seat finding without entity or org dims
    one = World(entity_mode="org").seats("business", 100, org="org-a").usage(1, org="org-a")
    bare = seat_finding("idle-seat", {"removable": 3}, plan="business", bucket="none_90d")
    assert "org:org-a business -3" in lever(plan_for(one, [bare]), SEAT).shapley.note


def test_pools_of_other_months_and_legacy_rows_are_ignored() -> None:
    w = p1_world(2_000_000)
    legacy = b.make_ai_usage_row(date_utc="2026-09-12", model="Claude Sonnet 5", credits="500",
                                 principal=b.make_principal("pr"),
                                 sku="copilot_premium_request")
    w.lines.append(legacy[0])
    w.aggs.append(legacy[1])
    august = World(month="2026-08").seats("business", 10).usage(1_000).pools()
    plan = plan_for(w, [idle(50)], pools=w.pools() + august)
    assert lever(plan, SEAT).shapley.nano == 950 * USD
    assert plan.sample == "copilot cells, 1 cells, month 2026-09"


def test_none_sequences_are_empty() -> None:
    w = p1_world(2_000_000)
    plan = cp.plan_copilot(w.cells(), w.pools(), [idle(50)], PRICER, lines=None,  # type: ignore
                           activity=None, month=w.month, config=None)  # type: ignore[arg-type]
    assert lever(plan, SEAT).shapley.nano == 950 * USD


def test_helpers() -> None:
    assert cp._billing_groups(["cc:x", "org:a"]) == {"cc:x": "org:a", "org:a": "org:a"}
    assert cp._billing_groups(["cc:x", "org:a", "org:b"]) == {
        "cc:x": "cc:x", "org:a": "org:a", "org:b": "org:b"}
    assert cp._billing_groups(["cc:x", "enterprise"]) == {"cc:x": "enterprise",
                                                          "enterprise": "enterprise"}
    pm = World().seats("business", 1).usage(1).pools()[0]
    ents = {f"org:o{i}": cp._Ent(replace(pm, entity_id=f"org:o{i}"), (0, 0, 0), f"org:o{i}",
                                 None, None) for i in range(14)}
    note = cp._regime_note(ents)
    assert note.endswith("… 2 more") and note.count("slack") == 12
    assert cp._regime_note({}) == "no pool entity"
    rates = PRICER.unit_rates(b.make_copilot_ctx("claude-sonnet-5"), ts_ms=1_790_000_000_000)
    assert rates is not None
    assert cp._tokens_nano(rates, UsageBuckets(web_search_requests=2)) == 2 * rates.web_search_nano
    assert cp._round(7) == 7
    assert cp._pct(cp.Fraction(1, 8)) == "12%"          # half-even: 12.5 → 12
