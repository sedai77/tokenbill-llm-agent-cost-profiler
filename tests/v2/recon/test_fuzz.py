"""Hypothesis fuzz and properties of RECON's parsers and arithmetic (SPEC §21 #5): only
``TokenbillError`` subclasses escape ``pull`` and ``reconcile``; the residual bridge
``gap = Σ signed claims + unexplained`` holds; reconciliation is order-independent."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core.builders import make_cost_line
from tokenbill.core.types import ReconRow
from tokenbill.recon import pull as pull_mod
from tokenbill.recon.reconcile import VERDICTS, merge_reports, reconcile
from tokenbill.recon.residuals import classify

from .helpers import DAY, DAY2, OPUS, PRICER, RECENT, SONNET, TODAY, WS, WS2, agg, record
from .test_pull import ENV, KEY, Opener, Response

FUZZ = settings(max_examples=int(os.environ.get("TB_RECON_FUZZ_EXAMPLES", "60")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

bodies = st.one_of(
    st.binary(max_size=200),
    st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=12),
                 lambda c: st.lists(c, max_size=4) | st.dictionaries(
                     st.sampled_from(["data", "has_more", "next_page", "results", "x"]), c,
                     max_size=4), max_leaves=12).map(lambda v: repr(v).encode()),
    st.fixed_dictionaries({"data": st.just([]), "has_more": st.booleans(),
                           "next_page": st.none() | st.text(max_size=8)}).map(
        lambda d: __import__("json").dumps(d).encode()),
)
statuses = st.sampled_from([200, 200, 200, 400, 401, 404, 429, 500, 503])


@FUZZ
@given(st.text(max_size=40))
def test_retry_after_never_raises(text: str) -> None:
    value = pull_mod._retry_after(text, 1_787_565_600)
    assert value is None or 0 <= value <= pull_mod.RETRY_AFTER_CAP_S


@FUZZ
@given(st.lists(st.tuples(statuses, bodies), min_size=1, max_size=6))
def test_pull_only_raises_tokenbill_errors(script: list[tuple[int, bytes]]) -> None:
    responses: list[Any] = [Response(s, b) for s, b in script]

    def answer(request: Any) -> Any:
        return responses.pop(0) if responses else Response(200, b'{"has_more": false}')

    old = os.environ.get(ENV)
    os.environ[ENV] = KEY
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                paths = pull_mod.pull("usage_report", key_env=ENV, since="2026-08-10",
                                      until="2026-08-11", out_dir=Path(tmp), opener=Opener(answer),
                                      sleep=lambda s: None)
            except TokenbillError as exc:
                assert KEY not in str(exc)
            else:
                assert KEY not in paths[0].read_text()
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


small = st.integers(min_value=0, max_value=3_000_000)
usages = st.fixed_dictionaries({"uncached_input": small, "cache_read": small,
                                "cache_write_5m": small, "output": small})
cells = st.tuples(st.sampled_from([DAY, DAY2, RECENT]), st.sampled_from([OPUS, SONNET, "x-9"]),
                  st.sampled_from([WS, WS2, None]), usages)


@st.composite
def worlds(draw: Any) -> tuple[list, list, list]:
    provider = draw(st.lists(cells, max_size=4))
    aggs = [agg(u, date=d, model=m, ws=w, tier=draw(st.sampled_from(["standard", "priority"])))
            for d, m, w, u in provider]
    lines = []
    for i, (d, m, w, _u) in enumerate(draw(st.lists(cells, max_size=4))):
        token_type = draw(st.sampled_from(["output_tokens", "uncached_input_tokens", "mystery"]))
        cost_type = draw(st.sampled_from(["tokens", "tokens", "code_execution", "fine_tuning"]))
        lines.append(make_cost_line(draw(st.integers(-10**9, 10**11)), date_utc=d, model=m,
                                    workspace_id=w, cost_type=cost_type,
                                    token_type=token_type if cost_type == "tokens" else None,
                                    line_id=f"l{i}"))
    ledger = [record(u, date=d, model=m, ws=w, n=i,
                     billing_path=draw(st.sampled_from(["api_key", "subscription"])))
              for i, (d, m, w, u) in enumerate(draw(st.lists(cells, max_size=4)))]
    return ledger, aggs, lines


@FUZZ
@given(worlds(), st.booleans(), st.booleans())
def test_reconcile_properties(world: tuple[list, list, list], closed_only: bool,
                              suggest: bool) -> None:
    ledger, aggs, lines = world
    try:
        report = reconcile(ledger, aggs, lines, PRICER, today=TODAY, closed_only=closed_only,
                           suggest_contract=suggest)
    except TokenbillError:
        return
    assert report.verdict in VERDICTS
    assert all(c.verdict in VERDICTS for c in report.channels)
    assert report.rerun_verdict in (None, *VERDICTS)
    again = reconcile(list(reversed(ledger)), list(reversed(aggs)), list(reversed(lines)),
                      PRICER, today=TODAY, closed_only=closed_only, suggest_contract=suggest)
    assert again == report
    assert merge_reports([report]) is report
    if closed_only:
        assert all(("date", RECENT) not in r.key for r in report.rows)


def _row(i: int, bucket: str, date: str, vals: tuple) -> ReconRow:
    lt, pt, led, priced, inv = vals
    key = (("channel", "anthropic_api"), ("date", date), ("workspace", "w"),
           ("model", f"m{i % 2}"), ("bucket", bucket))
    return ReconRow(key=key, ledger_tokens=lt, provider_tokens=pt, ledger_nano=led,
                    priced_provider_nano=priced, invoice_nano=inv, rate_card_error_pct=None,
                    coverage_pct=None, status="match", residual_code=None)


opt_int = st.none() | st.integers(min_value=0, max_value=10**10)
row_values = st.tuples(opt_int, opt_int, opt_int, opt_int, st.none() | st.integers(-10**9, 10**10))


@FUZZ
@given(st.lists(st.tuples(st.sampled_from(["output", "cache_read", "code_execution", "unmapped",
                                           "credits", "web_search"]),
                          st.sampled_from(["2026-08-10", "2026-09-20"]), row_values),
                max_size=8, unique_by=lambda t: (t[0], t[1])),
       st.integers(min_value=0, max_value=10**6))
def test_bridge_identity(specs: list, estimated: int) -> None:
    rows = [_row(i, b, d, v) for i, (b, d, v) in enumerate(specs)]
    codes, unexplained = classify(
        rows, ledger_estimated_nano={r.key: estimated for r in rows}, allowance_nano={},
        provisional_dates=frozenset({"2026-09-20"}), remainders_nano=0)
    gap = sum((r.invoice_nano or 0) - (r.ledger_nano or 0) for r in rows)
    assert sum(n for _, n in codes) + unexplained == gap


@FUZZ
@given(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("1.2"), places=2))
def test_uniform_multiplier_is_recovered(factor: Decimal) -> None:
    from .helpers import cost_lines
    a = agg()
    report = reconcile([record()], [a], cost_lines(a, factor=factor), PRICER, today=TODAY,
                       suggest_contract=True)
    if factor == 1:
        assert report.suggested_contract is None
    else:
        assert report.suggested_contract is not None
        assert report.suggested_contract.multiplier == factor
        assert report.rerun_verdict == "reconciled"
