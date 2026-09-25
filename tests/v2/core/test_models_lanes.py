"""SPEC §3.12 model-id normalization and §3.13 lane assembly."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import (
    lane_from_table,
    make_attempt,
    make_inference,
    make_lane,
    make_request,
    make_usage,
)
from tokenbill.core.lanes import group_lanes, ttl_of_last_write
from tokenbill.core.models import CONFIG_ALIASES, ModelId, normalize_model
from tokenbill.core.records import Attribution, LaneEvent, LaneKind, Session

# ---------- models ----------


@pytest.mark.parametrize(
    ("raw", "model", "hint", "scope"),
    [
        # SPEC §6.9 case 16
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5", None, "unknown"),
        ("claude-sonnet-4-6@20260101", "claude-sonnet-4-6", "vertex", "unknown"),
        ("claude-opus-5-5[1m]", "claude-opus-5-5", None, "unknown"),
        ("global.anthropic.claude-opus-5-5-v1:0", "claude-opus-5-5", "bedrock", "global"),
        # more Bedrock forms (in-region and geo profiles are regional; current ids carry no -vN)
        ("anthropic.claude-opus-5-5", "claude-opus-5-5", "bedrock", "regional"),
        ("us.anthropic.claude-opus-5-v1:0", "claude-opus-5", "bedrock", "regional"),
        (
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "claude-sonnet-4-5",
            "bedrock",
            "regional",
        ),
        (
            "apac.anthropic.claude-haiku-4-5-20251001-v1:0",
            "claude-haiku-4-5",
            "bedrock",
            "regional",
        ),
        ("global.anthropic.claude-fable-5-1", "claude-fable-5-1", "bedrock", "global"),
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-5-5",
            "claude-opus-5-5",
            "bedrock",
            "global",
        ),
        (
            "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-5-v1:0",
            "claude-opus-5",
            "bedrock",
            "regional",
        ),
        # Vertex
        ("claude-opus-5-5@latest", "claude-opus-5-5", "vertex", "unknown"),
        (
            "projects/p/locations/us-east5/publishers/anthropic/models/claude-opus-5@20260724",
            "claude-opus-5",
            "vertex",
            "unknown",
        ),
        # plain ids
        ("  claude-opus-5-5  ", "claude-opus-5-5", None, "unknown"),
        ("claude-sonnet-5", "claude-sonnet-5", None, "unknown"),
        ("gpt-5.6-sol", "gpt-5.6-sol", None, "unknown"),
        ("claude-opus-5-5[1M]", "claude-opus-5-5", None, "unknown"),
    ],
)
def test_normalize_model(raw: str, model: str, hint: str | None, scope: str) -> None:
    mid = normalize_model(raw)
    assert (mid.model, mid.channel_hint, mid.endpoint_scope, mid.reason) == (
        model,
        hint,
        scope,
        None,
    )


@pytest.mark.parametrize("raw", ["<synthetic>", " <synthetic> "])
def test_synthetic_is_not_priceable(raw: str) -> None:
    assert normalize_model(raw) == ModelId("", None, "unknown", "synthetic")


@pytest.mark.parametrize("raw", sorted(CONFIG_ALIASES) + ["Opus", "sonnet[1m]", "opus[1m]"])
def test_config_aliases_are_not_priceable(raw: str) -> None:
    assert normalize_model(raw) == ModelId("", None, "unknown", "config alias")


def test_empty_and_hints() -> None:
    assert normalize_model("").reason == "empty"
    assert normalize_model(None).reason == "empty"  # type: ignore[arg-type]
    assert normalize_model("[1m]").reason == "empty"
    assert normalize_model("claude-opus-5-5", provider_hint="vertex").channel_hint == "vertex"
    assert normalize_model("claude-opus-5-5", provider_hint="bedrock").channel_hint == "bedrock"
    assert normalize_model("claude-opus-5-5", provider_hint="anthropic").channel_hint is None
    assert CONFIG_ALIASES == {"opus", "sonnet", "haiku", "fable", "opusplan", "default"}


@given(st.text(max_size=60))
@settings(max_examples=300, deadline=None)
def test_normalize_model_never_raises(raw: str) -> None:
    mid = normalize_model(raw)
    assert mid.endpoint_scope in ("global", "regional", "unknown")
    assert (mid.model == "") == (mid.reason is not None)
    assert mid.model == mid.model.strip() or mid.model == ""


# ---------- lanes ----------


def _usage(**kw: int) -> object:
    return make_usage(**kw)


def test_group_lanes_uses_shells_and_sorts() -> None:
    r0 = make_request("main", 0, 3000, make_usage(cache_write_1h=10, output=1), session_key="s1")
    r1 = make_request("main", 1, 1000, make_usage(cache_write_1h=10, output=1), session_key="s1")
    r2 = make_request("orphan", 0, 500, make_usage(output=1), session_key="s0")
    shell = make_lane(
        [],
        kind=LaneKind.SUBAGENT,
        lane_key="main",
        session_key="s1",
        scope="ws:w1",
        parent_lane_key="root",
        lane_exact=False,
    )
    session = Session("s1", "claude-code", Attribution(), (shell,), 0, 10)
    e_late = LaneEvent("main", 900, "clear")
    e_early = LaneEvent("main", 100, "human_prompt")
    lanes = group_lanes([r0, r1, r2], [e_late, e_early], [session])
    assert [lane.lane_key for lane in lanes] == ["orphan", "main"]  # sorted by (session, lane)
    orphan, main = lanes
    assert (orphan.kind, orphan.cache_scope_key, orphan.session_key) == (
        LaneKind.UNKNOWN,
        "unknown",
        "s0",
    )
    assert (main.kind, main.cache_scope_key, main.parent_lane_key, main.lane_exact) == (
        LaneKind.SUBAGENT,
        "ws:w1",
        "root",
        False,
    )
    assert [r.seq for r in main.requests] == [1, 0]
    assert main.events == (e_early, e_late)
    assert main.ttl_observed == "1h" and orphan.ttl_observed == "unknown"
    assert group_lanes([r0, r1, r2], [e_late, e_early], [session]) == lanes  # deterministic
    assert group_lanes(reversed([r0, r1, r2]), [e_early, e_late], [session]) == lanes


def test_group_lanes_merges_shell_contents_without_duplicates() -> None:
    r0 = make_request("L", 0, 0, make_usage(output=1), session_key="s")
    r1 = make_request("L", 1, 10, make_usage(output=1), session_key="s")
    ev = LaneEvent("L", 5, "human_prompt")
    shell = make_lane([r0], events=[ev, ev], lane_key="L", session_key="s")
    lanes = group_lanes([r0, r1], [ev], [Session("s", "k", Attribution(), (shell,), 0, 1)])
    assert len(lanes) == 1
    assert [r.request_id for r in lanes[0].requests] == [r0.request_id, r1.request_id]
    assert lanes[0].events == (ev, ev)  # one loose + one extra from the shell
    events_only = group_lanes([], [LaneEvent("E", 1, "clear")])
    assert events_only[0].session_key == "" and events_only[0].requests == ()


@pytest.mark.parametrize(
    ("usages", "expected"),
    [
        ([dict(cache_write_5m=5)], "5m"),
        ([dict(cache_write_1h=5)], "1h"),
        ([dict(cache_write_5m=5), dict(cache_write_1h=5)], "mixed"),
        ([dict(cache_write_unknown=5)], "unknown"),
        ([dict(output=1)], "unknown"),
    ],
)
def test_ttl_observed(usages: list[dict], expected: str) -> None:
    reqs = [make_request("L", i, i, make_usage(**u)) for i, u in enumerate(usages)]
    assert group_lanes(reqs, [])[0].ttl_observed == expected


def test_ttl_observed_ignores_non_billable_writes() -> None:
    declined = make_inference(
        make_usage(cache_write_1h=9), kind="fallback_declined", billable=False, inference_id="d"
    )
    req = make_request("L", 0, 0, make_usage(cache_write_5m=5), extra_inferences=(declined,))
    assert group_lanes([req], [])[0].ttl_observed == "5m"


def test_ttl_of_last_write() -> None:
    lane = lane_from_table([(0, 0, 100, 0, 0, 1), (30, 100, 0, 50, 0, 1), (60, 150, 0, 0, 0, 1)])
    assert ttl_of_last_write(lane, 0) is None
    assert ttl_of_last_write(lane, 1) == 300
    assert ttl_of_last_write(lane, 2) == 3600
    assert ttl_of_last_write(lane, 3) == 3600  # request 2 wrote nothing
    assert ttl_of_last_write(lane, 99) == 3600
    mixed = lane_from_table([(0, 0, 10, 10, 0, 1)])
    assert ttl_of_last_write(mixed, 1) == 300  # the 5m tail expires first
    other = make_lane(
        [make_request("L", 0, 0, make_usage(cache_write_other=5, cache_write_other_ttl_s=1800))]
    )
    assert ttl_of_last_write(other, 1) == 1800
    hinted = make_lane(
        [make_request("L", 0, 0, make_usage(cache_write_unknown=5), write_ttl_hint="1h")]
    )
    assert ttl_of_last_write(hinted, 1) == 3600
    unknown = make_lane([make_request("L", 0, 0, make_usage(cache_write_unknown=5))])
    assert ttl_of_last_write(unknown, 1) is None
    compaction = make_inference(make_usage(cache_write_5m=7), kind="compaction", inference_id="c")
    two = make_request("L", 0, 0, make_usage(cache_write_1h=3), extra_inferences=(compaction,))
    assert ttl_of_last_write(make_lane([two]), 1) == 3600  # the last write of the request wins
    retried = make_request(
        "L",
        0,
        0,
        attempts=(
            make_attempt([make_inference(make_usage(cache_write_1h=5), inference_id="a")], ts_ms=0),
            make_attempt(
                [make_inference(make_usage(output=1), inference_id="b")], ts_ms=5, attempt_no=1
            ),
        ),
    )
    assert ttl_of_last_write(make_lane([retried]), 1) == 3600
