"""Conformance additions for GitHub Copilot (CORE-AMENDMENTS K-5; F-KIT-C acceptance):
lane independence of ``aggregate=True`` detectors, Copilot fixes and headroom in
``assert_detector_conforms``, ``p_`` / ``CANARY_LOGIN`` checks in ``assert_adapter_conforms``, and
``pool`` lanes in the replayer fake and suite."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import (
    CANARY_LOGIN,
    lane_from_table,
    make_ai_usage_row,
    make_license,
)
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.records import Lane
from tokenbill.core.registry import _finding_id
from tokenbill.core.types import (
    AnalysisContext,
    Finding,
    Fix,
    IngestOptions,
    IngestResult,
    Policy,
    Scope,
    SourceInfo,
)

T0_S = kit._ts("2026-09-23") // 1000


def ctx(**kw) -> AnalysisContext:
    base = {"pricer": kit.FakePricer(), "rules": None, "replayer": None, "calibration": None,
            "window": (0, 2**53), "capabilities": frozenset({"aggregates", "ext:copilot"})}
    base.update(kw)
    return AnalysisContext(**base)


def copilot_lanes() -> list[Lane]:
    return [lane_from_table([(T0_S, 0, 1000, 0, 0, 10), (T0_S + 30, 1000, 100, 0, 0, 10)],
                            lane_key=f"cp-{i}", provider="github", channel="github_copilot",
                            billing_path="copilot_pool",
                            attribution={"team": "t", "principal": f"r_u{i}",
                                         "billing_path": "copilot_pool"})
            for i in range(3)]


AUTO_FIX = kit.catalog.fix_for("copilot.org-scan", "auto-adoption", "copilot")


class PoolAggregateDetector:
    """A well-behaved aggregate detector: one entity finding from ``ctx.cost_lines`` alone."""

    id = "test.copilot-aggregate"
    version = "1"
    kinds = ("pool-share",)
    requires = frozenset()
    extension = "copilot"
    aggregate = True
    families = frozenset({"copilot"})

    def __init__(self, *, fix: Fix | None = AUTO_FIX, headroom_basis: Basis = Basis.LIST_EQUIVALENT,
                 scope_dims: dict[str, str] | None = None, lane_dependent: bool = False) -> None:
        self.fix, self.headroom_basis = fix, headroom_basis
        self.scope_dims = scope_dims if scope_dims is not None else {
            "entity": "enterprise", "product": "copilot"}
        self.lane_dependent = lane_dependent

    def detect(self, lanes: Sequence[Lane], c: AnalysisContext) -> list[Finding]:
        credits = sum(line.list_amount_nano or 0 for line in c.cost_lines)
        if self.lane_dependent:
            credits += len(lanes)
        scope = Scope(dims=tuple(sorted(self.scope_dims.items())))
        return [Finding(
            finding_id=_finding_id(self.id, "pool-share", scope), detector_id=self.id,
            kind="pool-share", detector_version=self.version, category="aggregate",
            lever_class="none", audience="org", title="Pooled credits",
            summary="Copilot credits are list-equivalent AI-credit value.", scope=scope,
            n_events=len(c.cost_lines), n_lanes=0, n_users=0, first_seen_ms=0,
            cost_observed=exact(credits, Basis.LIST_EQUIVALENT),
            recoverable=estimated(credits // 10, Basis.LIST, note="pool-converted"),
            lever_ids=("copilot.default_model_auto",), fix=self.fix,
            references=("copilot-billing",),
            headroom=estimated(credits // 20, self.headroom_basis, note="headroom"))]


def aggregate_ctx() -> AnalysisContext:
    line, _ = make_ai_usage_row(principal="p_" + "a" * 20, credits="250")
    return ctx(cost_lines=(line,))


def test_aggregate_detectors_are_checked_for_lane_independence() -> None:
    good = PoolAggregateDetector()
    out = kit.assert_detector_conforms(good, copilot_lanes(), aggregate_ctx())
    assert len(out) == 1 and out[0].headroom is not None
    with pytest.raises(AssertionError, match="lane independence"):
        kit.assert_detector_conforms(PoolAggregateDetector(lane_dependent=True),
                                     copilot_lanes(), aggregate_ctx())


def test_copilot_fixes_use_the_copilot_allowlist() -> None:
    bad_key = Fix(text="Set it.", config_patch=(("promptCacheTtl", '"1h"'),),
                  target="github-copilot", doc_url=None)
    with pytest.raises(AssertionError, match="not allowlisted"):
        kit.assert_detector_conforms(PoolAggregateDetector(fix=bad_key), [], aggregate_ctx())
    claude_target = Fix(text="Set it.", config_patch=(("copilot.managed.model", '"auto"'),),
                        target="claude-code-managed-settings", doc_url=None)
    with pytest.raises(AssertionError, match="not allowlisted"):
        kit.assert_detector_conforms(PoolAggregateDetector(fix=claude_target), [],
                                     aggregate_ctx())


def test_headroom_rules() -> None:
    with pytest.raises(AssertionError, match="headroom"):
        kit.assert_detector_conforms(PoolAggregateDetector(headroom_basis=Basis.LIST), [],
                                     aggregate_ctx())
    with pytest.raises(AssertionError, match="headroom"):
        kit.assert_detector_conforms(
            PoolAggregateDetector(scope_dims={"team": "t"}), [], aggregate_ctx())
    pool_scope = PoolAggregateDetector(scope_dims={"billing_class": "pool", "team": "t"})
    assert kit.assert_detector_conforms(pool_scope, [], aggregate_ctx())


class CopilotAdapter:
    """A toy GitHub source: seats and AI-usage rows from a JSONL file."""

    name = "toy-copilot"
    capabilities = frozenset({"licenses", "copilot_billing"})

    def __init__(self, *, leak_login: bool = False, raw_principal: bool = False) -> None:
        self.leak_login, self.raw_principal = leak_login, raw_principal

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.startswith(b"{")

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        key = opts.principal_key or b""
        licenses, lines = [], []
        for row in rows:
            who = kit.pseudonym(key, "p", row["login"])
            lic = make_license(who, team="t")
            if self.raw_principal:
                object.__setattr__(lic, "principal", "r_device-7")
            licenses.append(lic)
            line, _ = make_ai_usage_row(principal=who, credits=str(row["credits"]))
            if self.leak_login:
                line = dataclasses.replace(line, description=row["login"])
            lines.append(line)
        src = SourceInfo(source_id="toy", adapter=self.name, name_hmac="h_" + "3" * 20,
                         sha256="toy", bytes=1, name_key_id=opts.name_key_id,
                         principal_key_id=opts.principal_key_id)
        result = IngestResult(source=src, requests=[], sessions=[], events=[], aggregates=[],
                              cost_lines=lines, outcomes=[], quarantined=[], notes=[],
                              stats={"records": len(rows)}, capabilities=self.capabilities)
        result.licenses = licenses
        return result


def seats_file(tmp_path: Path) -> Path:
    path = tmp_path / "seats.jsonl"
    path.write_text("\n".join(json.dumps({"login": login, "credits": n})
                              for login, n in ((CANARY_LOGIN, 12), ("octo", 3))))
    return path


def test_adapter_conformance_on_a_copilot_source(tmp_path: Path) -> None:
    path = seats_file(tmp_path)
    res = kit.assert_adapter_conforms(CopilotAdapter(), path,
                                      expect_capabilities={"licenses", "copilot_billing"})
    assert len(res.licenses) == 2 and all(x.principal.startswith("p_") for x in res.licenses)
    with pytest.raises(AssertionError, match="canary login"):
        kit.assert_adapter_conforms(CopilotAdapter(leak_login=True), path,
                                    expect_capabilities={"licenses", "copilot_billing"})
    with pytest.raises(AssertionError, match="p_ pseudonym"):
        kit.assert_adapter_conforms(CopilotAdapter(raw_principal=True), path,
                                    expect_capabilities={"licenses", "copilot_billing"})


def test_fake_replayer_accepts_pool_lanes() -> None:
    pricer = kit.FakePricer()
    pool = copilot_lanes()
    policy = Policy(name="p1", fast_off=True)
    replayer = kit.FakeReplayer({(pool[0].lane_key, kit.FakeReplayer.policy_key(policy)): 5})
    res = replayer.replay(pool, policy, mode="documented", pricer=pricer, rules=None,
                          calibration=None)
    assert res.baseline.basis is Basis.LIST_EQUIVALENT and res.saving.nano == 5
    assert res.saving.basis is Basis.LIST_EQUIVALENT
    billed = lane_from_table([(T0_S, 0, 1000, 0, 0, 10)], lane_key="billed")
    with pytest.raises(UsageError, match="pool"):
        replayer.replay([*pool, billed], policy, mode="documented", pricer=pricer, rules=None,
                        calibration=None)
    with pytest.raises(UsageError):
        replayer.replay([pool[0], kit.lane_from_table_allowance()], policy, mode="documented",
                        pricer=pricer, rules=None, calibration=None)


def test_replayer_conformance_pool_option() -> None:
    summary = kit.assert_replayer_conforms(kit.FakeReplayer(), kit.FakePricer(), pool=True)
    assert summary["pool_baseline_nano"] > 0

    class NoPool(kit.FakeReplayer):
        def replay(self, lanes, policy, **kw):  # type: ignore[override]
            if any(lane.billing_class == "pool" for lane in lanes):
                raise UsageError("pool not supported")
            return super().replay(lanes, policy, **kw)

    kit.assert_replayer_conforms(NoPool(), kit.FakePricer())  # default: pool not checked
    with pytest.raises(UsageError):
        kit.assert_replayer_conforms(NoPool(), kit.FakePricer(), pool=True)

    class MixesPool(kit.FakeReplayer):
        def replay(self, lanes, policy, **kw):  # type: ignore[override]
            kinds = {lane.billing_class for lane in lanes}
            if kinds == {"billed", "pool"}:
                lanes = [lane for lane in lanes if lane.billing_class == "billed"]
            return super().replay(lanes, policy, **kw)

    with pytest.raises(AssertionError, match="pool lanes mixed"):
        kit.assert_replayer_conforms(MixesPool(), kit.FakePricer(), pool=True)


def test_pool_lane_builder() -> None:
    lane = kit.lane_from_table_pool()
    assert lane.billing_class == "pool" and len(lane.requests) == 2
    assert lane.requests[0].serving_inference.pricing.channel == "github_copilot"
