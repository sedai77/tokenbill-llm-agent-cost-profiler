"""The conformance suites catch what they promise to catch (SPEC §3.18; F-KIT acceptance)."""

from __future__ import annotations

import dataclasses
import gzip
import itertools
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import CANARY, lane_from_table, make_cost_line, make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.records import Lane
from tokenbill.core.registry import _finding_id
from tokenbill.core.types import (
    AnalysisContext,
    Finding,
    Fix,
    IngestOptions,
    IngestResult,
    Scope,
    SourceInfo,
)

T0_S = kit._ts("2026-09-23") // 1000


def lanes() -> list[Lane]:
    out = []
    specs = [("payments", "main", 3), ("payments", "subagent", 2), ("mobile", "main", 2),
             (None, "main", 1)]
    for n, (team, kind, count) in enumerate(specs):
        for i in range(count):
            attr = {"team": team, "principal": f"r_u{n}{i}"}
            out.append(lane_from_table([(T0_S + 60 * i, 0, 1000, 0, 0, 10),
                                        (T0_S + 60 * i + 400, 0, 1200, 0, 0, 10)],
                                       lane_key=f"lane-{n}-{i}", kind=kind, attribution=attr))
    return out


def ctx(**kw) -> AnalysisContext:
    base = {"pricer": kit.FakePricer(), "rules": None, "replayer": None, "calibration": None,
            "window": (0, 2**53), "capabilities": frozenset({"usage_sequence", "timing"})}
    base.update(kw)
    return AnalysisContext(**base)


class CohortDetector:
    """A well-behaved detector: one finding per (team, lane_kind, billing_class) cohort."""

    id = "test.cohort"
    version = "1"
    kinds = ("k1", "k2")
    requires = frozenset({"usage_sequence"})

    def finding(self, team, kind_value, members: Sequence[Lane], c: AnalysisContext,
                **over) -> Finding:
        dims = {"lane_kind": kind_value}
        if team is not None:
            dims["team"] = team
        scope = Scope(dims=tuple(sorted(dims.items())))
        cost = sum(c.pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure.nano or 0
                   for lane in members for r in lane.requests for att in r.attempts
                   for inf in att.inferences)
        fields = {
            "finding_id": _finding_id(self.id, "k1", scope), "detector_id": self.id,
            "kind": "k1", "detector_version": self.version, "category": "attribution",
            "lever_class": "none", "audience": "org", "title": "Cohort spend",
            "summary": "No mechanical fix: attribution only.", "scope": scope,
            "n_events": len(members), "n_lanes": len(members),
            "n_users": len({r.attribution.principal for lane in members for r in lane.requests}),
            "first_seen_ms": min(lane.requests[0].ts_start_ms for lane in members),
            "cost_observed": exact(cost, Basis.LIST), "recoverable": None,
            "references": ("test-ref",),
        }
        fields.update(over)
        return Finding(**fields)

    def detect(self, lanes: Sequence[Lane], c: AnalysisContext) -> list[Finding]:
        cohorts: dict[tuple, list[Lane]] = {}
        for lane in lanes:
            cohorts.setdefault((lane.team, lane.kind.value, lane.billing_class), []).append(lane)
        return [self.finding(team, kind, members, c)
                for (team, kind, _), members in sorted(cohorts.items(),
                                                       key=lambda kv: (kv[0][0] or "", kv[0][1]))]


def test_a_good_detector_conforms() -> None:
    found = kit.assert_detector_conforms(CohortDetector(), lanes(), ctx())
    assert len(found) == 4


class Undeclared(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, kind="k9", finding_id=_finding_id(self.id, "k9", f.scope))
                for f in super().detect(lanes, c)]


class NoReference(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, references=()) for f in super().detect(lanes, c)]


class ShardDependent(CohortDetector):
    """Cross-cohort logic: every finding counts all lanes it was given."""

    def detect(self, lanes, c):
        return [dataclasses.replace(f, n_lanes=len(lanes)) for f in super().detect(lanes, c)]


class ReadsTheShard(CohortDetector):
    def detect(self, lanes, c):
        out = super().detect(lanes, c)
        return out if c.shard is None else []


class NoFix(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, summary="Something happened.") for f in
                super().detect(lanes, c)]


class WrongId(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, finding_id=f"fd_wrong_{i}")
                for i, f in enumerate(super().detect(lanes, c))]


class PersonScope(CohortDetector):
    def detect(self, lanes, c):
        out = []
        for f in super().detect(lanes, c):
            scope = Scope(dims=tuple(sorted(f.scope.dims + (("principal", "p_" + "1" * 20),))))
            out.append(dataclasses.replace(f, scope=scope,
                                           finding_id=_finding_id(self.id, f.kind, scope)))
        return out


class SelfWithoutPrincipal(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, audience="self") for f in super().detect(lanes, c)]


class Nondeterministic(CohortDetector):
    calls = itertools.count()

    def detect(self, lanes, c):
        n = next(self.calls)
        return [dataclasses.replace(f, n_events=n) for f in super().detect(lanes, c)]


class BadPatch(CohortDetector):
    def detect(self, lanes, c):
        fix = Fix(text="x", config_patch=(("notAKey", "1"),), target=None, doc_url=None)
        return [dataclasses.replace(f, fix=fix) for f in super().detect(lanes, c)]


class Duplicates(CohortDetector):
    def detect(self, lanes, c):
        out = super().detect(lanes, c)
        return out + out[:1]


class LongTitle(CohortDetector):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, title="x" * 121) for f in super().detect(lanes, c)]


class AllowanceWithoutTitle(CohortDetector):
    def detect(self, lanes, c):
        f = super().detect(lanes, c)[0]
        scope = Scope(dims=tuple(sorted(f.scope.dims + (("billing_class", "allowance"),))))
        return [dataclasses.replace(f, scope=scope, finding_id=_finding_id(self.id, "k1", scope),
                                    cost_observed=exact(1, Basis.LIST_EQUIVALENT))]


class AllowanceOnBilledBasis(AllowanceWithoutTitle):
    def detect(self, lanes, c):
        return [dataclasses.replace(f, title="Allowance headroom: x",
                                    recoverable=estimated(5, Basis.LIST, note="n"))
                for f in super().detect(lanes, c)]


@pytest.mark.parametrize("detector, message", [
    (Undeclared(), "not declared"),
    (NoReference(), "no references"),
    (ShardDependent(), "shard invariance"),
    (ReadsTheShard(), "shard invariance"),
    (NoFix(), "no mechanical fix"),
    (WrongId(), "finding_id"),
    (PersonScope(), "principal"),
    (SelfWithoutPrincipal(), "self principal"),
    (Nondeterministic(), "deterministic"),
    (BadPatch(), "allowlisted"),
    (Duplicates(), "duplicate"),
    (LongTitle(), "too long"),
    (AllowanceWithoutTitle(), "allowance title"),
    (AllowanceOnBilledBasis(), "list-equivalent"),
])
def test_detector_conformance_catches(detector, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_detector_conforms(detector, lanes(), ctx())


def test_detector_conformance_surface_and_capabilities() -> None:
    with pytest.raises(AssertionError, match="required capabilities"):
        kit.assert_detector_conforms(CohortDetector(), lanes(),
                                     ctx(capabilities=frozenset({"timing"})))
    with pytest.raises(AssertionError, match="Detector"):
        kit.assert_detector_conforms(object(), lanes(), ctx())  # type: ignore[arg-type]

    class ListKinds(CohortDetector):
        kinds = ["k1"]  # type: ignore[assignment]

    with pytest.raises(AssertionError, match="kinds"):
        kit.assert_detector_conforms(ListKinds(), lanes(), ctx())

    class UnknownLever(CohortDetector):
        def detect(self, lanes, c):
            return [dataclasses.replace(f, lever_ids=("no.such",)) for f in
                    super().detect(lanes, c)]

    with pytest.raises(UsageError):
        kit.assert_detector_conforms(UnknownLever(), lanes(), ctx())


def test_self_and_break_glass_findings_are_allowed_with_context() -> None:
    kit.assert_detector_conforms(SelfWithoutPrincipal(), lanes(),
                                 ctx(self_principal="p_" + "1" * 20))

    class Session(CohortDetector):
        def detect(self, lanes, c):
            out = []
            for f in super().detect(lanes, c):
                scope = Scope(dims=tuple(sorted(f.scope.dims + (("session", "s_1"),))))
                out.append(dataclasses.replace(f, scope=scope,
                                               finding_id=_finding_id(self.id, f.kind, scope)))
            return out

    with pytest.raises(AssertionError, match="break-glass"):
        kit.assert_detector_conforms(Session(), lanes(), ctx())
    kit.assert_detector_conforms(Session(), lanes(), ctx(break_glass="incident 7"))


def test_aggregate_detectors_skip_the_shard_check() -> None:
    class OrgWide(ShardDependent):
        requires = frozenset({"aggregates"})

    found = kit.assert_detector_conforms(OrgWide(), lanes(),
                                         ctx(capabilities=frozenset({"aggregates"})))
    assert len(found) == 4


# ---------- adapter conformance ----------


class JsonlAdapter:
    """A toy adapter over lines ``{"id", "team", "in", "out", "text"}`` (content in ``text``)."""

    name = "toy-jsonl"
    capabilities = frozenset({"usage_sequence", "timing", "attribution.team"})

    def __init__(self, *, leak: str | None = None, flaky: bool = False, caps=None,
                 hashed: bytes | None = None, principal: str | None = None,
                 name_key_id: str | None = "match") -> None:
        self.leak, self.flaky, self.caps = leak, flaky, caps
        self.hashed, self.principal, self.nk = hashed, principal, name_key_id
        self.reads = 0

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.lstrip().startswith(b"{")

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        self.reads += 1
        data = kit._source_bytes(path)
        requests = []
        for n, line in enumerate(x for x in data.splitlines() if x.strip()):
            rec = json.loads(line)
            attr = {"team": rec["team"]}
            if self.leak == "canary" or self.leak == "text":
                attr["project"] = rec["text"]
            if self.hashed is not None:
                attr["repo"] = pseudonym(self.hashed, "h", rec["id"])
            if self.principal is not None:
                attr["principal"] = self.principal
            requests.append(make_request(
                f"toy-{rec['id']}", 0, kit._ts("2026-09-23") + n + (self.reads if self.flaky
                                                                   else 0),
                {"uncached_input": rec["in"], "output": rec["out"]}, attribution=attr))
        nk = opts.name_key_id if self.nk == "match" else self.nk
        src = SourceInfo(source_id="s_toy", adapter=self.name, name_hmac="x", sha256="y",
                         bytes=len(data), name_key_id=nk,
                         principal_key_id=opts.principal_key_id)
        caps = self.caps if self.caps is not None else frozenset({"usage_sequence", "timing"})
        return IngestResult(source=src, requests=requests, sessions=[], events=[], aggregates=[],
                            cost_lines=[], outcomes=[], quarantined=[], notes=[],
                            stats={"lines": len(requests)}, capabilities=caps)


def fixture(tmp_path: Path, *, gz: bool = False) -> Path:
    long_text = "the quick brown fox jumps over the lazy dog, " * 3 + CANARY
    lines = [{"id": f"r{i}", "team": "t", "in": 10 * i, "out": i, "text": long_text}
             for i in range(3)]
    body = "\n".join(json.dumps(x) for x in lines).encode()
    path = tmp_path / ("toy.jsonl.gz" if gz else "toy.jsonl")
    path.write_bytes(gzip.compress(body) if gz else body)
    return path


EXPECT = frozenset({"usage_sequence", "timing"})


def test_a_good_adapter_conforms(tmp_path: Path) -> None:
    result = kit.assert_adapter_conforms(JsonlAdapter(), fixture(tmp_path),
                                         expect_capabilities=EXPECT)
    assert len(result.requests) == 3
    kit.assert_adapter_conforms(JsonlAdapter(), fixture(tmp_path, gz=True),
                                expect_capabilities=EXPECT)
    folder = tmp_path / "tree"
    folder.mkdir()
    fixture(folder)
    kit.assert_adapter_conforms(JsonlAdapter(), folder, expect_capabilities=EXPECT)
    opts = kit.conformance_ingest_options()
    kit.assert_adapter_conforms(JsonlAdapter(hashed=kit.CONFORMANCE_NAME_KEY), fixture(tmp_path),
                                expect_capabilities=EXPECT, opts=opts)
    kit.assert_adapter_conforms(JsonlAdapter(principal=pseudonym(b"k", "p", "a")),
                                fixture(tmp_path), expect_capabilities=EXPECT)


@pytest.mark.parametrize("adapter, message", [
    (JsonlAdapter(flaky=True), "twice"),
    (JsonlAdapter(leak="canary"), "canary"),
    (JsonlAdapter(caps=frozenset({"usage_sequence"})), "expected"),
    (JsonlAdapter(caps=EXPECT | {"blocks"}), "expected"),
    (JsonlAdapter(hashed=b"k" * 32, name_key_id=None), "name_key_id"),
])
def test_adapter_conformance_catches(tmp_path: Path, adapter, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_adapter_conforms(adapter, fixture(tmp_path), expect_capabilities=EXPECT)


def test_adapter_conformance_catches_source_text_and_undeclared_capabilities(
        tmp_path: Path) -> None:
    path = tmp_path / "plain.jsonl"
    text = "an entirely ordinary sentence that goes on for well over sixty-four bytes of text"
    path.write_text(json.dumps({"id": "r0", "team": "t", "in": 1, "out": 1, "text": text}))
    with pytest.raises(AssertionError, match="source text"):
        kit.assert_adapter_conforms(JsonlAdapter(leak="text"), path, expect_capabilities=EXPECT)
    wide = JsonlAdapter(caps=EXPECT | {"blocks"})
    with pytest.raises(AssertionError, match="declared"):
        kit.assert_adapter_conforms(wide, path, expect_capabilities=EXPECT | {"blocks"})
    with pytest.raises(AssertionError, match="principal_key_id"):
        kit.assert_adapter_conforms(JsonlAdapter(principal="p_" + "1" * 20), path,
                                    expect_capabilities=EXPECT,
                                    opts=kit.conformance_ingest_options(principal_key=None,
                                                                        principal_key_id=None))
    with pytest.raises(AssertionError, match="Adapter"):
        kit.assert_adapter_conforms(object(), path, expect_capabilities=EXPECT)  # type: ignore


def test_provider_labels_may_be_long(tmp_path: Path) -> None:
    label = "$5.00 per million input tokens for Claude Opus 5 in US East (N. Virginia), global CRIS"

    class CostAdapter(JsonlAdapter):
        def read(self, path, opts):
            res = super().read(path, opts)
            res.cost_lines = [make_cost_line(1, description=label, sku=label)]
            return res

    path = tmp_path / "cur.jsonl"
    path.write_text(json.dumps({"id": "r0", "team": "t", "in": 1, "out": 1, "text": label}))
    kit.assert_adapter_conforms(CostAdapter(), path, expect_capabilities=EXPECT)


def test_conformance_ingest_options() -> None:
    opts = kit.conformance_ingest_options(k_anonymity=7)
    assert opts.k_anonymity == 7 and opts.name_key_id == key_id(kit.CONFORMANCE_NAME_KEY)
    assert str(opts.content_tier) == "none"


# ---------- store and pricer conformance negatives ----------


class PrincipalStore(kit.MemoryStore):
    def aggregate(self, *, since_ms, until_ms, group_by, where=None, pricer=None):
        group_by = [g for g in group_by if g not in ("principal", "session_key", "session")]
        return super().aggregate(since_ms=since_ms, until_ms=until_ms, group_by=group_by,
                                 where=where, pricer=pricer)


class ForgetfulStore(kit.MemoryStore):
    def iter_requests(self, **kw):
        for r in super().iter_requests(**kw):
            yield dataclasses.replace(r, attribution=dataclasses.replace(r.attribution,
                                                                         cost_center=None))


class MiscountingStore(kit.MemoryStore):
    def count_users(self, *, since_ms, until_ms, where):
        return super().count_users(since_ms=since_ms, until_ms=until_ms, where=where) + 1


@pytest.mark.parametrize("cls, message", [
    (PrincipalStore, "PrivacyError"),
    (ForgetfulStore, "OTel-only attribution"),
    (MiscountingStore, "count_users"),
])
def test_store_conformance_catches(cls, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_store_conforms(lambda **kw: cls(**kw), permutations=2)


class OffByOnePricer(kit.FakePricer):
    def unit_rates(self, ctx, *, ts_ms):
        unit = super().unit_rates(ctx, ts_ms=ts_ms)
        return None if unit is None else dataclasses.replace(unit, output=unit.output + 1)


class DoubleCountingPricer(kit.FakePricer):
    def price_usage(self, usage, ctx, *, ts_ms, **kw):
        return super().price_usage(usage + usage, ctx, ts_ms=ts_ms, **kw)


@pytest.mark.parametrize("pricer, message", [
    (OffByOnePricer(), "unit_rates"),
    (DoubleCountingPricer(), "case 1"),
])
def test_pricer_conformance_catches(pricer, message) -> None:
    with pytest.raises(AssertionError, match=message):
        kit.assert_pricer_conforms(pricer)
    with pytest.raises(AssertionError, match="Pricer"):
        kit.assert_pricer_conforms(object())  # type: ignore[arg-type]
