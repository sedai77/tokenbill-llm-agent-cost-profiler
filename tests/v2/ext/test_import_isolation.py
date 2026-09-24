"""Importing ``tokenbill.core.extensions`` imports no wave-2 module (and none of the other wave-1.5b
core modules): every hook is resolved lazily. Checked in a fresh interpreter (``sys.modules``)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

import pytest

from tokenbill.core import extensions as ext

from .support import PKG, fake_spec

Install = Callable[..., None]

_PROBE = """
import json, sys
before = set(sys.modules)
import tokenbill.core.extensions as ext
after_import = sorted(m for m in set(sys.modules) - before if m.startswith("tokenbill"))
ext.extensions(); ext.delegated_channels(); ext.rewrite_argv(["scan", "--copilot"])
after_pure = sorted(m for m in set(sys.modules) - before if m.startswith("tokenbill"))
notes = []
listed = [ext.command_modules(notes=notes), ext.policy_targets(notes=notes)]
after_listing = sorted(m for m in set(sys.modules) - before if m.startswith("tokenbill"))
print(json.dumps({"import": after_import, "pure": after_pure, "listing": after_listing}))
"""

#: The Copilot hook modules of the shipped ExtensionSpec (wave 2; absent or present).
_HOOK_MODULES = ("tokenbill.copilot.rates_verify", "tokenbill.copilot.recon",
                 "tokenbill.copilot.record_store", "tokenbill.copilot.enrich",
                 "tokenbill.pipeline.copilot", "tokenbill.copilot.render",
                 "tokenbill.copilot.focus", "tokenbill.copilot.showback",
                 "tokenbill.copilot.panel", "tokenbill.commands.copilot",
                 "tokenbill.copilot.data")
#: Other wave-1.5b core modules and the fakes: resolved lazily by the host, never imported.
_LAZY_CORE = ("tokenbill.core.pool", "tokenbill.core.kanon", "tokenbill.core.catalog",
              "tokenbill.core.findings", "tokenbill.core.testing")


def _probe() -> dict[str, list[str]]:
    out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                         check=True, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_import_loads_only_core_modules() -> None:
    seen = _probe()
    allowed_prefixes = ("tokenbill.core.",)
    allowed = {"tokenbill", "tokenbill.common", "tokenbill.core"}
    for phase in ("import", "pure"):
        wave2 = [m for m in seen[phase] if m not in allowed and not m.startswith(allowed_prefixes)]
        assert wave2 == [], (phase, wave2)
        assert not set(seen[phase]) & set(_LAZY_CORE), phase
    assert "tokenbill.core.extensions" in seen["import"]
    assert seen["pure"] == seen["import"]  # the pure readers import nothing
    # listing the command modules and policy targets only locates them (parent packages may be
    # imported by find_spec; the hook modules themselves never are)
    assert not set(seen["listing"]) & set(_HOOK_MODULES)
    extra = set(seen["listing"]) - set(seen["import"])
    assert extra <= {"tokenbill.commands", "tokenbill.pipeline", "tokenbill.copilot"}, extra


def test_listing_does_not_import_the_fake_command_module(install: Install,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"{PKG}.commands"
    monkeypatch.delitem(sys.modules, name, raising=False)
    install(fake_spec())
    assert ext.command_modules() == {"fake": name}
    assert name not in sys.modules
