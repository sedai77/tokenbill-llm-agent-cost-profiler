"""Publication of ``copilot.lanes`` findings through ``core.kanon`` (addendum §8.2, R-E16): every
scope maps to a ``requests`` count filter (the catalog's count source), team-scoped findings are
never exempt — ``aggregate`` category notwithstanding — and re-scope on the Copilot chain keeping
``product`` and ``billing_class``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tokenbill.core import kanon
from tokenbill.core.types import Finding, Scope

from .helpers import attr, ctx, lane, req, run
from .test_conformance import world
from .test_long_context import G5


class _Ledger:
    """A ledger stand-in recording the ``where`` filters ``scope_counter`` sends."""

    def __init__(self, users: int) -> None:
        self.users = users
        self.calls: list[dict[str, str]] = []

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    **_: Any) -> int:
        self.calls.append(dict(where))
        return self.users


def _findings() -> list[Finding]:
    return run(world(), ctx(min_usd="0.10"))


def test_every_scope_maps_to_a_requests_filter() -> None:
    found = _findings()
    ledger = _Ledger(users=9)
    count = kanon.scope_counter(ledger, since_ms=0, until_ms=1)  # type: ignore[arg-type]
    for f in found:
        assert count(f, f.scope) == 9, f.scope.dims
        where = ledger.calls[-1]
        dims = dict(f.scope.dims)
        assert where["channel"] == "github_copilot"
        assert where["billing_class"] == "pool" and where["lane_kind"] == dims["lane_kind"]
        for key in ("team", "model", "workload_class", "agent_product"):
            assert where.get(key) == dims.get(key)


def test_small_teams_rescope_on_the_copilot_chain() -> None:
    found = _findings()

    def users(_f: Finding, scope: Scope) -> int:
        return 2 if any(k == "team" for k, _ in scope.dims) else 12

    published = kanon.rescope_findings(found, k=5, count_users=users)
    assert published
    for f in published:
        dims = dict(f.scope.dims)
        assert "team" not in dims and "lane_kind" not in dims
        assert dims["product"] == "copilot" and dims["billing_class"] == "pool"
    kinds = {f.kind for f in published}
    assert {"subagent-share", "ci-uncapped"} <= kinds     # aggregate kinds are not exempt


def test_teams_at_k_publish_unchanged() -> None:
    lanes = [lane(f"vs-{i}", [req(f"vs-{i}", 0, 0, a=attr(principal=f"r_dev{i}"), **G5)])
             for i in range(6)]
    [f] = run(lanes, ctx())
    assert f.kind == "long-context-band" and f.n_users == 6
    assert kanon.rescope_findings([f], k=5, count_users=lambda _f, _s: 6) == [f]
