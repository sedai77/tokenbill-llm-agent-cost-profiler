"""Convention registry and the Anthropic usage rules (SPEC §3.11, §5.2; F-SEM).

A *convention* turns one provider usage object into disjoint canonical
:class:`~tokenbill.core.records.UsageBuckets`. F-SEM registers ``anthropic.messages`` (used for the
Claude API, Claude Platform on AWS, Foundry, Vertex rawPredict, Bedrock InvokeModel, Claude Code
transcripts, headless streams and recorded Anthropic responses) and a **disabled**
``codex.rollout``; every other convention is registered by
``tokenbill.adapters.conventions_ext`` (TELEM), which :func:`normalize` / :func:`get_convention`
import lazily the first time an unknown id is requested.

:func:`anthropic_inferences` applies the ``usage.iterations`` rule (one :class:`Inference` per
iteration, top-level usage not priced) with the invariant checks and the refusal table of D10.

Malformed usage (a non-integer, negative or out-of-range count, or a non-object where an object is
expected) raises :class:`BadUsageError` (a :class:`UsageError`) whose message starts with
``"bad_usage"``; adapters quarantine the record with reason ``bad_usage``. Messages name fields
only, never values.
"""

from __future__ import annotations

import dataclasses
import importlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tokenbill.core.errors import ContractViolation, PricingError, UsageError
from tokenbill.core.evidence import REFUSAL_AMBIGUOUS_MAX_OUTPUT
from tokenbill.core.ids import stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    MAX_TOKENS,
    Inference,
    InferenceKind,
    PricingContext,
    UsageBuckets,
    UsageSource,
)

__all__ = [
    "ANTHROPIC_MESSAGES",
    "CODEX_ROLLOUT",
    "REFUSAL_AMBIGUOUS",
    "REFUSAL_MID_STREAM",
    "REFUSAL_PRE_OUTPUT",
    "BadUsageError",
    "Convention",
    "anthropic_inferences",
    "get_convention",
    "normalize",
    "normalize_anthropic_messages",
    "register_convention",
    "registered_conventions",
    "sum_check",
]

ANTHROPIC_MESSAGES = "anthropic.messages"
CODEX_ROLLOUT = "codex.rollout"

#: Billing-rule ids of the refusal table (D10, §3.11).
REFUSAL_PRE_OUTPUT = "anthropic.refusal.pre_output"
REFUSAL_AMBIGUOUS = "anthropic.refusal.ambiguous"
REFUSAL_MID_STREAM = "anthropic.refusal.mid_stream"

_DEFAULT_REFUSAL_MAX = REFUSAL_AMBIGUOUS_MAX_OUTPUT.value
if type(_DEFAULT_REFUSAL_MAX) is not int:  # pragma: no cover - facts.json guarantees an int
    raise ContractViolation("REFUSAL_AMBIGUOUS_MAX_OUTPUT must be an int")


class BadUsageError(UsageError):
    """A provider usage object is malformed (quarantine reason ``bad_usage``).

    The message is ``"bad_usage: <field>"``: content-free, it names the offending field only.
    """


@dataclass(frozen=True)
class Convention:
    """A registered usage convention (§3.11)."""

    convention_id: str            # e.g. "anthropic.messages"
    provider: str
    inclusive_input: bool         # True: provider "input" includes cached/written tokens
    enabled: bool                 # False: normalize() raises PricingError("convention unverified")
    notes: str


NormalizeFn = Callable[[Mapping[str, object]], tuple[UsageBuckets, list[str]]]

_LOCK = threading.Lock()
_REGISTRY: dict[str, tuple[Convention, NormalizeFn]] = {}
_EXTENSIONS_LOADED = False


def register_convention(conv: Convention, fn: NormalizeFn) -> None:
    """Register *conv* with its normalizer *fn* (``raw_usage -> (buckets, dq codes)``).

    Re-registering the same id with an identical :class:`Convention` replaces the function (an
    idempotent re-import); a *different* Convention under an existing id raises
    :class:`ContractViolation` (conventions are never silently redefined).
    """
    if not isinstance(conv, Convention):
        raise ContractViolation("register_convention: conv must be a Convention")
    if not isinstance(conv.convention_id, str) or not conv.convention_id:
        raise ContractViolation("register_convention: convention_id must be a non-empty str")
    if not callable(fn):
        raise ContractViolation("register_convention: fn must be callable")
    with _LOCK:
        existing = _REGISTRY.get(conv.convention_id)
        if existing is not None and existing[0] != conv:
            raise ContractViolation(
                f"register_convention: {conv.convention_id} is already registered differently")
        _REGISTRY[conv.convention_id] = (conv, fn)


def _load_extensions() -> None:
    """Import ``core.registry.CONVENTION_MODULES`` once (they register their conventions)."""
    global _EXTENSIONS_LOADED
    if _EXTENSIONS_LOADED:
        return
    _EXTENSIONS_LOADED = True
    from tokenbill.core.registry import CONVENTION_MODULES

    for module in CONVENTION_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            # The owning package has not landed yet: only a missing module (or one of its parent
            # packages) is tolerated; a broken module propagates its error.
            if exc.name is None or not (module == exc.name or module.startswith(exc.name + ".")):
                raise


def _lookup(convention_id: str) -> tuple[Convention, NormalizeFn]:
    if not isinstance(convention_id, str):
        raise UsageError("unknown convention")
    entry = _REGISTRY.get(convention_id)
    if entry is None:
        _load_extensions()
        entry = _REGISTRY.get(convention_id)
    if entry is None:
        raise UsageError(f"unknown convention {convention_id[:64]!r}")
    return entry


def get_convention(convention_id: str) -> Convention:
    """The registered :class:`Convention` (imports the extension modules on the first unknown id;
    an id that is still unknown raises :class:`UsageError`)."""
    return _lookup(convention_id)[0]


def registered_conventions() -> tuple[Convention, ...]:
    """Every registered convention, sorted by id (informational; does not load extensions)."""
    with _LOCK:
        return tuple(conv for _, (conv, _fn) in sorted(_REGISTRY.items()))


def normalize(convention_id: str,
              raw_usage: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """Normalize *raw_usage* with the convention *convention_id*.

    Returns ``(buckets, data-quality codes)``. A disabled convention raises
    ``PricingError("convention unverified")``; malformed usage raises :class:`BadUsageError`.
    """
    conv, fn = _lookup(convention_id)
    if not conv.enabled:
        raise PricingError("convention unverified")
    if not isinstance(raw_usage, Mapping):
        raise BadUsageError("bad_usage: usage")
    return fn(raw_usage)


def sum_check(buckets: UsageBuckets, provider_total_input: int | None,
              provider_output: int | None) -> list[str]:
    """``["dq.sum_check_failed"]`` when the buckets do not reproduce a provider-reported total.

    *provider_total_input* is compared with ``buckets.total_input`` (uncached + reads + every
    write), *provider_output* with ``buckets.output``; ``None`` skips that comparison. Example: an
    OTel span reporting 17 input tokens for a call whose buckets hold 17,119 fails (§5.2).
    """
    if provider_total_input is not None and buckets.total_input != provider_total_input:
        return ["dq.sum_check_failed"]
    if provider_output is not None and buckets.output != provider_output:
        return ["dq.sum_check_failed"]
    return []


# ---------------------------------------------------------------------------------------------
# anthropic.messages
# ---------------------------------------------------------------------------------------------

_MISSING = object()


def _count(obj: Mapping[str, Any], key: str, where: str) -> int | None:
    """An int token count from *obj*; ``None`` when absent or JSON null; bad values raise."""
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_TOKENS:
        raise BadUsageError(f"bad_usage: {where}{key}")
    return value


def _sub(obj: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BadUsageError(f"bad_usage: {key}")
    return value


def normalize_anthropic_messages(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """The ``anthropic.messages`` mapping (§3.11) of one usage object (or one ``iterations[]``
    element, which has the same shape).

    ``uncached_input = input_tokens``; ``cache_read = cache_read_input_tokens``; 5m / 1h writes from
    ``cache_creation.ephemeral_{5m,1h}_input_tokens``; ``cache_write_unknown =
    cache_creation_input_tokens − 5m − 1h`` when positive (``dq.ttl_split_residual``); a negative
    residual keeps the split (``dq.ttl_split_exceeds_total``); an absent
    ``cache_creation_input_tokens`` means "the split is the total". ``output = output_tokens``;
    ``output_reasoning = output_tokens_details.thinking_tokens`` (dropped with
    ``dq.sum_check_failed`` when it exceeds output); server tool requests from ``server_tool_use``.
    Missing fields are 0.
    """
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []
    uncached = _count(raw, "input_tokens", "") or 0
    read = _count(raw, "cache_read_input_tokens", "") or 0
    total_write = _count(raw, "cache_creation_input_tokens", "")
    creation = _sub(raw, "cache_creation")
    w5 = w1 = 0
    if creation is not None:
        w5 = _count(creation, "ephemeral_5m_input_tokens", "cache_creation.") or 0
        w1 = _count(creation, "ephemeral_1h_input_tokens", "cache_creation.") or 0
    unknown = 0
    if total_write is not None:
        residual = total_write - w5 - w1
        if residual > 0:
            unknown = residual
            notes.append("dq.ttl_split_residual")
        elif residual < 0:
            notes.append("dq.ttl_split_exceeds_total")
    output = _count(raw, "output_tokens", "") or 0
    reasoning: int | None = None
    details = _sub(raw, "output_tokens_details")
    if details is not None:
        reasoning = _count(details, "thinking_tokens", "output_tokens_details.")
        if reasoning is not None and reasoning > output:
            reasoning = None
            notes.append("dq.sum_check_failed")
    web_search = web_fetch = 0
    server = _sub(raw, "server_tool_use")
    if server is not None:
        web_search = _count(server, "web_search_requests", "server_tool_use.") or 0
        web_fetch = _count(server, "web_fetch_requests", "server_tool_use.") or 0
    try:
        buckets = UsageBuckets(
            uncached_input=uncached,
            cache_read=read,
            cache_write_5m=w5,
            cache_write_1h=w1,
            cache_write_unknown=unknown,
            output=output,
            output_reasoning=reasoning,
            web_search_requests=web_search,
            web_fetch_requests=web_fetch,
        )
    except ContractViolation:  # pragma: no cover - every count was range-checked above
        raise BadUsageError("bad_usage: usage") from None
    return buckets, notes


def _codex_rollout(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    # Never reached through normalize() (the convention is disabled); direct calls refuse too.
    raise PricingError("convention unverified")


register_convention(
    Convention(
        convention_id=ANTHROPIC_MESSAGES,
        provider="anthropic",
        inclusive_input=False,
        enabled=True,
        notes="input_tokens excludes cache reads and writes; usage.iterations is the per-attempt "
              "billing record (anthropic.messages, SPEC §3.11)",
    ),
    normalize_anthropic_messages,
)
register_convention(
    Convention(
        convention_id=CODEX_ROLLOUT,
        provider="openai",
        inclusive_input=True,
        enabled=False,
        notes="convention unverified (codex-usage-format); shipped disabled (D24)",
    ),
    _codex_rollout,
)


# ---------------------------------------------------------------------------------------------
# iterations and the refusal rule
# ---------------------------------------------------------------------------------------------

_KIND_BY_TYPE = {
    "message": InferenceKind.MESSAGE,
    "compaction": InferenceKind.COMPACTION,
    "advisor_message": InferenceKind.ADVISOR,
    "fallback_message": InferenceKind.FALLBACK,
}


def _signature(u: UsageBuckets) -> tuple[int, int, int, int]:
    """Billed-token signature used by the invariant checks: the TTL split and the informational
    reasoning subset are ignored (an element may report a total where the top level splits)."""
    return (u.uncached_input, u.cache_read, u.cache_write, u.output)


def _sum_signature(items: list[UsageBuckets]) -> tuple[int, int, int, int]:
    return (sum(u.uncached_input for u in items), sum(u.cache_read for u in items),
            sum(u.cache_write for u in items), sum(u.output for u in items))


def _refusal(output: int, max_ambiguous: int) -> tuple[bool | None, str]:
    """The D10 refusal table for a declined attempt with *output* tokens."""
    if output == 0:
        return False, REFUSAL_PRE_OUTPUT
    if output <= max_ambiguous:
        return None, REFUSAL_AMBIGUOUS
    return True, REFUSAL_MID_STREAM


def _dedupe(notes: list[str]) -> list[str]:
    return list(dict.fromkeys(notes))


def anthropic_inferences(raw_usage: Mapping[str, object], *, message_model: str,
                         ctx: PricingContext, id_prefix: str,
                         usage_source: UsageSource = UsageSource.FINAL,
                         advisor_model: str | None = None,
                         refusal_ambiguous_max_output: int = _DEFAULT_REFUSAL_MAX,
                         ) -> tuple[list[Inference], list[str]]:
    """The inferences of one Anthropic response (§3.11) and its data-quality codes.

    *ctx* is the pricing context of the message (its model is *message_model*, normalized). Without
    ``usage.iterations`` (absent, null or empty) the result is one MESSAGE inference from the
    top-level usage. With a non-empty ``iterations`` list, every element becomes its own inference,
    priced at ``iterations[].model`` (normalized; channel, tier, speed, geo and billing path from
    *ctx*) or *message_model*, and the top-level usage is **not** priced. Kinds by ``type``:
    ``message`` → MESSAGE, ``compaction`` → COMPACTION, ``advisor_message`` → ADVISOR (model =
    element model, else *advisor_model*, else unpriced: empty model and ``dq.unpriced_model``),
    ``fallback_message`` → FALLBACK, anything else → OTHER. A ``message`` element immediately
    followed by a ``fallback_message`` is FALLBACK_DECLINED and priced by the refusal rule: 0
    output tokens → ``billable=False`` (``anthropic.refusal.pre_output``); 1 …
    *refusal_ambiguous_max_output* → ``billable=None`` (``anthropic.refusal.ambiguous``); more →
    ``billable=True`` (``anthropic.refusal.mid_stream``).

    Invariant (``dq.iterations_mismatch`` on violation; the iterations are kept): one element ⇒ it
    equals the top level; a fallback pair ⇒ the last element equals the top level; other shapes ⇒
    Σ elements equals the top level, or Σ MESSAGE elements does (compaction and advisor iterations
    are excluded from the top-level usage, §19.2). Equality compares uncached, read, total write
    and output tokens. Top-level ``server_tool_use`` counts that no element reports are attached to
    the serving (last MESSAGE/FALLBACK) inference so web-search requests are never lost.

    Inference ids are ``stable_id("inf", id_prefix, index)``.
    """
    if not isinstance(raw_usage, Mapping):
        raise BadUsageError("bad_usage: usage")
    if type(refusal_ambiguous_max_output) is not int or refusal_ambiguous_max_output < 0:
        raise UsageError("refusal_ambiguous_max_output must be a non-negative int")
    try:
        source = UsageSource(usage_source)
    except ValueError:
        raise UsageError("usage_source must be a UsageSource value") from None
    top, top_notes = normalize_anthropic_messages(raw_usage)
    iterations = raw_usage.get("iterations")
    if iterations is None or (isinstance(iterations, list) and not iterations):
        inf = Inference(
            inference_id=stable_id("inf", id_prefix, 0),
            kind=InferenceKind.MESSAGE,
            usage=top,
            pricing=ctx,
            usage_source=source,
        )
        return [inf], _dedupe(top_notes)
    if not isinstance(iterations, list):
        raise BadUsageError("bad_usage: iterations")

    notes: list[str] = []
    elements: list[tuple[str | None, str | None, UsageBuckets]] = []
    for element in iterations:
        if not isinstance(element, Mapping):
            raise BadUsageError("bad_usage: iterations")
        etype = element.get("type")
        emodel = element.get("model")
        if etype is not None and not isinstance(etype, str):
            raise BadUsageError("bad_usage: iterations.type")
        if emodel is not None and not isinstance(emodel, str):
            raise BadUsageError("bad_usage: iterations.model")
        buckets, element_notes = normalize_anthropic_messages(element)
        notes.extend(element_notes)
        elements.append((etype, emodel or None, buckets))

    def ctx_for(model_raw: str | None) -> PricingContext:
        if not model_raw or model_raw == message_model:
            return ctx
        model = normalize_model(model_raw).model
        if model == ctx.model and model_raw == ctx.model_raw:
            return ctx
        return dataclasses.replace(ctx, model=model, model_raw=model_raw)

    kinds: list[InferenceKind] = []
    for idx, (etype, _model, _usage) in enumerate(elements):
        kind = _KIND_BY_TYPE.get(etype or "", InferenceKind.OTHER)
        if (kind is InferenceKind.MESSAGE and idx + 1 < len(elements)
                and elements[idx + 1][0] == "fallback_message"):
            kind = InferenceKind.FALLBACK_DECLINED
        kinds.append(kind)

    # invariant check against the (unpriced) top level
    usages = [u for _, _, u in elements]
    top_sig = _signature(top)
    if len(elements) == 1:
        ok = _signature(usages[0]) == top_sig
    elif any(etype == "fallback_message" for etype, _, _ in elements):
        ok = _signature(usages[-1]) == top_sig
    else:
        messages = [u for u, k in zip(usages, kinds, strict=True) if k is InferenceKind.MESSAGE]
        ok = _sum_signature(usages) == top_sig or (
            bool(messages) and _sum_signature(messages) == top_sig)
    if not ok:
        notes.append("dq.iterations_mismatch")

    # server-tool requests reported only at the top level go to the serving inference
    serving_idx = next((i for i in range(len(kinds) - 1, -1, -1)
                        if kinds[i] in (InferenceKind.MESSAGE, InferenceKind.FALLBACK)),
                       len(kinds) - 1)
    if (not any(u.web_search_requests or u.web_fetch_requests for u in usages)
            and (top.web_search_requests or top.web_fetch_requests)):
        u = usages[serving_idx]
        usages[serving_idx] = dataclasses.replace(
            u, web_search_requests=top.web_search_requests,
            web_fetch_requests=top.web_fetch_requests)

    inferences: list[Inference] = []
    for idx, ((_etype, emodel, _u), kind) in enumerate(zip(elements, kinds, strict=True)):
        billable: bool | None = True
        rule: str | None = None
        if kind is InferenceKind.ADVISOR:
            model_raw = emodel or advisor_model
            if model_raw:
                pricing = ctx_for(model_raw)
            else:
                pricing = dataclasses.replace(ctx, model="", model_raw="")
                notes.append("dq.unpriced_model")
        else:
            pricing = ctx_for(emodel)
        if kind is InferenceKind.FALLBACK_DECLINED:
            billable, rule = _refusal(usages[idx].output, refusal_ambiguous_max_output)
        inferences.append(Inference(
            inference_id=stable_id("inf", id_prefix, idx),
            kind=kind,
            usage=usages[idx],
            pricing=pricing,
            usage_source=source,
            billable=billable,
            billing_rule_id=rule,
        ))
    return inferences, _dedupe(notes)
