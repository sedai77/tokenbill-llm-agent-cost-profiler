"""Property / fuzz tests for every input VERIFY parses (SPEC §21 #5: only TokenbillError
subclasses may escape)."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core.builders import FlatRates
from tokenbill.core.types import PanelRow
from tokenbill.verify import ab as A
from tokenbill.verify import estimators as E
from tokenbill.verify import its as I
from tokenbill.verify import label_policy as L
from tokenbill.verify import panel as P
from tokenbill.verify import receipts as RC
from tokenbill.verify import rollout as R

from .helpers import rtk_campaign

FUZZ = settings(max_examples=120, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

_scalars = st.one_of(st.none(), st.booleans(), st.integers(-2**70, 2**70),
                     st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=8),
                     st.binary(max_size=4))
_json_like = st.recursive(_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=4), inner, max_size=4)),
    max_leaves=12)
_dates = st.dates(min_value=_dt.date(2026, 1, 1),
                  max_value=_dt.date(2026, 3, 31)).map(lambda d: d.isoformat())


@FUZZ
@given(st.lists(st.one_of(
    st.tuples(st.one_of(_dates, st.text(max_size=10)), st.integers(-10**12, 10**12),
              st.integers(-3, 50)),
    _json_like), max_size=40), _dates, _dates)
def test_its_series_parser(series, change: str, placebo: str) -> None:
    try:
        I.event_study_its(series, change_date=change, placebo_date=placebo)
    except TokenbillError:
        pass


_panel_rows = st.lists(st.builds(
    PanelRow, cluster_id=st.sampled_from(["a", "b", "c", "d"]), date_utc=_dates,
    cost_baseline_nano=st.integers(0, 10**12), cost_actual_nano=st.integers(0, 10**12),
    active_dev_days=st.integers(0, 30), arm=st.none(), wave=st.none(), treated=st.booleans(),
    outcome_prs=st.one_of(st.none(), st.integers(0, 50))), max_size=60)


@FUZZ
@given(_panel_rows, st.integers(0, 3))
def test_estimators_on_arbitrary_panels(rows, washout: int) -> None:
    for call in (lambda: E.imputation_did(rows, washout_days=washout, boot=5),
                 lambda: E.cuped_cluster_dim(rows, pre_until="2026-02-01", boot=5),
                 lambda: E.placebo_did(rows, boot=5),
                 lambda: E.quality_lower_bound(rows, design="stepped_wedge", boot=5),
                 lambda: I.event_study_its(P.org_series(rows), change_date="2026-02-15",
                                           placebo_date="2026-01-20")):
        try:
            out = call()
        except TokenbillError:
            continue
        if isinstance(out, tuple) and len(out) == 3:
            assert out[1] <= out[0] <= out[2]


@FUZZ
@given(st.lists(st.one_of(st.dictionaries(
    st.sampled_from(["task_id", "arm", "trial", "success", "order"]),
    st.one_of(st.sampled_from(["task-000", "task-001", "baseline", "candidate"]),
              st.integers(-1, 3), st.booleans(), st.none())), _json_like), max_size=20))
def test_ab_outcome_parser(outcomes) -> None:
    base, cand, _ = rtk_campaign(seed=0, tasks=2, trials=1)
    try:
        A.paired_ab(base, cand, outcomes, pricer=FlatRates(), boot=10)
    except TokenbillError:
        pass


@FUZZ
@given(_json_like)
def test_canonical_bytes(obj) -> None:
    try:
        data = RC.canonical_bytes(obj)
    except TokenbillError:
        return
    assert json.loads(data) == json.loads(json.dumps(obj))
    assert RC.canonical_bytes(json.loads(data)) == data


class _Runner:
    def __call__(self, args, **kw):
        return subprocess.CompletedProcess(args, 1, b"", b"")


@FUZZ
@given(st.one_of(_json_like, st.fixed_dictionaries({
    "payload": st.one_of(st.text(max_size=12), st.binary(max_size=12).map(
        lambda b: base64.b64encode(b).decode())),
    "payloadType": st.sampled_from([RC.PAYLOAD_TYPE, "x"]),
    "signatures": st.one_of(_json_like, st.lists(st.fixed_dictionaries(
        {"sig": st.one_of(st.text(max_size=8), st.integers())}), max_size=2))})))
def test_envelope_parsing(env) -> None:
    signers = Path(__file__)       # any existing file; the fake runner never verifies
    assert RC.verify_envelope(env, allowed_signers=signers, identity="i",
                              runner=_Runner()) in (True, False)
    try:
        RC.envelope_receipt(env)
    except TokenbillError:
        pass


@FUZZ
@given(_json_like)
def test_receipt_rows_and_refusals(obj) -> None:
    for call in (lambda: RC.receipt_row(obj), lambda: RC.refusal_reasons(obj)):
        try:
            call()
        except TokenbillError:
            pass


@FUZZ
@given(st.text(max_size=30))
def test_created_and_arm_parsers(text: str) -> None:
    receipt = {"_type": RC.RECEIPT_TYPE, "subject": {"lever_id": "x"},
               "predicate": {"created": text, "label": "measured"}}
    try:
        RC.receipt_row(receipt)
    except TokenbillError:
        pass
    try:
        arm, date = P.parse_arm(text)
        assert arm and (date is None or len(date) == 10)
    except TokenbillError:
        pass


@FUZZ
@given(st.lists(st.one_of(_dates, st.text(max_size=10)), max_size=4),
       st.one_of(st.none(), st.dictionaries(st.sampled_from(["c0", "c1", "c2", "zz"]),
                                            st.text(max_size=6), max_size=4)),
       st.one_of(st.sampled_from(["0.1", "0.25", "0.3", "x"]), st.decimals(allow_nan=True),
                 st.floats(), st.integers()))
def test_plan_inputs(looks, treated, holdback) -> None:
    try:
        p = R.plan(["c0", "c1", "c2"], lever_id="lever", cluster_kind="team",
                   design="stepped_wedge", waves=2, holdback=holdback, seed=1, pre_panel=None,
                   projection=None, washout_hours=1, looks=looks, treated=treated)
    except TokenbillError:
        return
    assert R.verify_assignment(p)
    assert isinstance(Decimal(R.preregistration(p)["holdback"]), Decimal)


@FUZZ
@given(st.text(max_size=40))
def test_preregistration_parser(text: str) -> None:
    p = R.plan(["c0", "c1"], lever_id="lever", cluster_kind="team", design="stepped_wedge",
               waves=1, holdback="0.25", seed=1, pre_panel=None, projection=None,
               washout_hours=1, looks=())
    fake = p.__class__(**{**{f: getattr(p, f) for f in p.__slots__},
                          "preregistration_json": text,
                          "preregistration_sha256": hashlib.sha256(text.encode()).hexdigest()})
    try:
        R.preregistration(fake)
    except TokenbillError:
        pass
    assert R.is_randomized(fake) in (True, False)


@FUZZ
@given(st.lists(st.text(max_size=6), max_size=6), st.text(max_size=12),
       st.sampled_from(["cluster_rct", "stepped_wedge", "its", "x"]), st.integers(-1, 4),
       st.booleans())
def test_plan_clusters_and_ids(clusters, lever, design, waves, org_wide) -> None:
    try:
        p = R.plan(clusters, lever_id=lever, cluster_kind="mdm_group", design=design,
                   waves=waves, holdback="0.2", seed=3, pre_panel=None, projection=None,
                   washout_hours=2, looks=("2026-02-01",), org_wide_delivery=org_wide)
    except TokenbillError:
        return
    assert R.verify_assignment(p)
    assert sorted(set(p.holdback) | {c for _, m in p.waves for c in m}) == sorted(clusters)


_any_rows = st.lists(st.builds(
    PanelRow, cluster_id=st.one_of(st.sampled_from(["a", "b", ""]), _scalars),
    date_utc=st.one_of(_dates, st.text(max_size=10), _scalars),
    cost_baseline_nano=st.one_of(st.integers(-10**6, 10**12), _scalars),
    cost_actual_nano=st.one_of(st.integers(0, 10**12), _scalars),
    active_dev_days=st.one_of(st.integers(-2, 30), _scalars), arm=_scalars, wave=_scalars,
    treated=st.one_of(st.booleans(), _scalars),
    outcome_prs=st.one_of(st.none(), st.integers(-1, 50), _scalars)), max_size=12)


@FUZZ
@given(_any_rows)
def test_panel_consumers_on_malformed_rows(rows) -> None:
    """Rows with any field types: every panel consumer raises only TokenbillError."""
    for call in (lambda: E.imputation_did(rows, washout_days=1, boot=3),
                 lambda: E.cuped_cluster_dim(rows, pre_until="2026-02-01", boot=3),
                 lambda: E.placebo_cuped(rows, pre_until="2026-02-01", boot=3),
                 lambda: E.quality_lower_bound(rows, design="cluster_rct",
                                               pre_until="2026-02-01", boot=3),
                 lambda: P.org_series(rows), lambda: P.rate_variance(rows),
                 lambda: P.panel_window(rows)):
        try:
            call()
        except TokenbillError:
            pass


_PLAN = R.plan(["a", "b", "c", "d"], lever_id="cc.prompt_cache_ttl.main", cluster_kind="team",
               design="stepped_wedge", waves=2, holdback="0.25", seed=1, pre_panel=None,
               projection=None, washout_hours=1, looks=("2026-02-01",))


@FUZZ
@given(st.dictionaries(st.sampled_from(["panel", "placebo", "placebo_passed", "pre_until",
                                        "channels", "window", "cache_scopes", "look",
                                        "looks_taken", "washout_days", "quality_lower", "boot",
                                        "seed"]), _json_like, max_size=6))
def test_guard_inputs(inputs) -> None:
    """``guards`` on arbitrary ``result_inputs``: only TokenbillError escapes."""
    from .helpers import recon
    try:
        out = L.guards(inputs, plan=_PLAN, reconciliation=recon({"anthropic_api": "reconciled"}),
                       projection=None)
    except TokenbillError:
        return
    assert [g.name for g in out] == list(L.GUARD_NAMES)
