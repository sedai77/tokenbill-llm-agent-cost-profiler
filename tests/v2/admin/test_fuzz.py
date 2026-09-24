"""Hypothesis fuzz of every ADMIN parser (SPEC §21 #5): only ``TokenbillError`` subclasses may
escape, outputs stay valid records, person fields never leak, money stays exact."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters.anthropic_admin import (
    BadRecord,
    classify_head,
    money_scaled,
    scaled_to_nano,
    tokens,
    ts_ms,
)
from tokenbill.adapters.cloud_billing import token_unit, usage_tokens
from tokenbill.common import TokenbillError
from tokenbill.core.builders import CANARY
from tokenbill.core.money import cents_to_nano
from tokenbill.core.records import to_json

from .helpers import ADAPTERS, MANIFEST, fixture, opts

#: Examples per property; raise with TB_ADMIN_FUZZ_EXAMPLES for a longer local run.
FUZZ = settings(max_examples=int(os.environ.get("TB_ADMIN_FUZZ_EXAMPLES", "60")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
KEYS = ["data", "results", "starting_at", "ending_at", "start_time", "end_time", "amount",
        "list_amount", "currency", "model", "workspace_id", "api_key_id", "account_id",
        "uncached_input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation",
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens", "server_tool_use",
        "web_search_requests", "cost_type", "token_type", "description", "service_tier",
        "inference_geo", "speed", "context_window", "actor", "email_address", "api_key_name",
        "user_id", "email", "core_metrics", "model_breakdown", "tokens", "input", "output",
        "cache_read", "estimated_cost", "tool_actions", "object", "value", "line_item",
        "project_id", "input_tokens", "input_cached_tokens", "input_cache_write_tokens",
        "input_uncached_tokens", "batch", "date", "product", "response", "endpoint",
        "fetched_at", "fetched_ms", "data_refreshed_at", "organization_id"]
SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(-2**60, 2**60),
    st.decimals(allow_nan=False, allow_infinity=False, places=None).map(str),
    st.sampled_from(["2026-09-01T00:00:00Z", "2026-09-01", "USD", "usd", "claude-opus-5",
                     "organization.costs.result", "organization.usage.completions.result",
                     "bucket", "page", "1e999", "-0", "NaN", "", "\x1b[31m", "\ud800x",
                     f"x {CANARY}", "0-200k", "not_available", "tokens"]),
    st.text(max_size=12))
JSON = st.recursive(SCALARS, lambda children: st.one_of(
    st.lists(children, max_size=4),
    st.dictionaries(st.sampled_from(KEYS), children, max_size=6)), max_leaves=30)
PAGE_FILES = [e for e in MANIFEST["files"] if e["adapter"] not in ("aws-cur", "gcp-billing")]


def _run(adapter_name: str, data: bytes, suffix: str = ".json") -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f"input{suffix}"
        path.write_bytes(data)
        adapter = ADAPTERS[adapter_name]()
        adapter.sniff(path, data[:65536])
        try:
            result = adapter.read(path, opts())
        except TokenbillError:
            return
        json.dumps(to_json(result))            # every record encodes
        for agg in result.aggregates:
            assert not {k for k, _ in agg.dims} & {"account_id", "user_id", "actor"}


def _paths(obj: Any, prefix: tuple = ()) -> list[tuple]:
    out = [prefix]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _paths(v, (*prefix, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _paths(v, (*prefix, i))
    return out


def _set(obj: Any, path: tuple, value: Any) -> Any:
    if not path:
        return value
    target = obj
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return obj


@FUZZ
@given(st.sampled_from(sorted(ADAPTERS)), JSON)
def test_random_json_documents(adapter_name: str, doc: Any) -> None:
    data = json.dumps(doc, default=str).encode("utf-8", "surrogatepass")
    _run(adapter_name, data)
    _run(adapter_name, data + b"\n" + data, ".jsonl")


@FUZZ
@given(st.sampled_from(PAGE_FILES), st.data())
def test_mutated_fixture_pages(entry: dict, data: st.DataObject) -> None:
    text = fixture(entry["path"]).read_text(encoding="utf-8")
    docs = [json.loads(line) for line in text.splitlines() if line.strip()] if entry[
        "path"].endswith(".jsonl") else [json.loads(text)]
    doc = copy.deepcopy(docs[0])
    for _ in range(data.draw(st.integers(1, 4))):
        paths = _paths(doc)
        path = data.draw(st.sampled_from(paths[1:] or paths))
        doc = _set(doc, path, data.draw(JSON))
    _run(entry["adapter"], json.dumps(doc, default=str).encode("utf-8", "surrogatepass"))


@FUZZ
@given(st.sampled_from(sorted(ADAPTERS)), st.binary(max_size=400))
def test_random_bytes(adapter_name: str, blob: bytes) -> None:
    _run(adapter_name, blob)
    _run(adapter_name, blob, ".csv")
    _run(adapter_name, blob, ".gz")


CUR_COLS = ["line_item_usage_start_date", "line_item_usage_account_id",
            "line_item_line_item_type", "line_item_product_code", "line_item_usage_type",
            "line_item_usage_amount", "pricing_unit", "line_item_currency_code",
            "line_item_unblended_cost", "line_item_net_unblended_cost",
            "line_item_iam_principal", "tags"]
CELL = st.one_of(st.text(max_size=10), st.sampled_from(
    ["", "AmazonBedrock", "Usage", "Tax", "Credit", "USD", "1K tokens", "1M tokens", "Units",
     "2026-09-01T00:00:00Z", "USE1-MP:USE1_InputTokenCount-Units", "0.5", "-3", "1e400",
     "arn:aws:sts::111122223333:assumed-role/R/" + CANARY, '{"iamPrincipal/team": "t"}',
     "12345", "anthropic"]))


@FUZZ
@given(st.lists(st.lists(CELL, min_size=len(CUR_COLS), max_size=len(CUR_COLS)), max_size=6),
       st.booleans())
def test_random_cur_rows(rows: list[list[str]], legacy_ragged: bool) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CUR_COLS)
    for r in rows:
        w.writerow(r[:-1] if legacy_ragged and r[0] == "" else r)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cur.csv"
        path.write_text(buf.getvalue(), encoding="utf-8", errors="surrogatepass")
        try:
            result = ADAPTERS["aws-cur"]().read(path, opts())
        except TokenbillError:
            return
        blob = json.dumps(to_json(result))
        assert CANARY not in blob and "assumed-role" not in blob


GCP_KEYS = ["sku", "id", "description", "usage_start_time", "project", "labels", "key",
            "value", "location", "region", "cost", "credits", "amount", "usage", "unit",
            "currency", "cost_type", "amount_in_pricing_units", "pricing_unit"]
GCP_SCALARS = st.one_of(SCALARS, st.sampled_from(["Claude Opus 5 Input Tokens", "global",
                                                  "us-east5", "team", "regular", "tax"]))
GCP_JSON = st.recursive(GCP_SCALARS, lambda c: st.one_of(
    st.lists(c, max_size=3), st.dictionaries(st.sampled_from(GCP_KEYS), c, max_size=6)),
    max_leaves=25)


@FUZZ
@given(st.lists(GCP_JSON, max_size=5))
def test_random_gcp_rows(rows: list[Any]) -> None:
    data = "\n".join(json.dumps(r, default=str) for r in rows).encode("utf-8",
                                                                     "surrogatepass")
    _run("gcp-billing", data, ".jsonl")
    _run("gcp-billing", data, ".csv")


@FUZZ
@given(st.binary(max_size=300))
def test_classify_head_never_raises(head: bytes) -> None:
    classify_head(head)
    for cls in ADAPTERS.values():
        assert cls().sniff(Path("x.json"), head) in (True, False)


@FUZZ
@given(st.one_of(st.text(max_size=40), st.integers(), st.none(), st.floats()))
def test_scalar_parsers_raise_only_bad_record(value: Any) -> None:
    for fn in (lambda v: ts_ms(v, "t"), lambda v: tokens(v, "t"),
               lambda v: money_scaled(v, "a", cents=True), lambda v: usage_tokens(v, 1000)):
        try:
            fn(value)
        except BadRecord:
            pass
    assert token_unit(value) is None or isinstance(token_unit(value), int)


@FUZZ
@given(st.decimals(min_value=Decimal("-1e12"), max_value=Decimal("1e12"), allow_nan=False,
                   allow_infinity=False, places=30))
def test_cents_parse_is_exact(value: Decimal) -> None:
    text = format(value, "f")
    nano, rem_e18 = scaled_to_nano(money_scaled(text, "a", cents=True))
    ref_nano, ref_rem = cents_to_nano(text)
    assert nano == ref_nano
    exact_e18 = value / 100 * 10**18
    assert abs(Decimal(nano) * 10**9 + rem_e18 - exact_e18) <= Decimal("0.5")
    assert abs(rem_e18) <= 500_000_000
    assert abs(Decimal(rem_e18) - ref_rem * 10**18) <= Decimal("0.5")


@FUZZ
@given(st.lists(st.tuples(st.integers(0, 12), st.sampled_from(["a", "b", "c", "d", "e"]),
                          st.integers(0, 5)), min_size=1, max_size=30), st.integers(1, 7))
def test_k_anonymity_property(people: list[tuple[int, str, int]], k: int) -> None:
    """Random users, teams and k: no published group below k; every user is either published
    or counted as dropped; published commits never exceed the total."""
    recs, team_map = [], {}
    for uid, team, commits in people:
        email = f"u{uid}@x.io"
        team_map.setdefault(email, team)
        recs.append({"date": "2026-09-10", "actor": {"type": "user_actor",
                                                     "email_address": email},
                     "core_metrics": {"commits_by_claude_code": commits},
                     "model_breakdown": [{"model": "claude-opus-5",
                                          "tokens": {"input": commits + 1, "output": 1}}]})
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cc.json"
        path.write_text(json.dumps({"data": recs}))
        result = ADAPTERS["anthropic-cc-analytics"]().read(
            path, opts(team_map=tuple(sorted(team_map.items())), k_anonymity=k))
    users = len({uid for uid, _, _ in people})
    assert all(o.n_users >= k for o in result.outcomes)
    published = sum(o.n_users for o in result.outcomes)
    assert published + result.stats.get("users_dropped", 0) == users
    total_commits = sum(c for _, _, c in people)
    assert sum(o.commits for o in result.outcomes) <= total_commits
    if not result.stats.get("groups_dropped"):
        assert sum(o.commits for o in result.outcomes) == total_commits
    assert "@" not in json.dumps(to_json(result))
