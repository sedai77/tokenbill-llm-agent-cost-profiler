"""``recon_decisions_of`` (E-1): hypothesis properties of the decision merge — sorted union,
order independence, idempotence, and ``ContractViolation`` exactly when two reports disagree."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from tokenbill.core import extensions as ext
from tokenbill.core.errors import ContractViolation, TokenbillError

from .fake_ext import hooks

_KEYS = st.one_of(
    st.builds(lambda s: f"convention:{s}", st.sampled_from(["src_a", "src_b", "h_1", "file.csv"])),
    st.builds(lambda e, m: f"gross_is_list:{e}:{m}",
              st.sampled_from(["enterprise", "org:acme", "cc:platform"]),
              st.sampled_from(["2026-08", "2026-09"])),
    st.builds(lambda e, m: f"plan_fit:{e}:{m}", st.sampled_from(["enterprise", "org:acme"]),
              st.sampled_from(["2026-09"])))
_VALUES = {"convention:": ("excl", "incl", "undecidable"),
           "gross_is_list:": ("true", "false", "unknown"),
           "plan_fit:": ("business", "enterprise", "unknown")}


@st.composite
def _decisions(draw: st.DrawFn) -> tuple[tuple[str, str], ...]:
    keys = draw(st.sets(_KEYS, max_size=5))
    out = []
    for key in sorted(keys):
        prefix = next(p for p in _VALUES if key.startswith(p))
        out.append((key, draw(st.sampled_from(_VALUES[prefix]))))
    return tuple(out)


@given(st.lists(_decisions(), max_size=4))
def test_merge_properties(all_decisions: list[tuple[tuple[str, str], ...]]) -> None:
    reports = [hooks.make_report(d) for d in all_decisions]
    values: dict[str, set[str]] = {}
    for decisions in all_decisions:
        for key, value in decisions:
            values.setdefault(key, set()).add(value)
    conflict = any(len(v) > 1 for v in values.values())
    try:
        merged = ext.recon_decisions_of(reports)
    except ContractViolation:
        assert conflict
        return
    except TokenbillError:  # pragma: no cover - only ContractViolation may escape
        raise AssertionError("unexpected error type") from None
    assert not conflict
    assert merged == tuple(sorted((k, next(iter(v))) for k, v in values.items()))
    assert ext.recon_decisions_of(list(reversed(reports))) == merged   # order independent
    assert ext.recon_decisions_of(reports + reports) == merged          # idempotent
    assert ext.recon_decisions_of([hooks.make_report(merged)]) == merged
