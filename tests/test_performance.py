"""Performance regression guards for the analysis pipeline.

The pipeline used to re-render every call's full payload ~23x (analyzer LCP,
breaker classification, and one simulate() per detected breaker each
re-serialized the same frozen payloads), turning a 1,500-call trace into
minutes of work. These tests pin the fix two ways: a deterministic
render-count budget (each Call is serialized once), and a generous wall-clock
smoke budget on a mid-size synthetic run that was previously ~20x slower.
"""

from __future__ import annotations

import time

import tokenbill.trace
from tokenbill.analyzer import profile_run
from tokenbill.breakers import detect, repaired_calls
from tokenbill.common import canonical_json
from tokenbill.simulator import simulate
from tokenbill.trace import Call, Run, Usage


def _growing_run(n_calls: int, run_id: str = "run-perf") -> Run:
    """An agent-shaped run: stable system+tools, history grows every call."""
    tools = ({"name": "search", "input_schema": {"type": "object"}},)
    system = "You are a careful agent. " * 40  # ~1000 chars: cacheable prefix
    messages: list[dict] = []
    calls = []
    for i in range(n_calls):
        messages.append({"role": "user", "content": f"step {i}: " + "x" * 300})
        calls.append(
            Call(
                run_id=run_id,
                index=i,
                ts=1_784_000_000.0 + i,
                model="claude-sonnet-5",
                system=system,
                tools=tools,
                messages=tuple(dict(m) for m in messages),
                cache_breakpoints=0,
                usage=Usage(
                    input_tokens=2000 + 90 * i,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                    output_tokens=50,
                ),
                stop_reason="end_turn",
            )
        )
    return Run(run_id=run_id, calls=tuple(calls))


def _pipeline(run: Run) -> None:
    profile_run(run)
    found = detect(run)
    fixed = list(repaired_calls(run, found)) if found else None
    simulate(run, fixed_calls=fixed)


def test_each_call_is_serialized_once_across_the_whole_pipeline(monkeypatch) -> None:
    run = _growing_run(40)
    counted = {"n": 0}

    def counting_canonical_json(value):
        counted["n"] += 1
        return canonical_json(value)

    monkeypatch.setattr(tokenbill.trace, "canonical_json", counting_canonical_json)
    _pipeline(run)

    # One full render pass = one tools + one-per-message serialization per
    # call. Detection/simulation may add a handful for evidence excerpts and
    # repaired components, but nothing per-call-pair: allow 2x for headroom.
    one_pass = sum(1 + len(call.messages) for call in run.calls)
    assert counted["n"] <= 2 * one_pass, (
        f"pipeline serialized payloads {counted['n']} times for a "
        f"{len(run.calls)}-call run (one full pass is {one_pass}); "
        "the per-Call render cache has regressed"
    )


def test_mid_size_run_analyzes_within_wall_clock_budget() -> None:
    # 800 growing-history calls (~100M rendered chars total) took tens of
    # seconds before the render cache; with it the pipeline is ~1-2s. The
    # budget is deliberately loose for slow CI — it still fails hard if the
    # quadratic re-rendering ever returns.
    run = _growing_run(800)
    start = time.perf_counter()
    _pipeline(run)
    elapsed = time.perf_counter() - start
    assert elapsed < 20.0, f"800-call analysis took {elapsed:.1f}s (budget 20s)"
