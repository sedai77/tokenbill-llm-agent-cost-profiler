"""Provider-capability checks shared by cache and automation detectors."""

from __future__ import annotations

from collections.abc import Sequence

from tokenbill.core.errors import TokenbillError
from tokenbill.core.records import Lane
from tokenbill.core.types import AnalysisContext

__all__ = ["common_ttl_options", "supports_every_serving"]


def common_ttl_options(ctx: AnalysisContext, lanes: Sequence[Lane]) -> frozenset[int]:
    """Documented cache lifetimes common to every serving request in ``lanes``."""
    common: set[int] | None = None
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None:
                continue
            try:
                options = set(ctx.rules.rules_for(
                    inf.pricing.provider, inf.pricing.channel, inf.pricing.model).ttl_options_s)
            except (AttributeError, TokenbillError):
                return frozenset()
            common = options if common is None else common & options
    return frozenset() if common is None else frozenset(common)


def supports_every_serving(ctx: AnalysisContext, lanes: Sequence[Lane], feature: str) -> bool:
    """Whether every serving request explicitly supports ``feature``; unknown is unsupported."""
    seen = False
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None:
                continue
            seen = True
            try:
                if not ctx.pricer.supports(inf.pricing, feature, ts_ms=req.ts_start_ms):
                    return False
            except (AttributeError, TokenbillError):
                return False
    return seen
