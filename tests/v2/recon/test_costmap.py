"""``recon.costmap``: the cost-type map is data from the documented enumerations; SKU rules come
only from ``core.catalog.map_sku`` (VERIFY rules disabled)."""

from __future__ import annotations

import dataclasses

import pytest

from tokenbill.core import catalog
from tokenbill.core.builders import make_cost_line
from tokenbill.core.records import UsageBuckets
from tokenbill.recon import costmap
from tokenbill.recon.costmap import COST_TYPE_MAP, map_line, sku_rule_status

GLOBAL_INPUT = "USE1-MP:USE1_InputTokenCount_Global-Units"


def test_cost_type_map_is_the_documented_enumeration() -> None:
    token_types = {t for (c, t) in COST_TYPE_MAP if c == "tokens"}
    assert token_types == {"uncached_input_tokens", "cache_read_input_tokens",
                           "cache_creation.ephemeral_5m_input_tokens",
                           "cache_creation.ephemeral_1h_input_tokens", "output_tokens"}
    assert {c for c, _ in COST_TYPE_MAP} == {"tokens", "web_search", "code_execution",
                                             "session_usage"}
    fields = {f.name for f in dataclasses.fields(UsageBuckets)}
    assert all(b in fields or b in costmap.NON_TOKEN_BUCKETS for b in COST_TYPE_MAP.values())
    with pytest.raises(TypeError):
        COST_TYPE_MAP[("x", None)] = "y"  # type: ignore[index]


@pytest.mark.parametrize(("kw", "kind", "bucket"), [
    ({"token_type": "output_tokens"}, "token", "output"),
    ({"cost_type": "web_search", "model": None}, "token", "web_search"),
    ({"cost_type": "code_execution", "model": None}, "report_only", "code_execution"),
    ({"cost_type": "session_usage", "model": None}, "report_only", "session_usage"),
    ({"cost_type": "fine_tuning", "model": None}, "unmapped", None),
    ({"token_type": "mystery_tokens"}, "unmappable", None),
    ({"token_type": "output_tokens", "model": None}, "unmappable", "output"),
    ({"cost_type": None, "token_type": None, "model": None}, "unmappable", None),
])
def test_map_line_admin_cost_report(kw: dict, kind: str, bucket: str | None) -> None:
    mapping = map_line(make_cost_line(1, **kw))
    assert (mapping.kind, mapping.bucket) == (kind, bucket)


def test_enterprise_cost_types_are_unverified() -> None:
    line = make_cost_line(1, source_kind="anthropic.enterprise_cost", token_type="output_tokens")
    mapping = map_line(line)
    assert mapping.kind == "unmappable" and mapping.verified is False


def test_sku_rules_are_disabled_and_never_map() -> None:
    assert catalog.map_sku("aws.cur2", GLOBAL_INPUT) is None
    assert sku_rule_status("aws.cur2", GLOBAL_INPUT) == (None, True)
    assert sku_rule_status("aws.cur2", "no-such-usage-type") == (None, False)
    assert sku_rule_status("aws.cur2", None) == (None, False)
    line = make_cost_line(1, source_kind="aws.cur2", channel="bedrock", model=None,
                          cost_type=None, sku=GLOBAL_INPUT)
    assert map_line(line).kind == "unmappable" and map_line(line).verified is False


def test_verified_rules_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "SKU_RULES", tuple(dataclasses.replace(r, verified=True)
                                                    for r in catalog.SKU_RULES))
    line = make_cost_line(1, source_kind="aws.cur2", channel="bedrock", model="claude-opus-5",
                          cost_type=None, sku=GLOBAL_INPUT)
    mapping = map_line(line)
    assert (mapping.kind, mapping.bucket, mapping.endpoint_scope) == (
        "token", "uncached_input", "global")
    no_model = map_line(dataclasses.replace(line, model=None))
    assert no_model.kind == "unmappable" and no_model.bucket == "uncached_input"


def test_cloud_adjustment_lines() -> None:
    cur = make_cost_line(-1, source_kind="aws.cur2", channel="bedrock", model=None,
                         cost_type="Credit", sku="X")
    assert map_line(cur).kind == "credit"
    fee = dataclasses.replace(cur, cost_type="Fee")
    assert map_line(fee).kind == "unmapped"
    gcp = make_cost_line(1, source_kind="gcp.billing_export", channel="vertex", model=None,
                         cost_type="adjustment", sku="Y")
    assert map_line(gcp).kind == "unmapped"
    regular = dataclasses.replace(gcp, cost_type="regular")
    assert map_line(regular).kind == "unmappable"


def test_channel_provider() -> None:
    assert costmap.channel_provider("openai_api") == "openai"
    assert costmap.channel_provider("bedrock") == "anthropic"
