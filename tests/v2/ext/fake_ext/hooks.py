"""Hooks of the test-only ``fake`` extension, one per ``ExtensionSpec`` field. Every hook appends
``(hook, args, kwargs)`` to :data:`CALLS` so the host tests can assert the documented arguments;
the return values are fixed, documented below, and built only from core types."""

from __future__ import annotations

import dataclasses
import datetime as _dt
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.labels import Finality
from tokenbill.core.records import ActivityDay, ConfigSnapshot, LicenseSnapshot
from tokenbill.core.types import (
    AnalysisContext,
    FocusRow,
    IngestResult,
    PanelRow,
    PolicyPack,
    ReconciliationReport,
    RunResult,
)

#: (hook, positional args, keyword args) of every call, in call order (cleared by the fixtures).
CALLS: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

#: Decisions the fake reconciler reports (C-17 key shapes).
DECISIONS: tuple[tuple[str, str], ...] = (
    ("convention:src_fake_1", "excl"),
    ("gross_is_list:enterprise:2026-09", "true"),
)
FAKE_CHANNELS = ("fake_a", "fake_b")   # the fake extension's channels; rows only on fake_a
_DAY_MS = 86_400_000


def _record(hook: str, *args: Any, **kw: Any) -> None:
    CALLS.append((hook, args, kw))


def calls(hook: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """The recorded ``(args, kwargs)`` of *hook*."""
    return [(a, k) for h, a, k in CALLS if h == hook]


def make_report(decisions: tuple[tuple[str, str], ...] = DECISIONS) -> ReconciliationReport:
    """A minimal reconciliation report carrying *decisions*."""
    return ReconciliationReport(
        window=("2026-09-01", "2026-10-01"), tolerance_pct="0.5", unexplained_tolerance_pct="1.0",
        rows=(), token_coverage_pct=None, dollar_coverage_pct=None, rate_card_error=None,
        over_count_rows=0, effective_discount=(), residuals=(), unexplained_nano=0, channels=(),
        verdict="insufficient_data", finality=Finality.PROVISIONAL, suggested_contract=None,
        rerun_verdict=None, decisions=decisions)


# ---------- rate_verifier ----------


def verify(layer: Any, *, snapshot: Path | None = None, live: bool = False,
           opener: Any = None) -> list[Any]:
    """Rate verifier (listed by ``rate_verifiers``; never called by the host)."""
    _record("rate_verifier", layer, snapshot=snapshot, live=live, opener=opener)
    return []


# ---------- reconciler ----------


def reconcile(ledger: Any, record_stores: Sequence[Any], pricer: Any, *, since_ms: int,
              until_ms: int, tolerance_pct: str, unexplained_pct: str, closed_only: bool,
              today: str, rounding_remainders: Mapping[str, Decimal] | None = None
              ) -> ReconciliationReport:
    """``ChannelReconciler``: returns :func:`make_report` (decisions :data:`DECISIONS`)."""
    _record("reconciler", ledger, record_stores, pricer, since_ms=since_ms, until_ms=until_ms,
            tolerance_pct=tolerance_pct, unexplained_pct=unexplained_pct,
            closed_only=closed_only, today=today, rounding_remainders=rounding_remainders)
    return make_report()


def reconcile_wrong_type(*args: Any, **kw: Any) -> object:
    """A reconciler returning something that is not a report."""
    return {"verdict": "reconciled"}


# ---------- record_store ----------


def _date_ms(date_utc: str) -> int:
    return (_dt.date.fromisoformat(date_utc) - _dt.date(1970, 1, 1)).days * _DAY_MS


def _in(ms: int, window: Mapping[str, int]) -> bool:
    lo = window.get("since_ms", 0)
    hi = window.get("until_ms", 2**62)
    return lo <= ms < hi


class FakeRecordStore:
    """In-memory ``ExtRecordStore`` (name ``fake``, or the *name* given)."""

    def __init__(self, db_path: Path | None = None, *, create: bool = True,
                 name: str = "fake") -> None:
        _record("record_store", db_path, create=create)
        self.name = name
        self.db_path = db_path
        self._licenses: list[LicenseSnapshot] = []
        self._activity: list[ActivityDay] = []
        self._config: list[ConfigSnapshot] = []

    def put(self, result: IngestResult, *, principal_key_id: str | None) -> dict[str, int]:
        """Keep every license / activity day / config snapshot; counts per kind."""
        _record("put", result, principal_key_id=principal_key_id)
        self._licenses.extend(result.licenses)
        self._activity.extend(result.activity)
        self._config.extend(result.config)
        return {"licenses": len(result.licenses), "activity": len(result.activity),
                "config": len(result.config)}

    def licenses(self, **window: int) -> list[LicenseSnapshot]:
        """Licenses whose snapshot date starts in the window."""
        return [r for r in self._licenses if _in(_date_ms(r.snapshot_date), window)]

    def activity(self, **window: int) -> list[ActivityDay]:
        """Activity days starting in the window."""
        return [r for r in self._activity if _in(_date_ms(r.date_utc), window)]

    def config(self, **window: int) -> list[ConfigSnapshot]:
        """Config snapshots taken in the window."""
        return [r for r in self._config if _in(r.snapshot_ms, window)]

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str) -> int:
        """Distinct principals of the licenses (or activity days) in the window."""
        rows: Sequence[Any] = self.licenses(since_ms=since_ms, until_ms=until_ms) if (
            source == "licenses") else self.activity(since_ms=since_ms, until_ms=until_ms)
        return len({r.principal for r in rows})

    def retain(self, *, identity_before_ms: int) -> int:
        """Drop licenses and activity days before the cut-off; returns 2 (fixed, for sums)."""
        _record("retain", identity_before_ms=identity_before_ms)
        return 2

    def purge(self, *, principal: str | None, before_ms: int | None, actor: str) -> int:
        """Returns 3 (fixed, for sums)."""
        _record("purge", principal=principal, before_ms=before_ms, actor=actor)
        return 3


class NotARecordStore:
    """A ``record_store`` class whose instances do not implement ``ExtRecordStore``."""

    def __init__(self, db_path: Path | None = None, *, create: bool = True) -> None:
        self.db_path = db_path


# ---------- context_enricher ----------


def enrich(store: Any, record_stores: Sequence[Any], ctx: AnalysisContext, *, today: str,
           reconciled_channels: frozenset[str],
           recon_decisions: tuple[tuple[str, str], ...] = ()) -> AnalysisContext:
    """Copies the reconciled channels and decisions into the context and adds ``ext:fake``."""
    _record("context_enricher", store, record_stores, ctx, today=today,
            reconciled_channels=reconciled_channels, recon_decisions=recon_decisions)
    return dataclasses.replace(ctx, reconciled_channels=reconciled_channels,
                               recon_decisions=recon_decisions,
                               capabilities=ctx.capabilities | {"ext:fake"})


def enrich_wrong_type(*args: Any, **kw: Any) -> object:
    """An enricher returning something that is not a context."""
    return "not a context"


# ---------- summary_builder ----------


def summarize(store: Any, record_stores: Sequence[Any], ctx: AnalysisContext,
              findings: Sequence[Any], plan: Any, pricer: Any, *, today: str,
              k: int) -> dict[str, object]:
    """Returns ``{"summary": "fake", "k": k}``."""
    _record("summary_builder", store, record_stores, ctx, findings, plan, pricer, today=today,
            k=k)
    return {"summary": "fake", "k": k}


def summarize_none(*args: Any, **kw: Any) -> None:
    """A summary builder with nothing to say."""
    return None


# ---------- section_renderer ----------


class FakeSection:
    """``SectionRenderer`` of the fake extension."""

    name = "fake"

    def terminal(self, result: RunResult, *, width: int) -> str:
        _record("section_renderer.terminal", result, width=width)
        return "FAKE SECTION"[:width]

    def html(self, result: RunResult) -> str:
        _record("section_renderer.html", result)
        return '<section id="fake"></section>'

    def json(self, result: RunResult) -> dict | None:
        _record("section_renderer.json", result)
        return {"lines": [], "command": result.command}


class EmptySection:
    """A renderer with nothing to show (empty text, no JSON object)."""

    name = "empty"

    def terminal(self, result: RunResult, *, width: int) -> str:
        return ""

    def html(self, result: RunResult) -> str:
        return ""

    def json(self, result: RunResult) -> dict | None:
        return None


class BadSection:
    """A renderer whose methods return the wrong types."""

    name = "bad"

    def terminal(self, result: RunResult, *, width: int) -> str:
        return 42  # type: ignore[return-value]

    def html(self, result: RunResult) -> str:
        return 42  # type: ignore[return-value]

    def json(self, result: RunResult) -> dict | None:
        return ["not", "a", "dict"]  # type: ignore[return-value]


class NotASection:
    """Missing the ``SectionRenderer`` methods."""


# ---------- focus_rows ----------


def focus_rows(store: Any, record_stores: Sequence[Any], *, since_ms: int, until_ms: int,
               reconciled_channels: frozenset[str], k: int, allow_unreconciled: bool,
               role: str) -> Iterator[FocusRow]:
    """One row on ``fake_a`` (none on ``fake_b``)."""
    _record("focus_rows", store, record_stores, since_ms=since_ms, until_ms=until_ms,
            reconciled_channels=reconciled_channels, k=k,
            allow_unreconciled=allow_unreconciled, role=role)
    yield FocusRow(columns=(("BilledCost", "1.00"), ("x_Channel", "fake_a")), channel="fake_a",
                   reconciled="fake_a" in reconciled_channels)


def focus_rows_foreign(*args: Any, **kw: Any) -> Iterator[FocusRow]:
    """A row on a channel the fake extension does not own."""
    yield FocusRow(columns=(("BilledCost", "1.00"),), channel="anthropic_api", reconciled=True)


def focus_rows_wrong_type(*args: Any, **kw: Any) -> Iterator[object]:
    """Something that is not a FocusRow."""
    yield {"BilledCost": "1.00"}


# ---------- showback ----------


def showback(result: RunResult, out_dir: Path, formats: Sequence[str]) -> list[Path]:
    """Writes ``fake-showback.<fmt>`` per format; returns the paths."""
    _record("showback", result, out_dir, formats)
    paths = []
    for fmt in formats:
        path = Path(out_dir) / f"fake-showback.{fmt}"
        path.write_text("fake showback\n", encoding="utf-8")
        paths.append(path)
    return paths


def showback_wrong_type(*args: Any, **kw: Any) -> list[str]:
    """Returns strings instead of paths."""
    return ["fake-showback.html"]


# ---------- policy_targets ----------


def policy_packs(store: Any, record_stores: Sequence[Any], ctx: AnalysisContext,
                 findings: Sequence[Any], result: RunResult | None, *, out_dir: Path | None,
                 current: object, cohort_by: str, include_tradeoffs: bool) -> list[PolicyPack]:
    """One empty pack for target ``fake-target``."""
    _record("policy_targets", store, record_stores, ctx, findings, result, out_dir=out_dir,
            current=current, cohort_by=cohort_by, include_tradeoffs=include_tradeoffs)
    return [PolicyPack(target="fake-target", cohort="all", merge_patch_json="{}",
                       rollback_patch_json="{}", entries=(), otel_resource_attributes="",
                       readme_md="# fake\n", hooks=())]


def policy_packs_wrong_type(*args: Any, **kw: Any) -> list[object]:
    """Returns something that is not a PolicyPack."""
    return [{"target": "fake-target"}]


# ---------- panel_builder ----------


def panel(store: Any, record_stores: Sequence[Any], **kw: Any) -> list[PanelRow]:
    """One panel row per arm in ``kw["arms"]`` (default one row)."""
    _record("panel_builder", store, record_stores, **kw)
    arms = kw.get("arms") or ("control",)
    return [PanelRow(cluster_id="team-a", date_utc="2026-09-10", cost_baseline_nano=10,
                     cost_actual_nano=9, active_dev_days=5, arm=arm, wave=None,
                     treated=arm != "control") for arm in arms]


def panel_wrong_type(*args: Any, **kw: Any) -> list[object]:
    """Returns something that is not a PanelRow."""
    return [("team-a", "2026-09-10")]


# ---------- hooks that fail while running (the host must not swallow these) ----------


def raises_import_error(*args: Any, **kw: Any) -> Any:
    """Resolves fine, then raises ImportError while running."""
    raise ImportError("a dependency of a running hook is missing")


def raises_value_error(*args: Any, **kw: Any) -> Any:
    """Resolves fine, then fails while running."""
    raise ValueError("a running hook failed")
