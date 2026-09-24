"""Definition of done (SPEC §21 #5, §2.4, §8.8): no float in the oracle or the generators (money
never float; AST lint), no network, and the content canary is absent from every output these
modules produce, even when it is planted in every free-text attribution field of the input."""

from __future__ import annotations

import ast
import dataclasses
import socket
from pathlib import Path

import tokenbill.synth.lanes_gen as lanes_gen
import tokenbill.synth.oracle as oracle
from tokenbill.common import canonical_json
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.records import Attribution, to_json
from tokenbill.synth.lanes_gen import (
    FAMILIES,
    ab_campaign,
    closed_form,
    family_policies,
    random_lanes,
    rollout_panel,
)

from .helpers import replay

# lanes_gen samples shapes with float probabilities (never money); these functions produce money
# (panel costs, campaign targets, closed-form expectations) and must be float-free
_MONEY_FUNCTIONS = frozenset({"_panel_cells", "rollout_panel", "rollout_truth", "_round",
                              "_fraction", "ab_campaign", "_ab_candidate", "_cost_of",
                              "_closed_a4_gap", "_closed_a5", "_closed_a10", "_closed_a11"})


def _float_uses(path: Path, only: frozenset[str] | None = None) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    scopes: list[ast.AST] = [tree]
    if only is not None:
        scopes = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name in only]
        assert {n.name for n in scopes} == only   # type: ignore[attr-defined]
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                    node.func.id == "float":
                found.append(f"float( at line {node.lineno}")
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                found.append(f"float literal at line {node.lineno}")
    return found


def test_no_float_in_owned_modules() -> None:
    assert _float_uses(Path(oracle.__file__)) == []  # type: ignore[arg-type]
    gen = Path(lanes_gen.__file__)  # type: ignore[arg-type]
    assert _float_uses(gen, _MONEY_FUNCTIONS) == []
    assert not [u for u in _float_uses(gen) if u.startswith("float(")]


def _plant(lane):
    def attr(a: Attribution) -> Attribution:
        return dataclasses.replace(a, team=f"team {CANARY}", agent_type=f"agent {CANARY}",
                                   client_version=f"2.1.270 {CANARY}",
                                   project=f"proj {CANARY}")
    reqs = tuple(dataclasses.replace(r, attribution=attr(r.attribution)) for r in lane.requests)
    return dataclasses.replace(lane, requests=reqs)


def test_canary_never_reaches_replay_outputs() -> None:
    for family in FAMILIES:
        lanes = [_plant(ln) for ln in random_lanes(9, 12, family=family)]
        assert CANARY in canonical_json([to_json(ln) for ln in lanes])
        for policy in family_policies(family):
            res = replay(lanes, policy)
            assert_no_canary(canonical_json(to_json(res)), repr(res))


def test_generated_data_is_content_free() -> None:
    blobs = [canonical_json([to_json(ln) for f in FAMILIES for ln in random_lanes(1, 5, family=f)])]
    for name in ("A.1", "A.5", "A.10", "A.11"):
        lanes, expected = closed_form(name)
        blobs.append(canonical_json([to_json(ln) for ln in lanes]))
        blobs.append(canonical_json(expected))
    blobs.append(canonical_json([to_json(r) for r in rollout_panel(
        clusters=4, weeks=3, true_effect="0.25", waves=2, holdback="0.25", seed=0)]))
    base, cand, outcomes = ab_campaign(tasks=2, trials=2, cost_effect="0.07",
                                       token_effect="-0.38", turn_effect="0.14", seed=0)
    blobs.append(canonical_json([to_json(r) for r in base + cand]))
    blobs.append(canonical_json(outcomes))
    assert_no_canary(*blobs)


def test_no_network_is_needed(monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    lanes, _ = closed_form("A.1")
    assert replay(lanes, "ttl=1h").saving.nano == 1_150_800_000
