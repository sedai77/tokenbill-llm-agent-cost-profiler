"""The checked-in fixtures: MANIFEST.json is complete, the build script reproduces every file
byte-identically, and every adapter reproduces the closed-form expectations the script computed
without importing tokenbill. Also: the fixtures ingest into the foundation MemoryStore."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tokenbill.core.ids import key_id
from tokenbill.core.testing import MemoryStore

from .helpers import FIXTURES, MANIFEST, NAME_KEY, PRINCIPAL_KEY, fixture, read, tokens

REPO = Path(__file__).resolve().parents[3]


def _data_files() -> list[str]:
    return sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*")
                  if p.is_file() and p.name not in ("MANIFEST.json", "build_fixtures.py")
                  and "__pycache__" not in p.parts)


def test_manifest_lists_every_fixture() -> None:
    listed = sorted(e["path"] for e in MANIFEST["files"])
    assert listed == _data_files()
    assert MANIFEST["schema"] == "tokenbill/admin-fixtures@1" and MANIFEST["synthetic"] is True
    for entry in MANIFEST["files"]:
        assert entry["provenance"].startswith("synthetic; shape from ")
        assert entry["role"] in ("recon_pair", "pagination", "edge", "k_anonymity",
                                 "enterprise", "openai", "cloud")
    (pair,) = MANIFEST["recon_pairs"]
    assert (FIXTURES / pair["usage"]).is_file() and (FIXTURES / pair["cost"]).is_file()


def test_build_script_reproduces_fixtures(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = root / "tests" / "v2" / "fixtures" / "admin"
    target.mkdir(parents=True)
    shutil.copy(FIXTURES / "build_fixtures.py", target / "build_fixtures.py")
    (root / "tokenbill" / "core").mkdir(parents=True)
    shutil.copy(REPO / "tokenbill" / "core" / "facts.json", root / "tokenbill" / "core")
    subprocess.run([sys.executable, str(target / "build_fixtures.py")], check=True,
                   capture_output=True, env={**os.environ, "PYTHONHASHSEED": "0"}, cwd=tmp_path)
    for rel in [*_data_files(), "MANIFEST.json"]:
        built, checked_in = (target / rel).read_bytes(), (FIXTURES / rel).read_bytes()
        if not rel.endswith(".gz"):  # a Windows checkout may have converted LF to CRLF
            built, checked_in = (x.replace(b"\r\n", b"\n") for x in (built, checked_in))
        assert built == checked_in, rel


@pytest.mark.parametrize("entry", MANIFEST["files"], ids=lambda e: e["path"])
def test_expectations_match_adapters(entry: dict) -> None:
    result = read(entry["adapter"], fixture(entry["path"]))
    expect = entry["expect"]
    checks = {
        "aggregates": len(result.aggregates),
        "cost_lines": len(result.cost_lines),
        "usage_tokens": tokens(result),
        "amount_nano": sum(c.amount_nano for c in result.cost_lines),
        "list_amount_nano": sum(c.list_amount_nano or 0 for c in result.cost_lines),
        "reported_cost_nano": sum(a.reported_cost_nano or 0 for a in result.aggregates),
        "rounding_remainder_e18": result.stats["rounding_remainder_e18"],
        "dropped_groups": result.stats.get("groups_dropped", 0),
    }
    for key, got in checks.items():
        if key in expect:
            assert got == expect[key], key
    for key in ("rows_skipped_not_bedrock", "rows_skipped_tax", "rows_unit_unknown",
                "rows_skipped_not_claude"):
        if key in expect:
            assert result.stats[key] == expect[key], key
    if "outcomes" in expect:
        assert sorted((o.date_utc, o.team, o.n_users, o.commits) for o in result.outcomes) == \
            sorted((o["date"], o["team"], o["n_users"], o["commits"]) for o in expect["outcomes"])
    if "finality" in expect:
        assert {x.finality for x in [*result.aggregates, *result.cost_lines]} == {
            expect["finality"]}


def test_all_fixtures_ingest_into_memory_store() -> None:
    store = MemoryStore(org_key=PRINCIPAL_KEY, name_key_id=key_id(NAME_KEY))
    counts = {"aggregates": 0, "cost_lines": 0, "outcomes": 0}
    seen_lines: set[str] = set()
    seen_aggs: set[str] = set()
    for entry in MANIFEST["files"]:
        result = read(entry["adapter"], fixture(entry["path"]))
        got = store.ingest(result)
        assert got["dq.name_key_mismatch"] == 0 and got["principals_nulled"] == 0
        for k in counts:
            counts[k] += got[k]
        seen_lines |= {c.line_id for c in result.cost_lines}
        seen_aggs |= {a.agg_id for a in result.aggregates}
        again = store.ingest(result)
        assert again["skipped"] == 1                         # idempotent re-ingest
    window = {"since_ms": 0, "until_ms": 2**53}
    assert {c.line_id for c in store.cost_lines(**window)} == seen_lines
    assert {a.agg_id for a in store.aggregates(**window)} == seen_aggs
    principals = {c.principal for c in store.cost_lines("aws.cur2", **window) if c.principal}
    assert principals and all(p.startswith("p_") for p in principals)
    assert len(store.outcomes(**window)) == 4
    assert store.cost_lines("anthropic.cost_report", **window)
