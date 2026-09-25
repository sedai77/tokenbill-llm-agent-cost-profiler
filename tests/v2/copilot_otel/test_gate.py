"""Gate tests of CP-OTEL (``@pytest.mark.gate``; each guarded by ``importorskip`` on the sibling
package it needs): the mixed Claude + Copilot OTLP file through TELEM's ``otlp`` and WIRING's
deferral loop (addendum §21.6 gate-1 addition), registry sniffing across every installed adapter,
CP-VSCODE's collector extracts through these adapters, and RATES on the gh-aw utility model."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tokenbill.adapters.copilot_otel import CopilotOtelAdapter
from tokenbill.adapters.copilot_vscode import VsCodeAgentTracesAdapter
from tokenbill.adapters.gh_aw import GhAwTokenUsageAdapter
from tokenbill.core.builders import CANARY
from tokenbill.core.ids import key_id
from tokenbill.core.records import billing_class
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.core.types import IngestOptions

from .helpers import (
    CLI_FILE,
    COPILOT_OTLP,
    GH_AW,
    MIXED,
    T0_MS,
    VSCODE_DUMP,
    build_db,
    inference,
    opts,
    standard_spans,
)

pytestmark = pytest.mark.gate

ORG_KEY = bytes(range(200, 232))
NAME_KEY = bytes(range(160, 192))
COPILOT_MSGS = {"resp_mix_1", "resp_mix_2", "resp_mix_3"}
MIXED_OPTS = IngestOptions(identity_mode="central-ingest",
                           otel_service_names=("acme-copilot-proxy",))


def _env():
    common = pytest.importorskip("tokenbill.pipeline.common")
    config = pytest.importorskip("tokenbill.config")
    from tokenbill.core.cache_rules import RulesTable

    env = common.Env(config=config.Config(), pricer=FakePricer(), rules=RulesTable(), k=5,
                     jobs=1, org_key=ORG_KEY, name_key=NAME_KEY, name_key_id=key_id(NAME_KEY),
                     now_ms=T0_MS + 86_400_000)
    return common, env


def _stored_requests(store: MemoryStore) -> list:
    return [r for lane in store.iter_lanes() for r in lane.requests]


def _call_id(req) -> str | None:
    """The Copilot response id of a stored request: ``copilot-otel`` keeps it as the provider
    message id; TELEM's generic GenAI mapping (before A-4) as the provider request id."""
    att = req.attempts[0]
    return att.provider_message_id or att.provider_request_id


def test_gate_mixed_otlp_through_otlp_and_wiring_deferral(tmp_path: Path) -> None:
    """Both span sets present, none duplicated: Claude spans via ``otlp``, Copilot spans once
    (via ``copilot-otel`` once TELEM defers them, A-4)."""
    pytest.importorskip("tokenbill.adapters.otel")
    common, env = _env()
    store = MemoryStore(org_key=ORG_KEY)
    sources, _notes = common.ingest_paths(store, [MIXED], env, MIXED_OPTS)
    reqs = _stored_requests(store)
    ids = [_call_id(r) for r in reqs]
    copilot = [r for r in reqs if _call_id(r) in COPILOT_MSGS]
    claude = [r for r in reqs if r.model == "claude-opus-5-5"]
    assert sorted(i for i in ids if i in COPILOT_MSGS) == sorted(COPILOT_MSGS)   # each once
    assert len(claude) == 2
    assert len(reqs) == len(copilot) + len(claude) == 5
    adapters = [s.adapter for s in sources]
    assert adapters[0] == "otlp"
    if "copilot-otel" in adapters:        # TELEM defers Copilot resources (A-4)
        assert adapters.count("copilot-otel") == 1
        for req in copilot:
            inf = inference(req)
            assert inf.pricing.channel == "github_copilot"
            assert billing_class(inf.pricing.billing_path) == "pool"
    assert CANARY not in repr(reqs)


def test_gate_telem_defers_copilot_resources() -> None:
    """TELEM's late change request A-4: ``otlp`` skips Copilot resources and sets
    ``stats["defer:copilot-otel"]``; the two passes are disjoint and complete."""
    otel = pytest.importorskip("tokenbill.adapters.otel")
    theirs = otel.OtlpJsonAdapter().read(MIXED, opts(identity_mode="central-ingest",
                                                     otel_service_names=(
                                                         "acme-copilot-proxy",)))
    if "defer:copilot-otel" not in theirs.stats:
        pytest.xfail("TELEM late change request A-4 not applied yet (gate1-fixups.md #4)")
    assert theirs.stats["defer:copilot-otel"] == 3
    mine = CopilotOtelAdapter().read(MIXED, opts(identity_mode="central-ingest",
                                                 otel_service_names=("acme-copilot-proxy",)))
    their_ids = {r.request_id for r in theirs.requests}
    my_ids = {r.request_id for r in mine.requests}
    assert not their_ids & my_ids
    assert {r.attempts[0].provider_message_id for r in mine.requests} == COPILOT_MSGS
    assert not {r.attempts[0].provider_message_id for r in theirs.requests} & COPILOT_MSGS
    assert len(their_ids) == 2


def test_gate_registry_sniffing_picks_the_owning_adapter(tmp_path: Path) -> None:
    pytest.importorskip("tokenbill.adapters.otel")
    pytest.importorskip("tokenbill.adapters.claude_code")
    from tokenbill.core.registry import sniff_adapter

    db = build_db(tmp_path / "agent-traces.db", standard_spans())
    for path, name in ((VSCODE_DUMP, "copilot-otel"), (COPILOT_OTLP, "copilot-otel"),
                       (CLI_FILE, "copilot-otel"), (MIXED, "otlp"),
                       (db, "copilot-vscode-traces"), (GH_AW, "gh-aw-token-usage")):
        found = sniff_adapter(path)
        assert found is not None and found.name == name, (path.name, found)


def test_gate_otlp_copilot_only_file_through_wiring() -> None:
    common, env = _env()
    store = MemoryStore(org_key=ORG_KEY)
    sources, _ = common.ingest_paths(store, [COPILOT_OTLP], env, IngestOptions())
    assert [s.adapter for s in sources] == ["copilot-otel"]
    reqs = _stored_requests(store)
    assert len(reqs) == 3
    assert all(billing_class(r.attribution.billing_path) == "pool" for r in reqs)


def _collector_state(module, path: Path):
    state_cls = module.VsCodeCollectorState
    try:
        return state_cls.load(path)
    except Exception:  # noqa: BLE001 - the documented constructor is load(); fall back
        return state_cls()


def test_gate_vscode_collector_extracts_equal_direct_reads(tmp_path: Path) -> None:
    """CP-VSCODE's extracts (SQLite and OTel outfile) read by these adapters equal the direct
    reads of the source files; the collector principal reaches ``Attribution.principal``."""
    collect = pytest.importorskip("tokenbill.adapters.copilot_vscode_collect")
    db = build_db(tmp_path / "agent-traces.db", standard_spans())
    outfile = tmp_path / "otel.jsonl"
    shutil.copyfile(VSCODE_DUMP, outfile)
    principal = "c_0123456789abcdef0123"
    pkid = key_id(bytes(range(32, 64)))
    identity = collect.CollectorIdentity(principal=principal, principal_key_id=pkid,
                                         team="payments", name_key=NAME_KEY,
                                         name_key_id=key_id(NAME_KEY))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = collect.collect_vscode_extracts(
        collect.VsCodeSources(traces_dbs=(db,), otel_outfiles=(outfile,)),
        _collector_state(collect, tmp_path / "state.json"), out_dir=out_dir, identity=identity,
        now_ms=T0_MS + 60_000)
    files = list(result.files)
    assert files
    direct = {**{r.request_id: r for r in VsCodeAgentTracesAdapter().read(db, opts()).requests},
              **{r.request_id: r for r in CopilotOtelAdapter().read(outfile, opts()).requests}}
    seen: dict[str, object] = {}
    for f in files:
        adapter = VsCodeAgentTracesAdapter() if f.suffix == ".db" else CopilotOtelAdapter()
        read = adapter.read(f, opts(principal_key_id=pkid))
        for req in read.requests:
            assert req.request_id not in seen
            seen[req.request_id] = req
            assert req.attribution.principal == principal
            assert req.attribution.team == "payments"
            mine, theirs = inference(req), inference(direct[req.request_id])
            assert mine.usage == theirs.usage
            assert mine.provider_reported_cost_nano == theirs.provider_reported_cost_nano
    assert set(seen) == set(direct)


def test_gate_real_rate_card_leaves_the_gh_aw_utility_model_unpriced() -> None:
    common = pytest.importorskip("tokenbill.pipeline.common")
    pytest.importorskip("tokenbill.rates.engine")
    card = common.load_rate_card()
    result = GhAwTokenUsageAdapter(env={}).read(GH_AW, opts())
    req = next(r for r in result.requests if inference(r).pricing.model_raw
               == "gpt-4o-mini-2024-07-18")
    priced = card.price_inference(inference(req), ts_ms=req.ts_start_ms)
    assert priced.figure.nano is None
    assert inference(req).provider_reported_cost_basis == "provider_estimate"
