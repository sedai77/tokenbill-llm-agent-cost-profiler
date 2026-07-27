"""Token Bill: token economics and prompt-cache profiling for LLM agents.

Answers "why is our agent bill so high" with receipts: parses agent traces,
computes per-call token waterfalls from real billed usage, measures what share
of billed input tokens re-sent bytes the model had already seen, simulates what
prompt caching would actually save under the provider's documented rules, and
pinpoints the exact orchestration choices (a timestamp in the system prompt, a
reordered tool list) that break cache hits — each with a concrete fix and the
dollars it recovers.
"""

__version__ = "0.1.0"
