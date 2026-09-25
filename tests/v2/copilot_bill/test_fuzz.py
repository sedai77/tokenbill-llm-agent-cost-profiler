"""Hypothesis fuzz of the CSV and JSON parsers (only ``TokenbillError`` may escape, SPEC §21 #5)
and exactness properties of the AI usage report parser."""

from __future__ import annotations

import json
import os
import tempfile
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core.types import IngestResult

from .helpers import ADAPTERS, opts

EXAMPLES = int(os.environ.get("TB_CP_BILL_FUZZ_EXAMPLES", "60"))
SETTINGS = settings(max_examples=EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
AI_HEADER = ["date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
             "gross_amount", "discount_amount", "net_amount", "username", "organization",
             "repository", "cost_center_name", "model", "input", "output", "cache_read",
             "cache_write", "total_monthly_quota", "aic_quantity"]
METERED_HEADER = ["date", "product", "sku", "quantity", "unit_type", "gross_amount",
                  "discount_amount", "net_amount", "username", "organization", "repository",
                  "workflow_path", "cost_center_name"]
JSON_KEYS = ["usageItems", "timePeriod", "year", "month", "day", "sku", "product", "model",
             "unitType", "grossAmount", "discountAmount", "netAmount", "netQuantity", "quantity",
             "date", "organization", "user", "enterprise", "costCenter", "name", "id",
             "request", "response", "path", "query", "report_type", "status",
             "organizationName", "repositoryName"]

cells = st.one_of(
    st.text(max_size=12),
    st.sampled_from(["2026-09-01", "9/1/26", "2026-09-01T10:00:00Z", "copilot",
                     "copilot_ai_credit", "actions", "sandbox", "linux_16_core", "Unknown",
                     "3900", "0", "1e999", "-1", "0.4272621300000001", "NaN", "Infinity",
                     "Auto: Claude Haiku 4.5", "Copilot Code Review", "ai-credits",
                     "requests", ".github/workflows/x.lock.yml",
                     "dynamic/copilot-swe-agent/copilot", "tb-canary-login-7f3a91"]),
    st.integers(min_value=-10**20, max_value=10**20).map(str),
    st.decimals(allow_nan=False, allow_infinity=False, places=12).map(str))


def _csv_cell(cell: str) -> str:
    return '"' + cell.replace('"', '""') + '"' if any(c in cell for c in ',"\r\n') else cell


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    """The rows as CSV text, written by hand (QUOTE_MINIMAL, as the 3.11+ ``csv`` writer does):
    Python 3.10's writer refuses a NUL cell, and the adapter must see (and survive) one."""
    return "".join(",".join(_csv_cell(c) for c in row) + "\r\n" for row in [header, *rows])


def _read(name: str, data: bytes, suffix: str = ".csv") -> IngestResult | None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"fuzz{suffix}"
        path.write_bytes(data)
        adapter = ADAPTERS[name]
        adapter.sniff(path, data[:65536])
        try:
            result = adapter.read(path, opts(experimental=frozenset({"copilot-report-quota"})))
        except TokenbillError:
            return None
    for line in result.cost_lines:
        assert line.principal is None or line.principal.startswith("p_")
    assert "tb-canary-login-7f3a91" not in repr(result)
    return result


@SETTINGS
@given(st.lists(st.lists(cells, min_size=0, max_size=len(AI_HEADER) + 2), max_size=8))
def test_fuzz_ai_usage_rows(rows: list[list[str]]) -> None:
    result = _read("github-ai-usage", _csv_text(AI_HEADER, rows).encode())
    if result is not None:
        cov = [a for a in result.aggregates if a.source_kind.endswith(".coverage")]
        assert sum(a.reported_cost_nano or 0 for a in cov) == sum(
            c.amount_nano for c in result.cost_lines)
        assert len(result.cost_lines) + len(result.quarantined) <= max(1, len(rows))


@SETTINGS
@given(st.lists(st.lists(cells, min_size=0, max_size=len(METERED_HEADER) + 1), max_size=8))
def test_fuzz_metered_rows(rows: list[list[str]]) -> None:
    _read("github-metered-usage", _csv_text(METERED_HEADER, rows).encode())


@SETTINGS
@given(st.binary(max_size=400), st.sampled_from(["github-ai-usage", "github-metered-usage",
                                                 "github-billing-api"]))
def test_fuzz_raw_bytes(data: bytes, name: str) -> None:
    prefix = {"github-ai-usage": ",".join(AI_HEADER).encode() + b"\n",
              "github-metered-usage": ",".join(METERED_HEADER).encode() + b"\n",
              "github-billing-api": b""}[name]
    _read(name, prefix + data, ".jsonl" if name == "github-billing-api" else ".csv")


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(min_value=-10**30, max_value=10**30),
              st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=10),
              st.sampled_from(["Copilot", "Actions", "Copilot AI Credits", "AI Credit",
                               "2026-09-01", "credits", "/enterprises/e/settings/billing/usage",
                               "/orgs/o/settings/billing/ai_credit/usage"])),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.sampled_from(JSON_KEYS), children, max_size=6)),
    max_leaves=25)


@SETTINGS
@given(st.lists(json_values, min_size=1, max_size=4), st.booleans())
def test_fuzz_billing_json(docs: list[object], as_lines: bool) -> None:
    if as_lines:
        data = "".join(json.dumps(d) + "\n" for d in docs).encode()
        _read("github-billing-api", data, ".jsonl")
    else:
        _read("github-billing-api", json.dumps(docs[0]).encode(), ".json")


money = st.decimals(min_value=0, max_value=10**6, places=10, allow_nan=False,
                    allow_infinity=False)


@SETTINGS
@given(st.lists(st.tuples(st.integers(1, 28), st.sampled_from(["u1", "u2", ""]),
                          st.sampled_from(["Claude Sonnet 5", "Auto: GPT-5.5", "Code Review"]),
                          money, money, st.lists(st.integers(0, 10**9), min_size=4,
                                                 max_size=4)), max_size=12))
def test_exact_money_and_token_sums(rows: list[tuple]) -> None:
    body = [[f"2026-08-{d:02d}", "copilot", "copilot_ai_credit", "1", "ai-credits", "0.01",
             str(g), str(disc), str(g - disc), user, "acme", "", "", model, *map(str, t), "", ""]
            for d, user, model, g, disc, t in rows]
    result = _read("github-ai-usage", _csv_text(AI_HEADER, body).encode())
    assert result is not None and not result.quarantined
    nano = sum(int((Decimal(r[8]) * 10**9).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
               for r in body)
    assert sum(c.amount_nano for c in result.cost_lines) == nano
    tokens = sum(sum(t) for *_, t in rows)
    aggs = [a for a in result.aggregates if a.source_kind == "github.ai_usage_report"]
    assert sum(a.usage.total_input + a.usage.output for a in aggs) == tokens
    assert sum(a.reported_cost_nano or 0 for a in aggs) == nano
