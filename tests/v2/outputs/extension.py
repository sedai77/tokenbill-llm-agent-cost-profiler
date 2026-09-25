"""A test-only ``copilot`` section renderer registered through ``core.registry.EXTENSIONS`` (the
F-EXT host resolves it by dotted path), plus the minimal ``CopilotSummary`` that fills the slot."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core import registry
from tokenbill.core.labels import Basis, exact, figure_json
from tokenbill.core.records import COPILOT_CHANNELS
from tokenbill.core.registry import ExtensionSpec
from tokenbill.core.types import CopilotSummary, RunResult

MODULE = __name__


def summary() -> CopilotSummary:
    return CopilotSummary(window=("2026-09-01", "2026-10-01"), lines=(), pools=(), teams=None,
                          seat_counts=(), plan=None, actions=(), channel_verdicts=())


class FakeSection:
    """Implements ``core.protocols.SectionRenderer``."""

    name = "copilot"
    json_doc: dict[str, Any] | None = {
        "pools": [], "invoice": figure_json(exact(1_000_000_000, Basis.INVOICE)),
        "evidence": "exact"}
    html_text = '<section id="copilot"><h2>Copilot</h2><p>seats 12</p></section>'

    def terminal(self, result: RunResult, *, width: int) -> str:
        return "COPILOT BILL\n  seats 12 · credits 3,400"

    def html(self, result: RunResult) -> str:
        return type(self).html_text

    def json(self, result: RunResult) -> dict | None:
        return type(self).json_doc


def spec(renderer: str | None = None) -> ExtensionSpec:
    return ExtensionSpec(
        name="copilot", channels=COPILOT_CHANNELS, rate_files=(), rate_verifier=None,
        reconciler=None, record_store=None, context_enricher=None, summary_builder=None,
        section_renderer=renderer if renderer is not None else f"{MODULE}:FakeSection",
        focus_rows=None, showback=None, policy_targets=(), panel_builder=None,
        command_module=None)


def install(monkeypatch: pytest.MonkeyPatch, renderer: str | None = None) -> None:
    """Replace the registered extensions with the fake ``copilot`` renderer for one test."""
    monkeypatch.setattr(registry, "EXTENSIONS", {"copilot": spec(renderer)})
