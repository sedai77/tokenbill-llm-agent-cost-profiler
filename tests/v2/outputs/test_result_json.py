"""result@2 JSON (SPEC §14.1): the schema rule, billed-column honesty, determinism, extensions."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    estimated,
    exact,
    figure_json,
)
from tokenbill.core.types import BillSummary, RawAggregate
from tokenbill.outputs import result_json as rj

from . import extension
from .sample import bill, empty_result, full_result, unpriced_bill, usd


def doc() -> dict:
    return rj.to_result_json(full_result(), deterministic=True)


def test_full_result_conforms_and_round_trips_deterministically() -> None:
    d = doc()
    assert rj.validate_result_json(d) == []
    text = rj.dumps_result(full_result(), deterministic=True)
    assert text == rj.dumps_result(full_result(), deterministic=True)
    assert json.loads(text) == d
    assert rj.canonical_dumps(json.loads(text)) == text
    assert "generated_ms" not in d
    for key in ("bill", "data_quality", "reconciliation", "calibration", "findings",
                "action_plan", "policy_packs", "replays", "measure_plan", "measurements", "ab",
                "check", "pricing", "receipts"):
        assert d[key], key


def test_every_slot_key_and_shapes() -> None:
    d = doc()
    assert d["schema"] == "tokenbill/result@2"
    assert d["window"] == {"since": "2026-08-24", "until": "2026-09-23"}
    assert d["bill"]["exact"] == figure_json(full_result().bill.total.exact)
    assert d["bill"]["allowance"]["basis"] == "list_equivalent"
    assert d["bill"]["pool"]["basis"] == "list_equivalent"
    assert d["bill"]["coverage"]["unpriced_inferences"] == 3
    assert d["receipts"] == ["rcpt_a", "rcpt_b"]           # deterministic: sorted
    team = next(b for b in d["bill"]["breakdowns"] if b["dims"] == "team")
    unknown = [r for r in team["rows"] if r["notes"] == ["users_unknown"]]
    assert unknown and unknown[0]["n_users"] is None       # R-E47: never a 0 count
    assert all("readme" not in k or k == "readme_sha256" for k in d["policy_packs"][0])


def test_non_deterministic_carries_generated_ms() -> None:
    d = rj.to_result_json(full_result())
    assert isinstance(d["generated_ms"], int) and d["generated_ms"] > 0
    assert rj.validate_result_json(d) == []


def test_money_json_is_figure_json() -> None:
    fig = estimated(usd("1.5"), Basis.LIST, low=1, high=usd("2"),
                    calibration=Calibration.CALIBRATED)
    assert rj.money_json(fig) == figure_json(fig)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["bill"]["exact"].__setitem__("nano", 1.5), "float"),
    (lambda d: d["privacy"].pop("evidence"), "no evidence key"),
    (lambda d: d["bill"].__setitem__("total_nano", 5), "money not in MONEY form"),
    (lambda d: d["bill"].__setitem__("spend_usd", "5.00"), "money not in MONEY form"),
    (lambda d: d["bill"]["exact"].pop("finality"), "money not in MONEY form"),
    (lambda d: d["bill"]["exact"].__setitem__("usd", "1.00"), "exact decimal string"),
    (lambda d: d["bill"]["exact"].__setitem__("basis", "list_equivalent"), "list_equivalent"),
    (lambda d: d.__setitem__("schema", "tokenbill/result@1"), "schema"),
    (lambda d: d.pop("findings"), "missing top-level"),
    (lambda d: d["inputs"][0].__setitem__("evidence", "certain"), "not an Evidence value"),
])
def test_validate_rejects(mutate, message) -> None:
    d = copy.deepcopy(doc())
    mutate(d)
    errors = rj.validate_result_json(d)
    assert any(message in e for e in errors), errors


def test_validate_accepts_hand_made_money_and_rejects_bad_range() -> None:
    money = figure_json(estimated(5, Basis.LIST, low=1, high=9, note="x"))
    assert rj.rule_violations({"x": money}) == []
    money["range"]["low_usd"] = "0.1"
    assert rj.rule_violations({"x": money})
    money2 = figure_json(exact(3, Basis.LIST))
    money2["upper_bound"] = "no"
    money2["provenance"] = [1]
    money2["ci_level_pct"] = "95"
    money2["note"] = 5
    money2["evidence"] = "sure"
    money2["basis"] = "cash"
    assert len(rj.rule_violations({"x": money2})) == 6
    assert rj.rule_violations([1]) == ["/: document is not an object"]
    assert rj.rule_violations({"a": {"b": [1, {"c": 2, "evidence": "exact"}]}}) == [
        "/a: object with integer values has no evidence key"]
    assert rj.rule_violations({1: "x"})
    unpriced = figure_json(Figure(nano=None, evidence=Evidence.EXACT, basis=Basis.LIST,
                                  note="unpriced: x"))
    assert rj.rule_violations({"exact": unpriced}) == []
    unpriced["usd"] = "0"
    assert rj.rule_violations({"exact": unpriced})
    assert rj.rule_violations({"range": {"low_nano": 1.0}, "evidence": "exact"})


def test_list_equivalent_is_allowed_outside_billed_keys() -> None:
    le = figure_json(exact(5, Basis.LIST_EQUIVALENT))
    assert rj.rule_violations({"allowance": le}) == []
    for key in rj.BILLED_KEYS:
        assert rj.rule_violations({key: le}), key


def test_billed_slots_refuse_non_billed_figures() -> None:
    b = bill()
    est_total = dataclasses.replace(b.total, exact=estimated(5, Basis.LIST, note="x"))
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(bill=dataclasses.replace(b, total=est_total)))
    le_total = dataclasses.replace(b.total, exact=exact(5, Basis.LIST_EQUIVALENT))
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(bill=dataclasses.replace(b, total=le_total)))
    wrong_allowance = dataclasses.replace(b.total, allowance=exact(5, Basis.LIST))
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(bill=dataclasses.replace(b, total=wrong_allowance)))
    le_estimate = dataclasses.replace(b.total, estimated=estimated(5, Basis.LIST_EQUIVALENT,
                                                                   note="x"))
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(bill=dataclasses.replace(b, total=le_estimate)))
    plan = full_result().action_plan
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(action_plan=dataclasses.replace(
            plan, headline_monthly=estimated(5, Basis.LIST_EQUIVALENT, note="x"))))
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(action_plan=dataclasses.replace(
            plan, pool_headroom_monthly=estimated(5, Basis.LIST, note="x"))))
    with pytest.raises(ContractViolation):
        rj.require_billed("5", "x")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        rj.require_allowance("5", "x")  # type: ignore[arg-type]


def test_breakdowns_accept_only_published_aggregates() -> None:
    raw = RawAggregate(group_by=("team",), rows=(), window=(0, 1))
    bad = BillSummary(total=bill().total, esr=None, breakdowns=(("team", raw),))  # type: ignore
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(bill=bad))
    with pytest.raises(ContractViolation):
        rj.to_result_json("not a result")  # type: ignore[arg-type]


def test_unpriced_bill_is_null_not_zero() -> None:
    d = rj.to_result_json(empty_result(bill=unpriced_bill()), deterministic=True)
    assert d["bill"]["exact"]["nano"] is None and d["bill"]["exact"]["usd"] is None
    assert d["bill"]["exact"]["note"].startswith("unpriced:")
    assert rj.validate_result_json(d) == []


def test_empty_result_conforms() -> None:
    d = rj.to_result_json(empty_result(), deterministic=True)
    assert rj.validate_result_json(d) == []
    assert d["bill"] is None and d["findings"] == [] and d["rate_card"] is None


def test_canonical_dumps_refuses_floats() -> None:
    with pytest.raises(ContractViolation):
        rj.canonical_dumps({"a": [{"b": 0.5}]})
    assert rj.canonical_dumps({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_deterministic_orders_arrays_by_stable_ids() -> None:
    r = full_result()
    shuffled = dataclasses.replace(r, findings=tuple(reversed(r.findings)),
                                   inputs=tuple(reversed(r.inputs)),
                                   data_quality=tuple(reversed(r.data_quality)))
    assert rj.dumps_result(shuffled, deterministic=True) == rj.dumps_result(r, deterministic=True)


def test_policy_spec_falls_back_to_the_name() -> None:
    class Weird:
        name = "odd"

        def spec(self) -> str:
            raise ValueError("no grammar")

    assert rj.policy_spec(Weird()) == "odd"


def test_extension_section_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    extension.install(monkeypatch)
    r = full_result(copilot=extension.summary())
    d = rj.to_result_json(r, deterministic=True)
    assert d["copilot"]["invoice"]["basis"] == "invoice"
    assert rj.validate_result_json(d) == []
    no_slot = rj.to_result_json(full_result(), deterministic=True)
    assert "copilot" not in no_slot


def test_unavailable_extension_renderer_becomes_a_dq_note(monkeypatch: pytest.MonkeyPatch) -> None:
    extension.install(monkeypatch, renderer="tokenbill.copilot.absent_module:Section")
    d = rj.to_result_json(full_result(copilot=extension.summary()), deterministic=True)
    assert "copilot" not in d
    assert any(n["code"] == "dq.extension_unavailable" for n in d["data_quality"])


def test_extension_key_collision_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.core import registry

    spec = dataclasses.replace(extension.spec(), name="bill")
    monkeypatch.setattr(registry, "EXTENSIONS", {"bill": spec})
    with pytest.raises(ContractViolation):
        rj.to_result_json(full_result(), deterministic=True)


def test_date_of() -> None:
    assert rj.date_of(0) == "1970-01-01"
    assert rj.date_of(86_400_000 * 365) == "1971-01-01"


figures = st.builds(
    lambda nano, lo, hi, ev, basis, fin: (
        Figure(nano=nano, evidence=Evidence.EXACT,
               basis=Basis.LIST if basis is Basis.INVOICE else basis, finality=fin)
        if ev is Evidence.EXACT else
        Figure(nano=nano, evidence=ev, basis=Basis.LIST if basis is Basis.INVOICE else basis,
               low_nano=min(lo, nano), high_nano=max(hi, nano), finality=fin,
               ci_level_pct=None if ev is Evidence.ESTIMATED else 95,
               calibration=Calibration.UNCALIBRATED)),
    st.integers(-10**15, 10**15), st.integers(-10**15, 10**15), st.integers(-10**15, 10**15),
    st.sampled_from(list(Evidence)), st.sampled_from(list(Basis)), st.sampled_from(list(Finality)))


@settings(max_examples=150, deadline=None)
@given(figures)
def test_every_figure_encodes_to_a_valid_money_object(fig: Figure) -> None:
    assert rj.rule_violations({"value": figure_json(fig)}) == []


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.sampled_from(["usd", "nano", "evidence", "basis", "range", "a", "b_nano",
                                       "exact", "note"]), children, max_size=5),
    max_leaves=20)


@settings(max_examples=300, deadline=None)
@given(json_values)
def test_validator_never_raises(value: object) -> None:
    errors = rj.validate_result_json(value)  # type: ignore[arg-type]
    assert isinstance(errors, list)
    if isinstance(value, float):
        assert errors
