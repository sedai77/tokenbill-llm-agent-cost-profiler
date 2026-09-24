"""Edge branches of the kit: stubbed F-SEM hooks, fallbacks, and store/pricer corner cases."""

from __future__ import annotations

import dataclasses
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core import kanon, keys
from tokenbill.core import testing as kit
from tokenbill.core.builders import (
    assert_no_canary,
    make_attempt,
    make_block,
    make_fingerprint,
    make_inference,
    make_request,
)
from tokenbill.core.errors import ContractViolation, PricingError, PrivacyError, UsageError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.labels import Basis
from tokenbill.core.records import CacheDiagnostic, UsageBuckets, to_json
from tokenbill.core.types import AggRow, PricedTotal, RawAggregate, ResolvedRates

from . import fsem_stubs
from .test_conformance import EXPECT, JsonlAdapter, fixture

W = {"since_ms": 0, "until_ms": 2**53}
T0 = kit._ts("2026-09-23")


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


# ---------- the smoke pipeline's plumbing, with F-SEM stand-ins ----------


def test_smoke_pipeline_plumbing_with_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    fsem_stubs.install(monkeypatch)
    from tokenbill.core import registry

    plugins = dict(registry._PLUGIN_DETECTORS)
    run = kit.smoke_pipeline_on_fakes()
    assert registry._PLUGIN_DETECTORS == plugins  # the test detector was unregistered
    assert run.command == "smoke" and run.bill is not None and run.action_plan is not None
    (finding,) = run.findings
    assert finding.detector_id == "test.smoke-ttl" and finding.n_users == 6
    assert dict(finding.scope.dims) == {"lane_kind": "main", "team": "payments"}
    assert finding.cost_observed.nano > 0 and finding.recoverable.nano > 0
    plan = run.action_plan
    assert sum(lv.shapley.nano for lv in plan.levers) == plan.joint_saving.nano
    assert [lv.lever_id for lv in plan.levers] == ["cc.fast_mode_opt_in",
                                                   "cc.prompt_cache_ttl.main"]
    assert run.bill.breakdowns[0][0] == "team"
    assert all(r.n_users >= 5 for r in run.bill.breakdowns[0][1].rows)
    assert run.bill.total.exact.nano > 0 and run.bill.total.estimated is None
    encoded = to_json(run)
    assert not any(isinstance(v, float) for v in _walk(encoded))
    assert_no_canary(json.dumps(encoded))
    assert to_json(kit.smoke_pipeline_on_fakes()) == encoded


def test_stub_detector_conforms(monkeypatch: pytest.MonkeyPatch) -> None:
    fsem_stubs.install(monkeypatch)
    result = kit._smoke_result()
    store = kit.MemoryStore(org_key=kit.SMOKE_ORG_KEY, pricer=kit.FakePricer())
    store.ingest(result)
    lanes = list(store.iter_lanes(**W))
    replayer = kit.FakeReplayer.from_function(kit._smoke_saving(kit.FakePricer()))
    ctx = kit.AnalysisContext(pricer=kit.FakePricer(), rules=None, replayer=replayer,
                              calibration=None, window=(0, 2**53),
                              capabilities=result.capabilities)
    found = kit.assert_detector_conforms(kit.SmokeTtlDetector(), lanes, ctx)
    assert len(found) == 1
    no_replay = dataclasses.replace(ctx, replayer=None)
    (plain,) = kit.SmokeTtlDetector().detect(lanes, no_replay)
    assert plain.recoverable is None and plain.fix is None


def test_replayer_conformance_uses_the_rules_table(monkeypatch: pytest.MonkeyPatch) -> None:
    fsem_stubs.install(monkeypatch)
    seen = []

    def fn(lane, policy):
        seen.append(type(policy).__name__)
        return 0

    kit.assert_replayer_conforms(kit.FakeReplayer.from_function(fn), kit.FakePricer())
    assert seen


def test_sum_check_with_a_conventions_stub(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    class WithRaw(JsonlAdapter):
        def __init__(self, raw: dict, convention: str = "anthropic.messages", **kw) -> None:
            super().__init__(**kw)
            self.raw, self.convention = raw, convention

        def read(self, path, opts):
            res = super().read(path, opts)
            reqs = []
            for r in res.requests:
                usage = r.serving_inference.usage
                raw = {"input_tokens": usage.uncached_input, "output_tokens": usage.output,
                       **self.raw}
                att = dataclasses.replace(r.attempts[0], raw_usage_json=json.dumps(raw),
                                          convention_id=self.convention)
                reqs.append(dataclasses.replace(r, attempts=(att,)))
            res.requests = reqs
            return res

    path = fixture(tmp_path)
    monkeypatch.setitem(sys.modules, "tokenbill.core.conventions", fsem_stubs.conventions())
    kit.assert_adapter_conforms(WithRaw({}), path, expect_capabilities=EXPECT)
    for skipped in (WithRaw({"iterations": [1]}), WithRaw({}, convention="unknown")):
        kit.assert_adapter_conforms(skipped, path, expect_capabilities=EXPECT)
    with pytest.raises(AssertionError, match="sum check"):
        kit.assert_adapter_conforms(WithRaw({"input_tokens": 999_999}), path,
                                    expect_capabilities=EXPECT)
    with pytest.raises(AssertionError, match="long strings"):
        kit.assert_adapter_conforms(WithRaw({"model": "m" * 70}), path,
                                    expect_capabilities=EXPECT)
    monkeypatch.setitem(sys.modules, "tokenbill.core.conventions",
                        fsem_stubs.conventions(enabled=False))
    kit.assert_adapter_conforms(WithRaw({"input_tokens": 999_999}), path,
                                expect_capabilities=EXPECT)


# ---------- FakePricer internals ----------


def _resolved(**kw) -> ResolvedRates:
    base = dict(row_id="r", channel="c", model="m", input=Decimal("2"), output=Decimal("9"),
                cache_read=None, cache_write_5m=None, cache_write_1h=None, cache_write_other=None,
                per_request=(), modifier_ids=(), stacking_assumed=False, layer="builtin",
                min_cacheable_tokens=None, tokenizer_family="x", long_context_band=False)
    base.update(kw)
    return ResolvedRates(**base)


def test_effective_rate_fallbacks() -> None:
    bare = _resolved()
    assert kit._effective_rate(bare, "output") == Decimal("9")
    assert kit._effective_rate(bare, "cache_read") == Decimal("2")
    assert kit._effective_rate(bare, "cache_write_1h") == Decimal("2")
    assert kit._effective_rate(bare, "cache_write_other") == Decimal("2")
    assert kit._effective_rate(_resolved(cache_write_5m=Decimal("3")),
                               "cache_write_other") == Decimal("3")
    with pytest.raises(ContractViolation):
        kit._effective_rate(bare, "tokens")


class _Facts:
    """A facts stand-in with custom rows and modifiers."""

    def __init__(self, rows, modifiers) -> None:
        self.rate_rows, self.modifiers = tuple(rows), tuple(modifiers)

    def rate_rows_json(self):
        return [r.row_id for r in self.rate_rows]

    def modifiers_json(self):
        return [m.modifier_id for m in self.modifiers]


def test_unknown_predicates_and_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = load_facts()
    row = facts.rate_row("anthropic/anthropic_api/claude-opus-5-5/2026-09-22")
    bad_mod = dataclasses.replace(facts.modifiers[0], when=(("weather", "sunny"),))
    monkeypatch.setattr(kit, "load_facts", lambda: _Facts([row], [bad_mod]))
    with pytest.raises(PricingError):
        kit.FakePricer()
    closed = dataclasses.replace(row, effective_to="2026-09-25")
    monkeypatch.setattr(kit, "load_facts", lambda: _Facts([closed], []))
    fp = kit.FakePricer()
    p = fp.price_usage(UsageBuckets(output=1), kit._ctx("claude-opus-5-5"),
                       ts_ms=kit._ts("2026-09-30"))
    assert p.unpriced_reason == "no rate row"
    assert fp.supports(kit._ctx("claude-opus-5-5"), "batch", ts_ms=kit._ts("2026-09-30")) is False


def test_row_matches_and_unit_rate_skips() -> None:
    assert kit._row_matches(kit.FakePricer(), "gpt-5.6-sol", "openai_api", "2026-08-01") is False

    class NoBedrock(kit.FakePricer):
        def resolve(self, ctx, *, ts_ms):
            return None if ctx.channel == "bedrock" else super().resolve(ctx, ts_ms=ts_ms)

    summary = kit.assert_pricer_conforms(NoBedrock(), samples=40)
    assert "7" not in summary["golden_cases"]
    with pytest.raises(AssertionError, match="not enough"):
        kit._unit_rate_agreement(type("Never", (kit.FakePricer,), {
            "resolve": lambda self, ctx, *, ts_ms: None})(), 1, 0)


def test_source_bytes_of_a_broken_gzip(tmp_path: Path) -> None:
    bad = tmp_path / "x.jsonl.gz"
    bad.write_bytes(b"not gzip at all")
    assert kit._source_bytes(bad) == b"not gzip at all"


# ---------- MemoryStore corners ----------


def _store() -> kit.MemoryStore:
    return kit.MemoryStore(org_key=kit.STORE_ORG_KEY, name_key_id=kit.key_id(kit.STORE_NAME_KEY),
                           pricer=kit.FakePricer())


def test_store_input_validation_and_winner_diagnostics() -> None:
    s = _store()
    with pytest.raises(UsageError):
        s.ingest({"requests": []})  # type: ignore[arg-type]
    diag = CacheDiagnostic(reason="unavailable", provider_reason="unavailable",
                           missed_input_tokens_estimate=None, source="anthropic.cache_diagnostics")
    fp = make_fingerprint([make_block("h1")])
    win = make_request("L", 0, T0, {"output": 9}, message_id="m", diagnostics=diag,
                       request_id="rq_w", source=kit._ref("claude-code", "s", kit.Fidelity.FULL,
                                                          40, 0))
    lose = make_request("L", 0, T0, {"output": 9}, message_id="m", fingerprint=fp,
                        request_id="rq_w", source=kit._ref("trace@2", "s2", kit.Fidelity.FULL,
                                                           20, 0))
    s.ingest(kit._result(kit._src("s1", "claude-code"), [win]))
    s.ingest(kit._result(kit._src("s2", "trace@2"), [lose]))
    (m,) = list(s.iter_requests(**W))
    assert m.final_attempt.diagnostics == diag and m.fingerprint == fp


def test_attempt_windows_and_non_billable_inferences() -> None:
    first = make_attempt([make_inference(UsageBuckets(output=5), inference_id="i1")],
                         ts_ms=T0, attempt_no=0, attempt_id="a1")
    second = make_attempt([make_inference(UsageBuckets(output=7), inference_id="i2")],
                          ts_ms=T0 + 10_000, attempt_no=1, attempt_id="a2")
    retried = make_request("L1", 0, T0, attempts=[first, second], request_id="rq_retry",
                           attribution={"team": "t", "principal": "p_" + "1" * 20})
    declined = make_request("L2", 0, T0, {"output": 3}, billable=False, request_id="rq_no",
                            attribution={"team": "u", "principal": "p_" + "2" * 20})
    s = _store()
    s.ingest(kit._result(kit._src("s1", "claude-code"), [retried, declined]))
    recs = list(s.iter_usage_records(since_ms=T0, until_ms=T0 + 5))
    assert [r.inference_id for r in recs] == ["i1"]
    assert s.cluster_days(cluster_kind="team", since="2026-09-23", until="2026-09-24")[0] \
        .cluster_id == "t"
    by_model = s.aggregate(group_by=["team"], where={"model": "claude-opus-5-5"}, **W)
    assert [dict(r.dims)["team"] for r in by_model.rows] == ["t"]
    none = s.aggregate(group_by=["team"], where={"channel": "bedrock"}, **W)
    assert none.rows == ()
    rep = kit.FakeReplayer({})
    res = rep.replay(list(s.iter_lanes(**W)), kit.Policy.observed(), mode="documented",
                     pricer=kit.FakePricer(), rules=None, calibration=None)
    assert res.baseline.nano == 5 * 20_000 + 7 * 20_000


class _LenientStore(kit.MemoryStore):
    def _pseudonymize(self, principal, principals_ok, counts):
        if principal and principal.startswith("r_") and not self._org_key:
            return None
        return super()._pseudonymize(principal, principals_ok, counts)


class _CostRowsWithPeople(kit.MemoryStore):
    def cost_rows(self, *, since_ms, until_ms, group_by):
        return super().cost_rows(since_ms=since_ms, until_ms=until_ms,
                                 group_by=[g for g in group_by if g != "principal"])


@pytest.mark.parametrize("cls, message", [
    (_LenientStore, "without an org key"),
    (_CostRowsWithPeople, "cost_rows"),
])
def test_store_conformance_privacy_negatives(cls, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_store_conforms(lambda **kw: cls(**kw), permutations=2)


def test_misc_edges() -> None:
    zero = AggRow(dims=(("team", "a"),), n_users=1, n_requests=0, usage=UsageBuckets(),
                  priced=PricedTotal(exact=kit.zero(Basis.LIST), estimated=None, allowance=None,
                                     priced_inferences=0, unpriced_inferences=0,
                                     unpriced_tokens=0, coverage="1"))
    other = dataclasses.replace(zero, dims=(("team", "b"),), n_users=9)
    (merged,) = kanon.publish(RawAggregate(group_by=("team",), rows=(zero, other),
                                           window=(0, 1))).rows
    assert merged.priced.coverage == "1"
    assert keys._default_runner([sys.executable, "-c", "raise SystemExit(3)"]) == 3
    with pytest.raises(PrivacyError):
        kanon.require_self_or_aggregate(["principal"], "")
