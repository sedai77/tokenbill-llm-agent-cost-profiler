"""``cache.switch-churn``: every sub-kind hand-computed to the nano (including plan-toggle), the
replayed repairs of refusal fallbacks and fast-mode toggles, credited fallbacks, and the D28
effort exemption."""

from __future__ import annotations

from tokenbill.core.builders import make_lane
from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind, RequestParams
from tokenbill.detect.cache_miss import SwitchChurn

from .helpers import (
    FABLE5,
    OPUS48,
    OPUS55,
    SONNET5,
    attribution,
    ctx,
    event,
    fallback_request,
    lane,
    only,
    request,
    table_replayer,
)

MIN_10C = {"min_usd": "0.10"}


def test_refusal_fallback_without_credit_replays_fallback_credit() -> None:
    """Fable 5 wrote 100k; its refusal falls back to Opus 4.8, which rewrites the prefix:
    E = min(100k, 102k) = 100k at Opus 4.8's 5m write rate (6,250 nano) = $0.625."""
    r0 = request("F", 0, 0, w5=100_000, model=FABLE5, session_key="s_F")
    r1 = fallback_request("F", 1, 30, declined_model=FABLE5, w5=102_000, model=OPUS48)
    lane_ = make_lane([r0, r1], lane_key="F", session_key="s_F")
    replayer = table_replayer({("F", "repair=fallback_credit"): 600_000_000})
    f = only(SwitchChurn().detect([lane_], ctx(replayer=replayer, thresholds=MIN_10C)),
             "refusal-fallback-no-credit")
    assert f.cost_observed.nano == 625_000_000
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable is not None and f.recoverable.nano == 600_000_000
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.lever_ids == ("fallback.credit",)
    assert f.fix is not None and "fallback-credit" in f.fix.text


def test_credited_fallback_is_not_churn() -> None:
    r0 = request("C", 0, 0, w5=100_000, model=FABLE5, session_key="s_C")
    r1 = fallback_request("C", 1, 30, declined_model=FABLE5, w5=102_000, model=OPUS48)
    ev = event("C", 29, "model_fallback", from_model=FABLE5, to_model=OPUS48, trigger="refusal",
               credited=True)
    lane_ = make_lane([r0, r1], lane_key="C", session_key="s_C", events=[ev])
    assert SwitchChurn().detect([lane_], ctx(thresholds={"min_usd": "0"})) == []


def test_availability_ping_pong() -> None:
    """An overloaded error and an availability fallback: Opus 5.5 → Sonnet 5 rewrites 100k at
    Sonnet's 2,500 nano."""
    key = "AV"
    evs = [event(key, 10, "api_error", status=529, error_type="overloaded", retry_attempt=1,
                 max_retries=3, retry_in_ms=1000),
           event(key, 20, "model_fallback", from_model=OPUS55, to_model=SONNET5,
                 trigger="availability", credited=None)]
    lane_ = lane(key, [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)], events=evs,
                 per_request={1: {"model": SONNET5}})
    f = only(SwitchChurn().detect([lane_], ctx(thresholds=MIN_10C)), "availability-ping-pong")
    assert f.cost_observed.nano == 250_000_000
    assert f.recoverable is None                  # no mechanical repair: behavioral
    assert "trade-off" in f.summary


def test_ping_pong_without_fallback_events() -> None:
    """A → B → A inside the TTL on an SDK lane: the second switch is ping-pong."""
    lane_ = lane("PP", [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500),
                        (60, 0, 104_000, 0, 0, 500)], kind=LaneKind.API_RUN, product="agent_sdk",
                 per_request={1: {"model": SONNET5}})
    findings = SwitchChurn().detect([lane_], ctx(thresholds={"min_usd": "0"}))
    user = only(findings, "user-model-switch")
    pong = only(findings, "availability-ping-pong")
    assert user.cost_observed.nano == 100_000 * 2_500        # Sonnet 5 rewrites 100k
    assert pong.cost_observed.nano == 102_000 * 5_000        # Opus 5.5 rewrites 102k


def test_plan_toggle_opusplan() -> None:
    """A Claude Code main lane alternating Opus ↔ Sonnet on human prompts (likely opusplan):
    Sonnet rewrites 100k (2,500) and Opus 5.5 rewrites 102k (5,000)."""
    key = "PT"
    evs = [event(key, 25, "human_prompt"), event(key, 55, "human_prompt")]
    lane_ = lane(key, [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500),
                       (60, 0, 104_000, 0, 0, 500)], events=evs,
                 per_request={1: {"model": SONNET5}})
    f = only(SwitchChurn().detect([lane_], ctx(thresholds=MIN_10C)), "plan-toggle")
    assert f.n_events == 2
    assert f.cost_observed.nano == 100_000 * 2_500 + 102_000 * 5_000
    assert f.recoverable is None
    assert f.fix is not None and "opusplan switches models on every plan-mode toggle" in f.fix.text


def test_user_model_switch() -> None:
    lane_ = lane("US", [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)],
                 per_request={1: {"model": SONNET5}})
    f = only(SwitchChurn().detect([lane_], ctx(thresholds=MIN_10C)), "user-model-switch")
    assert f.cost_observed.nano == 250_000_000
    assert f.recoverable is None
    assert "/clear" in (f.fix.text if f.fix else "")


def test_fast_toggle_replays_fast_off_and_patches_managed_settings() -> None:
    """Fast mode on request 1: 100k rewritten at Opus 5.5 fast 5m write (10,000 nano)."""
    lane_ = lane("FT", [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)],
                 per_request={1: {"speed": "fast"}})
    replayer = table_replayer({("FT", "fast=off"): 1_500_000_000})
    f = only(SwitchChurn().detect([lane_], ctx(replayer=replayer, thresholds=MIN_10C)),
             "fast-toggle")
    assert f.cost_observed.nano == 1_000_000_000
    assert f.recoverable is not None and f.recoverable.nano == 1_500_000_000
    assert f.fix is not None and f.fix.config_patch == (("fastModePerSessionOptIn", "true"),)
    assert f.fix.target == "claude-code-managed-settings"
    assert f.lever_ids == ("cc.fast_mode_opt_in",)


def test_fast_toggle_without_replayer_gates_on_cost() -> None:
    lane_ = lane("FT2", [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)],
                 per_request={1: {"speed": "fast"}}, product="agent_sdk", kind=LaneKind.API_RUN)
    f = only(SwitchChurn().detect([lane_], ctx(thresholds=MIN_10C)), "fast-toggle")
    assert f.recoverable is None and f.fix is not None and f.fix.config_patch is None


def _effort_lane(key: str, product: str, kind: LaneKind, betas: tuple[str, ...] = ()):
    p0 = RequestParams(model_requested=OPUS55, effort="high", betas=betas)
    p1 = RequestParams(model_requested=OPUS55, effort="low", betas=betas)
    return lane(key, [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)], kind=kind,
                product=product, per_request={0: {"params": p0}, 1: {"params": p1}})


def test_effort_change_only_where_it_invalidates_the_cache() -> None:
    """D28: Claude Code on Opus 5.5 (≥ 2.1.260, first-party) keeps the cache; an SDK lane
    without the per-message effort beta does not; with the beta it does."""
    cc = _effort_lane("EC", "claude_code", LaneKind.MAIN)
    assert SwitchChurn().detect([cc], ctx(thresholds=MIN_10C)) == []
    sdk = _effort_lane("ES", "agent_sdk", LaneKind.API_RUN)
    f = only(SwitchChurn().detect([sdk], ctx(thresholds=MIN_10C)), "effort-change")
    assert f.cost_observed.nano == 500_000_000
    assert f.recoverable is None
    assert f.fix is not None and "mid-conversation-output-config-2026-07-01" in f.fix.text
    beta = _effort_lane("EB", "agent_sdk", LaneKind.API_RUN,
                        ("mid-conversation-output-config-2026-07-01",))
    assert SwitchChurn().detect([beta], ctx(thresholds=MIN_10C)) == []


def test_client_upgrade_is_not_switch_churn() -> None:
    lane_ = lane("CU", [(0, 0, 100_000, 0, 0, 500), (30, 0, 102_000, 0, 0, 500)],
                 per_request={1: {"attr": attribution(client_version="2.1.271")}})
    assert SwitchChurn().detect([lane_], ctx(thresholds={"min_usd": "0"})) == []
