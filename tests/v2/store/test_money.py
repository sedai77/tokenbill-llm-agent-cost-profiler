"""Money in the ledger (SPEC §7.1–§7.3, D6, D26, D27): int nano columns, per-line exactness,
allowance kept apart, ``cost_rows`` / ``aggregate`` totals equal to the ledger, repricing."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tokenbill.core.builders import FlatRates, make_request
from tokenbill.core.labels import Basis, Evidence, Figure
from tokenbill.core.records import UsageSource
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import ContractOverlay, PricedInference, PricedLine
from tokenbill.store import db as db_mod
from tokenbill.store import schema

from .helpers import (
    DAY,
    FOREVER,
    ORG_KEY,
    T0,
    Stores,
    five_sources,
    keyed,
    memory,
    result,
    src,
)

ALL_COST_DIMS = list(db_mod.COST_DIMS)
HALF = ContractOverlay(name="half", multiplier=Decimal("0.5"), overrides=(),
                       effective_from="2026-01-01", effective_to=None, derived=False,
                       assumed_fields=())


def _sum(store, sql: str) -> int:
    return store.connection.execute(sql).fetchone()[0] or 0


def _loaded(stores: Stores, **kw) -> object:
    store = keyed(stores, **kw)
    for r in five_sources():
        store.ingest(r)
    return store


def test_request_and_inference_totals_agree(stores: Stores) -> None:
    store = _loaded(stores)
    by_inference = _sum(store, "SELECT SUM(priced_nano) FROM inferences WHERE billable IS NOT 0")
    by_request = _sum(store, "SELECT SUM(s) FROM (SELECT SUM(priced_nano) AS s FROM inferences "
                             "WHERE billable IS NOT 0 GROUP BY request_id)")
    assert by_inference == by_request > 0
    assert sum(r.point_nano for r in store.lane_index(**FOREVER)) == by_inference
    exact = _sum(store, "SELECT SUM(exact_nano) FROM inferences WHERE priced_nano IS NOT NULL")
    est = _sum(store, "SELECT SUM(est_nano) FROM inferences WHERE priced_nano IS NOT NULL")
    assert exact + est == by_inference
    assert _sum(store, "SELECT COUNT(*) FROM inferences WHERE priced_nano IS NOT NULL AND "
                       "exact_nano + est_nano <> priced_nano") == 0


def test_per_bucket_columns_sum_to_the_point(stores: Stores) -> None:
    store = _loaded(stores)
    cols = " + ".join(f"COALESCE({c}, 0)" for c in schema.EXACT_MASK_BITS)
    assert _sum(store, f"SELECT COUNT(*) FROM inferences WHERE priced_nano IS NOT NULL AND "
                       f"{cols} <> priced_nano") == 0


def test_placeholder_output_is_estimated_while_input_stays_exact(stores: Stores) -> None:
    req = make_request("L-p", 0, T0, {"cache_read": 10_000, "uncached_input": 200, "output": 1},
                       "claude-opus-5-5", request_id="rq_placeholder",
                       usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=900,
                       attribution={"team": "t"})
    store = keyed(stores)
    store.ingest(result(src("s_p", "claude-code"), [req]))
    row = store.connection.execute(
        "SELECT exact_mask, exact_nano, est_nano, est_low_nano, est_high_nano, evidence, "
        "lines_json, nano_uncached, nano_read, nano_output FROM inferences").fetchone()
    mask, exact_n, est_n, lo, hi, evidence, lines, n_unc, n_read, n_out = row
    bits = schema.EXACT_MASK_BITS
    assert mask & bits["nano_uncached"] and mask & bits["nano_read"]
    assert not mask & bits["nano_output"]
    assert exact_n == n_unc + n_read and est_n == n_out
    assert evidence == Evidence.ESTIMATED.value and lo <= est_n < hi
    output = [ln for ln in json.loads(lines) if ln[0] == "output"]
    assert output and output[0][5] == 0 and output[0][4] > output[0][2]


def test_subscription_lands_in_allowance_never_in_exact(stores: Stores) -> None:
    store = _loaded(stores)
    days = store.cluster_days(cluster_kind="team", since=DAY, until="2026-09-24")
    payments = [d for d in days if d.cluster_id == "payments"]
    assert sum(d.allowance_nano for d in payments) > 0
    sub = _sum(store, "SELECT SUM(priced_nano) FROM inferences WHERE billing_path='subscription'")
    assert sum(d.allowance_nano for d in days) == sub
    rollup_exact = _sum(store, "SELECT SUM(exact_nano) FROM daily_rollup")
    billed_exact = _sum(store, "SELECT SUM(exact_nano) FROM inferences WHERE basis <> "
                               "'list_equivalent' AND priced_nano IS NOT NULL")
    assert rollup_exact == billed_exact
    assert _sum(store, "SELECT SUM(allowance_nano) FROM daily_rollup") == sub
    assert _sum(store, "SELECT SUM(exact_nano) FROM daily_rollup WHERE "
                       "billing_path='subscription'") == 0
    total = store.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert total.allowance is not None and total.allowance.nano == sub
    assert total.exact.nano == billed_exact


def test_cost_rows_per_bucket_sums_equal_the_ledger(stores: Stores) -> None:
    store = _loaded(stores)
    rows = store.cost_rows(group_by=ALL_COST_DIMS, **FOREVER)
    billed = [r for r in rows if r.basis is not Basis.LIST_EQUIVALENT]
    assert sum(r.priced_nano for r in billed) == _sum(
        store, "SELECT SUM(exact_nano) FROM inferences WHERE priced_nano IS NOT NULL AND "
               "basis <> 'list_equivalent' AND billable IS NOT 0")
    for bucket, col in (("cache_read", "nano_read"), ("output", "nano_output"),
                        ("cache_write_5m", "nano_w5m")):
        bit = schema.EXACT_MASK_BITS[col]
        expected = _sum(store, f"SELECT SUM({col}) FROM inferences WHERE exact_mask & {bit} "
                               "AND priced_nano IS NOT NULL AND billable IS NOT 0")
        assert sum(r.priced_nano for r in rows if r.bucket == bucket) == expected
    ranged = [r for r in rows if r.estimated_high_nano]
    assert sum(r.estimated_low_nano for r in rows) == _sum(
        store, "SELECT SUM(est_low_nano) FROM inferences WHERE priced_nano IS NOT NULL AND "
               "billable IS NOT 0")
    assert ranged and all(r.bucket == "cache_write_unknown" for r in ranged)


@pytest.mark.parametrize("group_by", [ALL_COST_DIMS, ["team"], ["date", "model"], [],
                                      ["billing_path", "lane_kind", "provider"]])
def test_cost_rows_equal_the_memory_reference(stores: Stores, group_by: list[str]) -> None:
    store = _loaded(stores)
    mem = memory()
    for r in five_sources():
        mem.ingest(r)
    assert store.cost_rows(group_by=group_by, **FOREVER) == mem.cost_rows(group_by=group_by,
                                                                          **FOREVER)


@pytest.mark.parametrize("group_by, where", [
    ([], None), (["team"], None), (["team", "model"], None), (["date", "lane_kind"], None),
    (["billing_path", "channel"], {"team": "payments"}), (["agent_product"], {"team": ""}),
    (["workload_class", "repo", "arm", "wave", "skill", "mcp_server", "agent_type",
      "cost_center", "workspace_id"], None),
    (["model"], {"billing_class": "allowance"}), (["team"], {"provider": "anthropic"}),
    (["team"], {"lane_kind": "main", "project": "proj-1"})])
def test_aggregate_equals_the_memory_reference(stores: Stores, group_by, where) -> None:
    store = _loaded(stores)
    mem = memory()
    for r in five_sources():
        mem.ingest(r)
    assert store.aggregate(group_by=group_by, where=where, **FOREVER) == mem.aggregate(
        group_by=group_by, where=where, **FOREVER)
    flat = FlatRates()
    assert store.aggregate(group_by=group_by, where=where, pricer=flat, **FOREVER) == \
        mem.aggregate(group_by=group_by, where=where, pricer=flat, **FOREVER)


def test_reprice_a_window_then_restore(stores: Stores) -> None:
    store = _loaded(stores)
    mem = memory()
    for r in five_sources():
        mem.ingest(r)
    half = FakePricer().with_contract(HALF)
    cut = T0 + 450_000
    assert store.reprice(half, since_ms=cut) == mem.reprice(half, since_ms=cut)
    assert store.cost_rows(group_by=ALL_COST_DIMS, **FOREVER) == mem.cost_rows(
        group_by=ALL_COST_DIMS, **FOREVER)
    assert store.aggregate(group_by=["team"], **FOREVER) == mem.aggregate(group_by=["team"],
                                                                          **FOREVER)
    assert any(r.basis is Basis.CONTRACT for r in store.cost_rows(group_by=["team"],
                                                                  **FOREVER))
    whole = FakePricer().with_contract(HALF)
    store.reprice(whole)
    mem.reprice(whole)
    total = store.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert total == mem.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert total.exact.basis is Basis.CONTRACT   # every request now on a contract card
    # the card's basis is recorded in meta, so a reopened store keeps the CONTRACT basis
    path = store.path
    store.close()
    again = db_mod.SqliteStore(path, org_key=ORG_KEY)
    assert again.aggregate(group_by=[], **FOREVER).rows[0].priced.exact.basis is Basis.CONTRACT
    again.close()


def test_without_a_pricer_everything_is_unpriced_until_repriced(stores: Stores) -> None:
    store = keyed(stores, pricer=None)
    mem = memory(pricer=None)
    for r in five_sources():
        store.ingest(r)
        mem.ingest(r)
    total = store.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert total.priced_inferences == 0 and total.coverage == "0"
    assert total == mem.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert _sum(store, "SELECT COUNT(*) FROM inferences WHERE unpriced_reason='no pricer'") > 0
    assert store.cost_rows(group_by=["team"], **FOREVER) == []
    n = store.reprice(FakePricer())
    assert n == mem.reprice(FakePricer())
    assert store.aggregate(group_by=["team"], **FOREVER) == mem.aggregate(group_by=["team"],
                                                                          **FOREVER)


def test_unpriced_models_are_coverage_not_zero(stores: Stores) -> None:
    req = make_request("L-u", 0, T0, {"uncached_input": 100, "output": 10}, "mystery-model-9",
                       request_id="rq_unknown", attribution={"team": "t"})
    ok = make_request("L-u", 1, T0 + 1, {"uncached_input": 100, "output": 10},
                      "claude-opus-5-5", request_id="rq_known", attribution={"team": "t"})
    store = keyed(stores)
    store.ingest(result(src("s_u", "claude-code"), [req, ok]))
    total = store.aggregate(group_by=[], **FOREVER).rows[0].priced
    assert (total.priced_inferences, total.unpriced_inferences, total.unpriced_tokens) == (1, 1,
                                                                                           110)
    assert total.coverage == "0.5"
    reason = store.connection.execute(
        "SELECT unpriced_reason FROM inferences WHERE priced_nano IS NULL").fetchone()[0]
    assert reason


class _TwoServerTools:
    """A pricer emitting web search *and* web fetch lines and a range line (the JSON form of
    ``lines_json``)."""

    rate_card_sha256 = "two-tools"
    basis = Basis.LIST

    def price_inference(self, inf, *, ts_ms):
        lines = (
            PricedLine(bucket="uncached_input", quantity=inf.usage.uncached_input,
                       unit_usd_per_mtok="1", amount_nano=1000, low_nano=None, high_nano=None,
                       exact=True, rate_row_id="r1", modifier_ids=(), layer="builtin"),
            PricedLine(bucket="web_search", quantity=inf.usage.web_search_requests,
                       unit_usd_per_mtok="10", amount_nano=20, low_nano=None, high_nano=None,
                       exact=True, rate_row_id="r1", modifier_ids=(), layer="builtin"),
            PricedLine(bucket="web_fetch", quantity=inf.usage.web_fetch_requests,
                       unit_usd_per_mtok="5", amount_nano=5, low_nano=None, high_nano=None,
                       exact=True, rate_row_id="r2", modifier_ids=(), layer="builtin"),
        )
        fig = Figure(nano=1025, evidence=Evidence.EXACT, basis=Basis.LIST)
        return PricedInference(inference_id=inf.inference_id, lines=lines, figure=fig,
                               exact_nano=1025, estimated=None, unpriced_reason=None)


def test_lines_that_share_a_column_keep_their_own_rows(stores: Stores) -> None:
    req = make_request("L-w", 0, T0, {"uncached_input": 7, "web_search_requests": 2,
                                      "web_fetch_requests": 1}, "claude-opus-5-5",
                       request_id="rq_web", attribution={"team": "t"})
    store = keyed(stores, pricer=_TwoServerTools())
    store.ingest(result(src("s_w", "claude-code"), [req]))
    lines = store.connection.execute("SELECT lines_json, nano_server_tools FROM "
                                     "inferences").fetchone()
    assert lines[0].startswith("[") and lines[1] == 25
    rows = {r.bucket: r for r in store.cost_rows(group_by=[], **FOREVER)}
    assert (rows["web_search"].quantity, rows["web_search"].priced_nano) == (2, 20)
    assert (rows["web_fetch"].quantity, rows["web_fetch"].rate_row_id) == (1, "r2")
    assert rows["uncached_input"].rate_row_id == "r1"


def test_simple_lines_are_rebuilt_from_the_columns(stores: Stores) -> None:
    req = make_request("L-s", 0, T0, {"uncached_input": 7, "cache_read": 1000, "output": 3,
                                      "web_search_requests": 2}, "claude-opus-5-5",
                       request_id="rq_simple", attribution={"team": "t"})
    store = keyed(stores)
    mem = memory()
    for s in (store, mem):
        s.ingest(result(src("s_s", "claude-code"), [req]))
    text = store.connection.execute("SELECT lines_json FROM inferences").fetchone()[0]
    assert text.startswith("=")
    assert store.cost_rows(group_by=ALL_COST_DIMS, **FOREVER) == mem.cost_rows(
        group_by=ALL_COST_DIMS, **FOREVER)
