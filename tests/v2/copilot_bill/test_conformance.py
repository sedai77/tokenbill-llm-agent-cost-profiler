"""Conformance, registration, sniffing, conventions (golden sum-checks), the no-float rule and the
checked-in fixtures (MANIFEST completeness, byte-identical rebuild, expectations)."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tokenbill.adapters import github_billing as gb
from tokenbill.core import conventions, registry
from tokenbill.core.conventions import BadUsageError
from tokenbill.core.records import PricingContext, UsageBuckets
from tokenbill.core.testing import FakePricer, assert_adapter_conforms, conformance_ingest_options

from .helpers import ADAPTERS, ENTRIES, FIXTURES, MANIFEST, ms, notes, read

MODULE = Path(gb.__file__)


@pytest.mark.parametrize(("name", "rel", "caps", "flags"), [
    ("github-ai-usage", "ai_usage_2026-09.csv", {"aggregates", "cost", "copilot_billing"}, ()),
    ("github-ai-usage", "ai_usage_quota_ui.csv",
     {"aggregates", "cost", "copilot_billing", "config"}, ("copilot-report-quota",)),
    ("github-metered-usage", "detailed_2026-09.csv", {"cost", "copilot_billing"}, ()),
    ("github-billing-api", "rest/pull_pages_2026-09.jsonl", {"cost", "copilot_billing"}, ()),
    ("github-billing-api", "rest/oas_ai_credit_user.json", {"cost", "copilot_billing"}, ())])
def test_assert_adapter_conforms(name: str, rel: str, caps: set[str], flags: tuple) -> None:
    team_map = tuple(sorted(MANIFEST["team_map"].items()))
    options = conformance_ingest_options(identity_mode="central-ingest", team_map=team_map,
                                         experimental=frozenset(flags))
    adapter = registry.get_adapter(name)
    assert isinstance(adapter, type(ADAPTERS[name]))
    assert_adapter_conforms(adapter, FIXTURES / rel, expect_capabilities=caps, opts=options)


def test_sniff_picks_the_right_adapter() -> None:
    for entry in MANIFEST["files"]:
        path = FIXTURES / entry["path"]
        head = path.read_bytes()[:65536]
        for name, adapter in ADAPTERS.items():   # exactly one of the three accepts
            assert adapter.sniff(path, head) is (name == entry["adapter"]), (name, path.name)
        chosen = registry.sniff_adapter(path)
        assert chosen is not None and chosen.name == entry["adapter"], entry["path"]


def test_sniffers_reject_other_shapes() -> None:
    ai, metered, api = (ADAPTERS[n] for n in ("github-ai-usage", "github-metered-usage",
                                              "github-billing-api"))
    detailed = (FIXTURES / "detailed_2026-09.csv").read_bytes()
    report = (FIXTURES / "ai_usage_2026-09.csv").read_bytes()
    assert not ai.sniff(Path("x"), detailed) and not metered.sniff(Path("x"), report)
    assert not api.sniff(Path("x"), report) and not ai.sniff(Path("x"), b'{"usageItems": []}')
    budgets = b'{"request": {"path": "/enterprises/e/settings/billing/budgets"}, "response": {}}'
    assert not api.sniff(Path("x"), budgets) and not api.sniff(Path("x"), b"\xff\xfe")
    assert api.sniff(Path("x"), b'{"request": {"path": "/orgs/o/settings/billing/usage"}, '
                                b'"response": {}}\n')
    for sniffer in (ai.sniff, metered.sniff, api.sniff):
        assert sniffer(Path("x"), None) is False  # type: ignore[arg-type]


def test_registry_lists_the_module_for_conventions() -> None:
    assert "tokenbill.adapters.github_billing" in registry.CONVENTION_MODULES
    excl = conventions.get_convention("github.ai_usage_report.excl")
    incl = conventions.get_convention("github.ai_usage_report.incl")
    assert (excl.inclusive_input, incl.inclusive_input) == (False, True)
    assert excl.enabled and incl.enabled and excl.provider == "github"


@pytest.mark.parametrize("conv", ["excl", "incl"])
def test_convention_golden_sum_check(conv: str) -> None:
    golden = json.loads((FIXTURES / f"conventions/golden_{conv}.json").read_text())
    pricer = FakePricer()
    for row in golden["rows"]:
        buckets, dq = conventions.normalize(golden["convention"], row["raw"])
        assert dq == [] and buckets == UsageBuckets(**row["buckets"])
        ctx = PricingContext(provider="github", channel="github_copilot", model=row["model"],
                             model_raw=row["model"], billing_path="copilot_pool",
                             routing=row["routing"])
        priced = pricer.price_usage(buckets, ctx, ts_ms=ms(row["date"]))
        assert priced.figure.nano == row["gross_nano"]           # Σ buckets reproduce gross
        # the other reading fails the sum check (incl: read + write > input is flagged)
        other = "incl" if conv == "excl" else "excl"
        wrong, dq = conventions.normalize(f"github.ai_usage_report.{other}", row["raw"])
        assert dq == ["dq.convention_mismatch"] or pricer.price_usage(
            wrong, ctx, ts_ms=ms(row["date"])).figure.nano != row["gross_nano"]


def test_convention_edge_cases() -> None:
    raw = {"input": 10, "cache_read": 8, "cache_write": 5, "output": 1}
    assert gb.normalize_report_incl(raw) == (UsageBuckets(
        uncached_input=10, cache_read=8, cache_write_unknown=5, output=1),
        ["dq.convention_mismatch"])
    alias = {"total_input_tokens": 30, "total_cache_read_tokens": 10,
             "total_cache_creation_tokens": 5, "total_output_tokens": 2}
    assert gb.normalize_report_incl(alias)[0] == UsageBuckets(
        uncached_input=15, cache_read=10, cache_write_unknown=5, output=2)
    assert gb.normalize_report_excl({})[0] == UsageBuckets()
    for bad in ({"input": -1}, {"output": "5"}, {"cache_read": True}, {"input": 2**60}):
        with pytest.raises(BadUsageError):
            gb.normalize_report_excl(bad)
    with pytest.raises(BadUsageError):
        gb.normalize_report_incl([1, 2])  # type: ignore[arg-type]


def test_no_float_in_the_money_module() -> None:
    tree = ast.parse(MODULE.read_text())
    floats = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant)
              and isinstance(n.value, float)]
    calls = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "float"]
    assert floats == [] and calls == []


def _data_files() -> list[str]:
    return sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*")
                  if p.is_file() and p.name not in ("MANIFEST.json", "build_fixtures.py")
                  and "__pycache__" not in p.parts)


def test_manifest_lists_every_fixture_with_a_provenance_class() -> None:
    listed = {e["path"] for e in MANIFEST["files"]}
    goldens = {p for p in _data_files() if p.startswith("conventions/")}
    assert listed | goldens == set(_data_files())
    assert MANIFEST["schema"] == "tokenbill/copilot-bill-fixtures@1"
    for e in MANIFEST["files"]:
        assert e["provenance"] in ("primary", "third-party", "real-redacted")
        assert e["values"] == "synthetic" and e["schema_source"] and e["adapter"] in ADAPTERS


def test_build_script_reproduces_the_fixtures(tmp_path: Path) -> None:
    target = tmp_path / "copilot_bill"
    target.mkdir()
    shutil.copy(FIXTURES / "build_fixtures.py", target / "build_fixtures.py")
    subprocess.run([sys.executable, str(target / "build_fixtures.py")], check=True,
                   capture_output=True, cwd=tmp_path)
    for rel in [*_data_files(), "MANIFEST.json"]:
        built = (target / rel).read_bytes().replace(b"\r\n", b"\n")
        assert built == (FIXTURES / rel).read_bytes().replace(b"\r\n", b"\n"), rel


_COUNTS = ("rows", "cost_lines", "quarantined", "export_envelopes", "pages",
           "duplicate_keys", "backfill_duplicates_dropped", "user_workflows_dropped",
           "actions_unattributed_dropped", "rows_other_products", "items_other_products",
           "rounding_remainder_e18", "quota_unknown")


@pytest.mark.parametrize("rel", sorted(ENTRIES))
def test_manifest_expectations(rel: str) -> None:
    exp = ENTRIES[rel]["expect"]
    flags = frozenset({"copilot-report-quota"}) if "quota_users" in exp else frozenset()
    r = read(rel, experimental=flags)
    for key in _COUNTS:
        if key in exp:
            assert r.stats.get(key, 0) == exp[key], key
    assert sum(c.amount_nano for c in r.cost_lines) == exp.get("net_nano", 0)
    if "gross_nano" in exp:
        assert sum(c.list_amount_nano for c in r.cost_lines) == exp["gross_nano"]
    if "tokens" in exp and exp["tokens"]:
        aggs = [a for a in r.aggregates if a.source_kind == "github.ai_usage_report"]
        assert sum(a.usage.total_input + a.usage.output for a in aggs) == exp["tokens"]
    if "quota_users" in exp:
        assert sum(dict(c.attrs)["n_users"] for c in r.config) == exp["quota_users"]
    if "unattributed" in exp:
        assert notes(r)["dq.copilot_unattributed_rows"] == exp["unattributed"]
    assert not (r.requests or r.sessions or r.events)       # no inferences (ruling R-E43)
