"""The checked-in fixtures: up to date, described by MANIFEST.json, conformant, canary-free."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.testing import MemoryStore, assert_adapter_conforms, fake_price_total

from .helpers import CC, FIXTURES, HEADLESS, bf, canonical, opts

MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
ENTRIES = {e["path"]: e for e in MANIFEST["files"]}
BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
           "cache_write_unknown", "output")


def _files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file()
                  and p.name not in ("MANIFEST.json", "build_fixtures.py")
                  and "__pycache__" not in p.parts)


def test_checked_in_fixtures_equal_a_fresh_build(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    manifest = bf.build(fresh)
    assert manifest == MANIFEST
    for path in _files():
        rel = path.relative_to(FIXTURES)
        assert (fresh / rel).read_bytes() == path.read_bytes(), f"{rel} is stale: rebuild"


def test_manifest_lists_every_fixture_file() -> None:
    assert sorted(ENTRIES) == sorted(p.relative_to(FIXTURES).as_posix() for p in _files())


def _sum(result: object, *, compaction: bool = False) -> dict[str, int]:
    acc = dict.fromkeys(BUCKETS, 0)
    for req in result.requests:
        if req.lane_key.endswith("#compaction") != compaction:
            continue
        for inf in req.attempts[0].inferences:
            for b in BUCKETS:
                acc[b] += getattr(inf.usage, b)
    return acc


@pytest.mark.parametrize("rel", sorted(p for p, e in ENTRIES.items()
                                       if e["adapter"] == "claude-code"))
def test_transcript_fixtures_match_their_manifest(rel: str) -> None:
    e = ENTRIES[rel]["expected"]
    r = CC.read(FIXTURES / rel, opts())
    assert len(r.requests) == e["requests"]
    assert _sum(r) == {b: e["inference_usage"].get(b, 0) for b in BUCKETS}
    naive = dict.fromkeys(BUCKETS, 0)
    for u in r.naive_usage.values():
        for b in BUCKETS:
            naive[b] += getattr(u, b)
    assert naive == {b: e["naive_usage"].get(b, 0) for b in BUCKETS}
    assert len(r.quarantined) == e.get("quarantined", 0)
    comp = e.get("compaction_estimated")
    if comp:
        est = _sum(r, compaction=True)
        assert est["cache_read"] + est["cache_write_5m"] + est["cache_write_1h"] == comp["pre"]
        assert est["output"] == comp["post"]


def test_headless_fixtures_match_their_manifest() -> None:
    stream = HEADLESS.read(FIXTURES / "headless" / "stream.jsonl", opts())
    e = ENTRIES["headless/stream.jsonl"]["expected"]
    steps = [q for q in stream.requests if q.attempts[0].provider_message_id]
    assert len(steps) == e["steps"]
    [res] = [q for q in stream.requests if not q.attempts[0].provider_message_id]
    assert {i.pricing.model: i.usage.output for i in res.attempts[0].inferences} == \
        e["residual_output"]
    only = HEADLESS.read(FIXTURES / "headless" / "result-only.json", opts())
    assert len(only.aggregates) == ENTRIES["headless/result-only.json"]["expected"]["aggregates"]


@pytest.mark.parametrize("path", [p for p in _files() if p.suffix == ".jsonl"
                                  and p.name != "journal.jsonl"
                                  and "headless" not in p.parts] + [FIXTURES / "projects"],
                         ids=lambda p: p.name)
def test_claude_code_adapter_conforms(path: Path) -> None:
    caps = CC.read(path, opts()).capabilities
    result = assert_adapter_conforms(CC, path, expect_capabilities=caps)
    assert result.capabilities <= CC.capabilities


@pytest.mark.parametrize("path", sorted((FIXTURES / "headless").iterdir()), ids=lambda p: p.name)
def test_headless_adapter_conforms(path: Path) -> None:
    caps = HEADLESS.read(path, opts()).capabilities
    assert_adapter_conforms(HEADLESS, path, expect_capabilities=caps)


def test_full_capability_sets() -> None:
    whole = CC.read(FIXTURES / "projects", opts())
    assert whole.capabilities == CC.capabilities
    ci = HEADLESS.read(FIXTURES / "headless" / "stream.jsonl",
                       opts(attr={"workload_class": "ci"}))
    assert ci.capabilities == {"usage_sequence", "ttl_split", "iterations", "params", "workload"}


@pytest.mark.parametrize("mode", ["install", "central", "two-stage"])
def test_canary_absent_from_every_result(mode: str) -> None:
    o = opts(identity_mode=mode, principal_ref="dev-7")
    for path in _files():
        for adapter in (CC, HEADLESS):
            if not adapter.sniff(path, path.read_bytes()[:65536]):
                continue
            r = adapter.read(path, o)
            assert_no_canary(repr(r), canonical(r))


def test_fixture_tree_ingests_and_prices_on_the_fakes() -> None:
    store = MemoryStore(org_key=bytes(range(64, 96)), name_key_id=opts().name_key_id)
    for path in _files():
        for adapter in (CC, HEADLESS):
            if adapter.sniff(path, path.read_bytes()[:65536]):
                store.ingest(adapter.read(path, opts(principal_ref="dev-7")))
    from tokenbill.core.testing import FakePricer

    pricer = FakePricer()
    items = [(inf, r.ts_ms) for r in store.iter_usage_records()
             for inf in [_as_inference(r)]]
    total = fake_price_total(pricer, items)
    assert total.exact.nano is not None and total.exact.nano > 0
    assert total.estimated is not None          # MSO output and the compaction estimate


def _as_inference(record: object) -> object:
    from tokenbill.core.records import Inference

    return Inference(inference_id=record.inference_id, kind=record.kind, usage=record.usage,
                     pricing=record.pricing, usage_source=record.usage_source,
                     billable=record.billable, billing_rule_id=record.billing_rule_id)
