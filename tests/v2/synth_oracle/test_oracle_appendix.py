"""Acceptance: the oracle reproduces SPEC Appendix A.1–A.6, A.2b and the replay-related parts of
A.10 / A.11 to the nano, and satisfies the replayer conformance suite (SPEC §3.18, §9.1, §9.8)."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import FlatRates, make_ctx
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import FakePricer, assert_replayer_conforms
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import Policy
from tokenbill.synth.lanes_gen import CLOSED_FORMS, closed_form
from tokenbill.synth.oracle import ReferenceReplay, request_costs

from .helpers import ORACLE, PRICER, RULES, replay

REPLAY_KEYS = ("cost:", "saving:", "pings:", "added_calls:")


def _check_replay_keys(name: str) -> None:
    lanes, expected = closed_form(name)
    observed = replay(lanes, Policy.observed())
    if "baseline" in expected:
        assert observed.baseline.nano == expected["baseline"], name
        assert observed.cost.nano == expected["baseline"]
    for key, value in expected.items():
        if not key.startswith(REPLAY_KEYS):
            continue
        what, spec = key.split(":", 1)
        res = replay(lanes, spec)
        got = {"cost": res.cost.nano, "saving": res.saving.nano,
               "pings": res.keepalive_pings, "added_calls": res.added_calls}[what]
        assert got == value, f"{name} {key}: {got} != {value}"
        assert res.saving.nano == res.baseline.nano - res.cost.nano  # type: ignore[operator]


@pytest.mark.parametrize("name", ["A.1", "A.2", "A.2b", "A.3", "A.4", "A.4-20min", "A.4-2h",
                                  "A.4-claude-code", "A.5", "A.6"])
def test_closed_form_replays_match_the_oracle(name: str) -> None:
    _check_replay_keys(name)


def test_every_closed_form_is_covered() -> None:
    assert set(CLOSED_FORMS) == {"A.1", "A.2", "A.2b", "A.3", "A.4", "A.4-20min", "A.4-2h",
                                 "A.4-claude-code", "A.5", "A.6", "A.10", "A.11"}


def test_a1_ttl_1h_to_the_nano() -> None:
    lanes, _ = closed_form("A.1")
    res = replay(lanes, "ttl=1h")
    assert (res.baseline.nano, res.cost.nano, res.saving.nano) == \
        (2_100_000_000, 949_200_000, 1_150_800_000)
    # request 0 re-rated to 1h ($0.80 + $0.01); requests 1–3 read E and write the 2k appended
    assert [c[0] for c in request_costs(res).values()] == [810_000_000, 46_000_000,
                                                           46_400_000, 46_800_000]
    assert res.saving.evidence.value == "estimated"
    assert all(o.changed for o in res.outcomes or ())
    assert [o.usage.cache_read for o in res.outcomes or ()] == [0, 100_000, 102_000, 104_000]
    assert [o.usage.cache_write_1h for o in res.outcomes or ()] == [100_000, 2_000, 2_000, 2_000]
    transitions = classify_transitions(lanes[0], pricer=PRICER, rules=RULES)
    assert [(t.cause, t.ambiguous) for t in transitions] == [("ttl-expiry", False)] * 3


def test_a2_bursty_lane_prefers_5m_and_a2b_recommends_5m() -> None:
    a2, _ = closed_form("A.2")
    up = replay(a2, "ttl=1h")
    assert (up.baseline.nano, up.cost.nano, up.saving.nano) == \
        (631_200_000, 949_200_000, -318_000_000)
    same = replay(a2, "ttl=5m")          # status quo: nothing changes
    assert same.saving.nano == 0
    assert all(not o.changed for o in same.outcomes or ())
    a2b, _ = closed_form("A.2b")
    down = replay(a2b, "ttl=5m")
    assert (down.baseline.nano, down.cost.nano, down.saving.nano) == \
        (949_200_000, 631_200_000, 318_000_000)


def test_a3_1h_to_5m_turns_hits_into_misses() -> None:
    lanes, _ = closed_form("A.3")
    res = replay(lanes, "ttl=5m")
    assert res.cost.nano == 2_100_000_000
    assert [o.usage.cache_read for o in res.outcomes or ()] == [0, 0, 0, 0]
    assert [o.usage.cache_write_5m for o in res.outcomes or ()] == [100_000, 102_000, 104_000,
                                                                     106_000]


def test_a4_keepalive_pings_and_warmth() -> None:
    lanes, _ = closed_form("A.4")
    res = replay(lanes, "keepalive=240s,max=3600s")
    assert res.cost.nano == 692_400_000
    assert res.keepalive_pings == 3
    outs = res.outcomes or ()
    assert [len(o.extra) for o in outs] == [0, 1, 1, 1]
    assert [o.extra[0].usage.cache_read for o in outs[1:]] == [100_000, 102_000, 104_000]
    assert all(o.extra[0].kind.value == "keepalive" for o in outs[1:])
    assert any("hindsight minimum pings (lower bound): 3" in a for a in res.assumptions)
    warm, _ = closed_form("A.4-20min")
    assert replay(warm, "keepalive=240s,max=3600s").keepalive_pings == 4
    cold, _ = closed_form("A.4-2h")
    res_cold = replay(cold, "keepalive=240s,max=3600s")
    assert res_cold.keepalive_pings == 15
    assert res_cold.outcomes is not None and res_cold.outcomes[1].usage.cache_read == 0
    cc, _ = closed_form("A.4-claude-code")
    res_cc = replay(cc, "keepalive=240s,max=3600s")
    assert res_cc.lanes_skipped == (("A4-claude-code", "keepalive not allowed for claude_code"),)
    assert res_cc.keepalive_pings == 0 and res_cc.saving.nano == 0


def test_a5_cold_resume_quantities() -> None:
    lanes, expected = closed_form("A.5")
    lane = lanes[0]
    # exactly one event: the only transition that is not alive under the lane's 1h TTL with a
    # context above the cold-resume minimum
    transitions = classify_transitions(lane, pricer=PRICER, rules=RULES)
    events = [t for t in transitions if t.gap_ms > (t.ttl_s or 0) * 1000
              and t.total > 100_000]
    assert len(events) == expected["events"] == 1
    observed = request_costs(replay(lanes, Policy.observed()))
    event_req = lane.requests[2]
    ctx = event_req.serving_inference.pricing  # type: ignore[union-attr]
    ts = event_req.ts_start_ms
    write = PRICER.price_usage(UsageBuckets(cache_write_1h=500_000), ctx, ts_ms=ts).figure.nano
    read = PRICER.price_usage(UsageBuckets(cache_read=500_000), ctx, ts_ms=ts).figure.nano
    assert write == expected["cost_observed"] == 4_000_000_000
    assert write - read == expected["premium"] == 3_900_000_000   # type: ignore[operator]
    assert observed[event_req.request_id][0] == 4_020_000_000   # + 1k output
    compact = replay(lanes, "cold-resume=compact,min=200000")
    assert compact.cost.nano == expected["cost:cold-resume=compact,min=200000"]
    assert compact.added_calls == 1
    assert compact.saving.upper_bound
    other = [e for o in compact.outcomes or () for e in o.extra]
    assert [(e.kind.value, e.usage.uncached_input, e.usage.output) for e in other] == \
        [("other", 500_000, 20_283)]
    assert any("20283" in a for a in compact.assumptions)
    assert replay(lanes, "cold-resume=clear,min=200000").cost.nano == 8_280_000_000


def test_a6_compaction_window_costs_more_on_a_short_lane() -> None:
    lanes, _ = closed_form("A.6")
    res = replay(lanes, "compact-window=400000,post=20000")
    assert (res.baseline.nano, res.cost.nano, res.saving.nano) == \
        (1_430_000_000, 1_999_000_000, -569_000_000)
    assert [c[0] for c in request_costs(res).values()] == [760_000_000, 1_070_000_000,
                                                           169_000_000]
    outs = res.outcomes or ()
    comp = outs[1].extra[0]
    assert (comp.kind.value, comp.usage.cache_read, comp.usage.cache_write_5m,
            comp.usage.output) == ("compaction", 300_000, 150_000, 20_000)
    assert (outs[1].usage.cache_read, outs[1].usage.cache_write_5m) == (0, 170_000)
    assert (outs[2].usage.cache_read, outs[2].usage.cache_write_5m) == (170_000, 50_000)
    assert res.added_calls == 1 and res.saving.upper_bound
    wide = replay(lanes, "compact-window=600000,post=20000")
    assert wide.cost.nano == 1_430_000_000 and wide.added_calls == 0


def test_a10_edit_churn_replay_parts() -> None:
    lanes, expected = closed_form("A.10")
    lane = lanes[0]
    res = replay(lanes, Policy.observed())
    edit = lane.requests[expected["edit_request_seq"]]
    outcome = next(o for o in res.outcomes or () if o.request_id == edit.request_id)
    ctx, ts = edit.serving_inference.pricing, edit.ts_start_ms  # type: ignore[union-attr]
    rewritten = outcome.usage.cache_write_5m
    assert rewritten == 62_000
    write = PRICER.price_usage(UsageBuckets(cache_write_5m=rewritten), ctx, ts_ms=ts).figure.nano
    read = PRICER.price_usage(UsageBuckets(cache_read=rewritten), ctx, ts_ms=ts).figure.nano
    cleared = sum(n for att in edit.attempts for _t, n in att.applied_edits)
    remaining = len(lane.requests) - 1 - expected["edit_request_seq"]
    benefit = remaining * PRICER.price_usage(UsageBuckets(cache_read=cleared), ctx,
                                             ts_ms=ts).figure.nano  # type: ignore[operator]
    assert write == expected["cost_observed"]
    assert write - read == expected["rewrite_premium"]  # type: ignore[operator]
    assert remaining == expected["k_rem"] and benefit == expected["benefit"]
    assert write - read - benefit == expected["net_loss"]  # type: ignore[operator]
    # the edit request is a compaction-cause transition: no usage-level policy flips it
    ttl = replay(lanes, "ttl=1h")
    edit_out = next(o for o in ttl.outcomes or () if o.request_id == edit.request_id)
    assert (edit_out.usage.cache_read, edit_out.usage.cache_write_1h) == (20_000, 62_000)


def test_a11_truncation_replay_parts() -> None:
    lanes, expected = closed_form("A.11")
    lane = lanes[0]
    res = replay(lanes, Policy.observed())
    per_request = request_costs(res)
    truncated = [r for r in lane.requests if r.final_attempt.stop_reason == "max_tokens"]
    assert len(truncated) == expected["truncated"]
    costs = [per_request[r.request_id][0] for r in truncated]
    assert costs == [expected["per_attempt"]] * 4
    assert sum(costs) == expected["truncated_cost"]  # type: ignore[arg-type]
    followed = []
    for r in truncated:
        nxt = [q for q in lane.requests if 0 < q.ts_start_ms - r.ts_start_ms <= 120_000]
        if any(q.serving_inference.usage.total_input >= 47_500 for q in nxt):  # type: ignore[union-attr]
            followed.append(per_request[r.request_id][0])
    assert len(followed) == expected["followed"]
    assert sum(followed) == expected["recoverable_upper"]  # type: ignore[arg-type]


def test_oracle_satisfies_the_replayer_conformance_suite() -> None:
    summary = assert_replayer_conforms(ReferenceReplay(), FakePricer())
    assert summary["ttl_saving_nano"] > 0
    assert_replayer_conforms(ORACLE, FlatRates())


def test_flat_rates_prices_the_a1_lane_too() -> None:
    lanes, _ = closed_form("A.1")
    res = replay(lanes, "ttl=1h", pricer=FlatRates())
    # FlatRates: $1 input, 5m ×1.25, 1h ×2, read ×0.1, $5 output
    assert res.baseline.nano == 412_000 * 1_250 + 2_000 * 5_000
    assert res.cost.nano == (106_000 * 2_000 + 306_000 * 100 + 2_000 * 5_000)
    assert make_ctx().model == "claude-opus-5-5"
