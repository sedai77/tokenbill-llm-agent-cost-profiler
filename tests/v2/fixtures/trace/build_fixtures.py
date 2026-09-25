"""Build the TRACE fixtures (run as a script from anywhere: ``python build_fixtures.py``).

Every file is synthetic and deterministic; ``tests/v2/trace/test_fixtures.py`` fails when a
checked-in file differs from a fresh build. Content fields of the trace@1 files carry the content
canary (SPEC §8.8); the trace@2 ``usage`` and ``fingerprint`` files must not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.v2.trace.helpers import conversation, sample, tools  # noqa: E402
from tokenbill.core.builders import CANARY  # noqa: E402

TS0 = 1_790_000_000.25   # 2026-09-21T13:33:20.250Z


def _call(run_id: str, index: int, model: str, messages: list[dict[str, Any]], *,
          count: int, usage: tuple[int, int, int, int], ts: float) -> dict[str, Any]:
    u_in, u_read, u_write, u_out = usage
    return {"schema": "tokenbill/trace@1", "run_id": run_id, "index": index, "ts": ts,
            "model": model, "system": f"You are a coding agent. {CANARY}",
            "tools": tools(), "messages": messages, "cache_breakpoints": count,
            "usage": {"input_tokens": u_in, "cache_read_input_tokens": u_read,
                      "cache_creation_input_tokens": u_write, "output_tokens": u_out},
            "stop_reason": "tool_use"}


def trace1_runs() -> list[dict[str, Any]]:
    """Run ``agent``: four calls on Sonnet 5, the third with an explicit 5m marker, the fourth
    with a 1h marker. Run ``mixed``: Opus 5.5 and Haiku 4.5 interleaved (two inferred lanes)."""
    lines = []
    for i in range(4):
        msgs = conversation(i, filler=5)
        if i == 2:
            msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        if i == 3:
            msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        lines.append(_call("agent", i, "claude-sonnet-5", msgs, count=1,
                           usage=(3, 1000 * i, 400, 50 + i), ts=TS0 + 20 * i))
    for i, model in enumerate(["claude-opus-5-5", "claude-haiku-4-5", "claude-opus-5-5",
                               "claude-haiku-4-5"]):
        lines.append(_call("mixed", i, model, conversation(1, filler=5), count=0,
                           usage=(2000, 0, 0, 10), ts=TS0 + 100 + 5 * i))
    return lines


def trace1_malformed() -> list[str]:
    good = [json.dumps(c) for c in trace1_runs()[:2]]
    bad_usage = trace1_runs()[2]
    del bad_usage["usage"]["output_tokens"]
    wrong_type = trace1_runs()[3]
    wrong_type["cache_breakpoints"] = "one"
    return [good[0], "{not json", "[1, 2]", json.dumps({"schema": "tokenbill/trace@9"}),
            json.dumps(bad_usage), json.dumps(wrong_type), good[1],
            json.dumps({**trace1_runs()[0], "index": 0}),
            json.dumps({**trace1_runs()[1], "run_id": "other", "ts": -5.0})]


def main() -> None:
    (HERE / "trace1_runs.jsonl").write_text(
        "".join(json.dumps(c, sort_keys=True, separators=(",", ":")) + "\n"
                for c in trace1_runs()), encoding="utf-8")
    (HERE / "trace1_malformed.jsonl").write_text("\n".join(trace1_malformed()) + "\n",
                                                 encoding="utf-8")
    for profile in ("usage", "fingerprint", "full"):
        sample(profile).write(HERE / f"trace2_{profile}.jsonl")
    sample("usage").write(HERE / "trace2_usage.jsonl.gz")
    lines = (HERE / "trace2_usage.jsonl").read_text(encoding="utf-8").splitlines()
    header, rest = lines[0], lines[1:]
    bad = [
        '{"rec":"event","schema":"tokenbill/trace@2","lane_key":"x","ts_ms":1,"kind":"clear",'
        '"attrs":{},"color":"red"}',
        '{"rec":"dq","schema":"tokenbill/trace@2","code":"dq.x","severity":"info","count":1.5,'
        '"detail":"d"}',
        '{"rec":"mystery","schema":"tokenbill/trace@2"}',
        '{"rec":"blocks","schema":"tokenbill/trace@2","key_id":"k_000000000000","blocks":[]}',
        '{"rec":"dq","schema":"tokenbill/trace@2","code":"dq.x","severity":"info","count":1,'
        '"detail":"' + "x" * 300 + '"}',
    ]
    (HERE / "trace2_malformed.jsonl").write_text("\n".join([header, *bad, *rest]) + "\n",
                                                 encoding="utf-8")


if __name__ == "__main__":
    main()
