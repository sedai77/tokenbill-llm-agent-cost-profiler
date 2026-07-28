# Token Bill Internals Specification

Authoritative contract for Token Bill's architecture. Contributors implement against
these interfaces exactly. Public rationale lives in DESIGN.md; this is the "what".

## What Token Bill is

A profiler for LLM agent spend. Given a **trace** (the sequence of API calls one agent
run made: full request payloads + real billed usage), it produces:

1. **Token waterfalls** — per call and per run: cached reads vs cache writes vs
   uncached input vs output, in tokens and dollars, from the trace's real `usage`.
2. **Redundancy analysis** — what fraction of cumulative billed input tokens
   re-sent byte-identical prefix the model had already seen earlier in the same
   run. (A token share, deliberately: a *spend* share would differ whenever a
   run mixes rate classes — reads 0.1x, writes 1.25x, uncached 1.0x — so the
   headline never claims dollars it did not compute.)
3. **Cache simulation** — replays the trace under documented provider cache rules in
   four scenarios: as-billed, no-cache, optimal-cache, and fixed-cache (after
   repairing detected breakers), each priced in dollars.
4. **Cache-breaker detection** — pinpoints where consecutive calls' prefixes diverge,
   classifies the cause (volatile system prompt, tool churn, history rewrite, model
   switch, missing breakpoints), and attaches a concrete fix + dollars recovered.
5. A **single-file HTML report** (inline SVG waterfall + scenario bars) and an
   aligned terminal table.

Honesty rules baked into the design:

- **All dollar totals come from real billed `usage` fields** (input_tokens,
  cache_read_input_tokens, cache_creation_input_tokens, output_tokens). Exact.
- **Approximate tokenization is used ONLY for proportional attribution** (splitting a
  call's billed input across system/tools/history segments, locating divergence
  points in token terms). It is chars-based, clearly labeled "approx" everywhere it
  surfaces, and every approximate number is scaled so segments sum to the call's
  billed total. Never use tiktoken (wrong tokenizer for Claude); never present an
  approximate number as billed.
- The demo runs on bundled deterministic synthetic traces with **planted waste
  patterns** the analyzer must recover — flagship-test style, same as Judgemetry.

**Zero runtime dependencies** (pure stdlib). Python 3.10+. The recorder helper
(`instrument.py`) duck-types against the Anthropic SDK client without importing it.

## Module map and ownership

```
tokenbill/
  __init__.py     version (exists)
  common.py       errors, rng(seed,*scope), canonical_json (exists — do not modify)
  trace.py        trace schema, dataclasses, JSONL IO, canonical rendering [Module A]
  demo_traces.py  deterministic synthetic agent scenarios with planted waste [Module A]
  pricing.py      pricing + cache-rule tables as versioned, sourced data [Module B]
  analyzer.py     waterfalls, redundancy, segment attribution           [Module B]
  instrument.py   SDK-level trace recorder (duck-typed wrap)            [Module B]
  simulator.py    cache-scenario replay engine                         [Module C]
  breakers.py     divergence detection + classification + fixes        [Module C]
  report.py       single-file HTML report (inline SVG)                 [Module D]
  cli.py          argparse: demo / analyze / --version                 [Module D]
  py.typed, __main__.py                                               [Module D]
tests/
  test_trace.py, test_demo_traces.py                                  [Module A]
  test_pricing.py, test_analyzer.py, test_instrument.py               [Module B]
  test_simulator.py, test_breakers.py                                 [Module C]
  test_report.py, test_cli.py                                         [Module D]
  test_demo_recovers_planted_waste.py   (flagship)                    [Module C]
README.md, DESIGN.md, LICENSE, CONTRIBUTING.md, CHANGELOG.md, SECURITY.md,
CODE_OF_CONDUCT.md, Makefile, .github/ (ci.yml, release.yml, templates)  [Module F]
```

Style rules: `from __future__ import annotations`; full type hints; dataclasses;
stdlib logging; docstrings on public functions; every stochastic step seeds via
`common.rng`. Same conventions as the sibling projects (WalFlux, Judgemetry).

---

## Module A — `trace.py` and `demo_traces.py`

### Trace schema (`tokenbill/trace@1`, JSONL, one API call per line)

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int                  # uncached input (per provider semantics)
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int

    @property
    def total_input(self) -> int: ...  # sum of the three input components

@dataclass(frozen=True)
class Call:
    run_id: str
    index: int                 # 0-based position within the run
    ts: float                  # unix seconds at request time (drives TTL simulation)
    model: str
    system: str                # rendered system prompt ("" if none)
    tools: tuple[dict, ...]    # tool definitions as sent, order preserved
    messages: tuple[dict, ...] # messages as sent, order preserved (role + content;
                               # content is str or list of content-block dicts)
    cache_breakpoints: int     # how many cache_control markers the request carried
    usage: Usage
    stop_reason: str

@dataclass(frozen=True)
class Run:                     # all calls sharing a run_id, sorted by index
    run_id: str
    calls: tuple[Call, ...]
```

JSONL IO: `read_trace(path) -> list[Run]` / `write_trace(path, calls)` with a
`schema` field per line and precise `TraceError` messages (path, 1-based line,
missing/invalid field). Reject: non-monotonic `index` within a run, negative usage,
unknown schema tag (name what was found and what is supported).

### Canonical rendering (the byte-comparison substrate)

```python
def render_segments(call: Call) -> list[Segment]
# Segment = (kind, label, text) where kind ∈ {"tools", "system", "message"};
# order MUST be: tools (one segment, canonical_json of the tuple), system (one
# segment), then one segment per message (canonical_json of the message dict).
# This mirrors the provider's documented render order tools → system → messages.

def rendered_text(call: Call) -> str          # concatenation of segment texts
def common_prefix_chars(a: Call, b: Call) -> int
def diverging_segment(prev: Call, cur: Call) -> tuple[int, str] | None
# index+kind of the first segment whose text differs (None = cur extends prev).
```

`approx_tokens(text: str) -> float` lives here: `len(text) / 3.7` (documented,
tunable constant `CHARS_PER_TOKEN = 3.7` with a comment on why approximate is
acceptable for attribution-only use).

### `demo_traces.py` — planted-waste scenarios

```python
@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    description: str
    seed: int = 7
    n_calls: int = 14
    model: str = "claude-sonnet-5"

def scenario(name: str, seed: int = 7) -> list[Call]   # by name below
def all_scenarios(seed: int = 7) -> dict[str, list[Call]]
```

Four scenarios, each a simulated coding-agent loop (system prompt ~3200 chars, 4
tool definitions ~2400 chars total, per-call: assistant tool_use + user tool_result
appended to history, tool results 300-2500 chars drawn via common.rng, occasional
text-only turns; realistic content — file paths, diffs, command output — generated
deterministically):

1. `well-behaved` — byte-stable prefix, `cache_breakpoints=1`; usage computed AS IF
   caching worked per the provider rules (reads grow, small writes per turn).
2. `timestamp` — identical except the system prompt embeds `[session 2026-07-26
   14:03:{index:02d}]` so every call's prefix diverges inside the system segment;
   `cache_breakpoints=1` but usage shows zero cache reads (all input billed
   uncached) — the planted "volatile system prompt" waste.
3. `tool-churn` — tools tuple order rotates every 4th call; zero cache reads.
4. `no-cache` — byte-stable prefix but `cache_breakpoints=0` and usage bills
   everything uncached — the planted "just add a breakpoint" waste.

Usage numbers must be **internally consistent**: for every call, usage input
components are derived from the same approx_tokens rendering the analyzer uses
(rounded to int), so the flagship test can state exact expectations. Document the
derivation in the module docstring. Every scenario is deterministic per seed.

Tests: JSONL round-trip + malformed-line errors; render order and canonical
stability (dict key order shuffled in input → identical rendering); scenario
determinism; planted properties hold (e.g. `timestamp` scenario: consecutive calls'
diverging_segment is the system segment; `well-behaved`: cur extends prev).

---

## Module B — `pricing.py`, `analyzer.py`, `instrument.py`

### `pricing.py` — versioned data, sourced comments

```python
@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_multiplier: float = 1.25   # 5-minute TTL writes
    cache_read_multiplier: float = 0.10
    min_cacheable_prefix_tokens: int = 1024

PRICING: dict[str, ModelPricing]  # keyed by model id; source comment per entry
```

Authoritative values (verified 2026-07; keep a "verify before each release" comment
+ the doc URL https://platform.claude.com/docs/en/about-claude/pricing.md):

| model | in/MTok | out/MTok | min cacheable prefix |
|---|---|---|---|
| claude-opus-5 | 5.00 | 25.00 | 512 |
| claude-fable-5 | 10.00 | 50.00 | 512 |
| claude-opus-4-8 | 5.00 | 25.00 | 1024 |
| claude-opus-4-7 | 5.00 | 25.00 | 2048 |
| claude-opus-4-6 | 5.00 | 25.00 | 4096 |
| claude-sonnet-5 | 3.00 | 15.00 | 1024 |
| claude-sonnet-4-6 | 3.00 | 15.00 | 1024 |
| claude-haiku-4-5 | 1.00 | 5.00 | 4096 |

`price_usd(model, usage) -> float | None` (None + a warning for unknown models,
logged ONCE per model id, not per lookup — never crash; tokens still reported).
`cost_breakdown(model, usage) -> dict[str, float]` (uncached/write/read/output
dollars).

Rate caveat: claude-sonnet-5 carries introductory billing ($2.00/$10.00 per MTok)
through 2026-08-31; the table deliberately pins the standard $3.00/$15.00 rates
above, and the report's pricing footnote must disclose that sonnet-5 dollar
figures can overstate real bills during the introductory window.

Cache-rule constants (same file, same sourcing):
`CACHE_TTL_SECONDS = 300`, `TTL_REFRESH_ON_READ = True` (documented assumption with
comment), `MAX_BREAKPOINTS = 4`, `RENDER_ORDER = ("tools", "system", "messages")`.

### `analyzer.py`

```python
@dataclass(frozen=True)
class CallProfile:
    call: Call
    dollars: dict[str, float] | None      # cost_breakdown or None
    segments: list[SegmentShare]          # per-segment approx attribution
    repeated_prefix_chars: int            # LCP with previous call (0 for first)
    repeated_fraction_of_input: float     # approx: repeated chars / rendered chars

@dataclass(frozen=True)
class RunProfile:
    run: Run
    calls: list[CallProfile]
    totals: RunTotals    # tokens by category, dollars by category (billed),
                         # redundancy_fraction: share of cumulative billed input
                         # tokens attributable to re-sent identical prefix that was
                         # NOT served from cache (the headline number), plus its
                         # approx-basis flag

def profile_run(run: Run) -> RunProfile
def profile_trace(runs: list[Run]) -> list[RunProfile]
```

Redundancy definition (state in docstring + DESIGN.md): for call i>0, the re-sent
prefix is `common_prefix_chars(call[i-1], call[i])` of rendered text; its token
value is the call's billed `total_input` scaled by the char fraction; the portion
already served as `cache_read_input_tokens` is subtracted (cache reads are cheap —
they are not waste). Sum over calls / sum of billed input = redundancy fraction.
Approximate by construction; label it.

### `instrument.py` — recording real traces

```python
class Recorder:
    def __init__(self, path: Path, run_id: str | None = None): ...
    def wrap(self, client: Any) -> Any
        # duck-typed: wraps client.messages.create and client.messages.stream;
        # captures model/system/tools/messages/cache_control count at call time and
        # usage+stop_reason from the returned Message (for stream: wraps the
        # context manager and reads get_final_message()). Async clients are
        # supported: a coroutine-function create is awaited before recording, and
        # the stream wrapper implements the async context-manager protocol.
        # Responses with no `usage` (raw messages.create(stream=True)) are NOT
        # recorded — a zero-usage line would understate spend — and a warning
        # (once per recorder) points at messages.stream instead. Appends one trace
        # line immediately per completed call (crash-safe; index allocation and
        # append share one lock so concurrent calls stay monotonic). No anthropic
        # import; raises TokenbillError with a clear message if the object lacks
        # messages.create.
```

Also `recording(path)` contextmanager sugar. Tests use a fake client double (five
lines) — never the real SDK.

---

## Module C — `simulator.py` and `breakers.py`

### `simulator.py`

```python
@dataclass(frozen=True)
class ScenarioResult:
    name: str            # "as-billed" | "no-cache" | "optimal-cache" | "fixed-cache"
    tokens: dict[str, int]
    dollars: float | None
    note: str            # one-line honest description of assumptions

def simulate(run: Run, fixed_calls: list[Call] | None = None) -> list[ScenarioResult]
```

Scenario semantics (document precisely in DESIGN.md):

- **as-billed**: straight from usage × pricing. Ground truth.
- **no-cache**: all input at full rate (cache_read + cache_creation + input, all
  priced as uncached input; no write premium).
- **optimal-cache**: replay calls in ts order applying the documented rules to the
  RENDERED text: a cache entry exists for the longest previously-written prefix
  that (a) was written under the SAME model (prompt caches are per-model), (b) is
  byte-identical to the current call's prefix, (c) was written by a call
  whose ts is within CACHE_TTL_SECONDS (sliding on read per TTL_REFRESH_ON_READ),
  (d) meets min_cacheable_prefix_tokens (approx tokens). Charge: matched prefix at
  read rate; the remainder at the write-premium rate ONLY when the written entry is
  read by a later call in the replay (retrospective accounting — an optimal policy
  knows the whole run and never pays a premium for an entry nothing reads back,
  including the final call's always-never-read extension), else at the plain
  uncached rate. The scenario's dollars are clamped at the no-cache price (not
  caching is always available to an optimal policy). Breakpoint assumed at end of
  messages each call — optimal placement; cap MAX_BREAKPOINTS is not binding for
  this single-breakpoint policy but state it.
  Token counts derive from the call's billed total_input split by char fractions
  (approx; scale so each call's input total matches billed).
- **fixed-cache**: optimal-cache applied to `fixed_calls` when provided (the
  breaker-repaired rendering from breakers.py), else same as optimal.

Simulator validation hook: when a run's billed usage shows nonzero cache activity,
compare as-billed cache reads vs optimal-cache predicted reads and report the
agreement ratio in the ScenarioResult note (this is the honesty check the README
cites: on the demo's well-behaved scenario the two must agree within rounding, and
a test asserts it).

### `breakers.py`

```python
@dataclass(frozen=True)
class Breaker:
    kind: str            # "volatile-system" | "tool-churn" | "history-rewrite"
                         # | "model-switch" | "missing-breakpoint"
    first_call_index: int
    evidence: str        # e.g. the exact changed span, truncated, with char offsets
    fix: str             # one concrete sentence, e.g. "move the timestamp out of the
                         # system prompt (inject it in the latest user message)"
    est_recovered_usd: float | None   # as-billed minus fixed-cache for this cause,
                                      # floored at 0 (see below)

def detect(run: Run) -> list[Breaker]
def repaired_calls(run: Run, breakers: list[Breaker]) -> list[Call]
    # returns calls with the breaker neutralized for simulation: volatile spans
    # replaced by a stable placeholder, tool order restored to first-seen order,
    # cache_breakpoints=1 when missing. Never mutates content semantics otherwise.
```

Classification rules (in priority order per divergence):
1. `model` differs from previous call → model-switch.
2. diverging segment is tools → tool-churn (report first-seen vs current order).
3. diverging segment is system → volatile-system if the changed span STRICTLY
   overlaps a volatile-regex match (ISO dates/times, unix timestamps, UUIDs,
   monotonic counters — keep the regex list a module constant with tests; a match
   merely touching the span does not count) AND substituting every volatile match
   makes the two systems byte-identical (the volatile values fully explain the
   divergence), else "system-edit" variant of the same kind with a different fix
   sentence (whose pin-the-system repair also covers mixed volatile-plus-edit
   changes).
4. diverging segment is a message with index < len(prev messages) → history-rewrite.
5. no divergence, prefix ≥ min cacheable, but `cache_breakpoints == 0` AND billed
   cache activity is zero → missing-breakpoint.
`est_recovered_usd`: run simulate() with only this breaker repaired; difference of
fixed-cache vs as-billed dollars, floored at 0 (billed caching can legitimately
beat the simulated single-breakpoint replay; the report words that case explicitly
rather than showing a negative "recovery"). None when pricing is unknown OR when
the kind has no mechanical repair (model-switch, history-rewrite —
`repaired_calls` leaves the run untouched there, so billed-minus-fixed would price
the optimal replay of the still-broken run, a number the displayed fix cannot
claim).

### Flagship test (`test_demo_recovers_planted_waste.py`)

For each demo scenario: run profile + detect + simulate and assert the planted
truth is recovered: `timestamp` → exactly one volatile-system breaker at call 1
with the timestamp span in evidence, redundancy fraction within a derived expected
band, fixed-cache dollars < as-billed dollars by an amount consistent with the
scenario's construction (derive expectations from demo_traces' documented usage
derivation — show the arithmetic in comments); `tool-churn` → tool-churn breaker(s)
at the rotation calls and no volatile-system false positive; `well-behaved` → zero
breakers, as-billed ≈ optimal-cache (agreement within rounding — the simulator
validation), redundancy fraction near zero (reads subtracted); `no-cache` →
missing-breakpoint breaker and fixed-cache ≈ optimal-cache < as-billed ≈ no-cache.
Plus byte-determinism across two processes.

---

## Module D — `report.py` and `cli.py`

`render_report(profiles: list[RunProfile], scenarios, breakers, meta) -> str`:
one self-contained HTML file (inline CSS, inline hand-rolled SVG, dark/light via
prefers-color-scheme, zero external resources; the sibling Judgemetry report.py is
the house style — same visual quality bar, don't import from it):

1. Header: trace name, runs/calls counts, models seen, date via meta (render pure);
   "synthetic demo data" banner when meta["synthetic"].
2. **The headline**: a single sentence with the aggregate redundancy fraction and
   the fixed-vs-billed dollar delta ("~62% of billed input tokens went to
   re-sending bytes the model had already seen; the two fixes below recover an
   estimated $4.31 of $5.12"). Token wording is mandatory — the fraction is a
   share of billed input tokens, not of dollars. Approx-basis footnote marker on
   the percentage.
3. Per-run **token waterfall** (stacked SVG bars per call: cache read / cache write /
   uncached / output) + a scenario comparison bar (as-billed vs no-cache vs
   optimal vs fixed) with dollar labels.
4. **Breaker cards**: kind, first occurrence, evidence span (escaped, truncated),
   the fix sentence, est. recovered dollars.
5. Methodology footnotes: exact vs approx numbers, cache-rule assumptions (TTL,
   sliding refresh, single end-breakpoint policy), pricing table version + link.

CLI (argparse; `main()`; `__main__.py`; py.typed):

```
tokenbill demo    [-o report.html] [--seed 7] [--scenario NAME]   # all four by default
tokenbill analyze TRACE.jsonl [TRACE2.jsonl ...] [-o report.html] [--model-price MODEL=IN,OUT]
tokenbill --version
```

`analyze` prints the aligned terminal summary (headline, per-run totals, breakers
with fixes, scenario dollars) and writes HTML when -o given. `--model-price` lets
users price unknown/self-hosted models. Friendly errors (TraceError → one tidy
stderr line, exit 1; usage errors exit 2). Recording is a documented python API
(README shows the 5-line `instrument` integration), not a subcommand, in v0.1.

Tests: report renders for all demo scenarios; self-containment (no external
resource refs); terminal output snapshot-ish assertions; exit codes; `python -m
tokenbill --version` subprocess.

---

## Module F — docs, CI, community, release

README (searchable phrases woven naturally: "LLM cost optimization", "prompt
caching", "agent observability", "token usage", "Anthropic prompt cache",
"cache hit rate"):

- H1 `Token Bill` — tagline: "Why is your agent bill so high? Profile the trace,
  find the cache breakers, get your money back."
- 60-second start: `pip install` via git URL until the PyPI release lands (label it,
  lesson from Judgemetry — never document `pip install tokenbill` as working before
  the release exists; put it as "from v0.1.0 onward"), then `tokenbill demo` —
  zero keys — with the actual demo terminal excerpt (generate it, don't invent it)
  and the headline sentence.
- Recording your own agent: the 5-line `instrument` snippet wrapping an Anthropic
  SDK client, then `tokenbill analyze trace.jsonl`. Honest note on what the
  recorder captures and that streaming is supported via get_final_message.
- "Exact vs approximate": a short section stating the honesty rules (billed usage
  = exact dollars; char-based attribution = approx, labeled). This is a
  differentiator — most cost tools hand-wave; we draw the line explicitly.
- How the simulator works: the documented cache rules with the source link, the
  four scenarios, the well-behaved-scenario agreement check as the validation story.
- Limitations: Anthropic-shaped traces only in v0.1 (schema is provider-neutral;
  adapters welcome); attribution is approximate; simulator models the documented
  rules, not undocumented server behavior; no live proxy yet. Roadmap: OpenAI
  adapter, recording proxy, Claude Code session-log importer, 1h-TTL scenarios,
  batch-pricing awareness. Related work: provider dashboards (show totals, not
  causes), LangSmith/W&B-style observability (traces, not cache economics) — be
  generous and specific about the gap Token Bill fills.
- DESIGN.md for the skeptical reader: every metric definition, the redundancy
  formula, scenario semantics + assumptions table, why chars/3.7, threats to
  validity (attribution error bounds, TTL assumption, single-breakpoint policy).
- CHANGELOG (0.1.0, cut-not-yet-published note), SECURITY.md (traces contain your
  prompts — treat them as secrets; the tool never phones home, everything is
  local), CODE_OF_CONDUCT, issue/PR templates (bug template asks for a redacted
  trace line), CONTRIBUTING (uv, pytest, ruff, spec pointer).
- ci.yml: lint (ruff), unit matrix py3.10 + py3.13 via uv, and a keyless `demo` job
  running `tokenbill demo -o /tmp/report.html` asserting non-empty output.
- release.yml: identical shape to Judgemetry's — tag v*, build + twine check +
  tag-vs-version guard, PyPI Trusted Publishing (environment "pypi", id-token:
  write, zero secrets), GitHub release from CHANGELOG. Header comment documents the
  one-time pending-publisher setup (project "tokenbill", owner "sedai77", repo
  "tokenbill-llm-agent-cost-profiler", workflow "release.yml", environment "pypi").
- Makefile: demo, test, lint, clean. LICENSE: MIT, "Copyright (c) 2026 Token Bill
  contributors".
```
