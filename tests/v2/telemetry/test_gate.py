"""Merge-gate-1 tests (SPEC §21 #4): the TELEM fixtures through the real ``RateCard`` (RATES) and
the real ``SqliteStore`` (STORE). Skipped until those packages are merged."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.core import registry
from tokenbill.core.ids import key_id
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestResult

pytestmark = pytest.mark.gate

FIXTURES = [h.OTLP_CC, h.OTLP_GENAI, h.OTLP_OI, h.OPENAI, h.BEDROCK, h.ANTHROPIC]


def _results() -> list[IngestResult]:
    return [registry.sniff_adapter(p).read(p, h.central()) for p in FIXTURES]


def _inferences(result: IngestResult) -> list[tuple[Any, int]]:
    return [(inf, req.ts_start_ms) for req in result.requests for att in req.attempts
            for inf in att.inferences]


def test_gate_ratecard_prices_telem_fixtures_like_the_fake_pricer() -> None:
    engine = pytest.importorskip("tokenbill.rates.engine")
    schema = pytest.importorskip("tokenbill.rates.schema")
    card = engine.RateCard([schema.load_builtin()])
    fake = FakePricer()
    compared = 0
    for result in _results():
        for inf, ts in _inferences(result):
            ours = card.price_inference(inf, ts_ms=ts)
            ref = fake.price_inference(inf, ts_ms=ts)
            if ref.unpriced_reason is None:
                assert ours.unpriced_reason is None
                assert (ours.exact_nano, ours.figure.nano) == (ref.exact_nano, ref.figure.nano)
                compared += 1
            if inf.provider_reported_cost_nano is not None:  # estimates never enter a price
                stripped = dataclasses.replace(inf, provider_reported_cost_nano=None,
                                               provider_reported_cost_basis=None)
                assert card.price_inference(stripped, ts_ms=ts).figure == ours.figure
    assert compared > 0


def test_gate_sqlite_store_ingests_telem_fixtures(tmp_path: Path) -> None:
    db = pytest.importorskip("tokenbill.store.db")
    store = db.SqliteStore(tmp_path / "tb.sqlite", org_key=h.ORG_KEY,
                           name_key_id=key_id(h.NAME_KEY), pricer=FakePricer())
    expected = 0
    for result in _results():
        store.ingest(result)
        store.ingest(result)  # the same source twice is a no-op
        expected += len(result.requests)
    assert sum(1 for _ in store.iter_requests()) == expected
    assert [a.source_kind for a in store.aggregates("otel.metric")] == ["otel.metric"]
