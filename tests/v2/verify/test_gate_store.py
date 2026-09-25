"""Gate (merge gate 1): ``build_panel`` on a real ``SqliteStore`` equals the MemoryStore result
(the same ingest, the same pricers)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core.records import OutcomeAggregate
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.verify import panel as P

from .helpers import CUT20, ORG_KEY, ingest, request

db = pytest.importorskip("tokenbill.store.db")

pytestmark = pytest.mark.gate

DAYS = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04")


def _requests():
    out = []
    for i, d in enumerate(DAYS):
        for dev in (1, 2):
            out.append(request("alpha", d, dev, seq=i, arm="control", wave="0"))
        for dev in (3, 4, 5):
            tagged = d >= "2026-09-03"
            out.append(request("beta", d, dev, seq=i, arm="cc.prompt_cache_ttl.main"
                               if tagged else None, wave="1" if tagged else None))
        out.append(request("gamma", d, 7, seq=i, kind="mdm_group"))
        out.append(request("alpha", d, 9, seq=10 + i, billing_path="subscription"))
    return out


def _outcomes():
    return [OutcomeAggregate(date_utc=DAYS[1], team="beta", n_users=5, sessions=9, commits=4,
                             pull_requests=7, lines_added=1, lines_removed=1, edits_accepted=1,
                             edits_rejected=0)]


@pytest.mark.parametrize("kind, billing_class", [("team", "billed"), ("team", "allowance"),
                                                 ("mdm_group", "billed")])
def test_build_panel_sqlite_equals_memory(tmp_path: Path, kind: str, billing_class: str) -> None:
    mem = MemoryStore(org_key=ORG_KEY)
    sql = db.SqliteStore(tmp_path / "ledger.db", org_key=ORG_KEY)
    for store in (mem, sql):
        ingest(store, _requests(), outcomes=_outcomes())
    kw = {"cluster_kind": kind, "since": DAYS[0], "until": "2026-09-05",
          "baseline_pricer": FakePricer(), "actual_pricer": FakePricer(contract=CUT20),
          "billing_class": billing_class}
    assert P.build_panel(sql, **kw) == P.build_panel(mem, **kw)
    arms = {"beta": "cc.prompt_cache_ttl.main@2026-09-02", "alpha": "control"}
    assert P.build_panel(sql, arms=arms, **kw) == P.build_panel(mem, arms=arms, **kw)
    window = {"cluster_kind": kind, "since": DAYS[0], "until": "2026-09-05"}
    assert P.panel_channels(sql, **window) == P.panel_channels(mem, **window)
    assert P.cache_scope_clusters(sql, **window) == P.cache_scope_clusters(mem, **window)
