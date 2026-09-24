"""``tokenbill/copilot/data/github_copilot.json``: generated from the revisions, SPEC §6.1 load
validation, the facts parity test (every ``facts.copilot.rates`` row and modifier, both ways;
ruling R-E34: shared rows only) and the row rules of the CP-RATES brief (Build #2)."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import rates_verify as rv
from tokenbill.core import extensions, registry
from tokenbill.core.catalog import COPILOT_PROMOTIONS
from tokenbill.core.errors import PricingError, SourceError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.money import EXACT_CTX
from tokenbill.core.types import DataQualityNote

from . import build_rate_file as build
from .support import REPO, layer, rate_doc, revisions, row

FACTS_META = ("provider", "source", "finding", "verification", "verified_on")


def test_committed_files_equal_the_generator_output() -> None:
    revs = revisions()
    assert build.RATE_PATH.read_text(encoding="utf-8") == build.render(build.build_rate_doc(revs))
    assert build.SNAPSHOT_PATH.read_text(encoding="utf-8") == build.render(
        build.build_snapshot_doc(revs))


def test_generator_main_prints_without_writing(capsys: pytest.CaptureFixture[str]) -> None:
    assert build.main([]) == 0
    out = capsys.readouterr().out
    assert "github_copilot.json" in out and "written" not in out


def test_data_package_is_docstring_only() -> None:
    tree = ast.parse((REPO / "tokenbill/copilot/data/__init__.py").read_text(encoding="utf-8"))
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)


# ---------------------------------------------------------------------------------------------
# load validation (acceptance: non-overlapping intervals, derived = published, exact Decimals,
# promotion ids exist)
# ---------------------------------------------------------------------------------------------


def test_packaged_file_loads_and_validates() -> None:
    lay = layer()
    assert lay.name == "builtin@2026-09-23" and lay.schema == "tokenbill/rates@1"
    assert len(lay.rows) == 55 and [m.modifier_id for m in lay.modifiers] == [
        "github.auto", "github.compliance", "github.fast.opus-4-8"]
    assert len(lay.sha256) == 64
    assert all(r.provider == "github" and r.channel == "github_copilot" for r in lay.rows)
    by_model: dict[str, list[Any]] = {}
    for r in lay.rows:
        by_model.setdefault(r.model, []).append(r)
    for rows in by_model.values():
        rows.sort(key=lambda r: r.effective_from)
        for a, b in zip(rows, rows[1:], strict=False):
            assert a.effective_to is not None and a.effective_to <= b.effective_from
    promo_ids = {p.promotion_id for p in COPILOT_PROMOTIONS}
    for r in lay.rows:
        for bucket, published in r.published_absolute:
            mult = {"cache_read": r.cache_read_mult, "cache_write_5m": r.cache_write_5m_mult,
                    "cache_write_1h": r.cache_write_1h_mult}[bucket]
            assert mult is not None and EXACT_CTX.multiply(r.input_usd_per_mtok, mult) == published
        assert r.promotion is None or r.promotion in promo_ids
        assert r.sources and r.sources[0].url == rv.DOCS_URL
        assert r.sources[0].retrieved == "2026-09-23" and " yml 2026-" in r.sources[0].finding
        assert r.verified_on == "2026-09-23" and r.enabled


def test_file_has_no_json_floats() -> None:
    def walk(node: Any) -> None:
        assert not isinstance(node, float)
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(json.loads((REPO / "tokenbill/copilot/data/github_copilot.json").read_text("utf-8")))


Mutator = Callable[[dict[str, Any]], None]
OPUS = "github/github_copilot/claude-opus-5-5/2026-09-22"
SOL = "github/github_copilot/gpt-5.6-sol/2026-08-21"


def _set(row_id: str, key: str, value: Any) -> Mutator:
    def mutate(doc: dict[str, Any]) -> None:
        row(doc, row_id)[key] = value
    return mutate


def _mod(index: int, key: str, value: Any) -> Mutator:
    def mutate(doc: dict[str, Any]) -> None:
        doc["modifiers"][index][key] = value
    return mutate


def _overlap(doc: dict[str, Any]) -> None:
    row(doc, "github/github_copilot/gpt-5.4/2026-06-01")["effective_to"] = "2026-06-05"


def _dup_row(doc: dict[str, Any]) -> None:
    doc["rows"].append(dict(row(doc, OPUS)))


def _dup_mod(doc: dict[str, Any]) -> None:
    doc["modifiers"].append(dict(doc["modifiers"][0]))


def _del(key: str) -> Mutator:
    def mutate(doc: dict[str, Any]) -> None:
        del row(doc, OPUS)[key]
    return mutate


BAD: dict[str, Mutator] = {
    "overlapping interval": _overlap,
    "published != derived": _set(OPUS, "published_absolute", {"cache_read": "0.21"}),
    "published bucket without multiplier": _set(OPUS, "published_absolute",
                                                {"cache_write_other": "1"}),
    "unknown promotion id": _set(SOL, "promotion", "github.nope"),
    "promotion of another model": _set(SOL, "promotion", "github.gemini-3.8-flash.2026"),
    "promotion not a string": _set(SOL, "promotion", 7),
    "cache-rule supports": _set(OPUS, "supports", ["effort_keeps_cache"]),
    "bad row_id": _set(OPUS, "row_id", "github/github_copilot/claude-opus-5-5/2026-09-21"),
    "no source": _set(OPUS, "sources", []),
    "source without url": _set(OPUS, "sources", [{"retrieved": "2026-09-23"}]),
    "bad source finding": _set(OPUS, "sources", [{"url": "u", "retrieved": "2026-09-23",
                                                  "finding": 3}]),
    "end before start": _set(OPUS, "effective_to", "2026-09-22"),
    "impossible date": _set(OPUS, "effective_to", "2026-02-30"),
    "date shape": _set(OPUS, "verified_on", "23.09.2026"),
    "unknown key": _set(OPUS, "discount", "0.1"),
    "missing key": _del("notes"),
    "missing output price": _set(OPUS, "usd_per_mtok", {"input": "4.00"}),
    "negative price": _set(OPUS, "usd_per_mtok", {"input": "-4.00", "output": "20.00"}),
    "price not a string": _set(OPUS, "usd_per_mtok", {"input": 4, "output": "20.00"}),
    "price not a decimal": _set(OPUS, "usd_per_mtok", {"input": "four", "output": "20.00"}),
    "unknown multiplier": _set(OPUS, "multipliers", {"cache_read_7d": "0.1"}),
    "multipliers not a map": _set(OPUS, "multipliers", ["0.1"]),
    "band without its buckets": _set(SOL, "long_context", {"threshold": 272000,
                                                           "usd_per_mtok": {"input": "4.00",
                                                                            "output": "15.00"}}),
    "band threshold": _set(SOL, "long_context", {"threshold": "272K", "usd_per_mtok": {}}),
    "band keys": _set(SOL, "long_context", {"threshold": 272000}),
    "generation": _set(OPUS, "generation", "5.5b"),
    "min cacheable": _set(OPUS, "min_cacheable_tokens", -1),
    "write ttl": _set(OPUS, "cache_write_other_ttl_s", 0),
    "enabled": _set(OPUS, "enabled", "yes"),
    "aliases": _set(OPUS, "aliases", "Claude Opus 5.5"),
    "per-request bucket": _set(OPUS, "per_request_usd", {"web_fetch": "0.01"}),
    "other channel": _set(OPUS, "channel", "anthropic_api"),
    "empty model": _set(OPUS, "model", ""),
    "duplicate row": _dup_row,
    "duplicate modifier": _dup_mod,
    "unknown predicate": _mod(0, "when", {"channel_in": "github_copilot", "plan": "business"}),
    "predicate value": _mod(0, "when", {"channel_in": "github_copilot", "routing": ""}),
    "modifier channel": _mod(0, "when", {"channel_in": "anthropic_api", "routing": "auto"}),
    "modifier model_in": _mod(2, "when", {"channel_in": "github_copilot", "speed": "fast",
                                          "model_in": "claude-opus-9"}),
    "modifier kind": _mod(0, "kind", "add"),
    "multiply with base": _mod(0, "base_usd_per_mtok", {"input": "1"}),
    "multiply factor": _mod(0, "factor", 0.9),
    "replace_base with factor": _mod(2, "factor", "2"),
    "replace_base without output": _mod(2, "base_usd_per_mtok", {"input": "10.00"}),
    "modifier bucket": _mod(0, "applies_to", ["tokens"]),
    "modifier stacking": _mod(0, "stacking", "maybe"),
    "modifier notes": _mod(0, "notes", 3),
    "modifier unknown key": _mod(0, "channel", "github_copilot"),
    "modifier id": _mod(0, "modifier_id", 5),
    "schema": lambda doc: doc.update(schema="tokenbill/rates@2"),
    "provider": lambda doc: doc.update(provider=""),
    "as_of": lambda doc: doc.update(as_of="today"),
    "rows not a list": lambda doc: doc.update(rows={}),
    "row not a map": lambda doc: doc["rows"].append("row"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_invalid_documents_fail_the_load(case: str) -> None:
    doc = rate_doc()
    BAD[case](doc)
    with pytest.raises(PricingError):
        rv.layer_from_json(doc, name="test")


def test_load_layer_from_a_path(tmp_path: Path) -> None:
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(rate_doc()), encoding="utf-8")
    lay = rv.load_layer(path)
    assert lay.name == "user:rates.json" and lay.rows == layer().rows
    path.write_text('{"schema": "tokenbill/rates@1", "as_of": 1.5}', encoding="utf-8")
    with pytest.raises(PricingError, match="floats"):
        rv.load_layer(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(PricingError, match="invalid JSON"):
        rv.load_layer(path)
    with pytest.raises(SourceError):
        rv.load_layer(tmp_path / "missing.json")


# ---------------------------------------------------------------------------------------------
# facts parity (acceptance: every row equals the facts row, and vice versa; R-E34 shared rows)
# ---------------------------------------------------------------------------------------------


def test_every_facts_row_is_in_the_file_unchanged() -> None:
    doc = rate_doc()
    mine = {r["row_id"]: r for r in doc["rows"]}
    facts_rows = load_facts().copilot_rate_rows_json()
    assert len(facts_rows) == 46
    for fr in facts_rows:
        assert fr["provider"] == doc["provider"]
        assert mine[fr["row_id"]] == {k: v for k, v in fr.items() if k not in FACTS_META} | {
            "verified_on": fr["verified_on"]}


def test_file_rows_equal_facts_rows_as_rate_rows() -> None:
    facts_rows = {r.row_id: r for r in load_facts().copilot_rates()}
    file_rows = {r.row_id: r for r in layer().rows}
    shared = set(facts_rows) & set(file_rows)
    assert shared == set(facts_rows)  # vice versa: no facts row is missing from the file
    for row_id in shared:
        assert file_rows[row_id] == facts_rows[row_id]
    # R-E34: rows beyond facts are whole models facts does not carry (closed intervals)
    facts_models = {r.model for r in facts_rows.values()}
    extra = {r.model for rid, r in file_rows.items() if rid not in shared}
    assert not extra & facts_models
    assert extra == {"gpt-4.1", "gpt-5.2", "gpt-5.2-codex", "gemini-2.5-pro", "gemini-3-flash",
                     "gemini-3.1-pro", "raptor-mini", "mai-code-1-flash"}
    assert all(file_rows[rid].effective_to is not None for rid in set(file_rows) - shared)


def test_modifiers_equal_facts_modifiers() -> None:
    raw = {m["modifier_id"]: m for m in rate_doc()["modifiers"]}
    for fm in load_facts().copilot.modifiers:
        assert fm in layer().modifiers
    for fm in json.loads((REPO / "tokenbill/core/facts.json").read_text("utf-8"))["copilot"][
            "modifiers"]:
        assert raw[fm["modifier_id"]] == {k: v for k, v in fm.items() if k not in FACTS_META}


# ---------------------------------------------------------------------------------------------
# row rules (brief Build #2)
# ---------------------------------------------------------------------------------------------


def test_claude_rows_carry_the_published_write_and_the_assumed_1h_write() -> None:
    claude = [r for r in rate_doc()["rows"] if r["model"].startswith("claude-")]
    assert len(claude) == 13
    for r in claude:
        inp = Decimal(r["usd_per_mtok"]["input"])
        assert set(r["published_absolute"]) == {"cache_read", "cache_write_5m"}
        assert Decimal(r["multipliers"]["cache_write_5m"]) * inp == Decimal(
            r["published_absolute"]["cache_write_5m"])
        assert r["multipliers"]["cache_write_1h"] == "2.0"
        assert "cache_write_1h unpublished: 2 x input assumed" in r["notes"]
        assert "VERIFY" in r["notes"]


def test_non_claude_write_prices_and_not_applicable() -> None:
    for r in rate_doc()["rows"]:
        if r["model"].startswith("claude-"):
            continue
        mult = r["multipliers"]
        if "cache_write_5m" in mult:
            assert mult["cache_write_1h"] == mult["cache_write_5m"]  # one price, both classes
            assert "cache_write_1h" not in r["published_absolute"]
        else:
            assert "cache_write_1h" not in mult  # "Not applicable" → null
            assert ("write bucket disabled (VERIFY)" in r["notes"]) == r["model"].startswith(
                "gpt-5.6-")


def test_gpt_5_6_rows_before_2026_08_03_ship_the_write_buckets_disabled() -> None:
    doc = rate_doc()
    for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
        r = row(doc, f"github/github_copilot/{model}/2026-07-30")
        assert r["effective_to"] == "2026-08-03" and set(r["multipliers"]) == {"cache_read"}
        assert set(r["long_context"]["usd_per_mtok"]) == {"input", "cache_read", "output"}
    assert set(row(doc, "github/github_copilot/gpt-5.6-sol/2026-07-09")["multipliers"]) == {
        "cache_read"}


def test_long_context_rows() -> None:
    bands = {r["model"]: r["long_context"]["threshold"] for r in rate_doc()["rows"]
             if r["long_context"] is not None}
    assert set(bands.values()) == {272_000, 200_000}
    assert {m for m, t in bands.items() if t == 200_000} == {
        "gpt-5.6-luna", "grok-4.5", "grok-4.6", "grok-4.7", "gemini-3.1-pro"}
    doc = rate_doc()
    for model in ("gpt-5.4", "gpt-5.5"):
        assert row(doc, f"github/github_copilot/{model}/2026-06-01")["long_context"] is None
        assert row(doc, f"github/github_copilot/{model}/2026-06-04")["long_context"] is not None
    for r in doc["rows"]:
        if r["long_context"] is not None:
            assert "threshold VERIFY" in r["notes"] and "hypothesis A" in r["notes"]


def test_fast_mode_modifier_and_supports() -> None:
    fast = [r["row_id"] for r in rate_doc()["rows"] if r["supports"]]
    assert fast == ["github/github_copilot/claude-opus-4-8/2026-06-01"]
    mod = {m.modifier_id: m for m in layer().modifiers}["github.fast.opus-4-8"]
    assert mod.kind == "replace_base" and dict(mod.base_usd_per_mtok) == {
        "input": Decimal("10.00"), "output": Decimal("50.00")}
    assert dict(mod.when) == {"channel_in": "github_copilot", "model_in": "claude-opus-4-8",
                              "speed": "fast"}
    mods = {m.modifier_id: m for m in layer().modifiers}
    assert mods["github.auto"].factor == Decimal("0.9")
    assert mods["github.compliance"].factor == Decimal("1.1")
    assert mods["github.auto"].stacking == mods["github.compliance"].stacking == "assumed"


def test_promotional_rows_and_date_sources() -> None:
    doc = rate_doc()
    promos = {r["row_id"]: r["promotion"] for r in doc["rows"] if r["promotion"]}
    assert promos == {
        "github/github_copilot/gpt-5.6-sol/2026-08-20": "github.gpt-5.6-sol.2026-08",
        "github/github_copilot/gpt-5.6-sol/2026-08-21": "github.gpt-5.6-sol.2026-08",
        "github/github_copilot/gemini-3.6-flash/2026-08-13": "github.gemini-3.6-flash.2026",
        "github/github_copilot/gemini-3.7-flash/2026-08-13": "github.gemini-3.7-flash.2026",
        "github/github_copilot/gemini-3.8-flash/2026-09-03": "github.gemini-3.8-flash.2026"}
    starts = {r["row_id"].split("/", 2)[2]: r["notes"].split(";")[0] for r in doc["rows"]}
    assert {k for k, v in starts.items() if v == "date_source=C"} == {
        "claude-opus-5-5/2026-09-22", "gpt-6-sol/2026-09-22", "gpt-6-luna/2026-09-22"}
    assert {k for k, v in starts.items() if v == "date_source=D"} == {"gpt-5.6-sol/2026-09-04"}
    ends_d = {r["row_id"] for r in doc["rows"] if "effective_to date_source=D" in r["notes"]}
    assert ends_d == {"github/github_copilot/gpt-5.6-sol/2026-08-21",
                      *(f"github/github_copilot/gemini-3.{v}-flash/{d}" for v, d in (
                          (6, "2026-08-13"), (7, "2026-08-13"), (8, "2026-09-03")))}
    assert all(row(doc, rid)["effective_to"] == "2027-01-01" for rid in ends_d if "gemini" in rid)


def test_aliases_generations_and_tokenizer_families() -> None:
    doc = rate_doc()
    for r in doc["rows"]:
        assert len(r["aliases"]) == 1 and r["aliases"][0][0].isupper()
        claude47 = r["model"] in ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
                                  "claude-opus-5-5", "claude-sonnet-5", "claude-fable-5",
                                  "claude-fable-5-1")
        assert (r["tokenizer_family"] == "claude-4.7+") == claude47
        if not r["model"].startswith("claude-"):
            assert "tokenizer family assumed" in r["notes"]
    assert row(doc, OPUS)["aliases"] == ["Claude Opus 5.5"]
    assert row(doc, OPUS)["generation"] == "5.5"
    assert row(doc, "github/github_copilot/kimi-k2.7-code/2026-07-01")["generation"] == "2.7"
    assert row(doc, "github/github_copilot/raptor-mini/2026-06-01")["generation"] == "0"
    retired = row(doc, "github/github_copilot/claude-opus-4-5/2026-06-01")
    assert retired["effective_to"] == "2026-09-04" and "retired 2026-09-01" in retired["notes"]


# ---------------------------------------------------------------------------------------------
# extension host (core.extensions): the rate file and the verifier resolve
# ---------------------------------------------------------------------------------------------


def test_extension_rate_file_and_verifier_resolve() -> None:
    notes: list[DataQualityNote] = []
    files = extensions.extension_rate_files(notes)
    assert [f.name for f in files] == ["github_copilot.json"] and notes == []
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert rv.layer_from_json(doc, name="ext").rows == layer().rows
    assert extensions.rate_verifiers(notes=notes) == ["tokenbill.copilot.rates_verify:verify"]
    assert registry.load("tokenbill.copilot.rates_verify:verify") is rv.verify
    assert rv.verify(rv.layer_from_json(doc, name="ext")) == []
