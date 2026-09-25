"""Performance budgets (addendum §17: ``copilot scan``, 5,000 seats, one month, ≤ 3 min single
process — the record-store and enricher share of it). Full size under the ``perf`` marker
(nightly); the PR variant runs at 1/10 size with 1/10 of the budget (CPU time, so a busy shared
machine does not fail it)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tokenbill.copilot.enrich import enrich_context
from tokenbill.copilot.record_store import CopilotRecordStore
from tokenbill.core import builders as b
from tokenbill.core import testing as kit
from tokenbill.core.ids import key_id
from tokenbill.core.types import AnalysisContext

from .support import ORG_KEY, W, ledger_meta, month_window, result, rows

#: CPU seconds for the full-size month (5,000 seats, 150,000 activity days): put + count +
#: enrich; well inside the 3-minute ``copilot scan`` budget.
FULL_BUDGET_S = 60.0


def month_of_records(seats: int) -> tuple[list, list]:
    people = [b.make_principal(f"perf{i}") for i in range(seats)]
    teams = [f"team-{i % 40}" for i in range(seats)]
    lics = [b.make_license(x, snapshot_date="2026-09-15", team=t,
                           plan="business" if i % 3 else "enterprise",
                           last_activity_bucket="0-7" if i % 7 else "none_90d")
            for i, (x, t) in enumerate(zip(people, teams, strict=True))]
    days = [b.make_activity(x, date_utc=f"2026-09-{d:02d}", team=t,
                            counts={"interactions": d, "ide:vscode": 1},
                            reported_cost_nano=10**6 * d)
            for d in range(1, 31) for x, t in zip(people, teams, strict=True)]
    return lics, days


def run_month(tmp_path: Path, seats: int) -> float:
    lics, days = month_of_records(seats)
    usage, aggs = rows(seats * 100, date="2026-09-10", users=[b.make_principal("perf0")])
    ledger = kit.MemoryStore(org_key=ORG_KEY)
    ledger.ingest(result(cost_lines=usage, aggregates=aggs, adapter="github-ai-usage"))
    path = tmp_path / "ledger.db"
    ledger_meta(path, org_key_id=key_id(ORG_KEY))
    start = time.process_time()
    store = CopilotRecordStore(path)
    store.put(result(lics, days), principal_key_id=key_id(ORG_KEY))
    assert store.count_users(where={"team": "team-1"}, source="licenses", **W) == len(
        [x for x in lics if x.team == "team-1"])
    assert store.count_users(where={}, source="activity", **W) == seats
    ctx = AnalysisContext(pricer=kit.FakePricer(), rules=None, replayer=None,  # type: ignore
                          calibration=None, window=month_window("2026-09"),
                          capabilities=frozenset())
    out = enrich_context(ledger, [store], ctx, today="2026-10-10",
                         reconciled_channels=frozenset())
    assert len(out.licenses) == seats and len(out.activity) == seats * 30 and out.pools
    return time.process_time() - start


def test_pr_variant_one_tenth(tmp_path: Path) -> None:
    assert run_month(tmp_path, 500) <= FULL_BUDGET_S / 10


@pytest.mark.perf
def test_full_size_month(tmp_path: Path) -> None:
    assert run_month(tmp_path, 5_000) <= FULL_BUDGET_S
