"""Cross-adapter behavior: conformance on every fixture, sniffing, registry paths, page loading,
wrappers, options, determinism and the shared parsers (SPEC §3.18, §5.1, §5.11, §5.13)."""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.adapters import anthropic_admin as aa
from tokenbill.adapters.anthropic_admin import (
    BadRecord,
    UsageReportAdapter,
    classify_head,
    classify_page,
    money_scaled,
    scaled_to_nano,
    tokens,
    ts_ms,
    unix_s_to_ms,
)
from tokenbill.core import registry
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.protocols import Adapter
from tokenbill.core.registry import _read_head
from tokenbill.core.testing import assert_adapter_conforms
from tokenbill.core.types import IngestOptions

from .helpers import (
    ADAPTERS,
    FIXTURES,
    MANIFEST,
    NAME_KEY,
    fixture,
    opts,
    read,
    result_json,
    write_json,
    write_jsonl,
)

REPO = Path(__file__).resolve().parents[3]
OWN_MODULES = ("tokenbill/adapters/anthropic_admin.py", "tokenbill/adapters/openai_admin.py",
               "tokenbill/adapters/cloud_billing.py")
FILES = [e["path"] for e in MANIFEST["files"]]


def _expected_caps(result_adapter: str, entry: dict) -> frozenset[str]:
    e = entry["expect"]
    caps = set()
    if e.get("aggregates") or e.get("usage_aggregates"):
        caps.add("aggregates")
    if e.get("cost_lines"):
        caps.add("cost")
    if e.get("outcomes"):
        caps.add("outcomes")
    if result_adapter in ("anthropic-cc-analytics", "aws-cur", "gcp-billing") or (
            entry["path"].endswith(("user_usage_report.json", "user_cost_report.json"))):
        caps.add("attribution.team")
    return frozenset(caps)


@pytest.mark.parametrize("rel", FILES)
def test_adapter_conforms_on_every_fixture(rel: str) -> None:
    entry = next(e for e in MANIFEST["files"] if e["path"] == rel)
    adapter = ADAPTERS[entry["adapter"]]()
    result = assert_adapter_conforms(adapter, fixture(rel), opts=opts(),
                                     expect_capabilities=_expected_caps(entry["adapter"], entry))
    assert result.quarantined == []
    assert_no_canary(result_json(result), repr(result))
    assert result.requests == [] and result.sessions == [] and result.events == []


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_conformance_with_default_options(name: str) -> None:
    """``assert_adapter_conforms`` with its own default options (install mode, its keys)."""
    rel = next(e["path"] for e in MANIFEST["files"] if e["adapter"] == name)
    adapter = ADAPTERS[name]()
    result = adapter.read(fixture(rel), opts())
    assert_adapter_conforms(adapter, fixture(rel), expect_capabilities=result.capabilities)


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_registry_paths(name: str) -> None:
    adapter = registry.get_adapter(name)
    assert isinstance(adapter, ADAPTERS[name])
    assert isinstance(adapter, Adapter)
    assert adapter.name == name
    assert isinstance(adapter.capabilities, frozenset) and adapter.capabilities


def test_sniff_matrix() -> None:
    """Each fixture is claimed by exactly its own adapter among the eight ADMIN adapters."""
    for entry in MANIFEST["files"]:
        path = fixture(entry["path"])
        head = _read_head(path)
        claimed = sorted(n for n, cls in ADAPTERS.items() if cls().sniff(path, head))
        assert claimed == [entry["adapter"]], entry["path"]


def test_sniff_on_truncated_heads() -> None:
    """Heads cut inside a large page still classify by key names."""
    for entry in MANIFEST["files"]:
        if entry["adapter"] in ("aws-cur", "gcp-billing"):
            continue
        path = fixture(entry["path"])
        head = _read_head(path)[:700]
        cls = ADAPTERS[entry["adapter"]]
        assert cls().sniff(path, head), entry["path"]


def test_sniff_rejects_other_sources(tmp_path: Path) -> None:
    samples = [b"", b"\x00\xff\xfe", b'{"type":"assistant","message":{"usage":{}}}',
               b"timestamp,foo\n1,2\n", b"[1,2,3]", b'{"data": []}', b'{"data": [1]}',
               b'{"results": [], "starting_at": "x"}']
    for data in samples:
        path = tmp_path / "x.json"
        path.write_bytes(data)
        for cls in ADAPTERS.values():
            assert cls().sniff(path, data) is False, (cls.name, data)


def test_classify_page_hints_and_shapes() -> None:
    assert classify_page({}, "https://api.anthropic.com/v1/organizations/cost_report?x=1") == (
        aa.K_COST)
    assert classify_page({"data": []}, "/v1/organization/usage/completions") == aa.K_OAI_USAGE
    assert classify_page({"data": []}) is None
    assert classify_page({"object": "page", "data": [{"object": "bucket", "results": [
        {"object": "organization.costs.result"}]}]}) == aa.K_OAI_COSTS
    assert classify_page({"object": "page", "data": [{"object": "bucket", "results": [
        {"object": "something.else"}]}]}) is None
    assert classify_page({"data": [{"actor": {}, "x": 1}]}) is None
    assert classify_page({"data": [{"core_metrics": {}}]}) == aa.K_CC
    assert classify_page({"data": [{"results": [{"x": 1}]}]}) is None
    assert classify_page({"data": [{"results": []}], "organization_id": "o"}) is None
    assert classify_head(b'{"endpoint": "/v1/organizations/usage_report/claude_code", '
                         b'"response": {"data": [') == aa.K_CC
    assert classify_head(b'{"data":[{"actor":{"id":1},"tokens":{') is None


def test_wrapped_pages_arrays_and_gzip(tmp_path: Path) -> None:
    page = json.loads(fixture("anthropic/usage_report_2026-08.json").read_text())
    direct = read("anthropic-usage-report", fixture("anthropic/usage_report_2026-08.json"))
    wrapped = {"endpoint": "/v1/organizations/usage_report/messages",
               "fetched_at": "2026-09-22T10:00:00Z", "response": page}
    variants = {
        "wrapped.json": json.dumps(wrapped).encode(),
        "array.json": json.dumps([page]).encode(),
        "pages.jsonl": (json.dumps(page) + "\n").encode(),
        "bom.json": b"\xef\xbb\xbf" + json.dumps(page).encode(),
    }
    for name, data in variants.items():
        path = tmp_path / name
        path.write_bytes(data)
        res = read("anthropic-usage-report", path)
        assert [a.usage for a in res.aggregates] == [a.usage for a in direct.aggregates], name
        if name == "wrapped.json":
            assert {a.fetched_ms for a in res.aggregates} == {ts_ms("2026-09-22T10:00:00Z",
                                                                    "t")}
    gz = tmp_path / "page.json.gz"
    gz.write_bytes(gzip.compress(json.dumps(wrapped).encode(), mtime=0))
    assert UsageReportAdapter().sniff(gz, _read_head(gz))
    res = read("anthropic-usage-report", gz)
    assert len(res.aggregates) == len(direct.aggregates)
    wrapped_ms = dict(wrapped, fetched_at=None, fetched_ms=123)
    res = read("anthropic-usage-report", write_json(tmp_path / "ms.json", wrapped_ms))
    assert {a.fetched_ms for a in res.aggregates} == {123}


def test_other_kind_page_is_quarantined_in_a_file_and_skipped_in_a_directory(
        tmp_path: Path) -> None:
    cost_page = json.loads(fixture("anthropic/cost_report_2026-08.json").read_text())
    path = write_json(tmp_path / "file" / "c.json", cost_page)
    res = read("anthropic-usage-report", path)
    assert [q.reason for q in res.quarantined] == ["bad_type:page"]
    d = tmp_path / "dir"
    write_json(d / "c.json", cost_page)
    write_json(d / "u.json", json.loads(fixture("anthropic/usage_report_2026-08.json")
                                        .read_text()))
    write_json(d / "junk.json", {"hello": "world"})
    res = read("anthropic-usage-report", d)
    assert res.quarantined == [] and res.stats["documents_skipped"] == 2
    assert len(res.aggregates) == 8
    res = read("anthropic-usage-report", write_json(tmp_path / "j.json", {"hello": 1}))
    assert [q.reason for q in res.quarantined] == ["bad_type:page"]
    res = read("anthropic-usage-report", write_json(tmp_path / "e.json", {"data": [
        {"starting_at": "2026-08-01", "ending_at": "2026-08-02", "results": []}]}))
    assert res.quarantined == [] and res.stats["empty_pages"] == 1


def test_bad_documents(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    page = json.loads(fixture("anthropic/usage_report_2026-08.json").read_text())
    path.write_bytes(json.dumps(page).encode() + b"\n{not json\n42\n\xff\xfe\n\n")
    res = read("anthropic-usage-report", path)
    assert [(q.locator, q.reason) for q in res.quarantined] == [
        ("line:2", "bad_json"), ("line:3", "not_object"), ("line:4", "bad_json")]
    arr = write_json(tmp_path / "arr.json", [page, 5])
    assert [q.reason for q in read("anthropic-usage-report", arr).quarantined] == ["not_object"]
    scalar = write_json(tmp_path / "s.json", "just a string")
    assert [q.reason for q in read("anthropic-usage-report", scalar).quarantined] == [
        "not_object"]
    for q in read("anthropic-usage-report", path).quarantined:
        assert q.source_id.startswith("s_")


def test_large_documents_are_streamed_as_jsonl(tmp_path: Path, monkeypatch) -> None:
    page = json.loads(fixture("anthropic/usage_report_2026-08.json").read_text())
    path = write_jsonl(tmp_path / "big.jsonl", [page, page])
    monkeypatch.setattr(aa, "MAX_DOC_BYTES", 100)
    res = read("anthropic-usage-report", path)
    assert res.stats["pages"] == 2 and len(res.aggregates) == 8


def test_name_key_required_and_options_type(tmp_path: Path) -> None:
    path = fixture("anthropic/usage_report_2026-08.json")
    with pytest.raises(UsageError, match="name key"):
        UsageReportAdapter().read(path, IngestOptions())
    with pytest.raises(UsageError):
        UsageReportAdapter().read(path, {"name_key": NAME_KEY})  # type: ignore[arg-type]
    with pytest.raises(SourceError, match="not found"):
        UsageReportAdapter().read(tmp_path / "missing.json", opts())


def test_window_filters_cost_lines_and_aggregates() -> None:
    since = ts_ms("2026-08-11", "d")
    res = read("anthropic-cost-report", fixture("anthropic/cost_report_2026-08.json"),
               since_ms=since)
    assert {c.date_utc for c in res.cost_lines} == {"2026-08-11"}
    res = read("anthropic-usage-report", fixture("anthropic/usage_report_2026-08.json"),
               until_ms=since)
    assert len(res.aggregates) == 4 and res.stats["filtered_by_window"] == 4


def test_source_info_is_content_free() -> None:
    path = fixture("anthropic/usage_report_2026-08.json")
    res = read("anthropic-usage-report", path)
    assert res.source.adapter == "anthropic-usage-report"
    assert res.source.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert res.source.bytes == path.stat().st_size
    assert path.name not in result_json(res)
    assert res.source.name_key_id == key_id(NAME_KEY)


def test_deterministic_across_processes() -> None:
    code = (
        "import hashlib, json, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from tests.v2.admin.helpers import MANIFEST, fixture, read, result_json\n"
        "out = hashlib.sha256()\n"
        "for e in MANIFEST['files']:\n"
        "    out.update(result_json(read(e['adapter'], fixture(e['path']))).encode())\n"
        "print(out.hexdigest())\n")
    digests = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              check=True, env={**os.environ, "PYTHONHASHSEED": seed},
                              cwd=REPO).stdout.strip() for seed in ("1", "2")}
    assert len(digests) == 1 and len(next(iter(digests))) == 64


def test_no_float_in_owned_modules() -> None:
    for rel in OWN_MODULES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "float"), rel
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), rel


def test_no_network_imports_in_owned_modules() -> None:
    for rel in OWN_MODULES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for mod in ("socket", "urllib", "http.client", "requests"):
            assert f"import {mod}" not in text and f"from {mod}" not in text


# ---------------------------------------------------------------------------------------------
# shared parsers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("2025-08-01T00:00:00Z", 1_754_006_400_000),
    ("2025-08-01", 1_754_006_400_000),
    ("2025-08-01T02:00:00+02:00", 1_754_006_400_000),
    ("2025-08-01T00:00:00.123456Z", 1_754_006_400_123),
    ("2025-07-31 22:00:00-02", 1_754_006_400_000),
    ("2025-08-01 00:00:00 UTC", 1_754_006_400_000),
    ("1970-01-01", 0),
])
def test_ts_ms(text: str, expected: int) -> None:
    assert ts_ms(text, "t") == expected


@pytest.mark.parametrize("bad", [None, 5, "", "2025-13-01", "2025-02-30", "2025-08-01T24:00:00Z",
                                 "1969-12-31T23:00:00Z", "2025-08-01T00:00:00+25:00", "x" * 90,
                                 "2025-08-01T00:00:61Z", "tomorrow"])
def test_ts_ms_rejects(bad: object) -> None:
    with pytest.raises(BadRecord):
        ts_ms(bad, "t")


def test_unix_seconds() -> None:
    assert unix_s_to_ms(1, "t") == 1000
    for bad in (None, True, 1.5, -1, 2**53):
        with pytest.raises(BadRecord):
            unix_s_to_ms(bad, "t")


def test_token_counts() -> None:
    assert tokens(5, "x") == 5
    assert tokens(Decimal("12.0"), "x") == 12
    assert tokens(Decimal("1E+2"), "x") == 100
    for bad in (-1, True, "5", Decimal("1.5"), Decimal("1E+30"), 2**53 + 1, None,
                Decimal("NaN")):
        with pytest.raises(BadRecord):
            tokens(bad, "x")


def test_money_scaled_and_rounding() -> None:
    sc = money_scaled("12345.678", "a", cents=True)
    assert scaled_to_nano(sc) == (123_456_780_000, 0)
    assert scaled_to_nano(money_scaled("0.00000000001", "a", cents=True)) == (0, 100_000)
    neg = money_scaled(Decimal("-0.0000000015"), "a", cents=False)
    assert scaled_to_nano(neg) == (-2, 500_000_000)   # half-even: −1.5 nano → −2
    assert money_scaled(0, "a", cents=False) == 0
    assert money_scaled("-0E-50", "a", cents=True) == 0
    for bad in (None, True, 1.5, "abc", "1e30", "1e-201", "NaN", "Infinity", "", "1" * 600, []):
        with pytest.raises(BadRecord):
            money_scaled(bad, "a", cents=False)


def test_canary_only_in_inputs() -> None:
    """The fixtures do carry the canary (so its absence from outputs means something)."""
    hits = [p for p in FIXTURES.rglob("*") if p.is_file() and p.suffix != ".py"
            and CANARY.encode() in (gzip.decompress(p.read_bytes()) if p.suffix == ".gz"
                                    else p.read_bytes())]
    assert len(hits) >= 6
