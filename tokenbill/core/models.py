"""Model-id normalization (SPEC §3.12).

Rules, in order: strip whitespace; ``<synthetic>`` → not priceable; Claude Code config aliases
(``opus``, ``sonnet``, ``haiku``, ``fable``, ``opusplan``, ``default``, also with a ``[1m]`` suffix)
→ not priceable; strip a trailing ``[1m]``; the Bedrock form
``[<geo>.]anthropic.<model>[-v<N>[:<M>]]`` (ARN prefixes allowed) → channel hint bedrock, scope
``global`` for the ``global.`` profile else ``regional``; the Vertex form
``<model>@<YYYYMMDD|latest>`` (``publishers/anthropic/models/`` prefixes allowed) → channel hint
vertex, scope ``unknown``; a trailing ``-YYYYMMDD`` snapshot suffix is removed.

The Bedrock version suffix is optional because current Bedrock ids carry none
(``anthropic.claude-opus-5-5``; models overview, verified 2026-09-23); the geo → scope mapping is
research-grade (SPEC §19.8 #3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CONFIG_ALIASES", "ModelId", "normalize_model"]

#: Claude Code config aliases: they name a model family, not a priceable model.
CONFIG_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable", "opusplan", "default"})

_ONE_M_RE = re.compile(r"\[1m\]\Z", re.IGNORECASE)
_BEDROCK_ARN_RE = re.compile(
    r"\Aarn:aws[a-z-]*:bedrock:[^:]*:[^:]*:(?:foundation-model|inference-profile)/")
_BEDROCK_RE = re.compile(
    r"\A(?:(?P<geo>[a-z]{2,8}(?:-[a-z]+)?)\.)?anthropic\.(?P<model>[A-Za-z0-9.-]+?)"
    r"(?:-v(?P<version>\d+)(?::(?P<minor>\d+))?)?\Z")
_VERTEX_PATH_RE = re.compile(r"\A(?:.*/)?publishers/anthropic/models/")
_VERTEX_RE = re.compile(r"\A(?P<model>[A-Za-z0-9.-]+)@(?:\d{8}|latest)\Z")
_SNAPSHOT_RE = re.compile(r"-\d{8}\Z")


@dataclass(frozen=True)
class ModelId:
    model: str            # canonical id, "" when not priceable
    channel_hint: str | None   # "bedrock" | "vertex" | None
    # "global" | "regional" | "unknown" (Vertex ids carry no region; plain ids "unknown")
    endpoint_scope: str
    reason: str | None    # why not priceable ("synthetic", "config alias", "empty")


def _not_priceable(reason: str) -> ModelId:
    return ModelId(model="", channel_hint=None, endpoint_scope="unknown", reason=reason)


def normalize_model(model_raw: str, provider_hint: str | None = None) -> ModelId:
    """Normalize a reported model id to the canonical id used as ``PricingContext.model``.

    *provider_hint* ``"bedrock"`` / ``"vertex"`` sets the channel hint for plain ids reported by
    those channels (scope stays ``unknown``).
    """
    raw = (model_raw or "").strip() if isinstance(model_raw, str) else ""
    if not raw:
        return _not_priceable("empty")
    if raw == "<synthetic>":
        return _not_priceable("synthetic")
    if raw.lower() in CONFIG_ALIASES:
        return _not_priceable("config alias")
    model = _ONE_M_RE.sub("", raw).strip()
    if model.lower() in CONFIG_ALIASES:
        return _not_priceable("config alias")
    if not model:
        return _not_priceable("empty")

    channel_hint: str | None = provider_hint if provider_hint in ("bedrock", "vertex") else None
    scope = "unknown"
    bedrock_id = _BEDROCK_ARN_RE.sub("", model)
    m = _BEDROCK_RE.match(bedrock_id)
    if m is not None:
        model = m.group("model")
        channel_hint = "bedrock"
        scope = "global" if m.group("geo") == "global" else "regional"
    else:
        vertex_id = _VERTEX_PATH_RE.sub("", model)
        m = _VERTEX_RE.match(vertex_id)
        if m is not None:
            model = m.group("model")
            channel_hint = "vertex"
            scope = "unknown"
    model = _SNAPSHOT_RE.sub("", model)
    if not model:
        return _not_priceable("empty")
    return ModelId(model=model, channel_hint=channel_hint, endpoint_scope=scope, reason=None)
