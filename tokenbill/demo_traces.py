"""Deterministic synthetic coding-agent traces with planted waste patterns.

Four scenarios simulate the same coding-agent loop — a system prompt of about
3,200 characters, four tool definitions totalling about 2,400 canonical-JSON
characters, and a history that grows by one assistant turn plus one user
tool_result per call (tool results 300-2,500 characters, occasional text-only
turns) — and differ only in the planted waste pattern:

- ``well-behaved``: byte-stable prefix, ``cache_breakpoints=1``; usage is what
  caching per the documented provider rules would bill (reads grow, small
  writes per turn). The control scenario.
- ``timestamp``: identical content except the system prompt embeds
  ``[session 2026-07-26 14:03:{index:02d}]``, so every call's prefix diverges
  inside the system segment; a breakpoint is present but usage shows zero
  cache reads — all input billed uncached.
- ``tool-churn``: the tools tuple order rotates every 4th call (rotated left
  by ``(index // 4) % 4``); zero cache reads.
- ``no-cache``: byte-stable prefix but ``cache_breakpoints=0`` and everything
  billed uncached — one line of config recovers most of the spend.

Usage derivation (the closed-form contract downstream tests rely on)
--------------------------------------------------------------------

Every usage number is derived from the very same canonical rendering the
analyzer uses (``trace.render_segments`` / ``trace.rendered_text``), with a
single rounding rule, so expectations are exact::

    tok(s)  = int(len(s) / trace.CHARS_PER_TOKEN)   # floor of chars / 3.7
    R_i     = trace.rendered_text(call_i)           # tools -> system -> messages
    total_i = tok(R_i)                              # billed total input, all scenarios
    P_i     = trace.common_prefix_chars(call_{i-1}, call_i)     (P_0 = 0)

``well-behaved`` (cache works; one breakpoint at the end of messages)::

    cache_read_input_tokens     = tok(R_i[:P_i]) == int(P_i / 3.7)
    cache_creation_input_tokens = total_i - cache_read_input_tokens
    input_tokens                = 0

Because the run is append-only, ``P_i == len(R_{i-1})``, and therefore
``reads_i == total_{i-1}`` and ``writes_i == total_i - total_{i-1}`` exactly
(call 0: reads 0, writes ``total_0``).

``timestamp`` / ``tool-churn`` / ``no-cache`` (nothing is ever cached)::

    input_tokens = total_i;  cache reads = cache writes = 0

``output_tokens_i = tok(canonical_json(reply_i))`` where ``reply_i`` is the
assistant message produced by call *i*. For ``i < n-1`` that reply is re-sent
verbatim as ``messages[1 + 2*i]`` of call ``i+1`` (whose message segment
renders as exactly ``canonical_json(reply_i)``); the final reply never
re-appears in a request.

Determinism and content sharing across scenarios: every stochastic draw is
seeded via ``common.rng(seed, "tokenbill.demo", ...)`` WITHOUT the scenario
name, so ``no-cache`` renders byte-identically to ``well-behaved``,
``timestamp`` differs only inside the system segment, and ``tool-churn``
differs only in tools order. Call timestamps start at ``_BASE_TS`` and
advance by a uniform 15-45 seconds per call — well inside the provider's
5-minute cache TTL, so TTL never expires cache entries in the demo. The
prefix shared by consecutive calls always exceeds the strictest relevant
minimum cacheable size (call 0 alone renders to well over 1,024 approx
tokens), so cacheability thresholds never bite either.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Any, Final

from tokenbill.common import TokenbillError, canonical_json, rng
from tokenbill.trace import Call, Usage, approx_tokens, common_prefix_chars, rendered_text

__all__ = [
    "SCENARIOS",
    "ScenarioSpec",
    "all_scenarios",
    "scenario",
]


@dataclass(frozen=True)
class ScenarioSpec:
    """Name, blurb, and generation parameters for one demo scenario."""

    name: str
    description: str
    seed: int = 7
    n_calls: int = 14
    model: str = "claude-sonnet-5"


SCENARIOS: Final[dict[str, ScenarioSpec]] = {
    "well-behaved": ScenarioSpec(
        name="well-behaved",
        description=(
            "Byte-stable prefix with one breakpoint; caching works as documented "
            "(reads grow, small writes per turn). The control scenario."
        ),
    ),
    "timestamp": ScenarioSpec(
        name="timestamp",
        description=(
            "A per-call session timestamp inside the system prompt breaks the prefix "
            "on every call: breakpoint present, zero cache reads, all input billed "
            "uncached."
        ),
    ),
    "tool-churn": ScenarioSpec(
        name="tool-churn",
        description=(
            "Tool definition order rotates every 4th call, so the first rendered "
            "segment diverges at each rotation; zero cache reads."
        ),
    ),
    "no-cache": ScenarioSpec(
        name="no-cache",
        description=(
            "Byte-stable prefix but no cache breakpoints at all; everything billed "
            "uncached. One line of config recovers most of the spend."
        ),
    ),
}

# Unix seconds for the first call of every demo run (2026-07-26 14:03 UTC-ish;
# the exact value only matters for TTL arithmetic, which uses deltas).
_BASE_TS: Final[float] = 1_784_037_780.0

_RNG_NS: Final[str] = "tokenbill.demo"

_ZERO_USAGE: Final[Usage] = Usage(
    input_tokens=0,
    cache_read_input_tokens=0,
    cache_creation_input_tokens=0,
    output_tokens=0,
)

_TASK: Final[str] = (
    "CI is red on tests/test_ttl.py::test_expired_entries_stay_dead — entries written "
    "with ttl=0.1 keep coming back from WalCache.get long after they should have "
    "expired, but only when a reader hammers the same key in a loop. Reproduce it, "
    "find the root cause, fix it without changing the public API, and make the whole "
    "suite green. Note anything user-visible in CHANGELOG.md."
)

_PATHS: Final[tuple[str, ...]] = (
    "src/walcache/store.py",
    "src/walcache/ttl.py",
    "src/walcache/lru.py",
    "src/walcache/wal.py",
    "src/walcache/codec.py",
    "tests/test_store.py",
    "tests/test_ttl.py",
)

_COMMANDS: Final[tuple[str, ...]] = (
    "uv run pytest tests/test_ttl.py -q",
    "uv run pytest -q",
    "uv run pytest tests/test_ttl.py::test_expired_entries_stay_dead -q",
    "uv run ruff check src tests",
    "uv run python -m walcache.bench --entries 5000 --ttl 0.1",
)

_QUERIES: Final[tuple[str, ...]] = (
    "expires_at",
    "refresh_on_read",
    "_evict_expired",
    "default_ttl",
    "move_to_end",
)

_TEST_NAMES: Final[tuple[str, ...]] = (
    "test_expired_entries_stay_dead",
    "test_ttl_refresh_on_read",
    "test_lru_eviction_order",
    "test_wal_replay_restores_ttl",
    "test_set_overwrites_expiry",
    "test_sweep_is_incremental",
    "test_get_returns_default_after_expiry",
)

_CODE_LINES: Final[tuple[str, ...]] = (
    "def get(self, key: str, default: bytes | None = None) -> bytes | None:",
    "entry = self._entries.get(key)",
    "if entry is None or entry.expires_at <= self._clock():",
    "    return default",
    "if self._refresh_on_read:",
    "    entry.expires_at = self._clock() + self._default_ttl",
    "self._lru.move_to_end(key)",
    "self._wal.append(Record(op=Op.SET, key=key, value=value))",
    "expires_at = now + (ttl if ttl is not None else self._default_ttl)",
    "with self._lock:",
    "self._entries[key] = Entry(value=value, expires_at=expires_at)",
    "self._evict_expired(now=self._clock())",
    "assert store.get('alpha') is None",
)

_THOUGHTS: Final[tuple[str, ...]] = (
    "Reading the TTL bookkeeping in the store before touching anything.",
    "The failure smells like expiry refresh on the read path; checking get().",
    "Running the focused test file first to confirm the failure mode.",
    "Searching for every site that writes expires_at.",
    "Applying the fix: only refresh expiry on read when refresh_on_read is set.",
    "Re-running the narrow test, then the whole suite.",
)

_STATUS_NOTES: Final[tuple[str, ...]] = (
    "Status: the bug is in WalCache.get — it refreshes expires_at on every read even "
    "when refresh_on_read is False, so a hammered key written with ttl=0.1 never dies. "
    "Next: patch get() to gate the refresh and add a regression test for the read-heavy "
    "path.",
    "Status: reproduced locally — test_expired_entries_stay_dead fails only when the "
    "reader loop keeps the key hot, which matches an unconditional expiry refresh on "
    "read. The WAL replay path is unaffected. Next: minimal patch in store.py plus a "
    "regression test.",
)

_FOLLOW_UPS: Final[tuple[str, ...]] = (
    "Makes sense. Please also add a regression test covering the read-heavy path.",
    "Good find — keep the public API unchanged and note the fix in CHANGELOG.md.",
)

_CLOSING: Final[str] = (
    "Done. WalCache.get no longer refreshes expires_at when refresh_on_read is False; "
    "added tests/test_ttl.py::test_no_refresh_when_disabled as a regression guard. "
    "Full suite is green (uv run pytest -q: 212 passed) and CHANGELOG.md carries a "
    "user-facing note under Unreleased. The diff touches only src/walcache/store.py, "
    "tests/test_ttl.py, and CHANGELOG.md."
)


def _tok(text: str) -> int:
    """The one documented rounding: floor of ``approx_tokens`` (chars / 3.7)."""
    return int(approx_tokens(text))


def _system_prompt(session_tag: str | None) -> str:
    """The ~3,200-char coding-agent system prompt; optionally embeds a session tag."""
    tag_line = f"Session: {session_tag}\n" if session_tag is not None else ""
    return (
        "You are the WalCache coding agent: a senior Python engineer embedded in the\n"
        "walcache repository, an append-only write-ahead-log cache library written in\n"
        "pure stdlib Python (3.10+). Your job on this run: reproduce the reported\n"
        "failure, find the root cause, patch it with the smallest correct change, and\n"
        "leave the whole test suite green.\n"
        + tag_line
        + "\n"
        "Repository layout:\n"
        "  src/walcache/store.py   WalCache: get/set/delete, TTL bookkeeping, locking\n"
        "  src/walcache/ttl.py     expiry clock, refresh_on_read policy, sweep loop\n"
        "  src/walcache/lru.py     size-bounded LRU index over WAL offsets\n"
        "  src/walcache/wal.py     append-only log: records, fsync policy, replay\n"
        "  src/walcache/codec.py   record framing: varint lengths, crc32 trailers\n"
        "  tests/                  pytest suite; tests/test_ttl.py is the hot spot\n"
        "\n"
        "Working rules:\n"
        "  1. Read before you write: never edit a file you have not opened in this\n"
        "     session, and never invent line numbers or contents from memory.\n"
        "  2. Smallest correct diff: prefer a three-line fix over a refactor; do not\n"
        "     reformat untouched code, reorder imports, or rename public symbols.\n"
        "  3. Prove it: after any edit, run the narrowest failing test first, then\n"
        "     the full suite before declaring victory.\n"
        "  4. Public API is frozen: WalCache.get/set/delete signatures, Entry fields,\n"
        "     and the WAL record format must not change without an explicit request.\n"
        "  5. Determinism: seed every randomized test; use the injected clock from\n"
        "     src/walcache/ttl.py — never call time.time() directly in library code.\n"
        "  6. Concurrency: hold self._lock for every mutation of _entries or _lru;\n"
        "     the WAL append is the linearization point and memory state follows it.\n"
        "  7. Failure honesty: if a command fails or its output surprises you, quote\n"
        "     the failing output in your reply instead of paraphrasing around it.\n"
        "  8. Keep CHANGELOG.md current: one line under Unreleased for any behavior\n"
        "     change, phrased for a user of the library, not for its maintainers.\n"
        "\n"
        "Tooling notes: run commands through the run_command tool (uv is available;\n"
        "the suite is 'uv run pytest -q'). Use search_code for cross-file questions\n"
        "before opening files one by one. Edits go through edit_file with exact old\n"
        "and new strings — whitespace matters. read_file returns 1-based numbered\n"
        "lines; trust those numbers over any earlier memory of the file. Never pipe\n"
        "destructive commands; there is no network access from the sandbox.\n"
        "\n"
        "Review checklist for the final summary:\n"
        "  - Show the diff hunk by hunk with one sentence of rationale per hunk.\n"
        "  - Quote the narrow test run and the full suite run, with pass counts.\n"
        "  - Call out any surviving flakiness, xfails, or skipped tests explicitly.\n"
        "  - Quote the CHANGELOG.md entry verbatim so the reviewer can approve it.\n"
        "  - Confirm no files changed beyond the ones named in your summary.\n"
        "  - List the exact commands a reviewer runs to reproduce the result.\n"
        "\n"
        "Definition of done: the originally reported failure is fixed, no other test\n"
        "regressed, the diff is minimal and reviewed line by line in your final\n"
        "summary, and CHANGELOG.md carries the user-facing note. If any of those is\n"
        "impossible, stop and say exactly why rather than approximating success.\n"
    )


def _tool_defs() -> tuple[dict, ...]:
    """The four static tool definitions (~2,400 chars of canonical JSON in total)."""
    return (
        {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from the repository and return its contents "
                "with 1-based line numbers. Use it before every edit; never guess at "
                "file contents from memory. Prefer narrow reads of the region you are "
                "about to change. Binary files are rejected with a clear error, and "
                "output is capped at 4,000 lines — page through longer files with "
                "start_line and end_line."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative path, e.g. src/walcache/store.py",
                    },
                    "start_line": {"type": "integer", "description": "First line, 1-based"},
                    "end_line": {"type": "integer", "description": "Last line, inclusive"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "edit_file",
            "description": (
                "Replace one exact string in a file with another. The old string must "
                "match the current file contents exactly, including whitespace, and "
                "must be unique in the file; the edit fails otherwise. Returns a short "
                "unified-diff style confirmation. To insert new code, use a unique "
                "anchor line as old_str and repeat it inside new_str."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path"},
                    "old_str": {"type": "string", "description": "Exact text to replace"},
                    "new_str": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
        {
            "name": "run_command",
            "description": (
                "Run a shell command from the repository root and return combined "
                "stdout and stderr plus the exit code. No network access; commands "
                "time out after 120 seconds by default. Long output is truncated in "
                "the middle, keeping head and tail — prefer quiet flags like -q so "
                "failures stay readable."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command line to run"},
                    "timeout_s": {
                        "type": "integer",
                        "description": "Optional timeout override in seconds, max 300",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "search_code",
            "description": (
                "Search the repository for a literal string or regular expression and "
                "return matching lines as path:line:text. Case-sensitive by default; "
                "regex uses Python re syntax. Matches are capped at 200 lines — refine "
                "the query or pass a glob when the result reports truncation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal string or regex"},
                    "glob": {"type": "string", "description": "Optional path filter glob"},
                    "max_results": {
                        "type": "integer",
                        "description": "Cap on returned matches, default 200",
                    },
                },
                "required": ["query"],
            },
        },
    )


def _tool_result_body(tool: str, args: dict[str, Any], target: int, r: random.Random) -> str:
    """Realistic tool output of exactly *target* characters (300-2,500 in practice)."""
    lines: list[str] = []
    if tool == "run_command":
        lines.append("$ " + str(args.get("command", "uv run pytest -q")))
    elif tool == "edit_file":
        lines.append(f"Applied 1 edit to {args.get('path', 'src/walcache/store.py')}:")
    size = sum(len(line) + 1 for line in lines)
    lineno = r.randint(1, 120)
    while size <= target:
        if tool == "read_file":
            line = f"{lineno:4d} | {r.choice(_CODE_LINES)}"
            lineno += 1
        elif tool == "run_command":
            outcome = "PASSED" if r.random() < 0.82 else "FAILED"
            line = f"tests/test_ttl.py::{r.choice(_TEST_NAMES)} {outcome}"
        elif tool == "search_code":
            line = f"{r.choice(_PATHS)}:{r.randint(8, 420)}:    {r.choice(_CODE_LINES)}"
        else:  # edit_file: a unified-diff-flavoured hunk with context lines
            sign = r.choice(("-", "+", " ", " "))
            line = f"{sign}   {r.choice(_CODE_LINES)}"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)[:target]


def _tool_exchange(i: int, r: random.Random) -> tuple[dict, dict]:
    """One assistant tool_use turn plus the matching user tool_result turn."""
    tool = r.choice(("read_file", "run_command", "search_code", "edit_file"))
    if tool == "read_file":
        args: dict[str, Any] = {"path": r.choice(_PATHS)}
    elif tool == "run_command":
        args = {"command": r.choice(_COMMANDS)}
    elif tool == "search_code":
        args = {"query": r.choice(_QUERIES)}
    else:
        args = {
            "path": r.choice(_PATHS[:5]),
            "old_str": r.choice(_CODE_LINES),
            "new_str": r.choice(_CODE_LINES),
        }
    tool_use_id = f"toolu_{i:02d}" + "".join(r.choices("0123456789abcdef", k=16))
    assistant = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": r.choice(_THOUGHTS)},
            {"type": "tool_use", "id": tool_use_id, "name": tool, "input": args},
        ],
    }
    body = _tool_result_body(tool, args, target=r.randint(300, 2500), r=r)
    user = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": body}],
    }
    return assistant, user


def _exchanges(seed: int, n_calls: int) -> list[tuple[dict, dict]]:
    """Per-call (assistant reply, next user turn) pairs, shared by all scenarios.

    The assistant half of pair *i* is call *i*'s reply; both halves are
    appended to history before call ``i+1``. The last pair is a text-only
    closing summary (its user half is generated but never sent).
    """
    pairs: list[tuple[dict, dict]] = []
    for i in range(n_calls):
        r = rng(seed, _RNG_NS, "exchange", i)
        if i == n_calls - 1:
            assistant = {"role": "assistant", "content": [{"type": "text", "text": _CLOSING}]}
            user = {"role": "user", "content": "Thanks — merging."}
        elif i > 0 and r.random() < 0.15:  # occasional text-only turn
            assistant = {
                "role": "assistant",
                "content": [{"type": "text", "text": r.choice(_STATUS_NOTES)}],
            }
            user = {"role": "user", "content": r.choice(_FOLLOW_UPS)}
        else:
            assistant, user = _tool_exchange(i, r)
        pairs.append((assistant, user))
    return pairs


def _timestamps(seed: int, n_calls: int) -> list[float]:
    """Call times: _BASE_TS plus uniform 15-45 s gaps (always inside the 5-min TTL)."""
    r = rng(seed, _RNG_NS, "ts")
    out: list[float] = []
    t = _BASE_TS
    for _ in range(n_calls):
        out.append(t)
        t += r.uniform(15.0, 45.0)
    return out


def _is_tool_use(assistant: dict) -> bool:
    content = assistant["content"]
    return isinstance(content, list) and any(
        block.get("type") == "tool_use" for block in content
    )


def scenario(name: str, seed: int = 7) -> list[Call]:
    """Build one named demo scenario, deterministic per *seed*.

    See the module docstring for the exact content construction and the
    closed-form usage derivation.
    """
    spec = SCENARIOS.get(name)
    if spec is None:
        known = ", ".join(sorted(SCENARIOS))
        raise TokenbillError(f"unknown demo scenario {name!r}; known scenarios: {known}")
    n = spec.n_calls
    exchanges = _exchanges(seed, n)
    timestamps = _timestamps(seed, n)
    base_tools = _tool_defs()
    run_id = f"demo-{name}-seed{seed}"

    calls: list[Call] = []
    for i in range(n):
        if name == "timestamp":
            system = _system_prompt(f"[session 2026-07-26 14:03:{i:02d}]")
        else:
            system = _system_prompt(None)
        if name == "tool-churn":
            k = (i // 4) % len(base_tools)
            tools = base_tools[k:] + base_tools[:k]
        else:
            tools = base_tools
        messages: list[dict] = [{"role": "user", "content": _TASK}]
        for assistant, user in exchanges[:i]:
            messages.extend((assistant, user))
        calls.append(
            Call(
                run_id=run_id,
                index=i,
                ts=timestamps[i],
                model=spec.model,
                system=system,
                tools=tools,
                messages=tuple(messages),
                cache_breakpoints=0 if name == "no-cache" else 1,
                usage=_ZERO_USAGE,
                stop_reason="tool_use" if _is_tool_use(exchanges[i][0]) else "end_turn",
            )
        )

    # Second pass: derive usage from the same rendering the analyzer uses.
    cached = name == "well-behaved"
    priced: list[Call] = []
    prev: Call | None = None
    for i, call in enumerate(calls):
        rendered = rendered_text(call)
        total = _tok(rendered)
        reads = writes = 0
        uncached = total
        if cached:
            prefix = common_prefix_chars(prev, call) if prev is not None else 0
            reads = _tok(rendered[:prefix])
            writes = total - reads
            uncached = 0
        usage = Usage(
            input_tokens=uncached,
            cache_read_input_tokens=reads,
            cache_creation_input_tokens=writes,
            output_tokens=_tok(canonical_json(exchanges[i][0])),
        )
        priced.append(replace(call, usage=usage))
        prev = call
    return priced


def all_scenarios(seed: int = 7) -> dict[str, list[Call]]:
    """All four demo scenarios keyed by name, each deterministic per *seed*."""
    return {name: scenario(name, seed) for name in SCENARIOS}
