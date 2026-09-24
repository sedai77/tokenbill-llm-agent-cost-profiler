"""CORE-AMENDMENTS S-5 / ruling R-E18: convention loading skips a missing ``CONVENTION_MODULES``
entry with a ``dq.convention_module_unavailable`` note surfaced by ``conventions.load_notes()``."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

from tokenbill.core import conventions as conv
from tokenbill.core import registry
from tokenbill.core.errors import UsageError
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import DataQualityNote

MISSING = "tokenbill.adapters.no_such_copilot_module_semc"


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A private registry, an unloaded extension state and a temp dir on ``sys.path``."""
    monkeypatch.setattr(conv, "_REGISTRY", dict(conv._REGISTRY))
    monkeypatch.setattr(conv, "_EXTENSIONS_LOADED", False)
    monkeypatch.setattr(conv, "_LOAD_NOTES", ())
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []

    def module(name: str, body: str) -> str:
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
        created.append(name)
        return name

    yield module
    for name in created:
        sys.modules.pop(name, None)


FAKE = """
    from tokenbill.core.conventions import Convention, register_convention
    from tokenbill.core.records import UsageBuckets
    register_convention(Convention("semc.fake", "github", True, True, "fake"),
                        lambda raw: (UsageBuckets(uncached_input=int(raw["n"])), []))
"""


def test_a_missing_module_is_skipped_and_noted(fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fresh("tb_semc_fake_ext", FAKE)
    monkeypatch.setattr(registry, "CONVENTION_MODULES", (MISSING, fake))
    assert conv.load_notes() == ()                      # nothing loaded yet: nothing to report
    assert conv.normalize("semc.fake", {"n": 3}) == (UsageBuckets(uncached_input=3), [])
    notes = conv.load_notes()
    assert notes == (DataQualityNote(code="dq.convention_module_unavailable", severity="warn",
                                     count=1, detail=MISSING),)
    assert conv.DQ_CONVENTION_MODULE_UNAVAILABLE == "dq.convention_module_unavailable"
    assert isinstance(notes, tuple)
    # the built-in convention is unaffected and loading happens once
    assert conv.normalize("anthropic.messages", {"input_tokens": 1})[0].uncached_input == 1
    with pytest.raises(UsageError, match="unknown convention"):
        conv.get_convention("semc.never.registered")
    assert conv.load_notes() == notes


def test_every_missing_entry_gets_one_note_in_registry_order(
        fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fresh("tb_semc_fake_ext2", FAKE.replace("semc.fake", "semc.fake2"))
    missing_pkg = "tb_semc_missing_package.submodule"
    monkeypatch.setattr(registry, "CONVENTION_MODULES", (missing_pkg, fake, MISSING))
    assert conv.get_convention("semc.fake2").provider == "github"
    assert [n.detail for n in conv.load_notes()] == [missing_pkg, MISSING]
    assert all(n.code == "dq.convention_module_unavailable" and n.count == 1
               for n in conv.load_notes())


def test_a_broken_module_still_propagates_and_keeps_earlier_notes(
        fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = fresh("tb_semc_broken_ext", "import tb_semc_missing_dependency\n")
    monkeypatch.setattr(registry, "CONVENTION_MODULES", (MISSING, broken))
    with pytest.raises(ModuleNotFoundError):
        conv.get_convention("semc.whatever")
    assert [n.detail for n in conv.load_notes()] == [MISSING]


def test_no_notes_when_every_module_imports(fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fresh("tb_semc_fake_ext3", FAKE.replace("semc.fake", "semc.fake3"))
    monkeypatch.setattr(registry, "CONVENTION_MODULES", (fake,))
    monkeypatch.setattr(conv, "_LOAD_NOTES", (DataQualityNote("dq.stale", "warn", 1, "x"),))
    assert conv.get_convention("semc.fake3").enabled
    assert conv.load_notes() == ()                      # a new load replaces older notes


def test_known_ids_never_load_extensions(fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "CONVENTION_MODULES", (MISSING,))
    conv.normalize("anthropic.messages", {"output_tokens": 2})
    assert conv._EXTENSIONS_LOADED is False and conv.load_notes() == ()


def test_the_real_registry_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the real ``CONVENTION_MODULES``, exactly the entries not installed in this tree are
    noted (the Copilot modules land in wave 2; the test adapts as they are merged). The real
    registry is used, so the installed modules' registrations persist as in production."""
    monkeypatch.setattr(conv, "_EXTENSIONS_LOADED", False)
    monkeypatch.setattr(conv, "_LOAD_NOTES", ())

    def installed(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            return False

    expected = [m for m in registry.CONVENTION_MODULES if not installed(m)]
    with pytest.raises(UsageError):
        conv.get_convention("semc.not-registered-anywhere")
    assert [n.detail for n in conv.load_notes()] == expected
