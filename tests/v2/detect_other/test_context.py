"""``context.size-tax``, ``context.compaction-window``, ``context.static-prefix`` and
``attrib.carry`` on hand-computed fixtures (to the nano)."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import make_block, make_fingerprint
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import AppendedItem, LaneKind
from tokenbill.detect.context import Carry, CompactionWindow, SizeTax, StaticPrefix

from .helpers import (
    CAPS,
    HAIKU45,
    OPUS55,
    SONNET5,
    CountingReplayer,
    ctx,
    event,
    evidence,
    lane,
    one,
)

LOW = {"min_usd": "0.10"}

# ---------------------------------------------------------------------------------------------
# context.size-tax
# ---------------------------------------------------------------------------------------------

# Sonnet 5 main lane, 5m: T = 150k (W 150k), 250k (R 150k, W 100k), 450k (R 250k, W 200k).
SIZE_ROWS = [(0, 0, 150_000, 0, 0, 1000), (30, 150_000, 100_000, 0, 0, 1000),
             (60, 250_000, 200_000, 0, 0, 1000)]


def _size_lane(**kw):
    return lane("L-size", SIZE_ROWS, model=SONNET5, **kw)


def test_size_tax_exact_decomposition() -> None:
    # X = 200k: req1 writes above = min(100k, 250k − 200k) = 50k × 2,500 = 125,000,000;
    # req2 reads above 50k × 200 = 10,000,000 + writes min(200k, 250k) × 2,500 = 500,000,000.
    f = one(SizeTax().detect([_size_lane()], ctx(thresholds=LOW)), "context-tax")
    assert f.cost_observed.nano == 635_000_000
    assert f.cost_observed.evidence is Evidence.EXACT and f.cost_observed.basis is Basis.LIST
    assert f.recoverable is None and f.category == "attribution"
    # X = 100k: 125M + (10M + 250M) + (30M + 500M); X = 400k: req2 writes 50k × 2,500
    assert evidence(f, "size-tax:x100k")["nano"] == 915_000_000
    assert evidence(f, "size-tax:x200k")["nano"] == 635_000_000
    assert evidence(f, "size-tax:x400k")["nano"] == 125_000_000
    ctx_item = evidence(f, "size-tax:context")
    assert (ctx_item["p50_tokens"], ctx_item["p90_tokens"], ctx_item["max_tokens"]) == \
        (250_000, 450_000, 450_000)
    # spend 385M / 290M / 560M: calls and $ at >= 200k and >= 400k
    assert evidence(f, "size-tax:share-200k") == {"calls_pct": "66.7", "spend_pct": "68.8"}
    assert evidence(f, "size-tax:share-400k") == {"calls_pct": "33.3", "spend_pct": "45.3"}
    assert f.n_events == 2 and f.scope.dims == (("lane_kind", "main"), ("team", "payments"))
    assert f.lever_ids == ("cc.autocompact_window",)


def test_size_tax_threshold_override_and_min_usd() -> None:
    assert SizeTax().detect([_size_lane()], ctx()) == []          # $0.635 < $1.00
    f = one(SizeTax().detect([_size_lane()], ctx(thresholds={
        "min_usd": "0.10", "context.size-tax.threshold_tokens": "100000"})), "context-tax")
    assert f.cost_observed.nano == 915_000_000
    with pytest.raises(UsageError):
        SizeTax().detect([_size_lane()], ctx(thresholds={
            "context.size-tax.threshold_tokens": "-1"}))


def test_size_tax_allowance_cohort_is_list_equivalent() -> None:
    f = one(SizeTax().detect([_size_lane(billing_path="subscription")], ctx(thresholds=LOW)),
            "context-tax")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.cost_observed.nano == 635_000_000
    assert f.title.startswith("Allowance headroom:")
    assert f.summary.endswith("list-equivalent, not invoice dollars.")
    assert ("billing_class", "allowance") in f.scope.dims


def test_size_tax_unknown_ttl_writes_are_a_range() -> None:
    rows = [(0, 0, 0, 0, 0, 1000), (30, 0, 0, 0, 0, 1000)]
    ln = lane("L-u", rows, model=SONNET5, per_request={
        0: {"wu": 300_000}, 1: {"r": 300_000, "wu": 100_000}})
    f = one(SizeTax().detect([ln], ctx(thresholds=LOW)), "context-tax")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    # writes above 200k priced [5m, 1h]: req0 100k, req1 100k (and 100k reads above at 200)
    assert f.cost_observed.low_nano == 200_000 * 2_500 + 100_000 * 200
    assert f.cost_observed.high_nano == 200_000 * 4_000 + 100_000 * 200


def test_size_tax_small_contexts_and_unpriced_models() -> None:
    small = lane("L-small", [(0, 0, 50_000, 0, 0, 100), (30, 50_000, 1_000, 0, 0, 100)])
    assert SizeTax().detect([small], ctx(thresholds={"min_usd": "0"})) == []
    unknown = lane("L-x", SIZE_ROWS, model="claude-foo-9")
    assert SizeTax().detect([unknown], ctx(thresholds=LOW)) == []
    mixed = SizeTax().detect([_size_lane(), lane("L-x", SIZE_ROWS, model="claude-foo-9")],
                             ctx(thresholds=LOW))
    assert "had no priced rate" in one(mixed, "context-tax").summary


# ---------------------------------------------------------------------------------------------
# context.compaction-window
# ---------------------------------------------------------------------------------------------

def _window_lanes(model: str = OPUS55, top: int = 600_000, product: str = "claude_code"):
    rows = [(0, 0, 100_000, 0, 5, 500), (30, 100_000, top - 100_000, 0, 5, 500)]
    return [lane(f"L-cw{i}", rows, model=model, product=product, session_key=f"s_cw{i}")
            for i in range(2)]


SPECS = {w: f"compact-window={w}" for w in (200_000, 300_000, 400_000, 500_000, 700_000)}
# saving per lane (nano) and extra compactions per lane, by window
SAVINGS = {SPECS[200_000]: 2_500_000_000, SPECS[300_000]: 2_000_000_000,
           SPECS[400_000]: 1_500_000_000, SPECS[500_000]: 1_750_000_000,
           SPECS[700_000]: 500_000_000}
ADDED = {SPECS[200_000]: 5, SPECS[300_000]: 4, SPECS[400_000]: 2, SPECS[500_000]: 1,
         SPECS[700_000]: 0}


def test_compaction_window_curve_with_the_extra_compactions_guard() -> None:
    rep = CountingReplayer(SAVINGS, ADDED)
    f = one(CompactionWindow().detect(_window_lanes(), ctx(replayer=rep)), "compaction-window")
    # 200k is below min_window; 300k projects 4 extra compactions per session (> 3): the best
    # eligible window is 500k (2 lanes × $1.75)
    assert f.recoverable is not None and f.recoverable.nano == 3_500_000_000
    assert f.recoverable.evidence is Evidence.ESTIMATED and f.recoverable.upper_bound
    assert f.needs_eval and f.lever_class == "trajectory"
    assert evidence(f, "compaction-window:300k")["eligible"] == "no"
    assert evidence(f, "compaction-window:300k")["per_session"] == "4.00"
    assert evidence(f, "compaction-window:500k")["recommended"] == "yes"
    assert evidence(f, "compaction-window:400k")["saving_nano"] == 3_000_000_000
    assert [k for k, _ in f.fix.config_patch] == ["autoCompactWindow",
                                                  "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
    assert dict(f.fix.config_patch)["env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == '"500000"'
    assert f.lever_ids == ("cc.autocompact_window",)
    # cost_observed: the replayed lanes' observed spend (EXACT)
    assert f.cost_observed.evidence is Evidence.EXACT and f.cost_observed.nano > 0


def test_compaction_window_thresholds_move_the_choice() -> None:
    rep = CountingReplayer(SAVINGS, ADDED)
    relaxed = ctx(replayer=rep,
                  thresholds={"context.compaction-window.max_extra_compactions": "5"})
    f = one(CompactionWindow().detect(_window_lanes(), relaxed), "compaction-window")
    assert f.recoverable.nano == 4_000_000_000          # 300k
    lower = ctx(replayer=rep, thresholds={"context.compaction-window.max_extra_compactions": "5",
                                          "context.compaction-window.min_window": "200000"})
    f = one(CompactionWindow().detect(_window_lanes(), lower), "compaction-window")
    assert f.recoverable.nano == 5_000_000_000          # 200k
    rep_post = CountingReplayer({f"{k},post=18000": v for k, v in SAVINGS.items()},
                                {f"{k},post=18000": v for k, v in ADDED.items()})
    post = ctx(replayer=rep_post, thresholds={"context.compaction-window.post_tokens": "18000"})
    f = one(CompactionWindow().detect(_window_lanes(), post), "compaction-window")
    assert f.recoverable.nano == 3_500_000_000
    assert evidence(f, "compaction-window:guard")["summary_tokens"] == 18_000


def test_compaction_window_eligibility() -> None:
    rep = CountingReplayer(SAVINGS, ADDED)
    assert CompactionWindow().detect(_window_lanes(), ctx()) == []           # no replayer
    assert CompactionWindow().detect(_window_lanes(top=190_000), ctx(replayer=rep)) == []
    # Haiku 4.5 has no 1M context window
    assert CompactionWindow().detect(_window_lanes(model=HAIKU45), ctx(replayer=rep)) == []
    sub = [lane("L-sub", [(0, 0, 300_000, 0, 0, 500)], kind=LaneKind.SUBAGENT)]
    assert CompactionWindow().detect(sub, ctx(replayer=rep)) == []
    nothing = CountingReplayer({}, {})
    assert CompactionWindow().detect(_window_lanes(), ctx(replayer=nothing)) == []
    sdk = one(CompactionWindow().detect(_window_lanes(product="agent_sdk"), ctx(replayer=rep)),
              "compaction-window")
    assert sdk.fix.config_patch is None and sdk.fix.target == "code"


# ---------------------------------------------------------------------------------------------
# context.static-prefix
# ---------------------------------------------------------------------------------------------

STATIC_ROWS = [(0, 0, 30_000, 0, 0, 500), (30, 30_000, 2_000, 0, 0, 500),
               (430, 0, 34_000, 0, 0, 500)]


def test_static_prefix_harness_cost_with_s_known() -> None:
    ln = lane("L-static", STATIC_ROWS)
    floor = {("ws:w1", OPUS55): 10_000}
    f = one(StaticPrefix().detect([ln], ctx(static_prefix_floor=floor,
                                            thresholds={"min_usd": "0.01"})), "static-prefix")
    # lane-first: min(S, W0) = 10k × 5,000; req1: min(S, R1) = 10k × 200; req2 (TTL-expiry miss):
    # min(S, W2) = 10k × 5,000
    assert f.cost_observed.nano == 50_000_000 + 2_000_000 + 50_000_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.recoverable is None
    assert dict(f.scope.dims)["model"] == OPUS55 and dict(f.scope.dims)["cache_scope"] == "ws:w1"
    assert evidence(f, "static-prefix:floor")["tokens"] == 10_000
    assert f.lever_ids == ("cc.tool_search",)


def test_static_prefix_absent_without_s() -> None:
    ln = lane("L-static", STATIC_ROWS)
    assert StaticPrefix().detect([ln], ctx(thresholds={"min_usd": "0"})) == []
    other = {("ws:other", OPUS55): 10_000}
    assert StaticPrefix().detect([ln], ctx(static_prefix_floor=other,
                                           thresholds={"min_usd": "0"})) == []


def _fp(*, deferred: bool = False, per_tool: int = 700):
    tools = [make_block(f"t{i}", tier="tools", kind="tool_def", role=None, est_tokens=per_tool,
                        deferred=deferred and i == 0) for i in range(20)]
    rest = [make_block("sys", tier="system", kind="system_text", role=None, est_tokens=3_000),
            make_block("m0", est_tokens=3_000)]
    return make_fingerprint([*tools, *rest])


def _bloat_lane(fp, *, product: str = "agent_sdk", reads: int = 30_000):
    rows = [(i * 30, reads, 10_000, 0, 0, 500) for i in range(5)]
    return lane("L-tools", rows, kind=LaneKind.API_RUN, product=product,
                per_request={i: {"fingerprint": fp} for i in range(5)})


def test_tool_defs_bloat_range_on_14k_of_non_deferred_tools() -> None:
    # 14,000 of 20,000 est tokens → 28,000 of the 40,000 billed input tokens per request; billed
    # read/write mix (30k × 200 + 10k × 5,000) / 40k = 1,400 nano per token → 39.2M per request
    f = one(StaticPrefix().detect([_bloat_lane(_fp())], ctx(thresholds={"min_usd": "0.01"})),
            "tool-defs-bloat")
    assert f.cost_observed.nano == 5 * 39_200_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    rec = f.recoverable
    assert rec is not None and rec.upper_bound and rec.evidence is Evidence.ESTIMATED
    assert (rec.low_nano, rec.nano, rec.high_nano) == (98_000_000, 137_200_000, 166_600_000)
    assert f.lever_ids == ("sdk.defer_loading",) and f.fix.target == "sdk"
    assert evidence(f, "tool-defs-bloat:requests")["mean_tool_def_tokens"] == 28_000


def test_tool_defs_bloat_none_when_tools_are_deferred_or_small() -> None:
    low = ctx(thresholds={"min_usd": "0"})
    assert StaticPrefix().detect([_bloat_lane(_fp(deferred=True))], low) == []
    # 20 × 400 = 8,000 of 14,000 est tokens → 22,857 billed tokens (> 10k): still bloat …
    assert StaticPrefix().detect([_bloat_lane(_fp(per_tool=400))], low)
    # … but 20 × 100 = 2,000 of 8,000 → 10,000 billed tokens, not above 10k
    assert StaticPrefix().detect([_bloat_lane(_fp(per_tool=100))], low) == []


def test_tool_defs_bloat_claude_code_patch_and_capability_note() -> None:
    f = one(StaticPrefix().detect([_bloat_lane(_fp(), product="claude_code")],
                                  ctx(thresholds={"min_usd": "0.01"})), "tool-defs-bloat")
    assert f.fix.config_patch == (("env.ENABLE_TOOL_SEARCH", '"true"'),)
    assert "cc.tool_search" in f.lever_ids
    notes = StaticPrefix().detect([], ctx(capabilities=CAPS - {"blocks"}))
    assert [(n.kind, n.category, n.scope.dims) for n in notes] == [
        ("missing-capabilities", "data-quality", (("kind", "tool-defs-bloat"),))]
    assert StaticPrefix().detect([], ctx()) == []


# ---------------------------------------------------------------------------------------------
# attrib.carry
# ---------------------------------------------------------------------------------------------

def _carry_lane(events=()):
    rows = [(0, 0, 20_000, 0, 0, 500), (30, 20_000, 3_000, 0, 0, 500),
            (60, 23_000, 3_000, 0, 0, 500), (90, 26_000, 3_000, 0, 0, 500)]
    return lane("L-carry", rows, events=events, per_request={
        1: {"appended": [AppendedItem(kind="tool_result", name="Read", n_bytes=5_000)]},
        2: {"appended": [AppendedItem(kind="tool_result", name="Bash", n_bytes=1_001),
                         AppendedItem(kind="user_text", name=None, n_bytes=90)]}})


def test_tool_output_carry_with_the_published_bytes_per_token() -> None:
    # fewer than 30 samples → 2.5 bytes/token (claude-4.7+): Read 2,000 tokens written at 5m
    # (10,000,000) + read on 2 later requests (800,000); Bash ceil(1,001/2.5) = 401 tokens
    # (2,005,000) + 1 later request (80,200)
    f = one(Carry().detect([_carry_lane()], ctx(thresholds={"min_usd": "0.001"})),
            "tool-output-carry")
    assert f.cost_observed.nano == 10_800_000 + 2_085_200
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.recoverable is None
    assert evidence(f, "tool:Read")["tokens"] == 2_000
    assert evidence(f, "tool:Bash")["nano"] == 2_085_200
    assert "claude-4.7+ 2.50 (default)" in str(evidence(f, "carry:distribution")["bytes_per_token"])
    assert evidence(f, "carry:distribution")["top_5pct_share_pct"] == "83.8"


def test_tool_output_carry_stops_at_a_reset() -> None:
    clear = event("L-carry", 70, "clear")      # between request 2 (60 s) and 3 (90 s)
    f = one(Carry().detect([_carry_lane([clear])], ctx(thresholds={"min_usd": "0.001"})),
            "tool-output-carry")
    # Read: 1 later request before the clear; Bash: none
    assert f.cost_observed.nano == 10_000_000 + 400_000 + 2_005_000


def test_cpt_fit_with_30_samples() -> None:
    # 31 requests each adding 1,000 tokens with a 3,000-byte tool result → cpt fit 3.0
    rows = [(i * 10, 1_000 * i, 1_000, 0, 0, 0) for i in range(32)]
    rows[0] = (0, 0, 1_000, 0, 0, 0)
    appended = [AppendedItem(kind="tool_result", name="Read", n_bytes=3_000)]
    ln = lane("L-fit", rows, per_request={i: {"appended": appended} for i in range(1, 32)})
    f = one(Carry().detect([ln], ctx(thresholds={"min_usd": "0.001"})), "tool-output-carry")
    assert "claude-4.7+ 3.00 (fit)" in str(evidence(f, "carry:distribution")["bytes_per_token"])
    assert evidence(f, "tool:Read")["tokens"] == 31 * 1_000


def test_config_tax_first_listing_only() -> None:
    evs = [event("L-carry", -5, "context_injection", att_type="claude_md", n_bytes=2_500),
           event("L-carry", 40, "context_injection", att_type="claude_md", n_bytes=9_999),
           event("L-carry", 45, "context_injection", att_type="skill_listing", n_bytes=500)]
    f = one(Carry().detect([_carry_lane(evs)], ctx(thresholds={"min_usd": "0.001"})),
            "config-tax")
    # claude_md: 1,000 tokens on request 0 (5,000,000) + 3 later reads (600,000); the second
    # claude_md listing is ignored; skill_listing: 200 tokens on request 2 (1,000,000 + 40,000)
    assert f.cost_observed.nano == 5_600_000 + 1_040_000
    assert evidence(f, "type:claude_md")["items"] == 1
    no_events = Carry().detect([_carry_lane(evs)], ctx(thresholds={"min_usd": "0.001"},
                                                        capabilities=CAPS - {"events"}))
    assert [f.kind for f in no_events] == ["tool-output-carry"]


def test_carry_allowance_and_unpriced() -> None:
    sub = lane("L-carry", [(0, 0, 20_000, 0, 0, 500), (30, 20_000, 3_000, 0, 0, 500)],
               billing_path="subscription", per_request={
                   1: {"appended": [AppendedItem(kind="tool_result", name="Read",
                                                 n_bytes=5_000)]}})
    f = one(Carry().detect([sub], ctx(thresholds={"min_usd": "0.001"})), "tool-output-carry")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")
    unknown = lane("L-u", [(0, 0, 20_000, 0, 0, 500), (30, 20_000, 3_000, 0, 0, 500)],
                   model="claude-foo-9", per_request={
                       1: {"appended": [AppendedItem(kind="tool_result", name="Read",
                                                     n_bytes=5_000)]}})
    assert Carry().detect([unknown], ctx(thresholds={"min_usd": "0"})) == []


def test_carry_notes_without_lanes() -> None:
    notes = Carry().detect([], ctx(capabilities=frozenset({"usage_sequence", "appended"})))
    assert [(n.kind, dict(n.scope.dims)["kind"]) for n in notes] == [
        ("missing-capabilities", "config-tax")]
    assert notes[0].cost_observed.nano is None and notes[0].recoverable is None
