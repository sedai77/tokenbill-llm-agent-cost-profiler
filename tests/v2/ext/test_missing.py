"""Ruling R-E18 in the host: a missing extension module, attribute or resource yields exactly one
``dq.extension_unavailable`` note per hook and a normal return; nothing else is swallowed."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core import extensions as ext
from tokenbill.core.builders import FlatRates, make_license
from tokenbill.core.errors import ContractViolation
from tokenbill.core.types import DataQualityNote

from .support import (
    ABSENT,
    ALL_HOOKS,
    HOOKS,
    PKG,
    PlainLedger,
    fake_spec,
    ghost_spec,
    ingest_result,
    make_ctx,
    make_run_result,
)

Install = Callable[..., None]


def _call_every_function(notes: list[DataQualityNote] | None, tmp_path: Path,
                         target: str = "ghost-target", panel_name: str = "ghost"
                         ) -> dict[str, Any]:
    """Call every host function that can resolve an extension module; returns the results."""
    ledger, ctx, result = PlainLedger(), make_ctx(), make_run_result()
    out: dict[str, Any] = {}
    out["rate_files"] = ext.extension_rate_files(notes=notes)
    out["rate_verifier"] = ext.rate_verifiers(notes=notes)
    out["command_module"] = ext.command_modules(notes=notes)
    out["policy_targets"] = ext.policy_targets(notes=notes)
    out["record_store"] = ext.open_record_stores(tmp_path / "db", create=True, notes=notes)
    out["reconciler"] = ext.run_reconcilers(
        ledger, [], FlatRates(), since_ms=0, until_ms=1, tolerance_pct="0.5",
        unexplained_pct="1.0", closed_only=False, today="2026-09-24", notes=notes)
    out["context_enricher"] = ext.enrich(ledger, [], ctx, today="2026-09-24",
                                         reconciled_channels=frozenset(), notes=notes)
    out["summary_builder"] = ext.summarize(ledger, [], ctx, [], None, FlatRates(),
                                           today="2026-09-24", k=5, notes=notes)
    out["section_renderer"] = [ext.render_sections(result, fmt, notes=notes)
                               for fmt in ext.SECTION_FORMATS]
    out["focus_rows"] = ext.focus_rows(ledger, [], since_ms=0, until_ms=1,
                                       reconciled_channels=frozenset(), k=5,
                                       allow_unreconciled=False, role="primary", notes=notes)
    out["showback"] = ext.showback(result, tmp_path, ("html",), notes=notes)
    out["policy_packs"] = ext.policy_packs(target, ledger, [], ctx, [], result, out_dir=None,
                                           current=None, cohort_by="team",
                                           include_tradeoffs=False, notes=notes)
    out["panel_builder"] = ext.panel(panel_name, ledger, [], notes=notes)
    out["ctx"] = ctx
    return out


def test_every_hook_missing_gives_one_note_each_and_normal_returns(install: Install,
                                                                   tmp_path: Path) -> None:
    install(ghost_spec())
    notes: list[DataQualityNote] = []
    out = _call_every_function(notes, tmp_path)
    assert out["rate_files"] == () and out["rate_verifier"] == []
    assert out["command_module"] == {} and out["policy_targets"] == {}
    assert out["record_store"] == [] and out["reconciler"] == []
    assert out["context_enricher"] is out["ctx"] and out["summary_builder"] == {}
    assert out["section_renderer"] == [[], [], []]
    assert out["focus_rows"] == ([], frozenset({"ghost_ch"}))  # channels stay owned
    assert out["showback"] == [] and out["policy_packs"] == [] and out["panel_builder"] == []
    assert all(n.code == "dq.extension_unavailable" and n.severity == "warn" and n.count == 1
               for n in notes)
    details = [n.detail for n in notes]
    # section_renderer is called three times (three formats) and policy_targets twice (listing +
    # packs): one note per hook per *call*
    expected = [f"ghost:{h}" for h in ("rate_files", "rate_verifier", "command_module",
                                       "policy_targets", "record_store", "reconciler",
                                       "context_enricher", "summary_builder")]
    expected += ["ghost:section_renderer"] * 3
    expected += ["ghost:focus_rows", "ghost:showback", "ghost:policy_targets",
                 "ghost:panel_builder"]
    assert details == expected
    assert {d.split(":")[1] for d in details} == set(ALL_HOOKS)


def test_one_note_per_hook_even_with_several_entries(install: Install) -> None:
    install(fake_spec(name="ghost", channels=("ghost_ch",),
                      rate_files=(f"{ABSENT}:a.json", f"{ABSENT}:b.json", f"{PKG}:nope.json"),
                      policy_targets=(("t1", f"{ABSENT}:x"), ("t2", f"{ABSENT}:y"))))
    notes: list[DataQualityNote] = []
    assert ext.extension_rate_files(notes=notes) == ()
    assert ext.policy_targets(notes=notes) == {}
    assert [n.detail for n in notes] == ["ghost:rate_files", "ghost:policy_targets"]


def test_missing_extension_does_not_hide_a_present_one(install: Install, tmp_path: Path) -> None:
    install(fake_spec(), ghost_spec())
    notes: list[DataQualityNote] = []
    stores = ext.open_record_stores(tmp_path / "db", create=True, notes=notes)
    assert [s.name for s in stores] == ["fake"]
    rows, owned = ext.focus_rows(PlainLedger(), stores, since_ms=0, until_ms=1,
                                 reconciled_channels=frozenset(), k=5,
                                 allow_unreconciled=False, role="primary", notes=notes)
    assert [r.channel for r in rows] == ["fake_a"]
    assert owned == frozenset({"fake_a", "fake_b", "ghost_ch"})
    assert [n.detail for n in notes] == ["ghost:record_store", "ghost:focus_rows"]


def test_missing_resource_in_an_existing_package(install: Install) -> None:
    install(fake_spec(rate_files=(f"{PKG}:no_such_rates.json", f"{PKG}:fake_rates.json")))
    notes: list[DataQualityNote] = []
    files = ext.extension_rate_files(notes=notes)
    assert [f.name for f in files] == ["fake_rates.json"]
    assert [n.detail for n in notes] == ["fake:rate_files"]


def test_resource_that_is_a_directory_is_missing(install: Install) -> None:
    parent = PKG.rsplit(".", 1)[0]  # the test area package; fake_ext is a directory inside it
    install(fake_spec(rate_files=(f"{parent}:fake_ext",)))
    notes: list[DataQualityNote] = []
    assert ext.extension_rate_files(notes=notes) == ()
    assert [n.detail for n in notes] == ["fake:rate_files"]


def test_missing_attribute_in_an_existing_module(install: Install, tmp_path: Path) -> None:
    # the module exists (the fake hooks), none of the ghost_* attributes do; the rate file is a
    # missing resource of the existing fake package
    install(ghost_spec(module=HOOKS, rate_package=PKG))
    notes: list[DataQualityNote] = []
    out = _call_every_function(notes, tmp_path)
    assert out["reconciler"] == [] and out["record_store"] == [] and out["panel_builder"] == []
    assert out["rate_verifier"] == []  # resolved attribute by attribute
    # the CLI listings only locate the module, find it present and list the entries
    assert out["command_module"] == {"ghost": HOOKS}
    assert out["policy_targets"] == {"ghost-target": f"{HOOKS}:ghost_policy_packs"}
    details = [n.detail for n in notes]
    assert len(details) == len(set(details)) + 2  # section_renderer noted once per format call
    assert set(details) == {f"ghost:{h}" for h in ALL_HOOKS} - {"ghost:command_module"}


def test_rate_file_anchor_must_be_a_package(install: Install) -> None:
    install(fake_spec(rate_files=(f"{HOOKS}:fake_rates.json",)))
    with pytest.raises(ContractViolation):
        ext.extension_rate_files(notes=[])


def test_missing_parent_package(install: Install) -> None:
    install(fake_spec(command_module="tokenbill_absent_pkg.sub.commands",
                      rate_verifier="tokenbill_absent_pkg.rates:verify",
                      rate_files=("tokenbill_absent_pkg.data:rates.json",)))
    notes: list[DataQualityNote] = []
    assert ext.command_modules(notes=notes) == {}
    assert ext.rate_verifiers(notes=notes) == []
    assert ext.extension_rate_files(notes=notes) == ()
    assert [n.detail for n in notes] == ["fake:command_module", "fake:rate_verifier",
                                         "fake:rate_files"]


def test_module_injected_without_spec_counts_as_present(install: Install,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    name = f"{PKG}.injected_commands"
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    install(fake_spec(command_module=name))
    notes: list[DataQualityNote] = []
    assert ext.command_modules(notes=notes) == {"fake": name} and notes == []


def test_notes_none_still_returns_normally_and_logs(install: Install, tmp_path: Path,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    install(ghost_spec())
    with caplog.at_level(logging.INFO, logger="tokenbill.core.extensions"):
        out = _call_every_function(None, tmp_path)
    assert out["reconciler"] == []
    assert any("ghost:reconciler" in r.getMessage() for r in caplog.records)


def test_notes_must_be_a_list(install: Install) -> None:
    install(ghost_spec())
    with pytest.raises(ContractViolation):
        ext.extension_rate_files(notes=())  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        ext.retain([], identity_before_ms=0, notes="x")  # type: ignore[arg-type]


def test_other_import_time_errors_propagate(install: Install) -> None:
    install(fake_spec(reconciler=f"{PKG}.broken:reconcile"))
    with pytest.raises(RuntimeError):
        ext.run_reconcilers(PlainLedger(), [], FlatRates(), since_ms=0, until_ms=1,
                            tolerance_pct="0.5", unexplained_pct="1.0", closed_only=False,
                            today="2026-09-24", notes=[])
    assert f"{PKG}.broken" not in sys.modules


def test_import_error_raised_by_a_running_hook_propagates(install: Install,
                                                          tmp_path: Path) -> None:
    install(fake_spec(context_enricher=f"{HOOKS}:raises_import_error",
                      showback=f"{HOOKS}:raises_import_error"))
    notes: list[DataQualityNote] = []
    with pytest.raises(ImportError):
        ext.enrich(PlainLedger(), [], make_ctx(), today="d", reconciled_channels=frozenset(),
                   notes=notes)
    with pytest.raises(ImportError):
        ext.showback(make_run_result(), tmp_path, ("html",), notes=notes)
    assert notes == []


@pytest.mark.parametrize("hook", ["reconciler", "summary_builder", "focus_rows",
                                  "policy_targets", "panel_builder"])
def test_hook_exceptions_propagate(install: Install, tmp_path: Path, hook: str) -> None:
    failing = f"{HOOKS}:raises_value_error"
    overrides: dict[str, Any] = {hook: failing}
    if hook == "policy_targets":
        overrides = {"policy_targets": (("fake-target", failing),)}
    install(fake_spec(**overrides))
    notes: list[DataQualityNote] = []
    with pytest.raises(ValueError):
        _call_every_function(notes, tmp_path, target="fake-target", panel_name="fake")
    assert notes == []


def test_persist_without_record_stores_is_a_noop() -> None:
    # with the record store module missing, open_record_stores returned [] (and noted it once)
    assert ext.persist([], ingest_result(licenses=(make_license(),))) == {}
