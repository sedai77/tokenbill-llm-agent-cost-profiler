"""SPEC §3.5 types: publish token, Policy delegation, report helpers, unit rates, protocols."""

from __future__ import annotations

import dataclasses
import sys
import types as pytypes

import pytest

from tokenbill.core import types as t
from tokenbill.core.builders import FlatRates
from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Calibration
from tokenbill.core.protocols import Adapter, Detector, LedgerStore, Pricer, Replayer
from tokenbill.core.records import UsageBuckets


def test_published_aggregate_requires_the_token() -> None:
    with pytest.raises(ContractViolation):
        t.PublishedAggregate(("team",), (), (0, 1), 5, 0, 0, token=object())
    with pytest.raises(ContractViolation):
        t.PublishedAggregate(("team",), (), (0, 1), 5, 0, 0, token=None)
    pa = t.PublishedAggregate(("team",), (), (0, 1), 5, 0, 0, token=t._PUBLISH_TOKEN)
    assert pa.k == 5
    assert "token" not in repr(pa)
    assert repr(t._PUBLISH_TOKEN) == "<publish token>"
    with pytest.raises(ContractViolation):
        dataclasses.replace(pa, token=object())


def test_published_aggregate_encodes_without_token_and_never_decodes() -> None:
    """RunResult/BillSummary with published breakdowns must encode (result@2, fixture dumps); the
    token is a construction guard, not data, and decoding cannot forge a PublishedAggregate."""
    from tokenbill.core.labels import Basis, zero
    from tokenbill.core.records import from_json, to_json

    total = t.PricedTotal(zero(Basis.LIST), None, None, 0, 0, 0, "1")
    row = t.AggRow((("team", "a"),), 5, 1, UsageBuckets(output=1), total)
    pa = t.PublishedAggregate(("team",), (row,), (0, 1), 5, 0, 0, token=t._PUBLISH_TOKEN)
    doc = to_json(pa)
    assert "token" not in doc and doc["k"] == 5 and doc["rows"][0]["n_users"] == 5
    with pytest.raises(ContractViolation):
        from_json(t.PublishedAggregate, doc)
    with pytest.raises(ContractViolation):
        from_json(t.PublishedAggregate, {**doc, "token": None})
    rr = t.RunResult(
        command="bill",
        window=(0, 1),
        inputs=(),
        privacy=t.PrivacyInfo("none", None, "install", 5, 0),
        rate_card=None,
        bill=t.BillSummary(total=total, esr=None, breakdowns=(("team", pa),)),
    )
    enc = to_json(rr)
    assert enc["bill"]["breakdowns"][0][1]["group_by"] == ["team"]


def test_ingest_options_repr_hides_key_material() -> None:
    opts = t.IngestOptions(name_key=b"name-key-SECRET", principal_key=b"principal-SECRET")
    assert "SECRET" not in repr(opts)
    assert opts.name_key == b"name-key-SECRET" and opts.principal_key == b"principal-SECRET"
    assert t.IngestOptions().name_key == b"" and t.IngestOptions().principal_key is None


def test_policy_observed() -> None:
    obs = t.Policy.observed()
    assert obs == t.Policy(name="observed") and obs.is_observed()
    assert t.Policy(name="anything").is_observed()
    assert not t.Policy(name="x", ttl=(("all", "1h"),)).is_observed()
    assert not t.Policy(name="x", fast_off=True).is_observed()


def test_policy_delegates_lazily_to_core_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    fake = pytypes.ModuleType("tokenbill.core.policy")

    def to_spec(p: t.Policy) -> str:
        calls.append("to_spec")
        return f"spec:{p.name}"

    def combine(a: t.Policy, b: t.Policy) -> t.Policy:
        calls.append("combine")
        return t.Policy(name=a.name + "+" + b.name)

    fake.to_spec = to_spec  # type: ignore[attr-defined]
    fake.combine = combine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenbill.core.policy", fake)
    import tokenbill.core as core_pkg

    monkeypatch.setattr(core_pkg, "policy", fake, raising=False)
    a, b = t.Policy(name="a"), t.Policy(name="b")
    assert a.spec() == "spec:a"
    assert a.combine(b).name == "a+b"
    assert calls == ["to_spec", "combine"]
    assert a.is_observed() and t.Policy.observed().name == "observed"  # local fallback
    fake.is_observed = lambda p: calls.append("is_observed") or False  # type: ignore[attr-defined]
    fake.observed = lambda: t.Policy(name="from-policy")  # type: ignore[attr-defined]
    assert not a.is_observed() and t.Policy.observed().name == "from-policy"
    assert calls[-1] == "is_observed"


def test_calibration_report_calibration() -> None:
    base = dict(
        granularity="month",
        n_periods=12,
        mode_used="documented",
        nmbe_pct="1",
        cvrmse_pct="2",
        nmbe_pct_calibrated=None,
        cvrmse_pct_calibrated=None,
        thresholds=("5", "15"),
        rho=(),
        diag_confusion=(),
        diag_precision_recall=(),
        unlabeled=0,
        no_comparison_labels=0,
        ttl_corroboration=(0, 0),
        notes=(),
    )
    assert t.CalibrationReport(status="pass", **base).calibration() is Calibration.CALIBRATED
    for status in ("fail", "insufficient_data"):
        assert t.CalibrationReport(status=status, **base).calibration() is Calibration.UNCALIBRATED


def test_unit_rates() -> None:
    u = t.UnitRates(8, 100, 10, 125, 200, 125, 500, 10_000_000, "flat")
    assert u.bucket_nano("uncached_input", 1_000_000) == 1_000_000_000
    assert u.bucket_nano("uncached", 1) == 1000
    assert u.bucket_nano("cache_read", 1_000_000) == 100_000_000
    assert u.bucket_nano("cache_write_5m", 1_000_000) == 1_250_000_000
    assert u.bucket_nano("cache_write_unknown", 1_000_000) == 1_250_000_000
    assert u.bucket_nano("cache_write_1h", 3) == 6000
    assert u.bucket_nano("cache_write_other", 2) == 2500
    assert u.bucket_nano("output", 1) == 5000
    fine = t.UnitRates(11, 1, 1, 1, 1, 1, 15, 0, "x")  # 15e-11 USD/token = 0.15 nano
    assert fine.bucket_nano("output", 10) == 2  # 1.5 nano → 2 (half-even)
    assert fine.bucket_nano("output", 30) == 4  # 4.5 → 4
    assert u.bucket_nano("web_search", 3) == 30_000_000
    with pytest.raises(ContractViolation):
        u.bucket_nano("bogus", 1)
    with pytest.raises(ContractViolation):
        t.UnitRates(25, 1, 1, 1, 1, 1, 1, 1, "x")
    with pytest.raises(ContractViolation):
        t.UnitRates(8, 1.0, 1, 1, 1, 1, 1, 1, "x")  # type: ignore[arg-type]


def test_run_result_defaults_and_ingest_result() -> None:
    rr = t.RunResult(
        command="bill",
        window=(0, 1),
        inputs=(),
        privacy=t.PrivacyInfo("none", None, "install", 5, 0),
        rate_card=None,
    )
    assert rr.findings == () and rr.bill is None and rr.synthetic is False
    for name in (
        "bill",
        "data_quality",
        "reconciliation",
        "calibration",
        "findings",
        "action_plan",
        "policy_packs",
        "replays",
        "measure_plan",
        "measurements",
        "ab",
        "check",
        "pricing",
        "receipts",
        "synthetic",
        "notes",
    ):
        assert hasattr(rr, name)
    res = t.IngestResult(
        t.SourceInfo("s", "a", "h", "0" * 64, 0, None, None),
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        {},
        frozenset(),
    )
    assert res.naive_usage == {}
    res.naive_usage["m"] = UsageBuckets(output=1)
    opts = t.IngestOptions()
    assert opts.identity_mode == "install" and opts.k_anonymity == 5 and not opts.renormalize


def test_protocols_runtime_checkable() -> None:
    assert isinstance(FlatRates(), Pricer)
    assert not isinstance(object(), Pricer)

    class A:
        name = "x"
        capabilities = frozenset()

        def sniff(self, path, head):  # noqa: ANN001, ANN201
            return False

        def read(self, path, opts):  # noqa: ANN001, ANN201
            raise NotImplementedError

    assert isinstance(A(), Adapter)
    for proto in (Detector, LedgerStore, Replayer):
        assert not isinstance(A(), proto)


def test_contract_field_names() -> None:
    """Spot-check field names other packages build against (SPEC §3.5)."""

    def names(cls: type) -> list[str]:
        return [f.name for f in dataclasses.fields(cls)]

    assert names(t.PricedInference) == [
        "inference_id",
        "lines",
        "figure",
        "exact_nano",
        "estimated",
        "unpriced_reason",
    ]
    assert names(t.PricedTotal) == [
        "exact",
        "estimated",
        "allowance",
        "priced_inferences",
        "unpriced_inferences",
        "unpriced_tokens",
        "coverage",
        "pool",
    ]
    assert names(t.ReplayResult)[:5] == ["policy", "mode", "baseline", "cost", "saving"]
    assert names(t.ShardKey) == ["team", "lane_kind"]
    assert names(t.LaneIndexRow) == [
        "lane_key",
        "team",
        "lane_kind",
        "billing_class",
        "requests",
        "point_nano",
    ]
    assert names(t.IngestOptions)[:7] == [
        "content_tier",
        "identity_mode",
        "name_key",
        "name_key_id",
        "principal_key",
        "principal_key_id",
        "principal_ref",
    ]
    assert "renormalize" in names(t.IngestOptions) and "team_map" in names(t.IngestOptions)
    assert names(t.ChannelVerdict) == ["channel", "verdict", "invoice_sources", "mapping_verified"]
    assert names(t.PublishedAggregate)[-1] == "token"
    assert names(t.CalibrationPartial)[0] == "granularity"
    assert "allowance_headroom_monthly" in names(t.ActionPlan)
    assert len(names(t.RunResult)) == 22
