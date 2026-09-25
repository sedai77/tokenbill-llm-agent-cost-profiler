"""Load validation of ``tokenbill/rates@1`` layers, the built-in registry and the user layers
(SPEC §6.1, §6.2; brief acceptance "load failures")."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core import extensions
from tokenbill.core.errors import PricingError, SourceError, UsageError
from tokenbill.core.records import UsageBuckets
from tokenbill.rates import schema
from tokenbill.rates.engine import RateCard
from tokenbill.rates.schema import (
    load_builtin,
    load_file,
    model_price_layer,
    parse_layer,
    parse_model_price,
)

from .helpers import builtin, ctx, doc, layer, row, ts

_MOD = {"modifier_id": "test.batch", "kind": "multiply", "factor": "0.5", "applies_to": ["*"],
        "when": {"service_tier": "batch"}, "stacking": "documented"}


def _fails(text_or_doc: Any, match: str) -> None:
    text = text_or_doc if isinstance(text_or_doc, (str, bytes)) else json.dumps(text_or_doc)
    with pytest.raises(PricingError, match=match):
        parse_layer(text, "user:test")


def test_valid_document_loads() -> None:
    lay = layer()
    assert lay.name == "user:test" and lay.schema == "tokenbill/rates@1"
    assert len(lay.rows) == 1 and len(lay.sha256) == 64
    r = lay.rows[0]
    assert r.provider == "test" and r.cache_read_mult is not None
    assert r.per_request_usd == (("web_search", r.per_request_usd[0][1]),)


def test_row_without_source_fails_naming_the_row() -> None:
    _fails(doc([row(sources=[])]), r"row test/anthropic_api/claude-test-1/2026-01-01: .*source")


def test_row_without_verified_on_fails() -> None:
    bad = row()
    del bad["verified_on"]
    _fails(doc([bad]), "verified_on")


def test_overlapping_interval_fails() -> None:
    a = row(effective_to="2026-06-01")
    b = row(row_id="test/anthropic_api/claude-test-1/2026-05-01", effective_from="2026-05-01")
    _fails(doc([a, b]), r"row test/anthropic_api/claude-test-1/2026-05-01: overlaps")
    # an open-ended row followed by a later one overlaps too
    c = row(row_id="test/x/2026-07-01", effective_from="2026-07-01")
    _fails(doc([row(), c]), "overlaps")
    # an alias naming another row's model on the same channel counts
    d = row(row_id="test/other", model="claude-other", aliases=["claude-test-1"])
    _fails(doc([row(), d]), "overlaps")
    # adjacent intervals and other channels are fine
    e = row(row_id="test/next", effective_from="2026-06-01")
    f = row(row_id="test/bedrock", channel="bedrock")
    assert len(layer([a, e, f]).rows) == 3


def test_published_absolute_mismatch_fails() -> None:
    _fails(doc([row(published_absolute={"cache_read": "0.21"})]),
           r"published_absolute.cache_read != input x multiplier")
    _fails(doc([row(published_absolute={"cache_write_other": "2.50"})]), "has no multiplier")


def test_unknown_predicate_key_fails() -> None:
    bad = dict(_MOD, when={"region": "us"})
    _fails(doc(modifiers=[bad]), r"modifier test.batch: unknown predicate key 'region'")


def test_unknown_bucket_in_modifier_fails() -> None:
    _fails(doc(modifiers=[dict(_MOD, applies_to=["tokens"])]), "unknown bucket 'tokens'")
    _fails(doc([row(multipliers={"cache_read": "0.1", "cache_write_2h": "3"})]),
           "unknown bucket 'cache_write_2h'")


def test_unknown_promotion_id_fails() -> None:
    _fails(doc([row(promotion="acme.not-a-promo")]), "unknown promotion id")
    # the SPEC's OpenAI promotion and the Copilot promotions exist
    assert layer([row(promotion="openai.gpt-5.6-sol.2026-08")]).rows[0].promotion
    assert layer([row(promotion="github.gpt-5.6-sol.2026-08")]).rows[0].promotion


@pytest.mark.parametrize("feature", ["effort_keeps_cache", "cache_preserving_effort",
                                     "effort_invalidates_all_tiers", "system_breaks"])
def test_cache_rule_supports_entry_fails(feature: str) -> None:
    _fails(doc([row(supports=["batch", feature])]), "cache-rule behavior")


def test_disabled_rows_load_but_never_price() -> None:
    lay = layer([row(enabled=False)])
    card = RateCard([lay])
    c = ctx("claude-test-1")
    p = card.price_usage(UsageBuckets(uncached_input=10), c, ts_ms=ts("2026-03-01"))
    assert p.figure.nano is None and p.unpriced_reason == "unverified rate row"
    assert card.resolve(c, ts_ms=ts("2026-03-01")) is None
    assert not card.supports(c, "batch", ts_ms=ts("2026-03-01"))
    assert card.tokenizer_family(c, ts_ms=ts("2026-03-01")) is None
    assert card.min_cacheable_tokens(c, ts_ms=ts("2026-03-01")) is None


@pytest.mark.parametrize(("patch", "match"), [
    ({"usd_per_mtok": {"input": 2.0, "output": "10"}}, "floats"),
    ({"usd_per_mtok": {"input": "-2", "output": "10"}}, "non-negative decimal"),
    ({"usd_per_mtok": {"input": "1e3", "output": "10"}}, "non-negative decimal"),
    ({"effective_from": "2026-02-30"}, "calendar date"),
    ({"effective_from": "26-01-01"}, "YYYY-MM-DD"),
    ({"effective_to": "2025-12-31"}, "after effective_from"),
    ({"generation": "five"}, "numeric"),
    ({"enabled": "yes"}, "true or false"),
    ({"min_cacheable_tokens": -1}, "non-negative integer"),
    ({"min_cacheable_tokens": True}, "non-negative integer"),
    ({"per_request_usd": {"code_execution": "0.05"}}, "unknown bucket"),
    ({"long_context": {"threshold": 0, "usd_per_mtok": {}}}, "positive integer"),
    ({"long_context": {"threshold": 10, "usd_per_mtok": {}}}, "threshold and band rates"),
    ({"multipliers": {"cache_write_other": "1.25"}, "published_absolute": {}},
     "cache_write_other_ttl_s"),
    ({"sources": [{"url": "", "retrieved": "2026-01-01"}]}, "sources"),
    ({"sources": [{"url": "u", "retrieved": "2026-01-01", "finding": 3}]}, "finding"),
    ({"aliases": "claude-x"}, "must be a list"),
    ({"supports": ["Fast Mode"]}, "supports"),
    ({"notes": 3}, "notes"),
    ({"row_id": ""}, r"rows\[0\]"),
    ({"model": None}, "model"),
    ({"usd_per_mtok": "4"}, "must be an object"),
])
def test_row_field_validation(patch: dict[str, Any], match: str) -> None:
    _fails(doc([row(**patch)]), match)


@pytest.mark.parametrize(("patch", "match"), [
    ({"kind": "divide"}, "multiply or replace_base"),
    ({"factor": None}, "a factor"),
    ({"kind": "replace_base"}, "base rates"),
    ({"kind": "replace_base", "factor": None, "base_usd_per_mtok": {"cache": "1"}},
     "unknown bucket"),
    ({"stacking": "maybe"}, "documented or assumed"),
    ({"applies_to": []}, "at least one bucket"),
    ({"when": {"generation_gte": "new"}}, "numeric"),
    ({"when": {"channel_in": "a,,b"}}, "non-empty"),
    ({"when": {"speed": 1}}, "must be a string"),
])
def test_modifier_field_validation(patch: dict[str, Any], match: str) -> None:
    _fails(doc(modifiers=[dict(_MOD, **patch)]), match)


def test_document_level_validation() -> None:
    _fails("{", "not valid JSON")
    _fails("[]", "JSON object")
    _fails(json.dumps(doc(schema="tokenbill/rates@2")), "schema")
    _fails(json.dumps(doc(as_of="soon")), "as_of")
    _fails(json.dumps(doc(rows="all")), "must be a list")
    _fails(json.dumps({**doc(), "rows": [3]}), "must be an object")
    _fails(json.dumps({**doc(), "modifiers": [3]}), "must be an object")
    _fails('{"schema": "tokenbill/rates@1", "x": NaN}', "floats")
    _fails(json.dumps(doc([row(), row()])), "duplicate row_id")
    _fails(json.dumps(doc(modifiers=[_MOD, _MOD])), "duplicate modifier_id")


def test_unknown_keys_are_ignored() -> None:
    lay = layer([row(date_source="K", category="Powerful")],
                [dict(_MOD, notes="documentation")])
    assert lay.rows[0].model == "claude-test-1" and lay.modifiers[0].modifier_id == "test.batch"


def test_load_file_names_the_user_layer(tmp_path: Path) -> None:
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(doc()), encoding="utf-8")
    assert load_file(path, "rates.json").name == "user:rates.json"
    assert load_file(path, "user:mine").name == "user:mine"
    with pytest.raises(SourceError):
        load_file(tmp_path / "missing.json", "missing")
    path.write_text(json.dumps(doc([row(sources=[])])), encoding="utf-8")
    with pytest.raises(PricingError, match=r"^rates.json: row "):
        load_file(path, "rates")


def test_load_file_refuses_oversized_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "_MAX_FILE_BYTES", 10)
    path = tmp_path / "big.json"
    path.write_text(json.dumps(doc()), encoding="utf-8")
    with pytest.raises(PricingError, match="16 MiB"):
        load_file(path, "big")


def test_builtin_registry_files_are_well_formed() -> None:
    lay = builtin()
    assert re.fullmatch(r"builtin@\d{4}-\d{2}-\d{2}", lay.name)
    for r in lay.rows:
        assert r.row_id == f"{r.provider}/{r.channel}/{r.model}/{r.effective_from}", r.row_id
        assert r.sources and r.verified_on >= "2026-09-23"
        assert r.channel in ("anthropic_api", "bedrock", "vertex", "openai_api", "github_copilot")
    channels = {r.channel for r in lay.rows}
    assert channels == {"anthropic_api", "bedrock", "vertex", "openai_api", "github_copilot"}
    ids = {m.modifier_id for m in lay.modifiers}
    assert {"anthropic.batch", "anthropic.inference_geo.us", "anthropic.fast.opus-5-5",
            "anthropic.fast.opus-5", "anthropic.priority", "bedrock.endpoint.regional",
            "vertex.endpoint.regional", "vertex.batch", "openai.batch", "openai.flex",
            "openai.fast", "openai.priority", "openai.regional_processing", "github.auto",
            "github.compliance", "github.fast.opus-4-8"} <= ids
    # the VERIFY launch row of gpt-5.6-sol ships disabled; every Priority note is assumed
    launch = [r for r in lay.rows if r.row_id == "openai/openai_api/gpt-5.6-sol/2026-07-09"]
    assert len(launch) == 1 and not launch[0].enabled
    stacking = {m.modifier_id: m.stacking for m in lay.modifiers}
    assert stacking["anthropic.priority"] == "assumed" and stacking["vertex.batch"] == "assumed"


def test_load_builtin_is_deterministic() -> None:
    assert load_builtin().sha256 == load_builtin().sha256 == builtin().sha256


@pytest.mark.parametrize(("provider", "channels"), [
    ("anthropic", {"anthropic_api", "bedrock", "vertex"}),
    ("openai", {"openai_api"}),
    ("github", {"github_copilot"}),
])
def test_load_builtin_provider_filter(provider: str, channels: set[str]) -> None:
    lay = load_builtin(provider)
    assert {r.channel for r in lay.rows} == channels
    assert {r.provider for r in lay.rows} == {provider}
    with pytest.raises(UsageError):
        load_builtin("acme")


def test_missing_extension_rate_file_is_a_dq_note_and_falls_back_to_facts() -> None:
    notes: list[Any] = []
    lay = load_builtin(notes=notes)
    if extensions.extension_rate_files():  # pragma: no cover - CP-RATES merged: file present
        pytest.skip("the Copilot rate file is installed")
    assert [(n.code, n.detail) for n in notes] == [("dq.extension_unavailable",
                                                    "copilot:rate_files")]
    from tokenbill.core import facts
    copilot = [r for r in lay.rows if r.channel == "github_copilot"]
    assert copilot == list(facts.copilot_rates())


def test_extension_rate_file_replaces_the_facts_subset(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    ext_row = row(row_id="github/github_copilot/claude-test-1/2026-06-01",
                  channel="github_copilot", provider="github", effective_from="2026-06-01")
    ext_mod = {"modifier_id": "github.test", "kind": "multiply", "factor": "0.9",
               "applies_to": ["*"], "when": {"channel_in": "github_copilot", "routing": "auto"},
               "stacking": "assumed"}
    path = tmp_path / "github_copilot.json"
    path.write_text(json.dumps(doc([ext_row], [ext_mod], provider="github")), encoding="utf-8")
    monkeypatch.setattr(extensions, "extension_rate_files", lambda notes=None: (path,))
    lay = load_builtin()
    copilot = [r for r in lay.rows if r.channel == "github_copilot"]
    assert [r.row_id for r in copilot] == ["github/github_copilot/claude-test-1/2026-06-01"]
    ids = {m.modifier_id for m in lay.modifiers}
    assert "github.test" in ids and "github.auto" not in ids
    assert [r.channel for r in load_builtin("github").rows] == ["github_copilot"]
    assert all(r.channel != "github_copilot" for r in load_builtin("openai").rows)


def test_parse_model_price() -> None:
    assert parse_model_price("acme-llm-1=3,15") == ("acme-llm-1", "3", "15")
    assert parse_model_price(" claude-x = 0.25 , 1.5 ") == ("claude-x", "0.25", "1.5")
    for bad in ("model=1", "model=1,2,3", "model=a,b", "model=nan,5", "model=3,inf",
                "model=-inf,5", "=1,2", "model=1e308,1e308", "model=3,1e307", "model=1000000,1",
                "mo\x00del=1,2"):
        with pytest.raises(UsageError):
            parse_model_price(bad)
    with pytest.raises(UsageError):
        parse_model_price(3)  # type: ignore[arg-type]


def test_model_price_layer_rows() -> None:
    lay = model_price_layer([("acme-llm-1", "3", "15"), ("acme-llm-1", "4", "16"),
                             ("claude-z", "1", "5")])
    assert lay.name == "model-price" and schema.layer_kind(lay) == "model-price"
    assert [r.model for r in lay.rows] == ["acme-llm-1", "claude-z"]
    acme = lay.rows[0]
    assert (acme.channel, str(acme.input_usd_per_mtok), str(acme.output_usd_per_mtok)) == \
        ("*", "4", "16")                                      # the last price of a model wins
    card = RateCard([lay])
    p = card.price_usage(UsageBuckets(uncached_input=1_000_000, cache_read=1_000_000,
                                      cache_write_1h=1_000_000, output=1_000_000),
                         ctx("acme-llm-1", channel="bedrock"), ts_ms=ts("2026-09-23"))
    # 4 + 0.4 + 8 + 16 = 28.4 USD at any channel and date
    assert p.figure.nano == 28_400_000_000
    with pytest.raises(UsageError):
        model_price_layer([("x", "1")])  # type: ignore[list-item]
    assert model_price_layer([]).rows == ()


def test_layer_kinds() -> None:
    assert schema.layer_kind(builtin()) == "builtin"
    assert schema.layer_kind(layer()) == "user"
