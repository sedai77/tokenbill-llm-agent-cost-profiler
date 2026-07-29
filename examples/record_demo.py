"""Your first real Token Bill trace: a miniature agent that makes ~12 real
Claude calls with the recorder attached, then leaves a ``trace.jsonl`` ready
for ``tokenbill analyze``.

Prerequisites::

    pip install tokenbill anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."   # console.anthropic.com -> API keys

Run::

    python record_demo.py
    tokenbill analyze trace.jsonl -o report.html

Cost: roughly $0.05 on claude-haiku-4-5 (the default below). The system
prompt is deliberately large and byte-stable so it clears Haiku's 4096-token
minimum cacheable prefix — you will see real ``cache_read`` tokens from turn 2
onward in both the per-turn printout and the report.

The experiment worth doing next: prepend something volatile to SYSTEM_TEXT —
e.g. ``f"[session {turn}] "`` — record to a second file (change
``Path("trace.jsonl")`` to ``Path("trace2.jsonl")`` on the Recorder line), and
analyze that. Cache reads collapse to zero and Token Bill names the exact
breaker with the dollars it would recover. That is the tool catching a
cache-poisoning bug you introduced on purpose.
"""

import os
import sys
from pathlib import Path

from anthropic import Anthropic

from tokenbill.instrument import Recorder

if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit(
        'set your key first: export ANTHROPIC_API_KEY="sk-ant-..."'
        "   (console.anthropic.com -> API keys)"
    )

MODEL = "claude-haiku-4-5"  # cheapest for a first run; any Claude model works
N_TURNS = 12

# Large, deliberately STABLE system prompt (~33k chars ≈ 9k tokens): big
# enough to clear every model's minimum cacheable prefix, byte-identical
# across calls so the provider's prefix cache can actually engage.
SYSTEM_TEXT = (
    "You are a meticulous release-engineering assistant for a data platform. "
    "When asked for a step, answer with one numbered step of at most three sentences. "
) * 220

client = Recorder(Path("trace.jsonl")).wrap(Anthropic())

messages: list[dict] = []
for turn in range(N_TURNS):
    messages.append(
        {
            "role": "user",
            "content": f"Give me step {turn + 1} of a 12-step rollout plan "
            "for a database schema migration.",
        }
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=[
            {
                "type": "text",
                "text": SYSTEM_TEXT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )
    reply = next(block.text for block in resp.content if block.type == "text")
    messages.append({"role": "assistant", "content": reply})
    print(
        f"turn {turn + 1:2d}: uncached_in={resp.usage.input_tokens} "
        f"cache_read={resp.usage.cache_read_input_tokens} "
        f"out={resp.usage.output_tokens}"
    )

print("done — trace.jsonl written; next: tokenbill analyze trace.jsonl -o report.html")
