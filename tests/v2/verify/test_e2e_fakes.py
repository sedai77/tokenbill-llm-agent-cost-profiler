"""VERIFY end to end on the foundation fakes: MemoryStore → pre-period panel → plan → tagged
rollout → panel at the baseline card → measure → receipt → sign (fake runner) → put_receipt."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from decimal import Decimal
from pathlib import Path

from tokenbill.common import rng
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.labels import Basis, Calibration, Evidence, estimated
from tokenbill.core.records import to_json
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.verify import label_policy as L
from tokenbill.verify import panel as P
from tokenbill.verify import receipts as RC
from tokenbill.verify import rollout as R

from .helpers import CUT20, ORG_KEY, ingest, recon, request

LEVER = "cc.prompt_cache_ttl.main"
START = _dt.date(2026, 7, 6)          # a Monday
PRE_DAYS, DAYS = 14, 42


def _day(i: int) -> str:
    return (START + _dt.timedelta(days=i)).isoformat()


def _usage(rnd, treated: bool) -> dict[str, int]:
    writes = 600 if treated else 2400
    return {"uncached_input": 800 + rnd.randint(0, 400), "cache_read": 30_000 + rnd.randint(0,
            4000), "cache_write_5m": writes + rnd.randint(0, 200), "output": 400}


def _requests(clusters, days, *, starts=None, lever=LEVER, holdback=()):
    rnd = rng(11, "tests.verify.e2e")
    out = []
    for ci, cluster in enumerate(clusters):
        for d in days:
            date = _day(d)
            start = (starts or {}).get(cluster)
            treated = start is not None and date >= start
            arm = "control" if cluster in holdback else (lever if treated else None)
            for dev in range(3 + ci % 3):
                if d % 7 >= 5 and dev > 0:
                    continue
                out.append(request(cluster, date, 100 * ci + dev, seq=d,
                                   usage=_usage(rnd, treated), arm=arm))
    return out


def test_measurement_pipeline_on_fakes(tmp_path: Path) -> None:
    clusters = [f"team-{CANARY}-{i}" for i in range(12)]
    store = MemoryStore(org_key=ORG_KEY)
    ingest(store, _requests(clusters, range(PRE_DAYS)), name="pre")
    base = FakePricer()
    pre = P.build_panel(store, cluster_kind="team", since=_day(0), until=_day(PRE_DAYS),
                        baseline_pricer=base, actual_pricer=base)
    assert len({r.cluster_id for r in pre}) == 12
    projection = estimated(2_000_000_000, Basis.LIST, calibration=Calibration.CALIBRATED)
    look = _day(DAYS)
    plan = R.plan(clusters, lever_id=LEVER, cluster_kind="team", design="stepped_wedge",
                  waves=3, holdback=Decimal("0.25"), seed=5, pre_panel=pre,
                  projection=projection, washout_hours=12, looks=(look,),
                  rate_card_sha256=base.rate_card_sha256)
    assert plan.mde_nano is not None
    starts = {n: _day(PRE_DAYS + 7 * (n - 1)) for n, _ in plan.waves}
    by_cluster = {c: starts[n] for n, members in plan.waves for c in members}
    ingest(store, _requests(clusters, range(PRE_DAYS, DAYS), starts=by_cluster,
                            holdback=plan.holdback), name="post")
    rows = P.build_panel(store, cluster_kind="team", since=_day(0), until=_day(DAYS),
                         baseline_pricer=base, actual_pricer=FakePricer(contract=CUT20),
                         arms=R.arms_for(plan, starts))
    assert {r.cluster_id for r in rows if r.treated} == {c for _, m in plan.waves for c in m}
    channels = P.panel_channels(store, cluster_kind="team", since=_day(0), until=_day(DAYS))
    scopes = P.cache_scope_clusters(store, cluster_kind="team", since=_day(0), until=_day(DAYS))
    m = L.measure(rows, plan=plan, reconciliation=recon({"anthropic_api": "reconciled"}),
                  look=look, rate_card_sha256=base.rate_card_sha256, channels=channels,
                  cache_scopes=scopes, boot=200, seed=3)
    assert m.estimate.evidence in (Evidence.MEASURED, Evidence.VERIFIED)
    assert m.estimate.nano > 0 and m.estimate.low_nano > 0          # a real saving
    assert m.rate_variance.evidence is Evidence.EXACT and m.rate_variance.nano < 0
    receipt = RC.build_receipt(m, lever_id=LEVER, patch_sha256="12" * 32,
                               shapley_credit=None, reconciliation_verdict="reconciled",
                               calibration=Calibration.CALIBRATED, tool_version="0.2.0",
                               created=look)
    assert receipt["predicate"]["label"] == m.estimate.evidence.value
    key = tmp_path / "k"
    key.write_text("key")

    def runner(args, **kw):
        Path(args[-1] + ".sig").write_text("sig")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    env = RC.sign(receipt, key_path=key, runner=runner)
    row = RC.store_receipt(store, receipt, envelope=env)
    assert store.receipts(lever_class="cache_transform") == [row]
    # content never reaches the outputs: cluster names are inputs, not results
    assert_no_canary(json.dumps(to_json(m)), RC.canonical_bytes(receipt), row.json, row.dsse)
    assert CANARY not in repr(m)
