"""Deterministic re-encodings of the v0.1 codebase cache experiments (SPEC §10.4), built from
request payloads with the §5.8 test fingerprinter (:mod:`tests.v2.blocksim.fp`).

Every text is generated from a fixed word list (no volatile values, no real content). Billed usage
is synthetic but shaped like the experiment's recorded billing:

* ``exp1`` — an 8-turn tool loop whose ``cache_control`` marker moves to the newest block each
  turn; billed usage of a working cache (reads = the previous request's input).
* ``exp2b_a3`` — six independent requests (one lane each) sharing a ~20k-character document and
  differing only in the final question; the marker sits on the question, so every request wrote
  its whole prompt.
* ``exp2b_a2`` — five byte-identical requests sent at the same timestamp (one lane each); every
  one billed a full write (none could read the others' entry before its first token).
* ``exp9_1`` — a tool loop whose tool definitions alternate between two JSON key orders; billed
  cold writes every call.
* ``exp6_2`` — each turn appends 25 text blocks and moves the marker to the last one; billed
  full rewrites (the previous entry is 25 positions back, beyond the 20-position lookback).

``build_all()`` returns ``{name: [Lane, …]}``; ``tests/v2/fixtures/blocksim/build_fixtures.py``
writes them as JSON (``core.records.to_json``), and ``test_fixtures.py`` checks the files are
current.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Any

from tests.v2.blocksim.fp import fingerprint, payload_request
from tokenbill.core.builders import make_lane
from tokenbill.core.records import Lane, LaneKind, UsageBuckets

#: 2026-09-23 00:00 UTC.
T0 = 1_790_121_600_000
MODEL = "claude-sonnet-5"
_WORDS = ("cache", "prefix", "block", "token", "ledger", "replay", "window", "schema", "vector",
          "buffer", "stream", "packet", "branch", "merge", "kernel", "module", "planner", "lookup",
          "report", "metric", "service", "adapter", "cluster", "journal", "segment", "archive")


def words(seed: int, n_chars: int) -> str:
    """Deterministic filler text of exactly *n_chars* characters."""
    rnd = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < n_chars:
        w = rnd.choice(_WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)[:n_chars]


def _tools(order: str = "a") -> list[dict[str, Any]]:
    tools = []
    for i, name in enumerate(("read_file", "run_command", "search_code")):
        desc = words(100 + i, 600)
        schema = {"type": "object", "properties": {"arg": {"type": "string"}}}
        if order == "a":
            tools.append({"name": name, "description": desc, "input_schema": schema})
        else:
            tools.append({"input_schema": schema, "description": desc, "name": name})
    return tools


_SYSTEM = words(7, 4200)


def _tokens(tools: Sequence[dict], system: Any, messages: Sequence[dict]) -> int:
    fp, _ = fingerprint(tools, system, messages)
    return sum(b.est_tokens or 0 for b in fp.blocks)


def _mark_last(messages: list[dict]) -> list[dict]:
    """A copy of *messages* with one 5m ``cache_control`` marker on the very last block."""
    out = []
    for i, msg in enumerate(messages):
        content = msg["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        blocks = [dict(b) for b in content]
        if i == len(messages) - 1:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        out.append({"role": msg["role"], "content": blocks})
    return out


def _tool_turn(i: int) -> tuple[dict, dict]:
    tool_id = f"toolu_{i:04d}"
    assistant = {"role": "assistant", "content": [
        {"type": "text", "text": words(300 + i, 160)},
        {"type": "tool_use", "id": tool_id, "name": "read_file", "input": {"arg": words(i, 20)}},
    ]}
    user = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": words(500 + i, 1800)},
    ]}
    return assistant, user


def exp1() -> list[Lane]:
    """8-turn loop, marker moved to the newest block each turn, working-cache billing."""
    tools = _tools()
    messages: list[dict] = [{"role": "user", "content": words(11, 900)}]
    reqs = []
    prev_total = 0
    for i in range(8):
        total = _tokens(tools, _SYSTEM, messages) * 97 // 100      # billed ≈ 3% below estimate
        billed = UsageBuckets(cache_read=prev_total, cache_write_5m=total - prev_total,
                              output=180)
        reqs.append(payload_request("exp1", i, T0 + 25_000 * i, tools=tools, system=_SYSTEM,
                                    messages=_mark_last(messages), billed=billed, model=MODEL,
                                    ttft_ms=900))
        prev_total = total
        messages.extend(_tool_turn(i))
    return [make_lane(reqs, kind=LaneKind.API_RUN, scope="ws:exp", lane_key="exp1")]


DOCUMENT = words(21, 20_000)
QUESTION_CHARS = 7_300


def exp2b_a3() -> list[Lane]:
    """Six independent requests sharing a ~20k-char document; the marker is on the question."""
    lanes = []
    for q in range(6):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": DOCUMENT},
            {"type": "text", "text": words(40 + q, QUESTION_CHARS),
             "cache_control": {"type": "ephemeral"}},
        ]}]
        total = _tokens([], "Answer from the document.", messages)
        billed = UsageBuckets(cache_write_5m=total, output=250)
        key = f"exp2b-a3-{q}"
        lanes.append(make_lane([payload_request(
            key, 0, T0 + 60_000 * q, tools=[], system="Answer from the document.",
            messages=messages, billed=billed, model=MODEL, ttft_ms=1200)],
            kind=LaneKind.API_RUN, scope="ws:exp", lane_key=key))
    return lanes


def exp2b_a2() -> list[Lane]:
    """Five byte-identical requests at the same timestamp (full writes billed for each)."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": words(61, 9000), "cache_control": {"type": "ephemeral"}},
    ]}]
    total = _tokens([], _SYSTEM, messages)
    lanes = []
    for i in range(5):
        key = f"exp2b-a2-{i}"
        billed = UsageBuckets(cache_write_5m=total, output=120)
        lanes.append(make_lane([payload_request(
            key, 0, T0, tools=[], system=_SYSTEM, messages=messages, billed=billed, model=MODEL,
            ttft_ms=1500)], kind=LaneKind.API_RUN, scope="ws:exp", lane_key=key))
    return lanes


def exp9_1() -> list[Lane]:
    """Tool definitions alternate between two key orders; cold writes billed every call."""
    messages: list[dict] = [{"role": "user", "content": words(13, 900)}]
    reqs = []
    for i in range(6):
        tools = _tools("a" if i % 2 == 0 else "b")
        total = _tokens(tools, _SYSTEM, messages)
        billed = UsageBuckets(cache_write_5m=total, output=150)
        reqs.append(payload_request("exp9-1", i, T0 + 20_000 * i, tools=tools, system=_SYSTEM,
                                    messages=_mark_last(messages), billed=billed, model=MODEL,
                                    ttft_ms=800))
        messages.extend(_tool_turn(100 + i))
    return [make_lane(reqs, kind=LaneKind.API_RUN, scope="ws:exp", lane_key="exp9-1")]


def exp6_2() -> list[Lane]:
    """Each turn appends 25 text blocks; the marker moves to the last one; full rewrites."""
    messages: list[dict] = [{"role": "user", "content": words(15, 600)}]
    reqs = []
    for i in range(6):
        messages.append({"role": "user" if i % 2 else "assistant", "content": [
            {"type": "text", "text": words(1000 + 25 * i + j, 220)} for j in range(25)]})
        total = _tokens(_tools(), _SYSTEM, messages)
        billed = UsageBuckets(cache_write_5m=total, output=90)
        reqs.append(payload_request("exp6-2", i, T0 + 30_000 * i, tools=_tools(), system=_SYSTEM,
                                    messages=_mark_last(messages), billed=billed, model=MODEL,
                                    ttft_ms=700))
    return [make_lane(reqs, kind=LaneKind.API_RUN, scope="ws:exp", lane_key="exp6-2")]


EXPERIMENTS: dict[str, Callable[[], list[Lane]]] = {
    "exp1": exp1, "exp2b_a3": exp2b_a3, "exp2b_a2": exp2b_a2, "exp9_1": exp9_1,
    "exp6_2": exp6_2,
}


def build_all() -> dict[str, list[Lane]]:
    return {name: fn() for name, fn in EXPERIMENTS.items()}
