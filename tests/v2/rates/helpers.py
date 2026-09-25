"""Area-local helpers for the RATES tests (imported only by tests in ``tests/v2/rates``).

Every fixture is synthetic: rate documents are built here or read from
``tests/v2/fixtures/rates/`` (see the README for provenance).
"""

from __future__ import annotations

import copy
import datetime as _dt
import functools
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.builders import make_ctx
from tokenbill.core.records import PricingContext
from tokenbill.core.types import RateLayer
from tokenbill.rates.engine import RateCard
from tokenbill.rates.schema import load_builtin, parse_layer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rates"
DAY_MS = 86_400_000

__all__ = ["DAY_MS", "FIXTURES", "builtin", "card", "ctx", "doc", "layer", "nano", "row", "ts"]


def ts(date: str, hour: int = 12) -> int:
    """Milliseconds of *date* at *hour* UTC."""
    return (_dt.date.fromisoformat(date) - _dt.date(1970, 1, 1)).days * DAY_MS + hour * 3_600_000


def nano(usd: str) -> int:
    """A USD decimal string as int nano-USD (exact)."""
    value = Decimal(usd) * 10**9
    assert value == value.to_integral_value(), usd
    return int(value)


@functools.lru_cache(maxsize=1)
def builtin() -> RateLayer:
    """The built-in layer (loaded once per test session)."""
    return load_builtin()


def card(**kw: Any) -> RateCard:
    """A fresh RateCard over the built-in layer."""
    return RateCard([builtin()], **kw)


def ctx(model: str = "claude-opus-5-5", **kw: Any) -> PricingContext:
    """``core.builders.make_ctx``."""
    return make_ctx(model, **kw)


_ROW: dict[str, Any] = {
    "row_id": "test/anthropic_api/claude-test-1/2026-01-01",
    "channel": "anthropic_api", "model": "claude-test-1", "aliases": [], "generation": "5",
    "effective_from": "2026-01-01", "effective_to": None,
    "usd_per_mtok": {"input": "2.00", "output": "10.00"},
    "multipliers": {"cache_read": "0.1", "cache_write_5m": "1.25", "cache_write_1h": "2"},
    "published_absolute": {"cache_read": "0.20", "cache_write_5m": "2.50"},
    "min_cacheable_tokens": 1024, "tokenizer_family": "claude-4.7+",
    "per_request_usd": {"web_search": "0.01"}, "long_context": None, "promotion": None,
    "supports": ["batch"], "enabled": True, "verified_on": "2026-09-01",
    "sources": [{"url": "https://example.test/pricing", "retrieved": "2026-09-01",
                 "finding": "synthetic"}],
}


def row(**overrides: Any) -> dict[str, Any]:
    """A valid synthetic row document with *overrides* applied (``None`` values are kept)."""
    out = copy.deepcopy(_ROW)
    out.update(overrides)
    return out


def doc(rows: list[dict[str, Any]] | None = None, modifiers: list[dict[str, Any]] | None = None,
        **top: Any) -> dict[str, Any]:
    """A ``tokenbill/rates@1`` document."""
    out: dict[str, Any] = {"schema": "tokenbill/rates@1", "provider": "test",
                           "as_of": "2026-09-01", "rows": rows if rows is not None else [row()],
                           "modifiers": modifiers or []}
    out.update(top)
    return out


def layer(rows: list[dict[str, Any]] | None = None,
          modifiers: list[dict[str, Any]] | None = None, name: str = "user:test") -> RateLayer:
    """A parsed layer of :func:`doc`."""
    return parse_layer(json.dumps(doc(rows, modifiers)), name)
