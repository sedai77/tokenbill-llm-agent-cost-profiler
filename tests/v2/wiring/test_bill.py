"""``bill_summary`` on ``MemoryStore`` (SPEC §15, §14.1, D26; addendum §21.4 WIRING row): exact /
allowance / pool kept apart, k-anonymous breakdowns, ESR, the Claude Code naive ratio, footnotes."""

from __future__ import annotations

import dataclasses
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.errors import PricingError, PrivacyError, UsageError
from tokenbill.core.kanon import other_label
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.money import ratio
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.core.types import IngestOptions, RawAggregate
from tokenbill.pipeline.common import (
    FOOTNOTE_PLACEHOLDER,
    FOOTNOTE_TRACE_V1,
    FOOTNOTE_UNPRICED,
    bill_summary,
    ingest_paths,
)

from .support import ORG_KEY, T0, make_env, req, write_fake

pytestmark = pytest.mark.usefixtures("fake_adapters")

WINDOW = {"since_ms": T0, "until_ms": T0 + 86_400_000}
BIG = {"u": 1000, "r": 5000, "w": 2000, "o": 300}      # 21,000,000 nano at Opus 5.5 list
SMALL = {"u": 1000, "o": 100}                           # 6,000,000 nano


def _team(team: str, n_users: int, *, lane_prefix: str, **kw: Any) -> list[dict[str, Any]]:
    return [req(f"{lane_prefix}{i}", 0, team=team, principal=f"r_{team}{i}", **{**BIG, **kw})
            for i in range(n_users)]


def _fleet(tmp_path: Path, *, store: MemoryStore | None = None, **header: Any) -> MemoryStore:
    """payments (5 users, billed), seats (5, subscription), copilot (5 on copilot_pool and one
    copilot_direct call), tiny (1, billed)."""
    records = (_team("payments", 5, lane_prefix="P")
               + _team("seats", 5, lane_prefix="S", billing_path="subscription")
               + _team("copilot", 5, lane_prefix="C", billing_path="copilot_pool")
               + [req("D0", 0, team="copilot", principal="r_copilot0",
                      billing_path="copilot_direct", **BIG)]
               + _team("tiny", 1, lane_prefix="T"))
    store = store if store is not None else MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "fleet.jsonl", records, **header)], make_env(),
                 IngestOptions())
    return store


# ---------------------------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------------------------


def test_exact_allowance_and_pool_are_kept_apart(tmp_path: Path) -> None:
    summary = bill_summary(_fleet(tmp_path), make_env(), group_by=[], **WINDOW)
    total = summary.total
    assert total.exact.nano == 6 * 21_000_000 and total.exact.basis is Basis.LIST
    assert total.exact.evidence is Evidence.EXACT and total.estimated is None
    assert total.allowance is not None and total.allowance.nano == 5 * 21_000_000
    assert total.allowance.basis is Basis.LIST_EQUIVALENT
    assert total.pool is not None and total.pool.nano == 6 * 21_000_000   # both Copilot paths
    assert total.pool.basis is Basis.LIST_EQUIVALENT
    assert (total.priced_inferences, total.unpriced_inferences, total.coverage) == (17, 0, "1")
    assert summary.breakdowns == () and summary.footnotes == ()


class _LumpingStore(MemoryStore):
    """A pre-Copilot store: every list-equivalent line in ``allowance``, ``pool`` never set."""

    def aggregate(self, **kw: Any) -> RawAggregate:
        raw = super().aggregate(**kw)
        rows = []
        for row in raw.rows:
            p = row.priced
            lumped = p.allowance
            if p.pool is not None:
                from tokenbill.core.labels import add

                lumped = p.pool if lumped is None else add(lumped, p.pool)
            rows.append(dataclasses.replace(row, priced=dataclasses.replace(
                p, allowance=lumped, pool=None)))
        return dataclasses.replace(raw, rows=tuple(rows))


def test_pool_is_split_out_of_a_lumping_store(tmp_path: Path) -> None:
    store = _fleet(tmp_path, store=_LumpingStore(org_key=ORG_KEY))
    lumped = store.aggregate(group_by=(), **WINDOW).rows[0].priced
    assert lumped.pool is None and lumped.allowance.nano == 11 * 21_000_000
    total = bill_summary(store, make_env(), group_by=[], **WINDOW).total
    assert total.allowance.nano == 5 * 21_000_000 and total.pool.nano == 6 * 21_000_000
    assert total.exact.nano == 6 * 21_000_000


def test_empty_store_and_window(tmp_path: Path) -> None:
    summary = bill_summary(MemoryStore(), make_env(), group_by=["team"], **WINDOW)
    assert summary.total.exact.nano == 0 and summary.total.coverage == "1"
    assert summary.total.allowance is None and summary.total.pool is None
    assert summary.esr is None and summary.naive_ratio is None
    assert summary.breakdowns[0][0] == "team" and summary.breakdowns[0][1].rows == ()
    # a contract pricer bills on basis contract even when nothing was priced
    contract_env = make_env(pricer=_ContractPricer())
    assert bill_summary(MemoryStore(), contract_env, group_by=[], **WINDOW).total.exact.basis \
        is Basis.CONTRACT
    # outside the window
    store = _fleet(tmp_path)
    later = bill_summary(store, make_env(), group_by=[], since_ms=T0 + 86_400_000,
                         until_ms=T0 + 2 * 86_400_000)
    assert later.total.exact.nano == 0 and later.total.priced_inferences == 0


class _ContractPricer(FakePricer):
    basis = Basis.CONTRACT

    def __init__(self) -> None:
        super().__init__()
        self.basis = Basis.CONTRACT


@pytest.mark.parametrize("since, until", [(5, 4), (1.0, 2), (0, "9")])
def test_window_is_validated(since: Any, until: Any) -> None:
    with pytest.raises(UsageError, match="window"):
        bill_summary(MemoryStore(), make_env(), since_ms=since, until_ms=until, group_by=[])


# ---------------------------------------------------------------------------------------------
# breakdowns (k enforced through core.kanon.publish)
# ---------------------------------------------------------------------------------------------


def test_breakdowns_are_k_anonymous(tmp_path: Path) -> None:
    store = _fleet(tmp_path)
    summary = bill_summary(store, make_env(), group_by=["team", "team, model", ""], **WINDOW)
    keys = [k for k, _ in summary.breakdowns]
    assert keys == ["team", "team,model"]
    teams = summary.breakdowns[0][1]
    labels = [dict(row.dims)["team"] for row in teams.rows]
    assert "tiny" not in labels and other_label(5) in labels
    assert teams.k == 5 and teams.suppressed_rows == 2      # tiny + its complement
    # published totals still add up to the raw totals (complementary suppression)
    assert sum(r.priced.exact.nano for r in teams.rows) == 6 * 21_000_000
    # with k = 1 every team is published
    everyone = bill_summary(store, make_env(k=1), group_by=["team"], **WINDOW).breakdowns[0][1]
    assert sorted(dict(r.dims)["team"] for r in everyone.rows) == [
        "copilot", "payments", "seats", "tiny"]


def test_self_audience_never_suppresses(tmp_path: Path) -> None:
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "me.jsonl", [
        req("M1", 0, team="payments", principal="r_me", **BIG),
        req("M2", 0, team="search", principal="r_me", **BIG)])], make_env(), IngestOptions())
    org = bill_summary(store, make_env(), group_by=["team"], **WINDOW).breakdowns[0][1]
    assert org.rows == () and org.suppressed_rows == 2          # one user: withheld for the org
    own = bill_summary(store, make_env(), group_by=["team"], audience="self",
                       **WINDOW).breakdowns[0][1]
    assert [dict(r.dims)["team"] for r in own.rows] == ["payments", "search"]
    with pytest.raises(UsageError, match="audience"):
        bill_summary(store, make_env(), group_by=[], audience="everyone", **WINDOW)


def test_group_by_as_one_string_and_duplicates(tmp_path: Path) -> None:
    summary = bill_summary(_fleet(tmp_path), make_env(), group_by="model,team", **WINDOW)
    assert [k for k, _ in summary.breakdowns] == ["model,team"]
    summary = bill_summary(_fleet(tmp_path), make_env(), group_by=["team", "team"], **WINDOW)
    assert [k for k, _ in summary.breakdowns] == ["team"]


@pytest.mark.parametrize("group_by, error", [
    (["principal"], PrivacyError), (["team,session"], PrivacyError),
    (["team,bogus"], UsageError), (["team,team"], UsageError), ([3], UsageError),
    (["api_key_id"], UsageError),
])
def test_group_by_is_validated(group_by: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        bill_summary(MemoryStore(), make_env(), group_by=group_by, **WINDOW)


# ---------------------------------------------------------------------------------------------
# ESR (rates.engine.no_cache_equivalent_nano when available)
# ---------------------------------------------------------------------------------------------


def _no_cache(pricer: Any, inference: Any, ts_ms: int) -> int:
    u = inference.usage
    flat = UsageBuckets(uncached_input=u.total_input, output=u.output)
    return pricer.price_usage(flat, inference.pricing, ts_ms=ts_ms).figure.nano


@pytest.fixture
def rate_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = types.ModuleType("tokenbill.rates.engine")
    engine.no_cache_equivalent_nano = _no_cache  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenbill.rates.engine", engine)


def _esr(num: int, den: int) -> str:
    value = ratio(den - num, den)
    assert value is not None
    return format(value.normalize(), "f")


def test_esr_over_exact_billed_inferences(rate_engine: None, tmp_path: Path) -> None:
    extra = [req("M0", 0, team="payments", principal="r_m", u=1000, o=100,
                 usage_source="message_start_only", output_upper=400),   # estimated: excluded
             req("X0", 0, team="payments", principal="r_x", model="mystery-model-9"),  # unpriced
             req("F0", 0, team="payments", principal="r_f", billable=False)]  # not billable
    records = _team("payments", 5, lane_prefix="P") + _team(
        "seats", 1, lane_prefix="S", billing_path="subscription") + extra
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "f.jsonl", records)], make_env(), IngestOptions())
    summary = bill_summary(store, make_env(), group_by=[], **WINDOW)
    # per BIG request: exact 21,000,000; no-cache 8,000 input × $4 + 300 × $20 = 38,000,000
    assert summary.esr == _esr(5 * 21_000_000, 5 * 38_000_000) == "0.4473684210526315789473684211"
    assert FOOTNOTE_PLACEHOLDER in summary.footnotes and FOOTNOTE_UNPRICED in summary.footnotes


def test_esr_none_without_engine_or_billed_usage(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "tokenbill.rates.engine", None)
    assert bill_summary(_fleet(tmp_path), make_env(), group_by=[], **WINDOW).esr is None
    engine = types.ModuleType("tokenbill.rates.engine")
    engine.no_cache_equivalent_nano = _no_cache  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenbill.rates.engine", engine)
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "s.jsonl", _team(
        "seats", 2, lane_prefix="S", billing_path="subscription"))], make_env(), IngestOptions())
    assert bill_summary(store, make_env(), group_by=[], **WINDOW).esr is None


# ---------------------------------------------------------------------------------------------
# naive ratio (Claude Code naive line sum ÷ de-duplicated, priced with the Env pricer)
# ---------------------------------------------------------------------------------------------


def _cc_store(tmp_path: Path, naive: dict[str, dict[str, int]], **kw: Any) -> MemoryStore:
    records = [req("L1", 0, **SMALL), req("L1", 1, **SMALL),
               req("L9", 0, model="claude-sonnet-5", u=500, o=50)]
    store = kw.pop("store", MemoryStore(org_key=ORG_KEY))
    ingest_paths(store, [write_fake(tmp_path / "cc.jsonl", records, source_adapter="claude-code",
                                    naive=naive, stats={"lines": 3})], make_env(), IngestOptions())
    return store


def test_naive_ratio(tmp_path: Path) -> None:
    store = _cc_store(tmp_path, {"claude-opus-5-5": {"uncached_input": 4000, "output": 500},
                                 "claude-haiku-4-5": {"output": 999}})   # no request: skipped
    other = write_fake(tmp_path / "otel.jsonl", [req("OT", 0, u=10**6, o=10**5)])
    ingest_paths(store, [other], make_env(), IngestOptions())   # not Claude Code: not counted
    summary = bill_summary(store, make_env(), group_by=[], **WINDOW)
    # de-duplicated: 2000 × $4 + 200 × $20 = 12,000,000; naive: 4000 × $4 + 500 × $20 = 26,000,000
    expected = ratio(26_000_000, 12_000_000)
    assert expected is not None and summary.naive_ratio == format(expected.normalize(), "f")
    assert summary.naive_ratio.startswith("2.1666")


def test_naive_ratio_absent(tmp_path: Path) -> None:
    assert bill_summary(_cc_store(tmp_path, {}), make_env(), group_by=[], **WINDOW
                        ).naive_ratio is None

    class NoStats(MemoryStore):
        source_stats = None  # type: ignore[assignment]

    store = _cc_store(tmp_path, {"claude-opus-5-5": {"output": 5}}, store=NoStats(org_key=ORG_KEY))
    assert bill_summary(store, make_env(), group_by=[], **WINDOW).naive_ratio is None


class _NoUnitRates(FakePricer):
    def unit_rates(self, ctx: Any, *, ts_ms: int) -> Any:
        raise PricingError("no unit rates")


def test_naive_ratio_when_unit_rates_fail(tmp_path: Path) -> None:
    store = _cc_store(tmp_path, {"claude-opus-5-5": {"output": 500}})
    assert bill_summary(store, make_env(pricer=_NoUnitRates()), group_by=[], **WINDOW
                        ).naive_ratio is None


def test_naive_ratio_skips_unpriced_models(tmp_path: Path) -> None:
    records = [req("L1", 0, model="mystery-model-9", **SMALL)]
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "cc.jsonl", records, source_adapter="claude-code",
                                    naive={"mystery-model-9": {"output": 7}})], make_env(),
                 IngestOptions())
    assert bill_summary(store, make_env(), group_by=[], **WINDOW).naive_ratio is None


# ---------------------------------------------------------------------------------------------
# footnotes
# ---------------------------------------------------------------------------------------------


def test_trace_v1_footnote(tmp_path: Path) -> None:
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "t.jsonl", [req("R1", 0, **SMALL)],
                                    source_adapter="trace@1")], make_env(), IngestOptions())
    assert bill_summary(store, make_env(), group_by=[], **WINDOW).footnotes == (FOOTNOTE_TRACE_V1,)
