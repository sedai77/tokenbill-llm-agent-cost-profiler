"""Gate (merge gate 1, PLAN §1.5): ``build_env`` loads the real RATES ``RateCard``; the layer order
(builtin < ``--rates`` < ``--model-price``) and the ESR through ``no_cache_equivalent_nano``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.config import Config
from tokenbill.core.builders import make_inference
from tokenbill.core.testing import MemoryStore, assert_pricer_conforms
from tokenbill.core.types import IngestOptions
from tokenbill.pipeline.common import bill_summary, build_env, ingest_paths

from .support import ORG_KEY, T0, req, write_fake

engine = pytest.importorskip("tokenbill.rates.engine")
pytest.importorskip("tokenbill.rates.schema")

pytestmark = pytest.mark.gate

TS = T0 + 43_200_000      # 2026-09-23 12:00 UTC


def test_build_env_loads_the_real_rate_card() -> None:
    env = build_env(Config(), now_ms=TS)
    assert isinstance(env.pricer, engine.RateCard)
    assert env.pricer.rate_card_sha256
    assert_pricer_conforms(env.pricer, samples=50)


def test_model_price_layer_wins_over_builtin() -> None:
    usage = {"uncached_input": 1_000_000}
    builtin = build_env(Config(), now_ms=TS).pricer
    custom = build_env(Config(), model_prices=[("claude-opus-5-5", "5.00", "25.00")],
                       now_ms=TS).pricer
    base = builtin.price_inference(make_inference(usage), ts_ms=TS).figure.nano
    over = custom.price_inference(make_inference(usage), ts_ms=TS).figure.nano
    assert (base, over) == (4_000_000_000, 5_000_000_000)


def test_esr_with_the_real_engine(fake_adapters: object, tmp_path: Path) -> None:
    env = build_env(Config(), now_ms=TS)
    store = MemoryStore(org_key=ORG_KEY)
    records = [req("L1", i, u=500, r=20_000, w=0, o=200) for i in range(3)]
    ingest_paths(store, [write_fake(tmp_path / "f.jsonl", records)], env, IngestOptions())
    summary = bill_summary(store, env, since_ms=T0, until_ms=T0 + 86_400_000, group_by=["team"])
    assert summary.esr is not None and 0 < Decimal(summary.esr) < 1   # reads save money
