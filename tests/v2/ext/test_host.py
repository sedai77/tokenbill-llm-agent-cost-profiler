"""F-EXT acceptance: with the test-only extension ``fake`` registered by ``monkeypatch``, every host
function resolves its hook lazily and calls it with the documented arguments (CA-39 + E-1)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core import extensions as ext
from tokenbill.core import registry
from tokenbill.core.builders import FlatRates, make_license
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.protocols import ExtRecordStore
from tokenbill.core.types import (
    AnalysisContext,
    DataQualityNote,
    FocusRow,
    PanelRow,
    PolicyPack,
    ReconciliationReport,
)

from .fake_ext import hooks
from .support import (
    COPILOT,
    HOOKS,
    PKG,
    PlainLedger,
    StatsLedger,
    bare_spec,
    fake_spec,
    ingest_result,
    make_ctx,
    make_run_result,
)

Install = Callable[..., None]


# ---------- pure table readers ----------


def test_extensions_sorted_by_name(install: Install) -> None:
    install(fake_spec(), COPILOT, bare_spec("aaa"))
    assert [s.name for s in ext.extensions()] == ["aaa", "copilot", "fake"]


def test_extensions_validates_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "EXTENSIONS", {"other": fake_spec()})
    with pytest.raises(ContractViolation):
        ext.extensions()
    monkeypatch.setattr(registry, "EXTENSIONS", {"fake": object()})
    with pytest.raises(ContractViolation):
        ext.extensions()


def test_shipped_table_is_copilot_only() -> None:
    assert [s.name for s in ext.extensions()] == ["copilot"]
    assert ext.delegated_channels() == frozenset(
        {"github_copilot", "github_actions", "github_sandbox"})


def test_delegated_channels(install: Install) -> None:
    install(fake_spec(), COPILOT)
    assert ext.delegated_channels() == frozenset(
        {"fake_a", "fake_b", "github_copilot", "github_actions", "github_sandbox"})
    install()
    assert ext.delegated_channels() == frozenset()


# ---------- rates and CLI tables ----------


def test_extension_rate_files(install: Install) -> None:
    install(fake_spec())
    notes: list[DataQualityNote] = []
    files = ext.extension_rate_files(notes=notes)
    assert notes == []
    assert len(files) == 1 and files[0].name == "fake_rates.json"
    assert json.loads(files[0].read_text(encoding="utf-8"))["provider"] == "fake"


def test_extension_rate_files_nested_resource_and_order(install: Install) -> None:
    install(fake_spec(rate_files=(f"{PKG}:fake_rates.json", f"{PKG}:fake_rates.json")),
            bare_spec("aaa", rate_files=(f"{PKG}:../fake_ext/fake_rates.json",)))
    with pytest.raises(ContractViolation):  # no parent traversal in resource names
        ext.extension_rate_files()
    install(fake_spec(rate_files=(f"{PKG}:fake_rates.json", f"{PKG}:fake_rates.json")))
    assert [f.name for f in ext.extension_rate_files()] == ["fake_rates.json"] * 2


@pytest.mark.parametrize("entry", ["no-colon.json", ":x.json", "pkg:", "pkg:a//b.json",
                                   "pkg:./x.json"])
def test_malformed_rate_file_entries_raise(install: Install, entry: str) -> None:
    install(fake_spec(rate_files=(entry,)))
    with pytest.raises(ContractViolation):
        ext.extension_rate_files(notes=[])


def test_rate_verifiers_command_modules_policy_targets(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa"))
    notes: list[DataQualityNote] = []
    assert ext.rate_verifiers(notes=notes) == [f"{HOOKS}:verify"]
    assert ext.command_modules(notes=notes) == {"fake": f"{PKG}.commands"}
    assert ext.policy_targets(notes=notes) == {"fake-target": f"{HOOKS}:policy_packs"}
    assert notes == []
    assert hooks.CALLS == []  # listing never calls (or imports) a hook


def test_duplicate_policy_target_is_a_contract_violation(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa", policy_targets=(("fake-target", f"{HOOKS}:panel"),)))
    with pytest.raises(ContractViolation):
        ext.policy_targets()
    with pytest.raises(ContractViolation):
        ext.policy_packs("fake-target", PlainLedger(), [], make_ctx(), [], None, out_dir=None,
                         current=None, cohort_by="team", include_tradeoffs=False)


def test_malformed_hook_paths_raise(install: Install) -> None:
    install(fake_spec(policy_targets=(("fake-target", "no_colon_here"),)))
    with pytest.raises(ContractViolation):
        ext.policy_targets()
    install(fake_spec(rate_verifier="no_colon_here"))
    with pytest.raises(UsageError):  # core.registry.load's own check
        ext.rate_verifiers()
    install(fake_spec(reconciler="no_colon_here"))
    with pytest.raises(UsageError):  # core.registry.load's own check
        ext.run_reconcilers(PlainLedger(), [], FlatRates(), since_ms=0, until_ms=1,
                            tolerance_pct="0.5", unexplained_pct="1.0", closed_only=False,
                            today="2026-09-24")


# ---------- record stores ----------


def test_open_record_stores(install: Install, tmp_path: Path) -> None:
    install(fake_spec(), bare_spec("aaa"))
    db = tmp_path / "ledger.db"
    notes: list[DataQualityNote] = []
    stores = ext.open_record_stores(db, create=False, notes=notes)
    assert notes == [] and len(stores) == 1
    assert isinstance(stores[0], ExtRecordStore) and stores[0].name == "fake"
    assert hooks.calls("record_store") == [((db,), {"create": False})]


def test_open_record_stores_rejects_non_conforming_class(install: Install,
                                                         tmp_path: Path) -> None:
    install(fake_spec(record_store=f"{HOOKS}:NotARecordStore"))
    with pytest.raises(ContractViolation):
        ext.open_record_stores(tmp_path / "x.db", create=True)


def test_persist_routes_records_and_sums_counts() -> None:
    a, b = hooks.FakeRecordStore(), hooks.FakeRecordStore(name="other")
    result = ingest_result(licenses=(make_license(),), activity=(), config=())
    assert ext.persist([a, b], result, notes=[]) == {"activity": 0, "config": 0, "licenses": 2}
    puts = hooks.calls("put")
    assert [(args[0], kw) for args, kw in puts] == [(result, {"principal_key_id": "k_org"})] * 2
    assert len(a.licenses()) == 1 and len(b.licenses()) == 1


def test_persist_skips_results_without_records() -> None:
    store = hooks.FakeRecordStore()
    assert ext.persist([store], ingest_result()) == {}
    assert hooks.calls("put") == []
    with pytest.raises(ContractViolation):
        ext.persist([store], "not a result")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        ext.persist([store], ingest_result(), notes=())  # type: ignore[arg-type]


def test_retain_and_purge_sum_over_stores() -> None:
    stores = [hooks.FakeRecordStore(), hooks.FakeRecordStore(name="other")]
    assert ext.retain(stores, identity_before_ms=123, notes=[]) == 4
    assert hooks.calls("retain") == [((), {"identity_before_ms": 123})] * 2
    assert ext.purge(stores, principal="p_" + "a" * 20, before_ms=None, actor="admin") == 6
    assert hooks.calls("purge") == [((), {"principal": "p_" + "a" * 20, "before_ms": None,
                                          "actor": "admin"})] * 2
    assert ext.retain([], identity_before_ms=0) == 0 and ext.purge(
        [], principal=None, before_ms=5, actor="x") == 0


# ---------- reconciliation and decisions ----------


def _reconcile(store: object, stores: list[object], **kw: object) -> list[ReconciliationReport]:
    return ext.run_reconcilers(store, stores, FlatRates(), since_ms=1, until_ms=2,  # type: ignore
                               tolerance_pct="0.5", unexplained_pct="1.0", closed_only=True,
                               today="2026-09-24", **kw)


def test_run_reconcilers_calls_with_documented_arguments(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa"))
    ledger, stores = PlainLedger(), [hooks.FakeRecordStore()]
    reports = _reconcile(ledger, stores, notes=[])
    assert len(reports) == 1 and reports[0].decisions == hooks.DECISIONS
    (args, kw), = hooks.calls("reconciler")
    assert args[0] is ledger and args[1] is stores and isinstance(args[2], FlatRates)
    assert kw == {"since_ms": 1, "until_ms": 2, "tolerance_pct": "0.5", "unexplained_pct": "1.0",
                  "closed_only": True, "today": "2026-09-24", "rounding_remainders": None}


def test_run_reconcilers_without_source_stats_passes_none(install: Install) -> None:
    install(fake_spec())
    _reconcile(PlainLedger(), [])
    assert hooks.calls("reconciler")[0][1]["rounding_remainders"] is None


def test_run_reconcilers_rounding_remainders_from_source_stats(install: Install) -> None:
    install(fake_spec())
    ledger = StatsLedger({"github-ai-usage": {"rounding_remainder_e18": 12, "rows": 3},
                          "github-metered-usage": {"rounding_remainder_e18": -7},
                          "claude-code": {"lines": 9}})
    _reconcile(ledger, [])
    remainders = hooks.calls("reconciler")[0][1]["rounding_remainders"]
    assert remainders == {"github-ai-usage": Decimal("1.2E-17"),
                          "github-metered-usage": Decimal("-7E-18")}
    assert all(isinstance(v, Decimal) for v in remainders.values())
    # a 40-digit remainder stays exact (no context rounding)
    ledger = StatsLedger({"github-ai-usage": {"rounding_remainder_e18": 10**40 + 1}})
    hooks.CALLS.clear()
    _reconcile(ledger, [])
    value = hooks.calls("reconciler")[0][1]["rounding_remainders"]["github-ai-usage"]
    assert value == Decimal(f"{10**40 + 1}E-18") and value.as_tuple().exponent == -18
    assert str(value) == "10000000000000000000000.000000000000000001"


def test_run_reconcilers_remainders_that_cancel_in_total(install: Install) -> None:
    install(fake_spec(), fake_spec(name="zzz", channels=("zzz_a",)))
    ledger = StatsLedger({"github-ai-usage": {"rounding_remainder_e18": 5},
                          "github-metered-usage": {"rounding_remainder_e18": -5}})
    _reconcile(ledger, [])
    first, second = hooks.calls("reconciler")
    expected = {"github-ai-usage": Decimal("5E-18"), "github-metered-usage": Decimal("-5E-18")}
    assert first[1]["rounding_remainders"] == expected == second[1]["rounding_remainders"]
    assert first[1]["rounding_remainders"] is not second[1]["rounding_remainders"]  # own copies


def test_run_reconcilers_stats_without_remainders_is_empty_dict(install: Install) -> None:
    install(fake_spec())
    ledger = StatsLedger({"claude-code": {"lines": 9}})
    _reconcile(ledger, [])
    assert hooks.calls("reconciler")[0][1]["rounding_remainders"] == {}
    assert ledger.stats_calls == [None]  # no per-adapter queries without a remainder


def test_run_reconcilers_reads_stats_only_when_a_reconciler_runs(install: Install) -> None:
    install(bare_spec("aaa"))
    ledger = StatsLedger({"github-ai-usage": {"rounding_remainder_e18": 1}})
    assert _reconcile(ledger, []) == []
    assert ledger.stats_calls == []


def test_run_reconcilers_rejects_wrong_report_type(install: Install) -> None:
    install(fake_spec(reconciler=f"{HOOKS}:reconcile_wrong_type"))
    with pytest.raises(ContractViolation):
        _reconcile(PlainLedger(), [])


def test_recon_decisions_of_merges_and_sorts() -> None:
    one = hooks.make_report((("gross_is_list:enterprise:2026-09", "true"),
                             ("convention:src_b", "incl")))
    two = hooks.make_report((("convention:src_a", "excl"),
                             ("gross_is_list:enterprise:2026-09", "true"),
                             ("plan_fit:org:acme:2026-08", "unknown")))
    assert ext.recon_decisions_of([one, two]) == (
        ("convention:src_a", "excl"), ("convention:src_b", "incl"),
        ("gross_is_list:enterprise:2026-09", "true"), ("plan_fit:org:acme:2026-08", "unknown"))
    assert ext.recon_decisions_of([]) == ()
    assert ext.recon_decisions_of([hooks.make_report(())]) == ()


def test_recon_decisions_of_raises_on_conflict() -> None:
    one = hooks.make_report((("convention:src_a", "excl"),))
    two = hooks.make_report((("convention:src_a", "incl"),))
    with pytest.raises(ContractViolation) as err:
        ext.recon_decisions_of([one, two])
    assert "src_a" not in str(err.value)  # the message names the prefix only
    with pytest.raises(ContractViolation):
        ext.recon_decisions_of([one, "not a report"])  # type: ignore[list-item]


# ---------- enrichment and summary ----------


def test_enrich_forwards_recon_decisions_unchanged(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa"))
    ctx = make_ctx()
    store, stores = PlainLedger(), [hooks.FakeRecordStore()]
    decisions = ext.recon_decisions_of([hooks.make_report()])
    channels = frozenset({"fake_a"})
    out = ext.enrich(store, stores, ctx, today="2026-09-24", reconciled_channels=channels,
                     recon_decisions=decisions, notes=[])
    (args, kw), = hooks.calls("context_enricher")
    assert args[0] is store and args[1] is stores and args[2] is ctx
    assert kw["recon_decisions"] is decisions and kw["reconciled_channels"] is channels
    assert kw["today"] == "2026-09-24"
    assert isinstance(out, AnalysisContext) and out.recon_decisions == hooks.DECISIONS
    assert "ext:fake" in out.capabilities and out.reconciled_channels == channels


def test_enrich_default_decisions_and_chaining(install: Install) -> None:
    install(fake_spec(), fake_spec(name="zzz", channels=("zzz_a",)))
    ctx = make_ctx()
    out = ext.enrich(PlainLedger(), [], ctx, today="2026-09-24",
                     reconciled_channels=frozenset())
    first, second = hooks.calls("context_enricher")
    assert first[1]["recon_decisions"] == () and second[0][2] is not ctx  # chained output
    assert out.recon_decisions == ()


def test_enrich_contract_checks(install: Install) -> None:
    install(fake_spec(context_enricher=f"{HOOKS}:enrich_wrong_type"))
    with pytest.raises(ContractViolation):
        ext.enrich(PlainLedger(), [], make_ctx(), today="d", reconciled_channels=frozenset())
    install(fake_spec())
    with pytest.raises(ContractViolation):
        ext.enrich(PlainLedger(), [], "ctx", today="d",  # type: ignore[arg-type]
                   reconciled_channels=frozenset())
    with pytest.raises(ContractViolation):
        ext.enrich(PlainLedger(), [], make_ctx(), today="d", reconciled_channels=frozenset(),
                   recon_decisions=[("convention:x", "excl")])  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        ext.enrich(PlainLedger(), [], make_ctx(), today="d", reconciled_channels=frozenset(),
                   recon_decisions=(("convention:x",),))  # type: ignore[arg-type]


def test_enrich_without_enrichers_returns_ctx(install: Install) -> None:
    install(bare_spec("aaa"))
    ctx = make_ctx()
    assert ext.enrich(PlainLedger(), [], ctx, today="d", reconciled_channels=frozenset()) is ctx


def test_summarize(install: Install) -> None:
    install(fake_spec(), fake_spec(name="quiet", summary_builder=f"{HOOKS}:summarize_none"),
            bare_spec("aaa"))
    store, stores, ctx = PlainLedger(), [hooks.FakeRecordStore()], make_ctx()
    pricer = FlatRates()
    out = ext.summarize(store, stores, ctx, [], None, pricer, today="2026-09-24", k=7, notes=[])
    assert out == {"fake": {"summary": "fake", "k": 7}}
    (args, kw), = hooks.calls("summary_builder")
    assert args == (store, stores, ctx, [], None, pricer) and kw == {"today": "2026-09-24", "k": 7}


# ---------- outputs ----------


def test_render_sections_three_formats(install: Install) -> None:
    install(fake_spec(), fake_spec(name="empty", section_renderer=f"{HOOKS}:EmptySection"),
            bare_spec("aaa"))
    result = make_run_result("scan")
    assert ext.render_sections(result, "terminal", width=4, notes=[]) == ["FAKE"]
    assert hooks.calls("section_renderer.terminal") == [((result,), {"width": 4})]
    assert ext.render_sections(result, "terminal") == ["FAKE SECTION"]  # default width 100
    assert ext.render_sections(result, "html") == ['<section id="fake"></section>']
    assert ext.render_sections(result, "json") == [{"fake": {"lines": [], "command": "scan"}}]


def test_render_sections_contract_checks(install: Install) -> None:
    install(fake_spec())
    with pytest.raises(UsageError):
        ext.render_sections(make_run_result(), "pdf")
    install(fake_spec(section_renderer=f"{HOOKS}:NotASection"))
    with pytest.raises(ContractViolation):
        ext.render_sections(make_run_result(), "terminal")
    install(fake_spec(section_renderer=f"{HOOKS}:BadSection"))
    for fmt in ext.SECTION_FORMATS:
        with pytest.raises(ContractViolation):
            ext.render_sections(make_run_result(), fmt)


def test_focus_rows_owns_channels_without_rows(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa", channels=("aaa_ch",)))
    store, stores = PlainLedger(), [hooks.FakeRecordStore()]
    rows, owned = ext.focus_rows(store, stores, since_ms=1, until_ms=2,
                                 reconciled_channels=frozenset({"fake_a"}), k=5,
                                 allow_unreconciled=False, role="primary", notes=[])
    assert [r.channel for r in rows] == ["fake_a"] and all(isinstance(r, FocusRow) for r in rows)
    assert rows[0].reconciled is True
    assert owned == frozenset({"fake_a", "fake_b"})  # fake_b has no row; aaa has no focus hook
    (args, kw), = hooks.calls("focus_rows")
    assert args == (store, stores)
    assert kw == {"since_ms": 1, "until_ms": 2, "reconciled_channels": frozenset({"fake_a"}),
                  "k": 5, "allow_unreconciled": False, "role": "primary"}


def test_focus_rows_contract_checks(install: Install) -> None:
    kw = {"since_ms": 1, "until_ms": 2, "reconciled_channels": frozenset(), "k": 5,
          "allow_unreconciled": True, "role": "enrichment"}
    install(fake_spec(focus_rows=f"{HOOKS}:focus_rows_foreign"))
    with pytest.raises(ContractViolation):
        ext.focus_rows(PlainLedger(), [], **kw)  # type: ignore[arg-type]
    install(fake_spec(focus_rows=f"{HOOKS}:focus_rows_wrong_type"))
    with pytest.raises(ContractViolation):
        ext.focus_rows(PlainLedger(), [], **kw)  # type: ignore[arg-type]


def test_showback(install: Install, tmp_path: Path) -> None:
    install(fake_spec(), bare_spec("aaa"))
    result = make_run_result()
    paths = ext.showback(result, tmp_path, ("html", "csv"), notes=[])
    assert paths == [tmp_path / "fake-showback.html", tmp_path / "fake-showback.csv"]
    assert all(p.is_file() for p in paths)
    assert hooks.calls("showback") == [((result, tmp_path, ("html", "csv")), {})]
    install(fake_spec(showback=f"{HOOKS}:showback_wrong_type"))
    with pytest.raises(ContractViolation):
        ext.showback(result, tmp_path, ("html",))


# ---------- policy, panels ----------


def test_policy_packs_dispatch(install: Install, tmp_path: Path) -> None:
    install(fake_spec(), bare_spec("aaa"))
    store, stores, ctx, result = PlainLedger(), [hooks.FakeRecordStore()], make_ctx(), \
        make_run_result()
    packs = ext.policy_packs("fake-target", store, stores, ctx, [], result, out_dir=tmp_path,
                             current={"model": "auto"}, cohort_by="team",
                             include_tradeoffs=True, notes=[])
    assert [p.target for p in packs] == ["fake-target"]
    assert all(isinstance(p, PolicyPack) for p in packs)
    (args, kw), = hooks.calls("policy_targets")
    assert args == (store, stores, ctx, [], result)
    assert kw == {"out_dir": tmp_path, "current": {"model": "auto"}, "cohort_by": "team",
                  "include_tradeoffs": True}


def test_policy_packs_unknown_target_and_type_check(install: Install) -> None:
    install(fake_spec())
    with pytest.raises(UsageError):
        ext.policy_packs("claude-code", PlainLedger(), [], make_ctx(), [], None, out_dir=None,
                         current=None, cohort_by="team", include_tradeoffs=False)
    install(fake_spec(policy_targets=(("fake-target", f"{HOOKS}:policy_packs_wrong_type"),)))
    with pytest.raises(ContractViolation):
        ext.policy_packs("fake-target", PlainLedger(), [], make_ctx(), [], None, out_dir=None,
                         current=None, cohort_by="team", include_tradeoffs=False)


def test_panel_dispatch(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa"))
    store, stores = PlainLedger(), [hooks.FakeRecordStore()]
    rows = ext.panel("fake", store, stores, notes=[], cluster_kind="team", since="2026-09-01",
                     until="2026-10-01", arms=("control", "auto"))
    assert [r.arm for r in rows] == ["control", "auto"]
    assert all(isinstance(r, PanelRow) for r in rows)
    (args, kw), = hooks.calls("panel_builder")
    assert args == (store, stores)
    assert kw == {"cluster_kind": "team", "since": "2026-09-01", "until": "2026-10-01",
                  "arms": ("control", "auto")}


def test_panel_errors(install: Install) -> None:
    install(fake_spec(), bare_spec("aaa"))
    with pytest.raises(UsageError):
        ext.panel("nope", PlainLedger(), [])
    with pytest.raises(UsageError):  # declared no panel
        ext.panel("aaa", PlainLedger(), [])
    install(fake_spec(panel_builder=f"{HOOKS}:panel_wrong_type"))
    with pytest.raises(ContractViolation):
        ext.panel("fake", PlainLedger(), [])


# ---------- a full pass through every hook of the fake extension ----------


def test_every_hook_is_reached_once(install: Install, tmp_path: Path) -> None:
    install(fake_spec())
    notes: list[DataQualityNote] = []
    store = PlainLedger()
    stores = ext.open_record_stores(tmp_path / "db", create=True, notes=notes)
    ext.persist(stores, ingest_result(licenses=(make_license(),)), notes=notes)
    reports = ext.run_reconcilers(store, stores, FlatRates(), since_ms=0, until_ms=1,
                                  tolerance_pct="0.5", unexplained_pct="1.0", closed_only=False,
                                  today="2026-09-24", notes=notes)
    ctx = ext.enrich(store, stores, make_ctx(), today="2026-09-24",
                     reconciled_channels=frozenset({"fake_a"}),
                     recon_decisions=ext.recon_decisions_of(reports), notes=notes)
    summary = ext.summarize(store, stores, ctx, [], None, FlatRates(), today="2026-09-24", k=5,
                            notes=notes)
    result = dataclasses.replace(make_run_result(), notes=tuple(sorted(summary)))
    ext.render_sections(result, "terminal", notes=notes)
    ext.focus_rows(store, stores, since_ms=0, until_ms=1, reconciled_channels=frozenset(), k=5,
                   allow_unreconciled=False, role="primary", notes=notes)
    ext.showback(result, tmp_path, ("json",), notes=notes)
    ext.policy_packs("fake-target", store, stores, ctx, [], result, out_dir=tmp_path,
                     current=None, cohort_by="team", include_tradeoffs=False, notes=notes)
    ext.panel("fake", store, stores, notes=notes)
    assert notes == []
    reached = [hook for hook, _args, _kw in hooks.CALLS]
    assert reached == ["record_store", "put", "reconciler", "context_enricher",
                       "summary_builder", "section_renderer.terminal", "focus_rows", "showback",
                       "policy_targets", "panel_builder"]
    assert ctx.recon_decisions == hooks.DECISIONS and "ext:fake" in ctx.capabilities


# ---------- the Copilot spec as shipped (pure reads only) ----------


def test_copilot_spec_hooks_are_declared() -> None:
    assert COPILOT.channels == ("github_copilot", "github_actions", "github_sandbox")
    assert dict(COPILOT.policy_targets) == {
        "github-copilot": "tokenbill.pipeline.copilot:policy_packs"}
    assert COPILOT.command_module == "tokenbill.commands.copilot"


def test_record_store_values_are_not_mutated() -> None:
    # persist hands the same result object to every store; the host never copies or edits it
    store = hooks.FakeRecordStore()
    lic = make_license()
    result = ingest_result(licenses=(lic,))
    ext.persist([store], result)
    assert result.licenses == [lic]
