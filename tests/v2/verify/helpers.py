"""Shared builders for the VERIFY tests (synthetic records only; imported only within this area)."""

from __future__ import annotations

import datetime as _dt
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from tokenbill.common import rng
from tokenbill.core.builders import make_request
from tokenbill.core.ids import key_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality
from tokenbill.core.records import OutcomeAggregate, Request
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import (
    ChannelVerdict,
    ContractOverlay,
    GuardResult,
    IngestResult,
    MeasurementResult,
    ReconciliationReport,
    SourceInfo,
)

ORG_KEY = b"verify-test-org-key-0123456789ab"
MODEL = "claude-sonnet-4-6"          # priced by FakePricer from 2026-02-17
EPOCH = _dt.date(1970, 1, 1)
DAY_MS = 86_400_000

#: A 20% contract discount on every bucket (a simultaneous price cut).
CUT20 = ContractOverlay(name="cut20", multiplier=Decimal("0.8"), overrides=(),
                        effective_from="2026-01-01", effective_to=None, derived=False,
                        assumed_fields=())


def ts(date: str, hour: int = 10, minute: int = 0) -> int:
    """Epoch milliseconds of *date* at *hour*:*minute* UTC."""
    d = _dt.date.fromisoformat(date)
    return (d - EPOCH).days * DAY_MS + hour * 3_600_000 + minute * 60_000


def source(name: str) -> SourceInfo:
    return SourceInfo(source_id=f"s_{name}", adapter="trace@2", name_hmac=f"h_{name}",
                      sha256=hashlib.sha256(name.encode()).hexdigest(), bytes=1,
                      name_key_id=None, principal_key_id=key_id(ORG_KEY))


def ingest(store: MemoryStore, requests: Sequence[Request], *, name: str = "verify",
           outcomes: Iterable[OutcomeAggregate] = ()) -> None:
    store.ingest(IngestResult(source=source(name), requests=list(requests), sessions=[],
                              events=[], aggregates=[], cost_lines=[], outcomes=list(outcomes),
                              quarantined=[], notes=[], stats={"records": len(requests)},
                              capabilities=frozenset({"usage_sequence"})))


def request(cluster: str, date: str, dev: int, *, seq: int = 0, hour: int = 10,
            usage: Mapping[str, int] | None = None, arm: str | None = None,
            wave: str | None = None, kind: str = "team", billing_path: str = "api_key",
            model: str = MODEL, extra: Mapping[str, str] | None = None,
            workspace: str | None = None) -> Request:
    """One request by developer *dev* of *cluster* on *date* (cluster kind team / mdm_group /
    workspace decides where the cluster id goes)."""
    ex = dict(extra or {})
    attr: dict[str, object] = {"principal": f"r_dev{dev}", "billing_path": billing_path,
                               "arm": arm, "wave": wave}
    if kind == "team":
        attr["team"] = cluster
    elif kind == "mdm_group":
        ex["mdm_group"] = cluster
        attr["team"] = "t-" + cluster
    elif kind == "workspace":
        attr["workspace_id"] = cluster
    if workspace is not None:
        attr["workspace_id"] = workspace
    attr["extra"] = tuple(sorted(ex.items()))
    lane = f"L-{cluster}-{dev}-{date}"
    return make_request(lane, seq, ts(date, hour, seq), usage or {
        "uncached_input": 1000, "cache_read": 20_000, "cache_write_5m": 2000, "output": 500},
        MODEL if model is None else model, attribution=attr,
        message_id=f"msg_{cluster}_{dev}_{date}_{seq}", billing_path=billing_path)


def recon(verdicts: Mapping[str, str], *, window: tuple[str, str] = ("2026-01-01", "2027-01-01"),
          overall: str | None = None) -> ReconciliationReport:
    """A minimal reconciliation report with per-channel verdicts."""
    chans = tuple(ChannelVerdict(channel=c, verdict=v, invoice_sources=("test",),
                                 mapping_verified=True) for c, v in sorted(verdicts.items()))
    if overall is None:
        overall = "reconciled" if all(v == "reconciled" for v in verdicts.values()) \
            else "not_reconciled"
    return ReconciliationReport(
        window=window, tolerance_pct="0.5", unexplained_tolerance_pct="1.0", rows=(),
        token_coverage_pct=None, dollar_coverage_pct=None, rate_card_error=None,
        over_count_rows=0, effective_discount=(), residuals=(), unexplained_nano=0,
        channels=chans, verdict=overall, finality=Finality.FINAL, suggested_contract=None,
        rerun_verdict=None)


# ---------------------------------------------------------------------------------------------
# an RTK-like lab campaign: tokens −38%, turns +14%, cost +7% (FlatRates)
# ---------------------------------------------------------------------------------------------

_BASE_USAGE = {"cache_read": 50_000, "cache_write_5m": 2000, "uncached_input": 500,
               "output": 800}
_CAND_USAGE = {"cache_read": 24_217, "cache_write_5m": 3473, "uncached_input": 500,
               "output": 800}


def rtk_campaign(*, seed: int = 0, tasks: int = 20, trials: int = 5, randomized: bool = True,
                 cand_scale: float = 1.0, task_prefix: str = "task"
                 ) -> tuple[list[Request], list[Request], list[dict[str, object]]]:
    """Baseline and candidate requests (``extra task_id``) and outcome rows. Per request the
    candidate reads far less cache but writes more (RTK-like compression breaking the cache):
    ~ −38% tokens, +14% turns, +7% cost at FlatRates; both arms succeed in 4 of 5 trials."""
    rnd = rng(seed, "tests.verify.rtk")
    base: list[Request] = []
    cand: list[Request] = []
    outcomes: list[dict[str, object]] = []
    day = "2026-09-01"
    for t in range(tasks):
        task = f"{task_prefix}-{t:03d}"
        mult = 0.5 + 1.5 * rnd.random()
        for trial in range(trials):
            cand_first = rnd.random() < 0.5 if randomized else False
            for arm, usage, turns in (("baseline", _BASE_USAGE, 14 + rnd.randint(-2, 2)),
                                      ("candidate", _CAND_USAGE, 16 + rnd.randint(-2, 2))):
                k = cand_scale if arm == "candidate" else 1.0
                scaled = {b: max(1, round(v * mult * k)) for b, v in usage.items()}
                for i in range(turns):
                    r = make_request(f"L-{arm}-{task}-{trial}", i, ts(day, 9, t) + i * 1000,
                                     scaled, MODEL,
                                     attribution={"extra": (("task_id", task),)},
                                     message_id=f"msg_{arm}_{task}_{trial}_{i}")
                    (base if arm == "baseline" else cand).append(r)
                order = (1 if arm == "baseline" else 0) if cand_first else \
                    (0 if arm == "baseline" else 1)
                outcomes.append({"task_id": task, "arm": arm, "trial": trial,
                                 "success": trial != 0, "order": order})
    return base, cand, outcomes


def sample_measurement(*, evidence: str = "verified", basis: str = "list",
                       signable: bool = True, projected_calibration: str | None = "calibrated",
                       rr: str | None = "0.8125") -> MeasurementResult:
    """A hand-built measurement (the receipts tests' fixture; no randomness)."""
    est = Figure(nano=1_234_567_891, evidence=Evidence(evidence), basis=Basis(basis),
                 low_nano=1_000_000_000 if evidence != "exact" else None,
                 high_nano=1_500_000_000 if evidence != "exact" else None,
                 ci_level_pct=95 if evidence in ("measured", "verified") else None,
                 note="" if evidence != "estimated" else "not a measurement: placebo")
    projected = None
    if projected_calibration is not None:
        projected = Figure(nano=1_519_468_174, evidence=Evidence.ESTIMATED, basis=Basis(basis),
                           calibration=Calibration(projected_calibration))
    return MeasurementResult(
        lever_id="cc.prompt_cache_ttl.main", design="stepped_wedge",
        unit="cost per active developer-day", estimate=est, projected=projected,
        realization_rate=(rr, "0.6581", "0.9872") if rr is not None else None,
        guards=(GuardResult("srm", True, "p=0.42", "p ≥ 0.001"),
                GuardResult("placebo", True, "CI [-3, 4]", "placebo CI includes 0")),
        scope=(("clusters", 24), ("unit_days", 30000), ("treated_unit_days", 12000)),
        scope_label="fleet:2026-06-01..2026-08-09",
        window=(("since", "2026-06-01"), ("until", "2026-08-09")), rate_card_sha256="cd" * 32,
        assignment_log_sha256="ef" * 32, preregistration_sha256="01" * 32,
        adjustments=("constant prices at the pre-registered rate card cdcdcdcdcdcd",),
        rate_variance=Figure(nano=-5, evidence=Evidence.EXACT, basis=Basis(basis)),
        signable=signable)
