"""The verification side of the pipeline (SPEC §13, D15, D18; CLI-SAVINGS).

``run_measure_plan`` (randomized rollout plan, or an ITS design for org-wide changes),
``run_measure_run`` (realized savings on a cluster-day panel at the pre-registered baseline card,
with the guards and the per-channel reconciliation), ``run_ab`` (paired lab comparison) and
``run_receipt`` (``create`` / ``sign`` / ``verify``). Pure functions over explicit inputs (a
store, or a panel, plan or measurement object) returning ``RunResult``; refusals raise
``GateFailed`` (CLI exit 3).

**Files** (JSON, canonical, owner-only when written):

* ``plan.json`` — ``{"schema": "tokenbill/measure-plan@1", "plan": <MeasurePlan>,
  "assignment_log": <JSON lines>}`` (``core.records.to_json``; the assignment log is the one whose
  SHA-256 the pre-registration records);
* ``measurement.json`` — ``{"schema": "tokenbill/measurement@1", "measurement":
  <MeasurementResult>, "context": {"reconciliation_verdict", "calibration"}}``: what ``receipt
  create`` needs besides the measurement;
* ``receipt.json`` — the canonical receipt bytes (``verify.receipts.canonical_bytes``);
  ``*.dsse.json`` — the DSSE envelope.

Parsers of user files raise ``UsageError`` with content-free messages (never echoing values).
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation, GateFailed, SourceError, UsageError
from tokenbill.core.jsonl import load_json_exact
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality
from tokenbill.core.protocols import ExtRecordStore, LedgerStore, Pricer
from tokenbill.core.records import Request, from_json, to_json
from tokenbill.core.types import (
    DataQualityNote,
    MeasurementResult,
    MeasurePlan,
    PanelRow,
    ReconciliationReport,
    RunResult,
)
from tokenbill.pipeline import savings as sv
from tokenbill.pipeline.common import Env

__all__ = [
    "MEASUREMENT_SCHEMA",
    "PLAN_SCHEMA",
    "load_clusters",
    "load_measurement",
    "load_outcomes",
    "load_plan",
    "load_projection",
    "load_treated",
    "measurement_json",
    "parse_holdback",
    "parse_looks",
    "plan_json",
    "run_ab",
    "run_measure_plan",
    "run_measure_run",
    "run_receipt",
]

PLAN_SCHEMA = "tokenbill/measure-plan@1"
MEASUREMENT_SCHEMA = "tokenbill/measurement@1"
_MAX_FILE = 64 << 20
_ID_RE = re.compile(r"[^\x00-\x1f\x7f/\\]{1,128}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_WAVE_RE = re.compile(r"(?:[1-9][0-9]{0,3}|control|holdback|0)\Z")
_NO_PATCH = "tokenbill:no-patch:"


# =============================================================================================
# file parsers
# =============================================================================================


def _read_text(path: Path, what: str) -> str:
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError:
        raise UsageError(f"{what}: cannot read the file") from None
    if len(raw) > _MAX_FILE:
        raise UsageError(f"{what}: file too large")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UsageError(f"{what}: not UTF-8 text") from None


def _json(path: Path, what: str) -> object:
    try:
        return load_json_exact(Path(path).expanduser(), max_bytes=_MAX_FILE)
    except SourceError:
        raise UsageError(f"{what}: not a readable JSON file") from None


def _cluster_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value.strip()) or not value.strip():
        raise UsageError(f"{what}: cluster ids are non-empty names of at most 128 characters "
                         "without control characters or slashes")
    return value.strip()


def parse_clusters(text: str, what: str = "--clusters") -> list[str]:
    """Cluster ids from a JSON array (or ``{"clusters": [...]}``) or plain text (one per line,
    ``#`` comments). Duplicates keep their first position."""
    stripped = text.strip()
    items: list[object]
    if stripped.startswith(("[", "{")):
        try:
            doc = json.loads(stripped)
        except (ValueError, RecursionError):
            raise UsageError(f"{what}: not valid JSON") from None
        if isinstance(doc, dict):
            doc = doc.get("clusters")
        if not isinstance(doc, list):
            raise UsageError(f"{what}: expected a JSON array of cluster ids")
        items = list(doc)
    else:
        items = [line.split("#", 1)[0].strip() for line in stripped.splitlines()]
        items = [i for i in items if i]
    out = [_cluster_id(i, what) for i in items]
    if not out:
        raise UsageError(f"{what}: no cluster ids")
    return list(dict.fromkeys(out))


def load_clusters(path: Path) -> list[str]:
    """``--clusters FILE`` (see :func:`parse_clusters`)."""
    return parse_clusters(_read_text(path, "--clusters"))


def parse_treated(doc: object, what: str = "--treated") -> dict[str, str]:
    """A user assignment: JSON object cluster → wave number (≥ 1) or ``control``/``holdback``."""
    if not isinstance(doc, dict) or not doc:
        raise UsageError(f"{what}: expected a JSON object of cluster → wave")
    out: dict[str, str] = {}
    for key, value in doc.items():
        cluster = _cluster_id(key, what)
        text = str(value).strip() if isinstance(value, (int, str)) and not isinstance(
            value, bool) else ""
        if not _WAVE_RE.match(text):
            raise UsageError(f"{what}: waves are positive integers or control")
        out[cluster] = text
    return out


def load_treated(path: Path) -> dict[str, str]:
    """``--treated FILE`` (see :func:`parse_treated`)."""
    return parse_treated(_json(path, "--treated"))


def parse_looks(text: str) -> list[str]:
    """``--looks DATES``: comma-separated ``YYYY-MM-DD`` dates, returned sorted and unique."""
    if not isinstance(text, str):
        raise UsageError("--looks: expected comma-separated dates")
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if not _DATE_RE.match(part):
            raise UsageError("--looks: dates must be YYYY-MM-DD")
        try:
            _dt.date.fromisoformat(part)
        except ValueError:
            raise UsageError("--looks: invalid date") from None
        out.append(part)
    return sorted(set(out))


def parse_holdback(text: str) -> Decimal:
    """``--holdback PCT``: a percentage (``20`` or ``20%``) or a fraction (``0.2``) → fraction."""
    raw = str(text).strip().rstrip("%").strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise UsageError("--holdback must be a percentage, e.g. 20") from None
    if not value.is_finite() or value < 0:
        raise UsageError("--holdback must be a percentage, e.g. 20")
    frac = value / 100 if value >= 1 else value
    if frac > 1:
        raise UsageError("--holdback must be at most 100%")
    return frac.normalize() if frac else Decimal(0)


def _figure(doc: object) -> Figure | None:
    """A ``Figure`` from a result@2 money object (``nano``, ``evidence``, ``basis``, …)."""
    if doc is None:
        return None
    if not isinstance(doc, Mapping):
        raise ValueError("money")
    nano = doc.get("nano")
    if nano is not None and type(nano) is not int:
        raise ValueError("nano")
    rng = doc.get("range") if isinstance(doc.get("range"), Mapping) else None
    low = rng.get("low_nano") if rng else None
    high = rng.get("high_nano") if rng else None
    if (low is None) != (high is None) or any(v is not None and type(v) is not int
                                             for v in (low, high)):
        raise ValueError("range")
    cal = doc.get("calibration", "n/a")
    return Figure(nano=nano, evidence=Evidence(str(doc.get("evidence"))),
                  basis=Basis(str(doc.get("basis"))),
                  finality=Finality(str(doc.get("finality", "n/a"))),
                  low_nano=low, high_nano=high,
                  calibration=Calibration(str(cal)) if cal is not None else Calibration.NA,
                  upper_bound=bool(doc.get("upper_bound", False)))


def parse_projection(doc: object, lever_id: str) -> Figure:
    """The monthly projection of *lever_id* from a ``findings`` / ``report`` / ``policy`` JSON
    output (``action_plan.levers[].projected_monthly``) or ``{"lever_id", "projected_monthly"}``.
    """
    try:
        if not isinstance(doc, Mapping):
            raise ValueError("doc")
        if "projected_monthly" in doc:
            if doc.get("lever_id") not in (None, lever_id):
                raise UsageError("--projection: the file names another lever")
            fig = _figure(doc["projected_monthly"])
        else:
            plan = doc.get("action_plan")
            levers = plan.get("levers") if isinstance(plan, Mapping) else None
            if not isinstance(levers, list):
                raise ValueError("levers")
            match = [lv for lv in levers if isinstance(lv, Mapping)
                     and lv.get("lever_id") == lever_id]
            if not match:
                raise UsageError("--projection: the plan has no such lever")
            fig = _figure(match[0].get("projected_monthly"))
    except (ValueError, TypeError, AttributeError, ContractViolation):
        raise UsageError("--projection: not a tokenbill plan or projection document") from None
    if fig is None or fig.nano is None:
        raise UsageError("--projection: the lever has no priced projection")
    return fig


def load_projection(path: Path, lever_id: str) -> Figure:
    """``--projection FILE`` (see :func:`parse_projection`)."""
    return parse_projection(_json(path, "--projection"), lever_id)


def parse_outcomes(text: str, what: str = "--outcomes") -> list[dict[str, object]]:
    """A/B outcomes: a JSON array, JSON lines, or CSV with header ``task_id,arm,trial,success[,
    order]``. Values are typed (ints, booleans) for ``verify.ab.paired_ab``."""
    stripped = text.strip()
    rows: list[dict[str, object]] = []
    if not stripped:
        raise UsageError(f"{what}: empty file")
    if stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except (ValueError, RecursionError):
            raise UsageError(f"{what}: not valid JSON") from None
        if not isinstance(doc, list) or not all(isinstance(r, dict) for r in doc):
            raise UsageError(f"{what}: expected an array of objects")
        rows = [dict(r) for r in doc]
    elif stripped.startswith("{"):
        for line in stripped.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                raise UsageError(f"{what}: not valid JSON lines") from None
            if not isinstance(obj, dict):
                raise UsageError(f"{what}: every line must be an object")
            rows.append(obj)
    else:
        reader = csv.DictReader(io.StringIO(stripped))
        if reader.fieldnames is None or not {"task_id", "arm", "trial", "success"} <= set(
                reader.fieldnames):
            raise UsageError(f"{what}: CSV needs the columns task_id, arm, trial, success")
        for raw in reader:
            rows.append({k: v for k, v in raw.items() if k is not None})
    return [_typed_outcome(r, what) for r in rows]


def _typed_outcome(row: Mapping[str, object], what: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in ("task_id", "arm"):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise UsageError(f"{what}: {key} must be a non-empty string")
        out[key] = value.strip()
    for key in ("trial", "order"):
        value = row.get(key)
        if value is None or value == "":
            if key == "trial":
                raise UsageError(f"{what}: trial is required")
            continue
        if isinstance(value, bool):
            raise UsageError(f"{what}: {key} must be an integer")
        try:
            number = int(str(value).strip(), 10)
        except ValueError:
            raise UsageError(f"{what}: {key} must be an integer") from None
        if not 0 <= number <= 2**31:
            raise UsageError(f"{what}: {key} out of range")
        out[key] = number
    success = row.get("success")
    if isinstance(success, str):
        success = {"true": True, "false": False, "1": True, "0": False}.get(
            success.strip().lower(), success)
    if success not in (True, False, 0, 1) or isinstance(success, float):
        raise UsageError(f"{what}: success must be true/false or 1/0")
    out["success"] = bool(success)
    return out


def load_outcomes(path: Path) -> list[dict[str, object]]:
    """``--outcomes FILE`` (see :func:`parse_outcomes`)."""
    return parse_outcomes(_read_text(path, "--outcomes"))


def plan_json(plan: MeasurePlan) -> dict[str, object]:
    """The ``plan.json`` document of *plan*."""
    from tokenbill.verify.rollout import assignment_log_text

    return {"schema": PLAN_SCHEMA, "plan": to_json(plan),
            "assignment_log": assignment_log_text(plan)}


def parse_plan(doc: object) -> tuple[MeasurePlan, str | None]:
    """``(MeasurePlan, assignment log)`` of a ``plan.json`` document."""
    if not isinstance(doc, Mapping) or doc.get("schema") != PLAN_SCHEMA:
        raise UsageError(f"--plan: not a {PLAN_SCHEMA} document")
    try:
        plan = from_json(MeasurePlan, doc.get("plan"))  # type: ignore[arg-type]
    except (ContractViolation, TypeError):
        raise UsageError("--plan: malformed plan") from None
    log = doc.get("assignment_log")
    if log is not None and not isinstance(log, str):
        raise UsageError("--plan: malformed assignment log")
    return plan, log


def load_plan(path: Path) -> tuple[MeasurePlan, str | None]:
    """``--plan plan.json`` (see :func:`parse_plan`)."""
    return parse_plan(_json(path, "--plan"))


def measurement_json(m: MeasurementResult, *, reconciliation_verdict: str,
                     calibration: str) -> dict[str, object]:
    """The ``measurement.json`` document of *m*."""
    return {"schema": MEASUREMENT_SCHEMA, "measurement": to_json(m),
            "context": {"reconciliation_verdict": reconciliation_verdict,
                        "calibration": calibration}}


@dataclass(frozen=True)
class LoadedMeasurement:
    """A measurement file: the result and the context ``receipt create`` needs."""

    measurement: MeasurementResult
    reconciliation_verdict: str
    calibration: str


def parse_measurement(doc: object) -> LoadedMeasurement:
    """A ``measurement.json`` document (see the module docstring)."""
    if not isinstance(doc, Mapping) or doc.get("schema") != MEASUREMENT_SCHEMA:
        raise UsageError(f"--measurement: not a {MEASUREMENT_SCHEMA} document")
    try:
        m = from_json(MeasurementResult, doc.get("measurement"))  # type: ignore[arg-type]
    except (ContractViolation, TypeError):
        raise UsageError("--measurement: malformed measurement") from None
    ctx = doc.get("context")
    if not isinstance(ctx, Mapping):
        raise UsageError("--measurement: missing context")
    verdict, cal = ctx.get("reconciliation_verdict"), ctx.get("calibration")
    if not isinstance(verdict, str) or not isinstance(cal, str) or cal not in (
            "calibrated", "uncalibrated", "n/a"):
        raise UsageError("--measurement: malformed context")
    return LoadedMeasurement(m, verdict, cal)


def load_measurement(path: Path) -> LoadedMeasurement:
    """``--measurement FILE`` (see :func:`parse_measurement`)."""
    return parse_measurement(_json(path, "--measurement"))


# =============================================================================================
# measure plan / measure run
# =============================================================================================


def _p90_session_hours(store: LedgerStore, since_ms: int, until_ms: int) -> int:
    """The p90 session length in whole hours (rounded up; ≥ 1) over the window's lanes."""
    spans: dict[str, list[int]] = {}
    for lane in store.iter_lanes(since_ms=since_ms, until_ms=until_ms):
        if not lane.requests:
            continue
        span = spans.setdefault(lane.session_key, [lane.requests[0].ts_start_ms,
                                                   lane.requests[-1].ts_start_ms])
        span[0] = min(span[0], lane.requests[0].ts_start_ms)
        span[1] = max(span[1], lane.requests[-1].ts_start_ms)
    lengths = sorted(hi - lo for lo, hi in spans.values())
    if not lengths:
        return 1
    p90 = lengths[max(0, -(-len(lengths) * 9 // 10) - 1)]
    return max(1, -(-p90 // 3_600_000))


def _dev_days_per_month(panel: Sequence[PanelRow]) -> int | None:
    days = {r.date_utc for r in panel}
    total = sum(r.active_dev_days for r in panel)
    if not days or total <= 0:
        return None
    return max(1, total * 30 // len(days))


def run_measure_plan(env: Env, *, lever_id: str, cluster_kind: str = "team",
                     design: str = "stepped_wedge", waves: int = 4,
                     holdback: Decimal = Decimal("0.2"), seed: int = 0,
                     store: LedgerStore | None = None, clusters: Sequence[str] | None = None,
                     projection: Figure | None = None, looks: Sequence[str] = (),
                     treated: Mapping[str, str] | None = None, org_wide: bool = False,
                     since_ms: int | None = None, until_ms: int | None = None,
                     change_date: str | None = None) -> RunResult:
    """A measurement plan (SPEC §13.2, ``verify.rollout.plan``). With *store*, the pre-period
    panel (MDE from A/A re-randomizations, clusters, the p90 session length for the washout and
    developer-days to express a monthly *projection* per developer-day) comes from the store's
    window. ``org_wide`` plans a server-managed change: without MDM or gateway clusters it becomes
    an ITS design (MEASURED ceiling)."""
    from tokenbill.verify import panel as vp
    from tokenbill.verify import rollout

    pre: list[PanelRow] | None = None
    washout = 1
    window = None
    if store is not None:
        window = sv.resolve_window(store, since_ms, until_ms)
        pre = vp.build_panel(store, cluster_kind=cluster_kind, since=sv._date(window[0]),
                             until=sv._date(window[1]), baseline_pricer=env.pricer,
                             actual_pricer=env.pricer)
        washout = _p90_session_hours(store, *window)
    names = list(clusters) if clusters else sorted({r.cluster_id for r in pre or ()})
    if not names:
        raise UsageError("measure plan needs clusters: pass --clusters FILE or a --db with "
                         "cluster data")
    notes: list[str] = []
    per_day = None
    if projection is not None:
        per_month = _dev_days_per_month(pre or ())
        if per_month is None:
            notes.append("the projection is not used: no pre-period developer-days to express "
                         "it per active developer-day")
        else:
            per_day = rollout.projection_per_dev_day(projection, per_month)
    plan = rollout.plan(names, lever_id=lever_id, cluster_kind=cluster_kind, design=design,
                        waves=waves, holdback=holdback, seed=seed, pre_panel=pre or None,
                        projection=per_day, washout_hours=washout, looks=list(looks),
                        treated=dict(treated) if treated else None, org_wide_delivery=org_wide,
                        change_date=change_date, rate_card_sha256=env.pricer.rate_card_sha256)
    win = window if window is not None else (env.now_ms - env.now_ms % sv.DAY_MS,
                                             env.now_ms - env.now_ms % sv.DAY_MS + sv.DAY_MS)
    return RunResult(command="measure plan", window=win, inputs=(),
                     privacy=sv._privacy(env, identity_mode="central"),
                     rate_card=sv.rate_card_info(env.pricer, sv.today_of(env)),
                     measure_plan=plan, notes=tuple(notes))


def _no_reconciliation(since: str, until: str) -> ReconciliationReport:
    return ReconciliationReport(
        window=(since, until), tolerance_pct="0.5", unexplained_tolerance_pct="1.0", rows=(),
        token_coverage_pct=None, dollar_coverage_pct=None, rate_card_error=None,
        over_count_rows=0, effective_discount=(), residuals=(), unexplained_nano=0, channels=(),
        verdict="insufficient_data", finality=Finality.NA, suggested_contract=None,
        rerun_verdict=None)


def _look(plan: MeasurePlan, panel_until: str, today: str) -> str:
    taken = [d for d in plan.looks if d <= today]
    if taken:
        return taken[-1]
    if plan.looks:
        return plan.looks[0]
    return panel_until


def run_measure_run(env: Env, *, plan: MeasurePlan, assignment_log: str | None = None,
                    store: LedgerStore | None = None, db_path: Path | None = None,
                    panel: Sequence[PanelRow] | None = None,
                    baseline_pricer: Pricer | None = None,
                    reconciliation: ReconciliationReport | None = None,
                    panel_name: str | None = None, since_ms: int | None = None,
                    until_ms: int | None = None, look: str | None = None, seed: int = 0,
                    boot: int = 2000, billing_class: str = "billed",
                    record_stores: Sequence[ExtRecordStore] | None = None) -> RunResult:
    """Realized savings (SPEC §13.3–§13.4): the cluster-day panel at the pre-registered baseline
    card (*baseline_pricer*, default the Env pricer; a card whose SHA-256 differs from the plan's
    pre-registration is noted), the plan's estimator, the guards with the per-channel
    reconciliation of the panel window (RECON + extensions, or *reconciliation*), the label and
    signability. *panel* (e.g. a synthetic one) replaces the store panel; *panel_name* selects an
    extension panel (``measure --panel copilot``). ``NotAMeasurement`` (no label applies) is a
    ``GateFailed`` (exit 3) whose message lists the guards."""
    from tokenbill.verify import label_policy
    from tokenbill.verify import panel as vp
    from tokenbill.verify.rollout import arms_for, assignment_log_text

    base = baseline_pricer if baseline_pricer is not None else env.pricer
    notes: list[str] = []
    dq: list[DataQualityNote] = []
    if plan.preregistration_sha256 and base.rate_card_sha256 not in plan.preregistration_json:
        notes.append("the baseline rate card differs from the pre-registered one (pass "
                     "--baseline-rates with the card used at planning time)")
    channels = cache_scopes = None
    if panel is None:
        if store is None:
            raise UsageError("measure run needs --db (the ledger) or a panel")
        window = sv.resolve_window(store, since_ms, until_ms)
        since, until = sv._date(window[0]), sv._date(window[1])
        kw: dict[str, Any] = {"cluster_kind": plan.cluster_kind, "since": since, "until": until,
                              "baseline_pricer": base, "actual_pricer": env.pricer,
                              "arms": arms_for(plan), "billing_class": billing_class}
        if panel_name is not None:
            stores = list(record_stores) if record_stores is not None else \
                sv._record_stores(db_path, dq)
            rows = extensions.panel(panel_name, store, stores, notes=dq, **kw)
        else:
            rows = vp.build_panel(store, **kw)
        channels = vp.panel_channels(store, cluster_kind=plan.cluster_kind, since=since,
                                     until=until)
        cache_scopes = vp.cache_scope_clusters(store, cluster_kind=plan.cluster_kind,
                                               since=since, until=until)
        if reconciliation is None:
            run = sv._analysis(store, env, db_path=db_path, since_ms=window[0],
                               until_ms=window[1], today=None, jobs=1, shard_max_requests=None,
                               self_view=False, principal_ref=None,
                               record_stores=record_stores)
            run.caps = extensions.capabilities_present(store, run.record_stores,
                                                       since_ms=window[0], until_ms=window[1],
                                                       notes=dq)
            merged, _ext = run.reconcile()
            reconciliation = merged if merged is not None else _no_reconciliation(since, until)
            dq.extend(run.notes)
    else:
        rows = vp.check_rows(panel)
        since, until = vp.panel_window(rows)
        window = (sv._date_ms(since), sv._date_ms(until) + sv.DAY_MS)
        if reconciliation is None:
            reconciliation = _no_reconciliation(since, until)
    if not rows:
        raise UsageError("the panel is empty: no cluster-day in the window")
    look_date = look if look is not None else _look(plan, vp.panel_window(rows)[1],
                                                    sv.today_of(env))
    log = assignment_log if assignment_log is not None else assignment_log_text(plan)
    basis = Basis.LIST_EQUIVALENT if billing_class == "allowance" else base.basis
    try:
        m = label_policy.measure(rows, plan=plan, reconciliation=reconciliation, look=look_date,
                                 rate_card_sha256=base.rate_card_sha256, channels=channels,
                                 cache_scopes=cache_scopes, assignment_log=log, basis=basis,
                                 boot=boot, seed=seed, looks_taken=[look_date])
    except label_policy.NotAMeasurement as exc:
        failed = ", ".join(g.name for g in exc.guards if not g.passed) or "none"
        raise GateFailed(f"not a measurement ({exc}); failed guards: {failed}") from None
    return RunResult(command="measure run", window=window, inputs=(),
                     privacy=sv._privacy(env, identity_mode="central"),
                     rate_card=sv.rate_card_info(base, sv.today_of(env)),
                     data_quality=tuple(dq), reconciliation=reconciliation,
                     measurements=(m,), notes=tuple(notes))


# =============================================================================================
# ab
# =============================================================================================


def _requests_of(path: Path, env: Env) -> list[Request]:
    from tokenbill.core.registry import sniff_adapter

    path = Path(path).expanduser()
    if not path.is_file():
        raise UsageError(f"{path.name}: not a file")
    if path.suffix in (".db", ".sqlite", ".sqlite3"):
        from tokenbill.pipeline.common import open_store

        store = open_store(path, env, create=False)
        try:
            return list(store.iter_requests())
        finally:
            sv.close_ledger(store)
    adapter = sniff_adapter(path)
    if adapter is None:
        raise UsageError(f"{path.name}: no adapter recognizes this file")
    from tokenbill.pipeline.common import ingest_options

    result = adapter.read(path, ingest_options(env))
    return list(result.requests)


def run_ab(env: Env, *, baseline: Path | Sequence[Request], candidate: Path | Sequence[Request],
           outcomes: Path | Sequence[Mapping[str, object]], boot: int = 10_000,
           seed: int = 0) -> RunResult:
    """Paired lab comparison (SPEC §13.5, ``verify.ab.paired_ab``): two usage sets (trace@2 files,
    any adapter's file, or ledgers; or request lists) with ``task_id`` attribution and an outcomes
    table (JSON array, JSON lines or CSV; or rows)."""
    from tokenbill.verify.ab import paired_ab

    if type(boot) is not int or not 1 <= boot <= 1_000_000:
        raise UsageError("--boot must be an int between 1 and 1000000")
    base = _requests_of(baseline, env) if isinstance(baseline, (str, Path)) else list(baseline)
    cand = _requests_of(candidate, env) if isinstance(candidate, (str, Path)) else list(candidate)
    rows = load_outcomes(outcomes) if isinstance(outcomes, (str, Path)) else [
        dict(r) for r in outcomes]
    result = paired_ab(base, cand, rows, pricer=env.pricer, boot=boot, seed=seed)
    starts = [a.ts_start_ms for r in (*base, *cand) for a in r.attempts]
    window = (min(starts), max(starts) + 1) if starts else (0, 1)
    return RunResult(command="ab", window=window, inputs=(),
                     privacy=sv._privacy(env, identity_mode="central"),
                     rate_card=sv.rate_card_info(env.pricer, sv.today_of(env)), ab=result)


# =============================================================================================
# receipts
# =============================================================================================


def _now_iso(env: Env) -> str:
    moment = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(
        milliseconds=env.now_ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _receipt_doc(path: Path, what: str) -> dict:
    doc = _json(path, what)
    if not isinstance(doc, dict):
        raise UsageError(f"{what}: not a JSON object")
    return doc


def _patch_sha(patch: Path | None, lever_id: str) -> tuple[str, str | None]:
    if patch is None:
        digest = hashlib.sha256((_NO_PATCH + lever_id).encode()).hexdigest()
        return digest, ("no --patch given: subject.patch_sha256 names the lever only "
                        "(sha256 of 'tokenbill:no-patch:<lever>')")
    try:
        data = Path(patch).expanduser().read_bytes()
    except OSError:
        raise UsageError("--patch: cannot read the file") from None
    return hashlib.sha256(data).hexdigest(), None


def run_receipt(action: str, env: Env, *, measurement: Path | None = None,
                receipt: Path | None = None, envelope: Path | None = None,
                key: Path | None = None, allowed_signers: Path | None = None,
                identity: str | None = None, patch: Path | None = None,
                shapley_credit: Figure | None = None, store: LedgerStore | None = None,
                created: str | None = None, runner: Any = None) -> tuple[RunResult, object]:
    """``receipt create | sign | verify`` (SPEC §13.6). Returns the ``RunResult`` and the
    document to write (the receipt for ``create``, the DSSE envelope for ``sign``, ``None`` for
    ``verify``). Refusals — an unsignable receipt, a signature that does not verify — raise
    ``GateFailed`` (exit 3); a missing ``ssh-keygen`` is a runtime error (exit 1)."""
    from tokenbill import __version__
    from tokenbill.verify import receipts as rc

    kw: dict[str, Any] = {} if runner is None else {"runner": runner}
    notes: list[str] = []
    doc: object = None
    if action == "create":
        if measurement is None:
            raise UsageError("receipt create needs --measurement FILE")
        loaded = load_measurement(measurement)
        m = loaded.measurement
        sha, note = _patch_sha(patch, m.lever_id)
        if note:
            notes.append(note)
        body = rc.build_receipt(m, lever_id=m.lever_id, patch_sha256=sha,
                                shapley_credit=shapley_credit,
                                reconciliation_verdict=loaded.reconciliation_verdict,
                                calibration=Calibration(loaded.calibration),
                                tool_version=__version__, created=created or _now_iso(env))
        reasons = rc.refusal_reasons(body)
        if reasons:
            notes.append("not signable: " + "; ".join(reasons))
        if store is not None:
            rc.store_receipt(store, body)
            notes.append("receipt stored in the ledger")
        rid, doc = rc.receipt_id(body), body
    elif action == "sign":
        if receipt is None or key is None:
            raise UsageError("receipt sign needs --key PATH and the receipt file")
        body = _receipt_doc(receipt, "receipt")
        doc = rc.sign(body, key_path=Path(key).expanduser(), **kw)
        rid = rc.receipt_id(body)
        if store is not None:
            rc.store_receipt(store, body, envelope=doc)  # type: ignore[arg-type]
        notes.append("signed (DSSE envelope, ssh-keygen namespace tokenbill-receipt)")
    elif action == "verify":
        if envelope is None or allowed_signers is None or not identity:
            raise UsageError("receipt verify needs --allowed-signers FILE, --identity ID and the "
                             "envelope file")
        env_doc = _receipt_doc(envelope, "envelope")
        ok = rc.verify_envelope(env_doc, allowed_signers=Path(allowed_signers).expanduser(),
                                identity=identity, **kw)
        if not ok:
            raise GateFailed("receipt signature does not verify for this identity")
        body = rc.envelope_receipt(env_doc)
        rid = rc.receipt_id(body)
        notes.append(f"verified for identity {identity[:128]}")
    else:
        raise UsageError("receipt action must be create, sign or verify")
    result = RunResult(command=f"receipt {action}", window=(env.now_ms, env.now_ms + 1),
                       inputs=(), privacy=sv._privacy(env, identity_mode="central"),
                       rate_card=None, receipts=(rid,), notes=tuple(notes))
    return result, doc

