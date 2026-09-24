"""Merge gate 1 smoke test (F-KIT; PLAN §1.5, SPEC Appendix G.1).

CC fixture tree (``tests/v2/fixtures/claude_code/``, read-only) → ``SqliteStore`` priced with the
real ``RateCard`` → ``reconcile`` against the ``tests/v2/fixtures/admin/`` pages read by the ADMIN
adapters → ``calibrate_lanes`` → ``run_detectors`` → ``rescope_findings`` → ``build_action_plan``
with the real ``UsageReplayer``. Every real module sits behind ``importorskip``; the test asserts
only structural invariants: labels, no floats, the exact bill equals the sum of the priced exact
lines, findings sorted, no content canary anywhere (outputs and the SQLite bytes). No pipeline code
is needed.

Written in wave 1 against the SPEC signatures (§3.6, §3.7, §6, §7.2, §9.6, §11.2, §12); it skips
until the wave-2 packages are merged and runs at the daily canary merge and at merge gate 1.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core import kanon, registry
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.ids import key_id
from tokenbill.core.labels import Basis, Evidence, Figure
from tokenbill.core.records import Inference, to_json
from tokenbill.core.types import AnalysisContext, IngestOptions, Scope

pytestmark = pytest.mark.gate

cc_mod = pytest.importorskip("tokenbill.adapters.claude_code")
admin_mod = pytest.importorskip("tokenbill.adapters.anthropic_admin")
db_mod = pytest.importorskip("tokenbill.store.db")
engine_mod = pytest.importorskip("tokenbill.rates.engine")
schema_mod = pytest.importorskip("tokenbill.rates.schema")
recon_mod = pytest.importorskip("tokenbill.recon.reconcile")
calibrate_mod = pytest.importorskip("tokenbill.sim.calibrate")
replay_mod = pytest.importorskip("tokenbill.sim.usage_replay")
plan_mod = pytest.importorskip("tokenbill.plan.action_plan")
rules_mod = pytest.importorskip("tokenbill.core.cache_rules")
shards_mod = pytest.importorskip("tokenbill.core.shards")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CC_DIR = FIXTURES / "claude_code"
ADMIN_DIR = FIXTURES / "admin"
ORG_KEY = bytes(range(224, 256))
NAME_KEY = bytes(range(0, 64, 2))
WINDOW = {"since_ms": 0, "until_ms": 2**53}
DAY_MS = 86_400_000
NOW_MS = 20_719 * DAY_MS  # 2026-09-23T00:00Z
ALL_COST_DIMS = ["date", "provider", "channel", "model", "team", "cost_center", "project",
                 "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path"]
ADMIN_ADAPTERS = ("anthropic-usage-report", "anthropic-cost-report", "anthropic-cc-analytics",
                  "anthropic-enterprise-analytics")


def _files(root: Path) -> list[Path]:
    skip = {"MANIFEST.json", "README.md"}
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name not in skip
                  and p.suffix not in (".py", ".md") and p.name != "journal.jsonl")


def _opts() -> IngestOptions:
    return IngestOptions(identity_mode="install", name_key=NAME_KEY, name_key_id=key_id(NAME_KEY),
                         principal_key=ORG_KEY, principal_key_id=key_id(ORG_KEY),
                         now_ms=NOW_MS)


def _leaves(obj: Any) -> Iterator[Any]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def _figures(obj: Any) -> Iterator[Figure]:
    if isinstance(obj, Figure):
        yield obj
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _figures(x)
    elif hasattr(obj, "__dataclass_fields__"):
        for name in obj.__dataclass_fields__:
            yield from _figures(getattr(obj, name))


def _sort_key(f: Any) -> tuple[int, str, str]:
    p50 = f.recoverable.nano if f.recoverable is not None and f.recoverable.nano is not None else 0
    return (-p50, f.detector_id, f.finding_id)


def _ingest_fixtures(store: Any, pricer: Any) -> tuple[frozenset[str], int]:
    capabilities: set[str] = set()
    cc = registry.get_adapter("claude-code")  # the SPEC §3.7 way to instantiate an adapter
    read = 0
    for path in _files(CC_DIR):
        head = path.read_bytes()[:64 * 1024]
        if not cc.sniff(path, head):
            continue
        result = cc.read(path, _opts())
        store.ingest(result, pricer=pricer)
        capabilities |= result.capabilities
        read += 1
    for path in _files(ADMIN_DIR):
        adapter = registry.sniff_adapter(path)
        if adapter is None or adapter.name not in ADMIN_ADAPTERS:
            continue
        result = adapter.read(path, _opts())
        store.ingest(result, pricer=pricer)
        capabilities |= result.capabilities
    return frozenset(capabilities), read


def _rate_card() -> Any:
    """The built-in ``RateCard``. SPEC §6 fixes ``load_builtin()`` but not the ``RateCard``
    constructor, so the common layer-sequence forms are tried in turn."""
    layer = schema_mod.load_builtin()
    for build in (lambda: engine_mod.RateCard([layer]), lambda: engine_mod.RateCard((layer,)),
                  lambda: engine_mod.RateCard(layers=[layer]), lambda: engine_mod.RateCard(layer)):
        try:
            return build()
        except TypeError:
            continue
    pytest.fail("cannot build a RateCard from load_builtin(); update _rate_card()")


def test_gate1_smoke(tmp_path: Path) -> None:
    if not CC_DIR.is_dir() or not ADMIN_DIR.is_dir():
        pytest.skip("CC and ADMIN fixtures are not merged yet")
    pricer = _rate_card()
    db_path = tmp_path / "ledger" / "tokenbill.db"
    store = db_mod.SqliteStore(db_path, org_key=ORG_KEY, name_key_id=key_id(NAME_KEY),
                               pricer=pricer)
    capabilities, transcripts = _ingest_fixtures(store, pricer)
    assert transcripts > 0, "no Claude Code transcript fixture was sniffed"

    # --- ledger gate: reconcile against the admin pages ---
    report = recon_mod.reconcile(store.iter_usage_records(**WINDOW), store.aggregates(**WINDOW),
                                 store.cost_lines(**WINDOW), pricer, today="2026-09-23")
    assert report.verdict in ("reconciled", "not_reconciled", "insufficient_data")
    assert all(c.verdict in ("reconciled", "not_reconciled", "insufficient_data")
               for c in report.channels)

    # --- the exact bill equals the sum of the priced exact lines ---
    exact_lines = 0
    for rec in store.iter_usage_records(**WINDOW):
        inf = Inference(inference_id=rec.inference_id, kind=rec.kind, usage=rec.usage,
                        pricing=rec.pricing, usage_source=rec.usage_source,
                        billable=rec.billable, billing_rule_id=rec.billing_rule_id)
        priced = pricer.price_inference(inf, ts_ms=rec.ts_ms)
        if priced.figure.nano is not None and priced.figure.basis is not Basis.LIST_EQUIVALENT:
            exact_lines += sum(ln.amount_nano for ln in priced.lines if ln.exact)
    rows = store.cost_rows(group_by=ALL_COST_DIMS, **WINDOW)
    assert sum(r.priced_nano for r in rows if r.basis is not Basis.LIST_EQUIVALENT) == exact_lines
    total = store.aggregate(group_by=[], **WINDOW)
    if total.rows:
        assert total.rows[0].priced.exact.nano == exact_lines
        assert total.rows[0].priced.exact.is_billed_eligible

    # --- model gate, detectors, publication, plan ---
    rules = rules_mod.RulesTable()
    lanes = list(store.iter_lanes(**WINDOW))
    assert lanes
    calibration = calibrate_mod.calibrate_lanes(lanes, pricer=pricer, rules=rules)
    assert calibration.status in ("pass", "fail", "insufficient_data")
    starts = [r.ts_start_ms for lane in lanes for r in lane.requests]
    window = (min(starts), max(starts) + 1)
    ctx = AnalysisContext(pricer=pricer, rules=rules, replayer=replay_mod.UsageReplayer(),
                          calibration=calibration, window=window,
                          capabilities=capabilities, now_ms=window[1],
                          aggregates=tuple(store.aggregates(**WINDOW)),
                          cost_lines=tuple(store.cost_lines(**WINDOW)))
    raw_findings = registry.run_detectors(lanes, ctx)
    assert [_sort_key(f) for f in raw_findings] == sorted(_sort_key(f) for f in raw_findings)

    def count_users(scope: Scope) -> int:
        allowed = ("team", "cost_center", "lane_kind", "model", "workspace_id", "billing_class")
        where = {k: v for k, v in scope.dims if k in allowed}
        return store.count_users(where=where, since_ms=window[0], until_ms=window[1])

    findings = kanon.rescope_findings(raw_findings, k=5, count_users=count_users)
    assert [_sort_key(f) for f in findings] == sorted(_sort_key(f) for f in findings)
    index = list(store.lane_index(since_ms=window[0], until_ms=window[1]))
    shards = shards_mod.plan_shards(index)

    def load_lanes(keys, shard):
        where = shards_mod.shard_where(shard) if shard is not None else None
        return list(store.iter_lanes(since_ms=window[0], until_ms=window[1], where=where,
                                     lane_keys=keys))

    days = max(1, (window[1] - window[0] + DAY_MS - 1) // DAY_MS)
    plan = plan_mod.build_action_plan(findings, index, load_lanes, shards, ctx, window_days=days)
    assert plan.headline_monthly.evidence is not Evidence.EXACT or plan.headline_monthly.nano == 0

    # --- labels, floats, canary ---
    for obj in (report, calibration, tuple(findings), plan):
        for fig in _figures(obj):
            assert isinstance(fig.evidence, Evidence) and isinstance(fig.basis, Basis)
            if fig.is_billed_eligible:
                assert fig.evidence is Evidence.EXACT
        encoded = to_json(obj) if hasattr(obj, "__dataclass_fields__") else [to_json(f)
                                                                               for f in obj]
        assert not any(isinstance(v, float) for v in _leaves(encoded))
        assert_no_canary(json.dumps(encoded))
    blobs = [p.read_bytes() for p in db_path.parent.iterdir() if p.is_file()]
    assert_no_canary(*blobs)
