"""``failure.path``: every sub-kind to the nano — Appendix A.11 truncation ($0.71376 EXACT,
$0.53532 ESTIMATED upper bound), cold retries (attempt and event paths), retry storms including
SDK attempts seen by the recorder's hooks, never-succeeding 400s and tool-error loops."""

from __future__ import annotations

from tokenbill.core.builders import make_lane
from tokenbill.core.labels import Evidence
from tokenbill.core.records import AppendedItem
from tokenbill.detect.failure import FailurePath

from .helpers import (
    CAPS,
    SONNET5,
    attempt,
    by_kind,
    ctx,
    event,
    evidence,
    lane,
    multi_request,
    one,
    request,
    table_replayer,
    usage,
)

ZERO = {"min_usd": "0"}

# ---------------------------------------------------------------------------------------------
# max-tokens-truncation (Appendix A.11)
# ---------------------------------------------------------------------------------------------


def _a11_lane():
    trunc = {"stop_reason": "max_tokens", "max_tokens": 16_384}
    retry = {"r": 50_000, "w5": 0, "o": 500}
    rows = [(0, 48_000, 2_000, 0, 0, 16_384), (60, 0, 0, 0, 0, 0),
            (200, 48_000, 2_000, 0, 0, 16_384), (260, 0, 0, 0, 0, 0),
            (400, 48_000, 2_000, 0, 0, 16_384), (460, 0, 0, 0, 0, 0),
            (600, 48_000, 2_000, 0, 0, 16_384), (800, 50_000, 0, 0, 0, 500)]
    per = {0: trunc, 1: retry, 2: trunc, 3: retry, 4: trunc, 5: retry, 6: trunc}
    return lane("L-a11", rows, model=SONNET5, product="agent_sdk", per_request=per)


def test_a11_truncation_exact_cost_and_upper_bound() -> None:
    # each truncated attempt: 48,000 × $0.20/M + 2,000 × $2.50/M + 16,384 × $10/M = $0.17844
    f = one(FailurePath().detect([_a11_lane()], ctx(thresholds={"min_usd": "0.10"})),
            "max-tokens-truncation")
    assert f.cost_observed.nano == 713_760_000 and f.cost_observed.evidence is Evidence.EXACT
    # three are followed within 120 s by T ≥ 0.95 × 50,000; the fourth (200 s later) is not
    rec = f.recoverable
    assert rec is not None and rec.nano == 535_320_000
    assert rec.evidence is Evidence.ESTIMATED and rec.upper_bound
    assert (f.category, f.lever_class, f.n_events) == ("failure", "hygiene", 4)
    rule = evidence(f, "truncation:rule")
    assert (rule["anthropic_max_tokens"], rule["retried"], rule["max_tokens"]) == (4, 3, 16_384)
    assert "anthropic.max_tokens" in f.summary and "64,000" in f.fix.text


def test_truncation_needs_three_and_obeys_thresholds() -> None:
    two = make_lane(list(_a11_lane().requests[:4]), lane_key="L-a11", session_key="s_L-a11")
    assert by_kind(FailurePath().detect([two], ctx(thresholds=ZERO)), "max-tokens-truncation") \
        == []
    lowered = ctx(thresholds={"min_usd": "0", "failure.path.min_truncations": "2"})
    assert one(FailurePath().detect([two], lowered), "max-tokens-truncation")
    tight = ctx(thresholds={"min_usd": "0.10", "failure.path.retry_window_s": "30"})
    f = one(FailurePath().detect([_a11_lane()], tight), "max-tokens-truncation")
    assert f.recoverable is None and f.cost_observed.nano == 713_760_000


# ---------------------------------------------------------------------------------------------
# cold-retry
# ---------------------------------------------------------------------------------------------

def _retry_lane():
    first = request("L-cr", 0, 0, w5=100_000, o=500, attr=None)
    failed = attempt(100, outcome="http_error", http_status=529, error_type="overloaded",
                     retry_layer="agent")
    ok = attempt(500, usage=usage(w5=102_000, o=500), n=1)
    return make_lane([first, multi_request("L-cr", 1, [failed, ok])], lane_key="L-cr",
                     session_key="s_L-cr")


def test_cold_retry_attempt_path_with_the_repair_replay() -> None:
    rep = table_replayer({"repair=retry_backoff_cap": 470_000_000})
    f = one(FailurePath().detect([_retry_lane()], ctx(replayer=rep,
                                                      thresholds={"min_usd": "0.10"})),
            "cold-retry")
    # the success started 400 s after the failed attempt (> τ = 300 s) and rewrote
    # min(W, E) = 100,000 tokens at $5/M; the failed attempt was not billed
    assert f.cost_observed.nano == 500_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable.nano == 470_000_000
    assert f.lever_ids == ("retry.single_owner",) and f.lever_class == "cache_transform"
    assert [e.kind for e in f.evidence] == ["attempt_chain"]


def test_cold_retry_without_a_replayer_uses_the_triage_premium() -> None:
    f = one(FailurePath().detect([_retry_lane()], ctx(thresholds={"min_usd": "0.10"})),
            "cold-retry")
    # 100,000 × ($5.00 − $0.20)/M
    assert f.recoverable.nano == 480_000_000 and f.recoverable.upper_bound


def test_cold_retry_event_path() -> None:
    rows = [(0, 0, 100_000, 0, 0, 500), (400, 0, 102_000, 0, 0, 500)]
    err = event("L-ev", 30, "api_error", status=529, error_type="overloaded", retry_attempt=1)
    ln = lane("L-ev", rows, events=[err])
    f = one(FailurePath().detect([ln], ctx(thresholds={"min_usd": "0.10"})), "cold-retry")
    assert f.cost_observed.nano == 500_000_000 and f.recoverable.nano == 480_000_000
    assert [e.kind for e in f.evidence] == ["event"]
    quiet = lane("L-q", rows)
    assert by_kind(FailurePath().detect([quiet], ctx(thresholds=ZERO)), "cold-retry") == []


# ---------------------------------------------------------------------------------------------
# retry-storm
# ---------------------------------------------------------------------------------------------

def _storm_lane(attempts):
    return make_lane([multi_request("L-st", 0, attempts)], lane_key="L-st", session_key="s_L-st")


def test_retry_storm_counts_sdk_attempts_from_recorder_hooks() -> None:
    # the recorder's HTTP hooks record SDK-internal retries as attempts (retry_layer "sdk")
    atts = [attempt(i * 10, n=i, outcome="timeout", retry_layer="sdk",
                    usage=usage(u=10_000, o=200), billable=None, usage_source="partial_stream")
            for i in range(4)]
    atts.append(attempt(50, n=4, usage=usage(u=10_000, o=200)))
    f = one(FailurePath().detect([_storm_lane(atts)], ctx(thresholds={"min_usd": "0.01"})),
            "retry-storm")
    # the four retried attempts are a billing range [0, full]: point 4 × (10,000 × 4,000 +
    # 200 × 20,000) = 176,000,000
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert (f.cost_observed.nano, f.cost_observed.low_nano) == (176_000_000, 0)
    assert f.recoverable is None and "retry.single_owner" in f.lever_ids
    assert evidence(f, "retry-storm:layers")["layer_sdk"] == 1


def test_retry_storm_layers_and_retry_count_header() -> None:
    layered = [attempt(0, outcome="http_error", error_type="rate_limit", retry_layer="sdk"),
               attempt(5, n=1, outcome="http_error", error_type="rate_limit",
                       retry_layer="gateway"),
               attempt(9, n=2, usage=usage(u=1_000))]
    assert one(FailurePath().detect([_storm_lane(layered)], ctx(thresholds=ZERO)), "retry-storm")
    header = [attempt(0, usage=usage(u=1_000), sdk_retry_count=4)]
    assert one(FailurePath().detect([_storm_lane(header)], ctx(thresholds=ZERO)), "retry-storm")
    calm = [attempt(0, outcome="http_error", error_type="rate_limit"),
            attempt(9, n=1, usage=usage(u=1_000))]
    assert by_kind(FailurePath().detect([_storm_lane(calm)], ctx(thresholds=ZERO)),
                   "retry-storm") == []
    no_attempts = ctx(thresholds=ZERO, capabilities=CAPS - {"attempts"})
    assert by_kind(FailurePath().detect([_storm_lane(layered)], no_attempts), "retry-storm") == []


# ---------------------------------------------------------------------------------------------
# never-succeeding-400
# ---------------------------------------------------------------------------------------------

def test_never_succeeding_400_attempts_and_events() -> None:
    same = [attempt(0, outcome="http_error", http_status=400, error_type="prompt_too_long"),
            attempt(5, n=1, outcome="http_error", http_status=400,
                    error_type="prompt_too_long")]
    f = one(FailurePath().detect([_storm_lane(same)], ctx(thresholds=ZERO)),
            "never-succeeding-400")
    assert f.cost_observed.nano == 0 and evidence(f, "never-succeeding:reasons")["same_error"] \
        == 1
    against = [attempt(0, outcome="http_error", http_status=500, error_type="api_error",
                       should_retry=False), attempt(5, n=1, usage=usage(u=1_000))]
    assert evidence(one(FailurePath().detect([_storm_lane(against)], ctx(thresholds=ZERO)),
                        "never-succeeding-400"), "never-succeeding:reasons")[
        "should_retry_false"] == 1
    cap = [attempt(0, outcome="http_error", http_status=429, error_type="spend_cap"),
           attempt(5, n=1, outcome="http_error", http_status=429, error_type="spend_cap")]
    assert one(FailurePath().detect([_storm_lane(cap)], ctx(thresholds=ZERO)),
               "never-succeeding-400")
    evs = [event("L-e", 10, "api_error", status=400, error_type="thinking_binding"),
           event("L-e", 20, "api_error", status=400, error_type="thinking_binding")]
    ln = lane("L-e", [(0, 0, 10_000, 0, 0, 100)], events=evs)
    f = one(FailurePath().detect([ln], ctx(thresholds=ZERO)), "never-succeeding-400")
    assert evidence(f, "never-succeeding:reasons")["events"] == 1
    # a success between the two errors breaks the run
    ok_between = lane("L-e", [(0, 0, 10_000, 0, 0, 100), (15, 10_000, 100, 0, 0, 100)],
                      events=evs)
    assert by_kind(FailurePath().detect([ok_between], ctx(thresholds=ZERO)),
                   "never-succeeding-400") == []


# ---------------------------------------------------------------------------------------------
# tool-error-loop
# ---------------------------------------------------------------------------------------------

def test_tool_error_loop_three_consecutive() -> None:
    bad = [AppendedItem(kind="tool_result", name="Bash", n_bytes=300, is_error=True)]
    good = [AppendedItem(kind="tool_result", name="Bash", n_bytes=300)]
    rows = [(i * 10, 10_000 * i, 10_000, 0, 0, 200) for i in range(6)]
    per = {1: {"appended": bad}, 2: {"appended": bad}, 3: {"appended": bad},
           4: {"appended": good}, 5: {"appended": bad}}
    f = one(FailurePath().detect([lane("L-loop", rows, per_request=per)],
                                 ctx(thresholds={"min_usd": "0.01"})), "tool-error-loop")
    # requests 1-3: reads 10k/20k/30k × 200, writes 10k × 5,000, output 200 × 20,000 each
    assert f.cost_observed.nano == (60_000 * 200) + 3 * (50_000_000 + 4_000_000)
    assert f.n_events == 1 and f.recoverable is None
    no_appended = ctx(thresholds=ZERO, capabilities=CAPS - {"appended"})
    assert by_kind(FailurePath().detect([lane("L-loop", rows, per_request=per)], no_appended),
                   "tool-error-loop") == []


def test_failure_capability_notes() -> None:
    notes = FailurePath().detect([], ctx(capabilities=frozenset({"usage_sequence", "timing"})))
    assert sorted(dict(n.scope.dims)["kind"] for n in notes) == ["retry-storm", "tool-error-loop"]
    assert all(n.category == "data-quality" for n in notes)
