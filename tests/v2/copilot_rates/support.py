"""Shared helpers of the CP-RATES tests (imported only within ``tests/v2/copilot_rates``)."""

from __future__ import annotations

import copy
import datetime as _dt
import functools
import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot import rates_verify as rv
from tokenbill.core.facts import load as load_facts
from tokenbill.core.records import PricingContext
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import RateLayer

REPO = Path(__file__).resolve().parents[3]
YML_DIR = REPO / "tests" / "v2" / "fixtures" / "copilot_rates" / "yml"
RATE_PATH = REPO / "tokenbill" / "copilot" / "data" / rv.RATE_FILE


def ts(date: str) -> int:
    """Noon UTC of *date* (``YYYY-MM-DD``) in epoch milliseconds."""
    day = _dt.datetime.fromisoformat(date).replace(hour=12, tzinfo=_dt.timezone.utc)
    return int(day.timestamp()) * 1000


def ctx(model: str, **kw: Any) -> PricingContext:
    """A Copilot pricing context on billing path ``copilot_pool`` unless overridden."""
    return PricingContext(provider="github", channel=rv.CHANNEL, model=model, model_raw=model,
                          billing_path=kw.pop("billing_path", "copilot_pool"), **kw)


@functools.lru_cache(maxsize=1)
def revisions() -> tuple[rv.Revision, ...]:
    """The 38 fixture revisions (parsed once per session)."""
    return rv.load_revisions(YML_DIR)


@functools.lru_cache(maxsize=1)
def layer() -> RateLayer:
    """The packaged rate file as a layer (parsed once per session)."""
    return rv.load_layer()


def rate_doc() -> dict[str, Any]:
    """A fresh deep copy of the committed rate file document (tests mutate it)."""
    return copy.deepcopy(_rate_doc())


@functools.lru_cache(maxsize=1)
def _rate_doc() -> dict[str, Any]:
    return json.loads(RATE_PATH.read_text(encoding="utf-8"))


def row(doc: dict[str, Any], row_id: str) -> dict[str, Any]:
    """The row *row_id* of *doc* (KeyError when absent)."""
    for r in doc["rows"]:
        if r["row_id"] == row_id:
            return r
    raise KeyError(row_id)


class FilePricer(FakePricer):
    """The reference FakePricer with its Copilot rows and modifiers taken from a rate *layer*
    (the shipped file) instead of ``facts.copilot`` — the unit stand-in for RATES' RateCard."""

    def __init__(self, source: RateLayer) -> None:
        super().__init__()
        facts = load_facts()
        self._rows = tuple(facts.rate_rows) + tuple(source.rows)
        self._mods = tuple(facts.modifiers) + tuple(source.modifiers)
        self.rate_card_sha256 = source.sha256


def ratecard() -> Any:
    """RATES' real ``RateCard`` over its builtin layer, which carries the extension rate files
    (CORE-AMENDMENTS A-1); skips until RATES is merged (gate tests only)."""
    engine = pytest.importorskip("tokenbill.rates.engine")
    schema = pytest.importorskip("tokenbill.rates.schema")
    return engine.RateCard([schema.load_builtin()])
