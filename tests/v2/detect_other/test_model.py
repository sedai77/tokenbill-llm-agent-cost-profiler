"""``model.routing``: delegation, same-tier (exact arithmetic labeled ESTIMATED, never Opus 4.8),
default model and default effort (Claude Code main lanes only), effort mix and rebaseline."""

from __future__ import annotations

from decimal import Decimal

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.model import Routing

from .helpers import (
    CAPS,
    DAY_MS,
    FABLE5,
    OPUS5,
    OPUS48,
    OPUS55,
    SONNET5,
    SONNET46,
    T0,
    ScaledPricer,
    by_kind,
    ctx,
    evidence,
    lane,
    one,
    table_replayer,
)

CASE1 = (0, 100_000, 2_000, 3_000, 1_000, 500)
SUB_SPEC = "model=claude-sonnet-5@lane_kind:subagent"
EXPLORE_SPEC = "model=claude-haiku-4-5@agent_type:Explore,lane_kind:subagent"
MAIN_SPEC = "model=claude-sonnet-5@agent_product:claude_code,lane_kind:main"
EFFORT_SPEC = "effort=medium,scale=0.5@agent_product:claude_code,lane_kind:main"


def _sub(key: str, model: str, **kw):
    return lane(key, [(0, 0, 20_000, 0, 5, 800), (20, 20_000, 3_000, 0, 5, 800)],
                kind=LaneKind.SUBAGENT, model=model, **kw)


# ---------------------------------------------------------------------------------------------
# delegation-routing
# ---------------------------------------------------------------------------------------------

def test_delegation_on_top_tier_subagents_with_band() -> None:
    rep = table_replayer({SUB_SPEC: 2_000_000_000, EXPLORE_SPEC: 2_500_000_000})
    lanes = [_sub("L-opus", OPUS55), _sub("L-explore", OPUS55, agent_type="Explore"),
             _sub("L-sonnet", SONNET5)]
    found = Routing().detect(lanes, ctx(replayer=rep))
    f = one(found, "delegation-routing")
    assert f.n_lanes == 2 and f.recoverable.nano == 4_000_000_000
    assert f.recoverable.evidence is Evidence.ESTIMATED and f.recoverable.upper_bound
    assert "price-only" in f.recoverable.note and f.needs_eval
    # spend of the two Opus 5.5 lanes: 2 × (20,000 × 5,000 + 5 × 4,000 + 800 × 20,000
    #   + 20,000 × 200 + 3,000 × 5,000 + 5 × 4,000 + 800 × 20,000)
    assert f.cost_observed.nano == 2 * (100_020_000 + 16_000_000 + 4_000_000 + 15_020_000
                                        + 16_000_000)
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.lever_ids == ("cc.subagent_model",)
    assert f.fix.config_patch == (("env.CLAUDE_CODE_SUBAGENT_MODEL", '"claude-sonnet-5"'),)
    explore = evidence(f, "explore:claude-haiku-4-5")
    assert explore["saving_nano"] == 2_500_000_000 and explore["retires"] == "2026-10-15"
    assert "retires not sooner than 2026-10-15" in f.fix.text
    assert evidence(f, "replay:policy")["policy"] == SUB_SPEC


def test_delegation_needs_a_replayer_and_top_tier_lanes() -> None:
    assert by_kind(Routing().detect([_sub("L-opus", OPUS55)], ctx()), "delegation-routing") == []
    rep = table_replayer({SUB_SPEC: 2_000_000_000})
    assert by_kind(Routing().detect([_sub("L-s", SONNET5)], ctx(replayer=rep)),
                   "delegation-routing") == []
    wf = lane("L-wf", [(0, 0, 20_000, 0, 5, 800)], kind=LaneKind.WORKFLOW_AGENT, model=OPUS5,
              product="agent_sdk")
    rep_wf = table_replayer({"model=claude-sonnet-5@lane_kind:workflow_agent": 1_500_000_000})
    f = one(Routing().detect([wf], ctx(replayer=rep_wf)), "delegation-routing")
    assert f.recoverable.nano == 1_500_000_000 and f.fix.config_patch is None


# ---------------------------------------------------------------------------------------------
# same-tier-upgrade
# ---------------------------------------------------------------------------------------------

def test_same_tier_exact_arithmetic_labeled_estimated() -> None:
    # case 1 on Opus 5 = $0.110; on Opus 5.5 = $0.068 → $0.042 on identical tokens
    ln = lane("L-o5", [CASE1], model=OPUS5)
    f = one(Routing().detect([ln], ctx(thresholds={"min_usd": "0.01"})), "same-tier-upgrade")
    assert f.cost_observed.nano == 110_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable.nano == 42_000_000
    assert f.recoverable.evidence is Evidence.ESTIMATED and f.recoverable.upper_bound
    assert "behavior unvalidated" in f.recoverable.note and f.needs_eval
    assert dict(f.scope.dims)["model"] == OPUS5
    assert f.fix.config_patch == (("env.ANTHROPIC_DEFAULT_OPUS_MODEL", '"claude-opus-5-5"'),)
    assert "claude-opus-5 retires not sooner than 2027-07-24" in f.fix.text
    assert evidence(f, f"same-tier:{OPUS5}")["successor"] == OPUS55
    assert f.lever_ids == ("model.same_tier_upgrade",)


def test_same_tier_fable_and_no_successor_for_opus_4_8() -> None:
    low = ctx(thresholds={"min_usd": "0.01"})
    fable = one(Routing().detect([lane("L-f", [CASE1], model=FABLE5)], low), "same-tier-upgrade")
    assert fable.recoverable.nano == 100_000 * (1_000 - 250)     # reads 0.1× → 0.025×
    assert fable.fix.config_patch is None
    assert by_kind(Routing().detect([lane("L-48", [CASE1], model=OPUS48)], low),
                   "same-tier-upgrade") == []
    assert by_kind(Routing().detect([lane("L-55", [CASE1], model=OPUS55)], low),
                   "same-tier-upgrade") == []


def test_same_tier_successor_unpriced_on_the_channel() -> None:
    bed = lane("L-bed", [CASE1], model=OPUS5, billing_path="bedrock",
               per_request={0: {"channel": "bedrock", "endpoint_scope": "global"}})
    f = one(Routing().detect([bed], ctx(thresholds={"min_usd": "0.01"})), "same-tier-upgrade")
    assert f.recoverable.nano is None and f.recoverable.note.startswith("unpriced:")


# ---------------------------------------------------------------------------------------------
# default-model / default-effort
# ---------------------------------------------------------------------------------------------

def _main(key: str, model: str = OPUS55, **kw):
    return lane(key, [(0, 0, 30_000, 0, 5, 1_000), (30, 30_000, 4_000, 0, 5, 1_000)],
                model=model, **kw)


def test_default_model_on_claude_code_main_lanes_only() -> None:
    rep = table_replayer({MAIN_SPEC: 1_500_000_000})
    f = one(Routing().detect([_main("L-1"), _main("L-2")], ctx(replayer=rep)), "default-model")
    assert f.recoverable.nano == 3_000_000_000 and f.recoverable.upper_bound and f.needs_eval
    assert f.fix.config_patch == (("model", '"claude-sonnet-5"'),)
    assert f.lever_ids == ("cc.default_model",) and evidence(f, "default-model:share")[
        "share_pct"] == "100.0"
    sdk = [_main("L-1", product="agent_sdk"), _main("L-2", product="agent_sdk")]
    assert by_kind(Routing().detect(sdk, ctx(replayer=rep)), "default-model") == []
    cheap = [_main("L-1", model=SONNET5), _main("L-2", model=SONNET5), _main("L-3")]
    assert by_kind(Routing().detect(cheap, ctx(replayer=rep)), "default-model") == []
    lowered = ctx(replayer=rep, thresholds={"model.routing.default_model_share": "0.1"})
    assert one(Routing().detect(cheap, lowered), "default-model").recoverable.nano == \
        4_500_000_000


def test_default_effort_on_high_effort_claude_code_main_lanes() -> None:
    rep = table_replayer({EFFORT_SPEC: 800_000_000})
    high = [_main(f"L-{i}", per_request={0: {"effort": "high"}, 1: {"effort": "xhigh"}})
            for i in range(2)]
    f = one(Routing().detect(high, ctx(replayer=rep)), "default-effort")
    assert f.recoverable.nano == 1_600_000_000 and f.recoverable.upper_bound
    assert f.fix.config_patch == (("effortLevel", '"medium"'),)
    assert evidence(f, "default-effort:share")["share_pct"] == "100.0"
    medium = [_main("L-m", per_request={0: {"effort": "medium"}, 1: {"effort": "medium"}})]
    assert by_kind(Routing().detect(medium, ctx(replayer=rep)), "default-effort") == []
    no_params = ctx(replayer=rep, capabilities=CAPS - {"params"})
    assert by_kind(Routing().detect(high, no_params), "default-effort") == []
    sdk = [_main("L-s", product="agent_sdk", per_request={0: {"effort": "high"}})]
    assert by_kind(Routing().detect(sdk, ctx(replayer=rep)), "default-effort") == []


def test_effort_mix_info() -> None:
    lanes = [_main("L-a", per_request={0: {"effort": "high", "reasoning": 400},
                                       1: {"effort": "high", "reasoning": 600}}),
             _main("L-b", model=SONNET5, per_request={0: {"effort": "medium"}})]
    f = one(Routing().detect(lanes, ctx(thresholds={"min_usd": "0.01"})), "effort-mix")
    assert f.recoverable is None and f.cost_observed.evidence is Evidence.EXACT
    assert evidence(f, "effort:high")["requests"] == 2
    assert evidence(f, "effort:unset")["requests"] == 1
    assert evidence(f, "effort-mix:thinking")["thinking_share_pct"] == "50.0"
    assert f.lever_ids == ("cc.max_effort",)
    assert by_kind(Routing().detect([_main("L-n")], ctx(thresholds={"min_usd": "0"})),
                   "effort-mix") == []


# ---------------------------------------------------------------------------------------------
# rebaseline
# ---------------------------------------------------------------------------------------------

def _migration(old: str = OPUS48, new: str = OPUS5, out_before: int = 1_000,
               out_after: int = 1_300, days: int = 14, per_day: int = 10):
    lanes = []
    for day in range(2 * days):
        model, out = (old, out_before) if day < days else (new, out_after)
        rows = [(day * 86_400 + 3_600 + 30 * i, 0, 10_000, 0, 5, out) for i in range(per_day)]
        lanes.append(lane(f"L-day{day:02d}", rows, model=model))
    return lanes


def test_rebaseline_on_a_synthetic_migration() -> None:
    f = one(Routing().detect(_migration(), ctx()), "rebaseline")
    change = evidence(f, "rebaseline:change")
    assert (change["from_model"], change["to_model"], change["change_date"]) == \
        (OPUS48, OPUS5, "2026-10-07")
    tokens = evidence(f, "rebaseline:tokens")
    assert tokens["delta_output_per_request"] == "300.000000"
    assert tokens["output_ratio"] == "1.3000" and tokens["stale_prompts"] == "yes"
    assert (tokens["delta_output_pct"], tokens["delta_input_pct"]) == ("30.0", "0.0")
    assert Decimal(str(tokens["output_ratio"])) - 1 == Decimal("0.3")
    # Δ$ per request = 300 output tokens × 25,000 nano; × 140 requests after
    assert evidence(f, "rebaseline:cost")["delta_usd_per_request_nano"] == 7_500_000
    assert f.cost_observed.nano == 1_050_000_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.recoverable is None
    assert f.lever_class == "behavioral" and f.category == "attribution"
    assert "thinking is on by default on claude-opus-5" in f.fix.text
    assert "stale prompts" in f.fix.text and "tokenizer" not in f.fix.text


def test_rebaseline_prices_both_windows_at_current_rates() -> None:
    # the old model's price doubles after the window; at current rates (now = day 30) a before
    # request costs 2 × (10,000 × 6,250 + 5 × 5,000 + 1,000 × 25,000) = 175,050,000 and an after
    # request 10,000 × 6,250 + 5 × 5,000 + 1,300 × 25,000 = 95,025,000: Δ −80,025,000 per request
    # (at each request's own date the old model would look 7,500,000 cheaper instead)
    later = T0 + 29 * DAY_MS
    repriced = ScaledPricer(lambda c, ts: 2 if c.model == OPUS48 and ts >= later else 1)
    now = T0 + 30 * DAY_MS
    f = one(Routing().detect(_migration(), ctx(pricer=repriced, now_ms=now)), "rebaseline")
    cost = evidence(f, "rebaseline:cost")
    assert cost["delta_usd_per_request_nano"] == -80_025_000
    assert (cost["rates_at"], cost["priced_at_own_date"]) == ("2026-10-23", 0)
    assert f.cost_observed.nano == -80_025_000 * 140 and "current rates" in f.cost_observed.note
    # before the Opus 5 row takes effect the current card cannot price the new model: those
    # requests are priced at their own date (never as zero)
    early = T0 - 90 * DAY_MS
    f = one(Routing().detect(_migration(), ctx(now_ms=early)), "rebaseline")
    cost = evidence(f, "rebaseline:cost")
    assert cost["priced_at_own_date"] == 140 and cost["delta_usd_per_request_nano"] == 7_500_000


def test_rebaseline_tokenizer_note_and_signed_delta() -> None:
    f = one(Routing().detect(_migration(old=SONNET46, new=SONNET5, out_before=1_300,
                                        out_after=1_000), ctx(thresholds={"min_usd": "0.10"})),
            "rebaseline")
    assert "tokenizer change (claude-legacy -> claude-4.7+)" in f.fix.text
    assert evidence(f, "rebaseline:tokens")["stale_prompts"] == "no"
    assert f.cost_observed.nano < 0            # the new model is cheaper per request


def test_rebaseline_needs_a_move_and_enough_requests() -> None:
    assert by_kind(Routing().detect(_migration(new=OPUS48), ctx(thresholds={"min_usd": "0"})),
                   "rebaseline") == []
    assert by_kind(Routing().detect(_migration(per_day=3), ctx(thresholds={"min_usd": "0"})),
                   "rebaseline") == []
    lots = ctx(thresholds={"min_usd": "0", "model.routing.min_requests": "10"})
    assert one(Routing().detect(_migration(per_day=3), lots), "rebaseline")
    seat = [lane(ln.lane_key, [(r.ts_start_ms / 1000 - 1_790_121_600, 0, 10_000, 0, 5,
                                r.serving_inference.usage.output) for r in ln.requests],
                 model=ln.requests[0].model, billing_path="subscription")
            for ln in _migration()]
    f = one(Routing().detect(seat, ctx()), "rebaseline")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.title.startswith("Allowance")
