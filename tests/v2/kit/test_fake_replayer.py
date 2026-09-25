"""FakeReplayer: savings of exactly the given lanes; replayer conformance (F-KIT acceptance)."""

from __future__ import annotations

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import FlatRates
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence
from tokenbill.core.protocols import Replayer
from tokenbill.core.types import CalibrationReport, Policy

TTL = Policy(name="ttl-1h", ttl=(("lane_kind:main", "1h"),))
KW = {"mode": "documented", "rules": None, "calibration": None}


def lanes():
    return kit._replayer_lanes()


def table():
    key = kit.FakeReplayer.policy_key(TTL)
    return {("R-main", key): 1_000_000, ("R-bursty", key): -300_000, ("R-sub", key): 50_000}


def test_policy_key_is_the_spec_or_the_name() -> None:
    key = kit.FakeReplayer.policy_key(TTL)
    try:
        spec = TTL.spec()
    except ImportError:  # F-SEM not merged yet
        spec = TTL.name
    assert key == spec


def test_sums_only_the_lanes_it_is_given() -> None:
    fp = kit.FakePricer()
    rep = kit.FakeReplayer(table())
    all_lanes = lanes()
    whole = rep.replay(all_lanes, TTL, pricer=fp, **KW)
    assert whole.saving.nano == 750_000
    parts = [rep.replay([lane], TTL, pricer=fp, **KW) for lane in all_lanes]
    assert sum(p.saving.nano for p in parts) == whole.saving.nano
    assert sum(p.baseline.nano for p in parts) == whole.baseline.nano
    cohort = rep.replay([all_lanes[0], all_lanes[1]], TTL, pricer=fp, **KW)
    assert cohort.saving.nano == 700_000
    assert whole.per_lane == tuple(sorted(
        (p.per_lane[0] for p in parts), key=lambda kv: kv[0]))
    assert (whole.n_lanes, whole.n_requests) == (3, 9)
    assert whole.saving.evidence is Evidence.ESTIMATED
    assert whole.calibration is Calibration.UNCALIBRATED and whole.mode == "documented"
    assert whole.cost.nano == whole.baseline.nano - whole.saving.nano
    assert rep.replay([], TTL, pricer=fp, **KW).saving.nano == 0


def test_baseline_is_priced_with_the_given_pricer() -> None:
    fp, flat = kit.FakePricer(), FlatRates()
    rep = kit.FakeReplayer({})
    a = rep.replay(lanes(), Policy.observed(), pricer=fp, **KW)
    b = rep.replay(lanes(), Policy.observed(), pricer=flat, **KW)
    assert a.baseline.nano != b.baseline.nano
    assert a.cost == a.baseline and a.saving.nano == 0


def test_from_function_and_outcomes() -> None:
    seen = []

    def fn(lane, policy):
        seen.append((lane.lane_key, policy.name))
        return 10 if lane.kind.value == "main" else 0

    rep = kit.FakeReplayer.from_function(fn)
    res = rep.replay(lanes(), TTL, pricer=kit.FakePricer(), keep_outcomes=True, **KW)
    assert res.saving.nano == 20 and sorted(seen) == [("R-bursty", "ttl-1h"),
                                                      ("R-main", "ttl-1h"),
                                                      ("R-sub", "ttl-1h")]
    changed = [o for o in res.outcomes if o.changed]
    assert len(changed) == 2  # the last request of each main lane carries its saving
    bad = kit.FakeReplayer.from_function(lambda lane, policy: 1.5)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        bad.replay(lanes(), TTL, pricer=kit.FakePricer(), **KW)
    with pytest.raises(UsageError):
        kit.FakeReplayer({("lane", "spec"): "10"})  # type: ignore[dict-item]


def test_modes_and_billing_classes() -> None:
    fp = kit.FakePricer()
    rep = kit.FakeReplayer(table())
    report = CalibrationReport(granularity="day", n_periods=30, status="pass",
                               mode_used="documented", nmbe_pct="1", cvrmse_pct="5",
                               nmbe_pct_calibrated=None, cvrmse_pct_calibrated=None,
                               thresholds=("10", "30"), rho=(), diag_confusion=(),
                               diag_precision_recall=(), unlabeled=0, no_comparison_labels=0,
                               ttl_corroboration=(0, 0), notes=())
    cal = rep.replay(lanes(), TTL, pricer=fp, mode="calibrated", rules=None, calibration=report)
    assert cal.calibration is Calibration.CALIBRATED and cal.mode == "calibrated"
    failed = rep.replay(lanes(), TTL, pricer=fp, mode="calibrated", rules=None,
                        calibration=None)
    assert failed.calibration is Calibration.UNCALIBRATED and failed.mode == "documented"
    with pytest.raises(UsageError):
        rep.replay(lanes(), TTL, pricer=fp, mode="clairvoyant", rules=None, calibration=None)
    allowance = kit.lane_from_table_allowance()
    only_allowance = rep.replay([allowance], TTL, pricer=fp, **KW)
    assert only_allowance.baseline.basis is Basis.LIST_EQUIVALENT
    with pytest.raises(UsageError):
        rep.replay([lanes()[0], allowance], TTL, pricer=fp, **KW)


def test_fake_replayer_conforms() -> None:
    rep = kit.FakeReplayer.from_function(lambda lane, policy: 5 if lane.kind.value == "main"
                                         else 0)
    assert isinstance(rep, Replayer)
    summary = kit.assert_replayer_conforms(rep, kit.FakePricer())
    assert summary["ttl_saving_nano"] == 10
    kit.assert_replayer_conforms(kit.FakeReplayer({}), FlatRates())


class _Broken:
    """A replayer whose observed cost is not the baseline."""

    def replay(self, lanes, policy, *, mode, pricer, rules, calibration, static_prefix_floor=None,
               keep_outcomes=False):
        res = kit.FakeReplayer({}).replay(lanes, policy, mode=mode, pricer=pricer, rules=rules,
                                          calibration=calibration, keep_outcomes=keep_outcomes)
        from dataclasses import replace

        from tokenbill.core.labels import exact

        return replace(res, cost=exact((res.cost.nano or 0) + 1, res.cost.basis))


class _LeakyTtl:
    """A replayer that changes lanes outside the policy's selector."""

    def replay(self, lanes, policy, *, mode, pricer, rules, calibration, static_prefix_floor=None,
               keep_outcomes=False):
        inner = kit.FakeReplayer.from_function(lambda lane, p: 7)
        return inner.replay(lanes, policy, mode=mode, pricer=pricer, rules=rules,
                            calibration=calibration, keep_outcomes=keep_outcomes)


class _Mixed:
    """A replayer that silently accepts mixed billing classes."""

    def replay(self, lanes, policy, *, mode, pricer, rules, calibration, static_prefix_floor=None,
               keep_outcomes=False):
        lanes = [lane for lane in lanes if lane.billing_class == "billed"]
        return kit.FakeReplayer({}).replay(lanes, policy, mode=mode, pricer=pricer, rules=rules,
                                           calibration=calibration, keep_outcomes=keep_outcomes)


@pytest.mark.parametrize("replayer, message", [
    (_Broken(), "cost"),
    (_LeakyTtl(), "outside the policy"),
    (_Mixed(), "mixed billing"),
])
def test_replayer_conformance_catches(replayer, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_replayer_conforms(replayer, kit.FakePricer())
