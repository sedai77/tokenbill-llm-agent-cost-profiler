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

GitHub Copilot (CORE-AMENDMENTS C-23, addendum CA-20): :func:`normalize_copilot_model` turns
billing-report display names, metrics ids and OTel model strings into the canonical ids of the
Copilot rate rows plus routing (``Auto:``), speed (``(fast mode)``) and pseudo labels (code review,
cloud agent, unattributed Auto, unknown); :func:`is_copilot_resource` is the one Copilot OTel
resource predicate (TELEM skips, CP-OTEL claims).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "CONFIG_ALIASES",
    "COPILOT_SERVICE_NAMES",
    "CopilotModel",
    "ModelId",
    "is_copilot_resource",
    "normalize_copilot_model",
    "normalize_model",
]

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
    those channels (scope stays ``unknown``). ``"github"`` delegates to
    :func:`normalize_copilot_model` (a compatibility view: routing, speed and pseudo labels are only
    available from that function, which every Copilot adapter calls directly); a pseudo label gives
    ``model=""`` with reason ``"copilot pseudo model"``.
    """
    raw = (model_raw or "").strip() if isinstance(model_raw, str) else ""
    if not raw:
        return _not_priceable("empty")
    if provider_hint == "github":
        cm = normalize_copilot_model(raw)
        if cm.pseudo is not None:
            reason: str | None = "copilot pseudo model"
        else:
            reason = None if cm.model else "empty"
        return ModelId(model=cm.model, channel_hint=None, endpoint_scope="unknown", reason=reason)
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


# ---------------------------------------------------------------------------------------------
# GitHub Copilot model labels and OTel resources (C-23)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CopilotModel:
    model: str                  # canonical Copilot rate-row id; "" for pseudo labels
    routing: str                # "direct" | "auto" | "unknown"
    speed: str                  # "standard" | "fast"
    pseudo: str | None          # core.records.COPILOT_PSEUDO value, or None
    suffix_stripped: str | None  # id suffix removed ("-1m-internal", "-1m", "-internal"), or None


_AUTO_PREFIX_RE = re.compile(r"\Aauto\s*:\s*", re.IGNORECASE)
_FOOTNOTE_RE = re.compile(r"\[\^[^\]]*\]")
_FAST_RE = re.compile(r"\s*\(fast mode\)", re.IGNORECASE)
_PREVIEW_RE = re.compile(r"\s*\(preview\)", re.IGNORECASE)
_COPILOT_SUFFIXES = ("-1m-internal", "-1m", "-internal")
_CLOUD_AGENT_MARKERS = ("coding agent", "padawan", "cloud agent")


def normalize_copilot_model(model_raw: str) -> CopilotModel:
    """Normalize a GitHub Copilot model label (addendum CA-20), rules in order: strip; a leading
    ``Auto:`` (any case) → routing ``auto``; exactly ``auto`` → pseudo ``auto_unattributed``;
    ``unknown`` / ``others`` → pseudo ``unknown`` (routing ``unknown``); containing ``code review``
    → pseudo ``code_review``; containing ``coding agent``, ``padawan`` or ``cloud agent`` → pseudo
    ``cloud_agent`` (**VERIFY** the native labels); remove ``[^…]`` footnotes and quotes;
    `` (fast mode)`` → speed ``fast``; `` (preview)`` removed; lowercase; spaces → ``-``; in
    ``claude-…`` ids ``.`` → ``-``; strip a trailing ``-1m-internal``, ``-1m`` or ``-internal``;
    collapse ``--``. Pseudo labels have ``model=""`` (never token-priced).

    ``"Auto: Claude Haiku 4.5"`` → ``claude-haiku-4-5`` routed ``auto``;
    ``"Claude Opus 4.8 (fast mode) (preview)"`` → ``claude-opus-4-8`` speed ``fast``.
    """
    text = model_raw.strip() if isinstance(model_raw, str) else ""
    routing = "direct"
    m = _AUTO_PREFIX_RE.match(text)
    if m is not None:
        routing = "auto"
        text = text[m.end():].strip()
    low = text.lower()
    if low == "auto" or (routing == "auto" and not low):
        return CopilotModel("", "auto", "standard", "auto_unattributed", None)
    if low in ("unknown", "others"):
        return CopilotModel("", "unknown" if routing == "direct" else routing, "standard",
                            "unknown", None)
    if "code review" in low:
        return CopilotModel("", routing, "standard", "code_review", None)
    if any(marker in low for marker in _CLOUD_AGENT_MARKERS):
        return CopilotModel("", routing, "standard", "cloud_agent", None)
    text = _FOOTNOTE_RE.sub("", text).replace("'", "").replace('"', "")
    speed = "standard"
    if _FAST_RE.search(text):
        speed = "fast"
        text = _FAST_RE.sub("", text)
    text = _PREVIEW_RE.sub("", text)
    model = "-".join(text.strip().lower().split())
    if model.startswith("claude-"):
        model = model.replace(".", "-")
    suffix = None
    for candidate in _COPILOT_SUFFIXES:
        if model.endswith(candidate) and len(model) > len(candidate):
            suffix = candidate
            model = model[: -len(candidate)]
            break
    while "--" in model:
        model = model.replace("--", "-")
    return CopilotModel(model, routing, speed, None, suffix)


#: Default ``service.name`` values of Copilot OTel resources (CLI ``github-copilot``, VS Code
#: ``copilot-chat``); more come from ``IngestOptions.otel_service_names``.
COPILOT_SERVICE_NAMES = frozenset({"github-copilot", "copilot-chat"})
_COPILOT_SCOPE_PREFIX = "github.copilot"
_COPILOT_ATTR_PREFIXES = ("github.copilot.", "copilot_chat.")


def is_copilot_resource(service_name: str | None, scope_names: Iterable[str],
                        attr_keys: Iterable[str], *,
                        extra_service_names: Iterable[str] = ()) -> bool:
    """Whether an OTel resource is a GitHub Copilot resource (addendum §5.10).

    True iff its ``service.name`` is ``github-copilot``, ``copilot-chat`` or one of
    *extra_service_names* (entries may be ``NAME=<agent_product>``; only ``NAME`` counts), **or** an
    instrumentation scope name starts with ``github.copilot``, **or** a span attribute key starts
    with ``github.copilot.`` or ``copilot_chat.``. TELEM's ``otlp`` skips such resources and CP-OTEL
    claims them."""
    names = set(COPILOT_SERVICE_NAMES)
    for entry in extra_service_names:
        name = entry.split("=", 1)[0].strip()
        if name:
            names.add(name)
    if service_name is not None and service_name in names:
        return True
    if any(isinstance(s, str) and s.startswith(_COPILOT_SCOPE_PREFIX) for s in scope_names):
        return True
    return any(isinstance(k, str) and k.startswith(_COPILOT_ATTR_PREFIXES) for k in attr_keys)
