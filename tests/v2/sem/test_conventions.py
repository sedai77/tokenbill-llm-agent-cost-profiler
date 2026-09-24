"""SPEC §3.11 / §5.2: the convention registry, ``anthropic.messages``, iterations and the refusal
rule (Appendix A.8)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core import conventions as conv
from tokenbill.core import registry
from tokenbill.core.builders import FlatRates, make_ctx
from tokenbill.core.errors import ContractViolation, PricingError, UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Evidence
from tokenbill.core.records import InferenceKind, UsageBuckets, UsageSource

CTX = make_ctx("claude-fable-5", model_raw="claude-fable-5")


def usage(i: int = 0, r: int = 0, total: int | None = None, w5: int | None = None,
          w1: int | None = None, o: int = 0, **extra: Any) -> dict[str, Any]:
    """An Anthropic ``usage`` object."""
    out: dict[str, Any] = {"input_tokens": i, "cache_read_input_tokens": r, "output_tokens": o}
    if total is not None:
        out["cache_creation_input_tokens"] = total
    if w5 is not None or w1 is not None:
        out["cache_creation"] = {"ephemeral_5m_input_tokens": w5 or 0,
                                 "ephemeral_1h_input_tokens": w1 or 0}
    out.update(extra)
    return out


def it(kind: str, model: str | None = None, **u: Any) -> dict[str, Any]:
    out = {"type": kind, **usage(**u)}
    if model is not None:
        out["model"] = model
    return out


def infs(raw: dict[str, Any], **kw: Any):
    kw.setdefault("message_model", "claude-fable-5")
    kw.setdefault("ctx", CTX)
    kw.setdefault("id_prefix", "rq_test")
    return conv.anthropic_inferences(raw, **kw)


# ---------------------------------------------------------------------------------------------
# anthropic.messages mapping
# ---------------------------------------------------------------------------------------------


def test_5m_1h_split_maps_exactly() -> None:
    raw = usage(i=10, r=100, total=300, w5=100, w1=200, o=50,
                output_tokens_details={"thinking_tokens": 20},
                server_tool_use={"web_search_requests": 2, "web_fetch_requests": 1})
    buckets, notes = conv.normalize("anthropic.messages", raw)
    assert buckets == UsageBuckets(uncached_input=10, cache_read=100, cache_write_5m=100,
                                   cache_write_1h=200, output=50, output_reasoning=20,
                                   web_search_requests=2, web_fetch_requests=1)
    assert notes == []
    assert buckets.total_input == 410


def test_unknown_ttl_residual_is_noted() -> None:
    buckets, notes = conv.normalize("anthropic.messages", usage(total=500, w5=100, w1=200))
    assert (buckets.cache_write_5m, buckets.cache_write_1h, buckets.cache_write_unknown) == \
        (100, 200, 200)
    assert notes == ["dq.ttl_split_residual"]
    # no split object at all: the whole write has an unknown TTL
    buckets, notes = conv.normalize("anthropic.messages", usage(total=400))
    assert buckets.cache_write_unknown == 400 and notes == ["dq.ttl_split_residual"]


def test_negative_residual_trusts_the_split() -> None:
    buckets, notes = conv.normalize("anthropic.messages", usage(total=250, w5=100, w1=200))
    assert (buckets.cache_write_5m, buckets.cache_write_1h, buckets.cache_write_unknown) == \
        (100, 200, 0)
    assert notes == ["dq.ttl_split_exceeds_total"]


def test_absent_total_means_the_split_is_the_total() -> None:
    buckets, notes = conv.normalize("anthropic.messages", usage(w5=7, w1=3))
    assert buckets.cache_write == 10 and notes == []


def test_missing_and_null_fields_are_zero() -> None:
    assert conv.normalize("anthropic.messages", {}) == (UsageBuckets(), [])
    raw = {"input_tokens": None, "cache_creation": None, "cache_creation_input_tokens": None,
           "output_tokens_details": None, "server_tool_use": None, "output_tokens": 3}
    assert conv.normalize("anthropic.messages", raw) == (UsageBuckets(output=3), [])


def test_thinking_above_output_is_dropped_with_a_note() -> None:
    buckets, notes = conv.normalize(
        "anthropic.messages", usage(o=5, output_tokens_details={"thinking_tokens": 9}))
    assert buckets.output_reasoning is None and notes == ["dq.sum_check_failed"]


@pytest.mark.parametrize("raw", [
    {"input_tokens": -1},
    {"input_tokens": "5"},
    {"input_tokens": 5.0},
    {"input_tokens": True},
    {"output_tokens": 2**53 + 1},
    {"cache_creation": [1, 2]},
    {"cache_creation": {"ephemeral_5m_input_tokens": -3}},
    {"output_tokens_details": {"thinking_tokens": "x"}},
    {"server_tool_use": {"web_search_requests": 1.5}},
])
def test_bad_usage_is_quarantinable(raw: dict[str, Any]) -> None:
    with pytest.raises(conv.BadUsageError) as exc:
        conv.normalize("anthropic.messages", raw)
    assert str(exc.value).startswith("bad_usage")
    assert isinstance(exc.value, UsageError)


def test_normalize_rejects_non_mappings() -> None:
    with pytest.raises(conv.BadUsageError):
        conv.normalize("anthropic.messages", [1, 2])  # type: ignore[arg-type]
    with pytest.raises(conv.BadUsageError):
        conv.normalize_anthropic_messages("x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------------


def test_builtin_conventions() -> None:
    anth = conv.get_convention("anthropic.messages")
    assert anth.enabled and anth.provider == "anthropic" and anth.inclusive_input is False
    codex = conv.get_convention("codex.rollout")
    assert codex.enabled is False
    ids = [c.convention_id for c in conv.registered_conventions()]
    assert "anthropic.messages" in ids and "codex.rollout" in ids and ids == sorted(ids)


def test_codex_rollout_raises_pricing_error() -> None:
    with pytest.raises(PricingError, match="convention unverified"):
        conv.normalize("codex.rollout", {"input_tokens": 1})
    fn = conv._REGISTRY["codex.rollout"][1]
    with pytest.raises(PricingError, match="convention unverified"):
        fn({})


@pytest.fixture
def private_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registrations made by a test go into a copy of the registry (no cross-test leakage)."""
    monkeypatch.setattr(conv, "_REGISTRY", dict(conv._REGISTRY))


def test_register_convention_rules(private_registry: None) -> None:
    c = conv.Convention("sem.test.a", "anthropic", False, True, "test")

    def fn(raw):
        return UsageBuckets(output=int(raw["n"])), ["dq.test"]

    conv.register_convention(c, fn)
    conv.register_convention(c, fn)  # identical re-registration is idempotent
    assert conv.normalize("sem.test.a", {"n": 4}) == (UsageBuckets(output=4), ["dq.test"])
    with pytest.raises(ContractViolation):
        conv.register_convention(conv.Convention("sem.test.a", "openai", True, True, ""), fn)
    with pytest.raises(ContractViolation):
        conv.register_convention("x", fn)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        conv.register_convention(conv.Convention("", "a", False, True, ""), fn)
    with pytest.raises(ContractViolation):
        conv.register_convention(conv.Convention("sem.test.b", "a", False, True, ""),
                                 3)  # type: ignore[arg-type]


def test_unknown_convention_loads_extensions_once(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path, private_registry: None) -> None:
    mod = tmp_path / "tb_sem_fake_ext.py"
    mod.write_text(textwrap.dedent("""
        from tokenbill.core.conventions import Convention, register_convention
        from tokenbill.core.records import UsageBuckets
        register_convention(Convention("sem.fake.ext", "openai", True, True, "fake"),
                            lambda raw: (UsageBuckets(uncached_input=1), []))
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry, "CONVENTION_MODULES",
                        ("tokenbill.adapters.no_such_module_sem", "tb_sem_fake_ext"))
    monkeypatch.setattr(conv, "_EXTENSIONS_LOADED", False)
    assert conv.get_convention("sem.fake.ext").provider == "openai"
    assert conv.normalize("sem.fake.ext", {}) == (UsageBuckets(uncached_input=1), [])
    with pytest.raises(UsageError, match="unknown convention"):
        conv.get_convention("sem.never.registered")
    with pytest.raises(UsageError):
        conv.get_convention(7)  # type: ignore[arg-type]
    sys.modules.pop("tb_sem_fake_ext", None)


def test_broken_extension_module_propagates(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    (tmp_path / "tb_sem_broken_ext.py").write_text("import tb_sem_missing_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry, "CONVENTION_MODULES", ("tb_sem_broken_ext",))
    monkeypatch.setattr(conv, "_EXTENSIONS_LOADED", False)
    with pytest.raises(ModuleNotFoundError):
        conv.get_convention("sem.whatever")
    sys.modules.pop("tb_sem_broken_ext", None)


def test_sum_check() -> None:
    crewai = UsageBuckets(uncached_input=17, cache_read=17_102)   # implies 17,119 input tokens
    assert conv.sum_check(crewai, 17, None) == ["dq.sum_check_failed"]
    assert conv.sum_check(crewai, 17_119, None) == []
    assert conv.sum_check(UsageBuckets(output=5), None, 6) == ["dq.sum_check_failed"]
    assert conv.sum_check(UsageBuckets(output=5), None, 5) == []
    assert conv.sum_check(UsageBuckets(), None, None) == []


# ---------------------------------------------------------------------------------------------
# anthropic_inferences: iterations
# ---------------------------------------------------------------------------------------------


def test_without_iterations_one_message_inference() -> None:
    raw = usage(i=5, total=100, w5=100, o=7)
    for variant in (raw, {**raw, "iterations": None}, {**raw, "iterations": []}):
        inferences, notes = infs(variant, usage_source=UsageSource.MESSAGE_START_ONLY)
        assert notes == []
        assert len(inferences) == 1
        inf = inferences[0]
        assert inf.kind is InferenceKind.MESSAGE and inf.pricing is CTX
        assert inf.inference_id == stable_id("inf", "rq_test", 0)
        assert inf.usage == UsageBuckets(uncached_input=5, cache_write_5m=100, output=7)
        assert inf.usage_source is UsageSource.MESSAGE_START_ONLY
        assert inf.billable is True and inf.billing_rule_id is None


def test_single_iteration_equal_to_top_level() -> None:
    top = usage(i=3, r=40, total=10, w5=10, o=9)
    raw = {**top, "iterations": [it("message", i=3, r=40, total=10, w5=10, o=9)]}
    inferences, notes = infs(raw)
    assert notes == []                 # len 1: element == top
    assert [i.kind for i in inferences] == [InferenceKind.MESSAGE]
    assert inferences[0].usage.cache_write_5m == 10
    assert inferences[0].pricing is CTX
    # the comparison ignores the TTL split; the element's own mapping is what gets priced
    raw = {**top, "iterations": [it("message", i=3, r=40, total=10, o=9)]}
    inferences, notes = infs(raw)
    assert notes == ["dq.ttl_split_residual"]
    assert inferences[0].usage.cache_write_unknown == 10


def test_single_iteration_mismatch_is_flagged() -> None:
    raw = {**usage(i=3, o=9), "iterations": [it("message", i=3, o=10)]}
    inferences, notes = infs(raw)
    assert notes == ["dq.iterations_mismatch"]
    assert inferences[0].usage.output == 10    # the iterations are kept


def fallback_raw(declined_output: int, *, top_output: int = 30) -> dict[str, Any]:
    """SPEC Appendix A.8: a declined Fable 5 attempt followed by an Opus 4.8 fallback."""
    return {
        **usage(i=1000, o=top_output),
        "iterations": [
            it("message", "claude-fable-5", i=1000, o=declined_output),
            it("fallback_message", "claude-opus-4-8", i=1000, o=30),
        ],
    }


def test_appendix_a8_refusal_pre_output_not_billable() -> None:
    inferences, notes = infs(fallback_raw(0))
    assert notes == []                 # fallback pair: last == top
    declined, fallback = inferences
    assert declined.kind is InferenceKind.FALLBACK_DECLINED
    assert declined.billable is False
    assert declined.billing_rule_id == "anthropic.refusal.pre_output"
    assert fallback.kind is InferenceKind.FALLBACK and fallback.billable is True
    assert fallback.pricing.model == "claude-opus-4-8"
    assert fallback.pricing.model_raw == "claude-opus-4-8"
    assert declined.pricing is CTX     # the declined element is on the message model
    priced = FlatRates().price_inference(declined, ts_ms=0)
    assert priced.figure.nano == 0 and priced.figure.evidence is Evidence.EXACT


def test_appendix_a8_refusal_ambiguous_is_a_range() -> None:
    inferences, _ = infs(fallback_raw(6))
    declined = inferences[0]
    assert declined.billable is None
    assert declined.billing_rule_id == "anthropic.refusal.ambiguous"
    priced = FlatRates().price_inference(declined, ts_ms=0)
    assert priced.figure.evidence is Evidence.ESTIMATED
    assert priced.figure.low_nano == 0 and priced.figure.high_nano > 0


def test_appendix_a8_refusal_mid_stream_is_billable() -> None:
    inferences, _ = infs(fallback_raw(2_127))
    declined = inferences[0]
    assert declined.billable is True
    assert declined.billing_rule_id == "anthropic.refusal.mid_stream"
    priced = FlatRates().price_inference(declined, ts_ms=0)
    assert priced.figure.evidence is Evidence.EXACT and priced.figure.nano > 0


@pytest.mark.parametrize(("output", "limit", "billable", "rule"), [
    (1, 16, None, "anthropic.refusal.ambiguous"),
    (16, 16, None, "anthropic.refusal.ambiguous"),
    (17, 16, True, "anthropic.refusal.mid_stream"),
    (6, 4, True, "anthropic.refusal.mid_stream"),
    (0, 0, False, "anthropic.refusal.pre_output"),
    (1, 0, True, "anthropic.refusal.mid_stream"),
])
def test_refusal_threshold_is_configurable(output: int, limit: int, billable: bool | None,
                                           rule: str) -> None:
    inferences, _ = infs(fallback_raw(output), refusal_ambiguous_max_output=limit)
    assert (inferences[0].billable, inferences[0].billing_rule_id) == (billable, rule)


def test_fallback_last_mismatch_is_flagged() -> None:
    inferences, notes = infs(fallback_raw(0, top_output=31))
    assert notes == ["dq.iterations_mismatch"] and len(inferences) == 2


def test_compaction_plus_message_gives_two_inferences() -> None:
    compaction = it("compaction", i=150_000, o=20_000)
    message = it("message", r=20_000, i=500, o=800)
    # top-level usage excludes the compaction iteration (§19.2): Σ message elements == top
    raw = {**usage(r=20_000, i=500, o=800), "iterations": [compaction, message]}
    inferences, notes = infs(raw)
    assert [i.kind for i in inferences] == [InferenceKind.COMPACTION, InferenceKind.MESSAGE]
    assert notes == []
    assert inferences[0].usage.uncached_input == 150_000
    assert [i.inference_id for i in inferences] == [stable_id("inf", "rq_test", 0),
                                                    stable_id("inf", "rq_test", 1)]
    # Σ of all elements == top is accepted too
    raw_sum = {**usage(r=20_000, i=150_500, o=20_800), "iterations": [compaction, message]}
    assert infs(raw_sum)[1] == []
    # neither holds → mismatch
    raw_bad = {**usage(r=1, i=1, o=1), "iterations": [compaction, message]}
    assert infs(raw_bad)[1] == ["dq.iterations_mismatch"]


def test_advisor_with_element_model() -> None:
    raw = {**usage(i=100, o=50), "iterations": [
        it("message", i=100, o=50), it("advisor_message", "claude-opus-5-5", i=900, o=40)]}
    inferences, notes = infs(raw, advisor_model="claude-sonnet-5")
    assert notes == []
    advisor = inferences[1]
    assert advisor.kind is InferenceKind.ADVISOR
    assert advisor.pricing.model == "claude-opus-5-5"     # element model wins over advisor_model


def test_advisor_with_advisor_model_only() -> None:
    raw = {**usage(i=100, o=50), "iterations": [
        it("message", i=100, o=50), it("advisor_message", i=900, o=40)]}
    inferences, notes = infs(raw, advisor_model="claude-opus-5-5-20260922")
    assert notes == []
    assert inferences[1].pricing.model == "claude-opus-5-5"
    assert inferences[1].pricing.model_raw == "claude-opus-5-5-20260922"
    same, _ = infs(raw, advisor_model="claude-fable-5")
    assert same[1].pricing is CTX   # advisor on the message model


def test_advisor_without_any_model_is_unpriced() -> None:
    raw = {**usage(i=100, o=50), "iterations": [
        it("message", i=100, o=50), it("advisor_message", i=900, o=40)]}
    inferences, notes = infs(raw)
    assert notes == ["dq.unpriced_model"]
    advisor = inferences[1]
    assert advisor.pricing.model == "" and advisor.pricing.model_raw == ""
    priced = FlatRates().price_inference(advisor, ts_ms=0)
    assert priced.figure.nano is None and priced.unpriced_reason is not None


def test_unknown_iteration_type_is_other_and_models_normalize() -> None:
    raw = {**usage(i=10, o=2), "iterations": [
        it("message", "claude-haiku-4-5-20251001", i=5, o=1),
        it("tool_turn", i=5, o=1)]}
    inferences, notes = infs(raw)
    assert [i.kind for i in inferences] == [InferenceKind.MESSAGE, InferenceKind.OTHER]
    assert inferences[0].pricing.model == "claude-haiku-4-5"
    assert inferences[0].pricing.channel == CTX.channel
    assert notes == []   # Σ elements == top


def test_top_level_web_search_goes_to_the_serving_inference() -> None:
    raw = {**usage(i=10, o=2, server_tool_use={"web_search_requests": 3}),
           "iterations": [it("compaction", i=4, o=1), it("message", i=10, o=2)]}
    inferences, _ = infs(raw)
    assert inferences[0].usage.web_search_requests == 0
    assert inferences[1].usage.web_search_requests == 3
    # an element that reports its own server tools keeps them
    raw2 = {**usage(i=10, o=2, server_tool_use={"web_search_requests": 3}),
            "iterations": [it("message", i=10, o=2,
                              server_tool_use={"web_search_requests": 1})]}
    assert infs(raw2)[0][0].usage.web_search_requests == 1


def test_iteration_notes_are_collected_and_deduplicated() -> None:
    raw = {**usage(i=1, total=20, w5=5, w1=5, o=1),
           "iterations": [it("message", i=1, total=10, w5=4, o=1),
                          it("message", i=0, total=10, w5=6, o=0)]}
    inferences, notes = infs(raw)
    assert notes == ["dq.ttl_split_residual"]
    assert sum(i.usage.cache_write_unknown for i in inferences) == 10


@pytest.mark.parametrize("raw", [
    {"iterations": "x"},
    {"iterations": [3]},
    {"iterations": [{"type": 5}]},
    {"iterations": [{"type": "message", "model": 5}]},
    {"iterations": [{"type": "message", "input_tokens": -1}]},
    "not a mapping",
])
def test_malformed_iterations(raw: Any) -> None:
    with pytest.raises(conv.BadUsageError):
        infs(raw)


def test_invalid_arguments() -> None:
    with pytest.raises(UsageError):
        infs(usage(), refusal_ambiguous_max_output=-1)
    with pytest.raises(UsageError):
        infs(usage(), usage_source="bogus")


# ---------------------------------------------------------------------------------------------
# fuzz: only TokenbillError subclasses escape; valid usage keeps its totals
# ---------------------------------------------------------------------------------------------

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 2**54) | st.floats(allow_nan=False)
    | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.sampled_from([
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation",
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens", "output_tokens",
        "output_tokens_details", "thinking_tokens", "server_tool_use", "web_search_requests",
        "iterations", "type", "model", "other"]), children, max_size=6),
    max_leaves=20,
)


@settings(max_examples=300, deadline=None)
@given(json_values)
def test_fuzz_only_tokenbill_errors_escape(raw: Any) -> None:
    try:
        inferences, notes = infs(raw)
    except TokenbillError:
        return
    assert inferences and all(n.startswith("dq.") for n in notes)


counts = st.integers(0, 10**7)


@settings(max_examples=200, deadline=None)
@given(i=counts, r=counts, w5=counts, w1=counts, extra=st.integers(-10**6, 10**6), o=counts)
def test_property_totals(i: int, r: int, w5: int, w1: int, extra: int, o: int) -> None:
    total = max(0, w5 + w1 + extra)
    buckets, notes = conv.normalize("anthropic.messages", usage(i=i, r=r, total=total, w5=w5,
                                                                  w1=w1, o=o))
    assert buckets.uncached_input == i and buckets.cache_read == r and buckets.output == o
    assert buckets.cache_write == max(total, w5 + w1)
    if total > w5 + w1:
        assert notes == ["dq.ttl_split_residual"]
    elif total < w5 + w1:
        assert notes == ["dq.ttl_split_exceeds_total"]
    else:
        assert notes == []


# ---------------------------------------------------------------------------------------------
# content canary (SPEC §8.8): nothing from content-bearing fields reaches outputs or errors
# ---------------------------------------------------------------------------------------------


def test_canary_never_reaches_outputs_or_errors() -> None:
    from tokenbill.core.builders import CANARY, assert_no_canary, plant_canary
    from tokenbill.core.records import to_json

    raw = plant_canary({
        **usage(i=5, total=10, w5=10, o=3),
        "content": [{"type": "text", "text": "hello"}],
        "iterations": [{**it("message", i=5, total=10, w5=10, o=3),
                        "summary": "private summary"}],
    })
    inferences, notes = infs(raw)
    assert_no_canary(repr(inferences), repr(notes),
                     *(repr(to_json(inf)) for inf in inferences))
    with pytest.raises(conv.BadUsageError) as exc:
        conv.normalize("anthropic.messages", {"input_tokens": f"12 {CANARY}"})
    assert_no_canary(str(exc.value), repr(exc.value.args))
    with pytest.raises(conv.BadUsageError) as exc:
        infs({"iterations": [{"type": "message", "output_tokens": [CANARY]}]})
    assert_no_canary(str(exc.value))
