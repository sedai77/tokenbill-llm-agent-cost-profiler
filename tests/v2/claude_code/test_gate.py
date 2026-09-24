"""Merge-gate-1 tests of the CC seams (run at the canary merge; skipped until the sibling packages
land): CC records through the real ``SqliteStore`` (incremental collection merges to the one-shot
import under the store's own tie rule, §7.3) and priced by the real ``RateCard`` (equal to the
fakes on the facts rows, D37)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.adapters.cc_collect import QUIESCENT_MS, CollectorState, collect_incremental
from tokenbill.core.ids import key_id
from tokenbill.core.records import Inference, to_json
from tokenbill.core.testing import FakePricer, MemoryStore, fake_price_total

from .helpers import CC, FIXTURES, NAME_KEY, bf, opts, set_mtime

pytestmark = pytest.mark.gate

ORG_KEY = bytes(range(224, 256))
NOW = bf.T0_MS + 3_600_000


def _sqlite(tmp: Path, name: str) -> Any:
    db = pytest.importorskip("tokenbill.store.db")
    (tmp / name).mkdir(parents=True, exist_ok=True)
    return db.SqliteStore(tmp / name / "tokenbill.db", org_key=ORG_KEY,
                          name_key_id=key_id(NAME_KEY))


def _lanes(store: Any) -> str:
    return json.dumps([to_json(lane) for lane in store.iter_lanes(since_ms=0, until_ms=2**53)],
                      sort_keys=True)


def test_incremental_equals_one_shot_in_sqlite_store(tmp_path: Path) -> None:
    data = bf.alpha_main().text().encode()
    root = tmp_path / "projects"
    path = root / "-home-dev-gate" / f"{bf.SID_ALPHA}.jsonl"
    path.parent.mkdir(parents=True)
    state = CollectorState()
    results = []
    for i, cut in enumerate((len(data) // 3, 2 * len(data) // 3, len(data))):
        path.write_bytes(data[:cut])
        set_mtime(path, NOW + i * 1_000)
        results += list(collect_incremental(root, state, opts(), now_ms=NOW + i * 1_000))
    results += list(collect_incremental(root, state, opts(), now_ms=NOW + QUIESCENT_MS * 2))
    inc = _sqlite(tmp_path, "inc")
    for r in results:
        inc.ingest(r)
    one = _sqlite(tmp_path, "one")
    one.ingest(CC.read(path, opts()))
    assert _lanes(inc) == _lanes(one)


def _rate_card() -> Any:
    schema = pytest.importorskip("tokenbill.rates.schema")
    engine = pytest.importorskip("tokenbill.rates.engine")
    layer = schema.load_builtin()
    for build in (lambda: engine.RateCard([layer]), lambda: engine.RateCard((layer,)),
                  lambda: engine.RateCard(layers=[layer]), lambda: engine.RateCard(layer)):
        try:
            return build()
        except TypeError:
            continue
    pytest.fail("cannot build a RateCard from load_builtin()")


def test_rate_card_prices_the_fixture_tree_like_the_fakes() -> None:
    card = _rate_card()
    store = MemoryStore(org_key=ORG_KEY, name_key_id=key_id(NAME_KEY))
    store.ingest(CC.read(FIXTURES / "projects", opts()))
    items = [(Inference(inference_id=r.inference_id, kind=r.kind, usage=r.usage,
                        pricing=r.pricing, usage_source=r.usage_source, billable=r.billable,
                        billing_rule_id=r.billing_rule_id), r.ts_ms)
             for r in store.iter_usage_records()]
    real = fake_price_total(card, items)
    fake = fake_price_total(FakePricer(), items)
    assert real.exact.nano == fake.exact.nano
    assert (real.estimated.nano if real.estimated else None) == \
        (fake.estimated.nano if fake.estimated else None)
