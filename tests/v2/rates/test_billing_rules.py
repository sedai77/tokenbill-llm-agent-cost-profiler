"""Failure-mode billing rules (SPEC §6.6, D10)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.labels import Evidence
from tokenbill.core.records import UsageBuckets
from tokenbill.rates.billing_rules import parse_rules, refusal_rule, rule, rule_for, rules

from .helpers import card, ctx, ts

#: SPEC §6.6 seed table: rule id → (billable, confidence)
SEED = {
    "anthropic.refusal.pre_output": (False, "documented"),
    "anthropic.refusal.ambiguous": (None, "assumed"),
    "anthropic.refusal.mid_stream": (True, "documented"),
    "anthropic.batch.errored_canceled_expired": (False, "documented"),
    "anthropic.web_search.failed": (False, "documented"),
    "anthropic.max_tokens": (True, "documented"),
    "anthropic.abort.client": (None, "unknown"),
    "anthropic.overloaded.mid_stream": (None, "unknown"),
    "anthropic.pre_token_429_529": (None, "unknown"),
    "vertex.non_200": (False, "documented"),
    "azure_openai.content_filter_400": (True, "documented"),
    "azure_openai.timeout_408": (True, "documented"),
    "openai.flex_429": (False, "documented"),
    "openai.max_output_tokens": (True, "documented"),
}


def test_seed_rows_match_the_spec_table() -> None:
    assert {r.rule_id: (r.billable, r.confidence) for r in rules()} == SEED
    for r in rules():
        assert r.rule_id == f"{r.provider}.{r.failure_mode}" and r.source and r.known
    assert rule("anthropic.max_tokens").billed_partial_output == "yes"
    assert rule("openai.max_output_tokens").notes.startswith("incomplete responses")


def test_rule_for_and_default_rule() -> None:
    assert rule_for("anthropic", "refusal.pre_output") is rule("anthropic.refusal.pre_output")
    default = rule_for("openai", "abort.client")
    assert not default.known and default.confidence == "unknown" and default.billable is None
    assert default.rule_id == "openai.abort.client"
    with pytest.raises(UsageError):
        rule("no.such.rule")
    with pytest.raises(UsageError):
        rule_for("anthropic", 3)  # type: ignore[arg-type]


@pytest.mark.parametrize(("tokens", "rule_id"), [
    (0, "anthropic.refusal.pre_output"), (1, "anthropic.refusal.ambiguous"),
    (16, "anthropic.refusal.ambiguous"), (17, "anthropic.refusal.mid_stream"),
])
def test_refusal_rule_thresholds(tokens: int, rule_id: str) -> None:
    assert refusal_rule(tokens).rule_id == rule_id


def test_refusal_rule_threshold_is_configurable() -> None:
    assert rule("anthropic.refusal.ambiguous").param("max_output_tokens") == 16
    assert refusal_rule(10, threshold=8).rule_id == "anthropic.refusal.mid_stream"
    for bad in (-1, True):
        with pytest.raises(UsageError):
            refusal_rule(bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        refusal_rule(3, threshold=-2)
    with pytest.raises(UsageError):
        rule("anthropic.max_tokens").param("max_output_tokens")


def test_rules_drive_pricing_exactness() -> None:
    usage = UsageBuckets(uncached_input=2000, output=40)
    c = card()
    for rule_id, (billable, _) in SEED.items():
        if not rule_id.startswith("anthropic."):
            continue
        p = c.price_usage(usage, ctx("claude-fable-5"), ts_ms=ts("2026-09-23"),
                          billable=rule(rule_id).billable)
        if billable is None:
            assert p.figure.evidence is Evidence.ESTIMATED and p.figure.low_nano == 0
        else:
            assert p.figure.evidence is Evidence.EXACT
            assert (p.figure.nano == 0) is (billable is False)


def _doc(**patch: Any) -> str:
    base = {"rule_id": "x.y", "provider": "x", "failure_mode": "y", "billed_input": "no",
            "billed_partial_output": "no", "cache_written": "no", "confidence": "documented",
            "source": "s"}
    base.update(patch)
    return json.dumps({"schema": "tokenbill/billing-rules@1", "rules": [base]})


@pytest.mark.parametrize(("text", "match"), [
    ("{", "not valid JSON"),
    ('{"schema": "x", "rules": []}', "schema"),
    ('{"schema": "tokenbill/billing-rules@1", "rules": [3]}', "must be an object"),
    (_doc(rule_id="x.z"), "rule_id"),
    (_doc(billed_input="maybe"), "billed_input"),
    (_doc(confidence="likely"), "confidence"),
    (_doc(params={"n": -1}), "params"),
    (_doc(verified_on="yesterday"), "verified_on"),
    (_doc(notes=3), "notes"),
    (_doc(source=""), "source"),
])
def test_parse_rules_validation(text: str, match: str) -> None:
    with pytest.raises(PricingError, match=match):
        parse_rules(text)


def test_parse_rules_duplicates_and_partial_billing() -> None:
    one = json.loads(_doc())
    one["rules"].append(dict(one["rules"][0]))
    with pytest.raises(PricingError, match="duplicate"):
        parse_rules(json.dumps(one))
    mixed = parse_rules(_doc(billed_input="no", billed_partial_output="unknown"))
    assert mixed[0].billable is None
