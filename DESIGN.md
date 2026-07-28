# Token Bill design notes

This document is written for the skeptical reader: what each number means, why
it is defined that way, which assumptions the simulator makes, and where the
whole approach can mislead you. The implementation contract lives in
[docs/SPEC.md](docs/SPEC.md); this is the rationale.

Throughout, a *trace* is a JSONL file of `tokenbill/trace@1` lines — one per
API call, carrying the full request payload (model, system prompt, tool
definitions, messages, cache-breakpoint count) plus the provider's real billed
`usage` (`input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `output_tokens`) and `stop_reason`. A *run* is
all calls sharing a `run_id`, ordered by `index`.

## 1. The honesty line: exact vs. approximate

Every number Token Bill surfaces belongs to exactly one of two ledgers:

- **Billed (exact).** All token totals and all dollar totals come from the
  trace's real `usage` fields multiplied by published prices. If the report
  says a run cost $5.12, that is the provider's own accounting of the calls in
  the trace, not an estimate.
- **Attributed (approximate).** Splitting one call's billed input across its
  system / tools / history segments, and locating where two calls' prefixes
  diverge in token terms, requires a tokenizer we do not have. Token Bill uses
  a character heuristic (§3) for these *proportions only*, and every such
  number is labeled "approx" wherever it surfaces — terminal, HTML, API.

The bridge between the ledgers is a strict invariant: **approximate splits are
rescaled so they sum to the call's billed total.** Attribution reallocates an
exact quantity; it never invents tokens. If the char heuristic says the system
prompt is 18% of the rendered text, the system segment is charged 18% of the
call's billed `total_input` — so segment numbers can be individually off, but
they always add up to the truth.

Why not tiktoken? It is the wrong tokenizer for Claude models. A wrong
tokenizer produces precise-looking numbers with an unknown, content-dependent
error and no label saying so. An explicit chars-based heuristic is *honestly*
approximate: it claims nothing beyond proportionality, and the design confines
it to places where only proportions matter.

## 2. Canonical rendering: the byte-comparison substrate

Prompt caching is a byte business: the provider caches a prefix of the
rendered request, and any byte difference before the breakpoint is a miss.
So Token Bill's unit of comparison is a canonical rendering of each call
(`trace.render_segments`):

1. **tools** — one segment: `canonical_json` of the tool-definition tuple,
2. **system** — one segment: the system string as sent,
3. **messages** — one segment per message: `canonical_json` of the message dict,

concatenated in that order, mirroring the provider's documented render order
(tools → system → messages). `canonical_json` sorts keys and fixes separators,
so a dict-ordering difference between two SDK calls can never masquerade as a
prompt change — only real content changes (including tool *tuple* order, which
the provider does see) move bytes.

On this substrate, two primitives drive everything downstream:
`common_prefix_chars(prev, cur)` (the longest common prefix, in characters)
and `diverging_segment(prev, cur)` (the first segment whose text differs —
`None` means `cur` merely extends `prev`, the cache-friendly case).

Caveat, stated plainly: this rendering is Token Bill's *model* of what the
provider hashes, built from the documented order. It is the right model for
locating which part of *your request* changed — which is the actionable
question — but it is not a byte-for-byte reconstruction of the provider's
internal serialization (§8).

## 3. Why chars ÷ 3.7

`approx_tokens(text) = len(text) / 3.7` (`CHARS_PER_TOKEN = 3.7`).

For modern BPE-family tokenizers on the English-plus-code mix that agent
prompts are made of, observed densities cluster roughly between 3 and 4.5
characters per token — prose sits near 4, dense JSON and code lower. 3.7 is a
defensible middle for mixed agent payloads; it is a named constant precisely
so that a project with unusual content can tune it.

The important point is not the value but the exposure. Because of the
rescaling invariant (§1), the *absolute* constant cancels everywhere a number
is a fraction of one call's rendering: attribution error depends only on
*differences in density between segments of the same call* (a code-heavy tool
result vs. a prose system prompt), not on whether 3.7 is right. The places
where the absolute value does bite are threshold checks — chiefly the
simulator's `min_cacheable_prefix_tokens` gate (§5), where a prefix near the
model's minimum can be misclassified in either direction. §8 quantifies both
exposures.

## 4. Redundancy: the headline number

The question: *what fraction of this run's billed input tokens re-sent bytes
the model had already seen — and paid full price for them?* (A token share,
deliberately: a dollar share would differ whenever a run mixes rate classes —
reads at 0.1×, writes at 1.25×, uncached at 1.0× — and the headline never
claims dollars it did not compute.)

For each call `i > 0` in a run:

```text
frac_i   = common_prefix_chars(call[i-1], call[i]) / len(rendered_text(call[i]))
resent_i = total_input_i * frac_i          # approx tokens of re-sent prefix
waste_i  = max(0, resent_i - cache_read_i) # reads already served cheap are not
                                           # waste; clamped at 0 per call so a
                                           # working cache cannot go "negative"

redundancy_fraction = Σ waste_i / Σ total_input_i
```

where `total_input_i` is the call's billed input (uncached + cache reads +
cache writes) and `cache_read_i` is its billed `cache_read_input_tokens`.

Design choices, each deliberate:

- **Previous-call prefix, not any-earlier-call.** The provider's cache is fed
  by recent requests; the immediately preceding call is what a working cache
  would have hot. An agent loop that appends turn after turn re-sends almost
  exactly the previous call's rendering plus a suffix — that is the structure
  the number measures.
- **Token value via the billed total, scaled by char fraction.** The re-sent
  prefix's token count is unknowable without the provider's tokenizer, so it
  is the call's *exact* billed input scaled by the *approximate* char
  fraction — the standard §1 construction. The redundancy fraction is
  therefore labeled approximate.
- **Cache reads are subtracted.** Re-sending a prefix the cache serves at the
  read rate (0.10× base input) is the *intended* mechanics of prompt caching,
  not waste. Subtracting billed cache reads makes the number specifically
  "re-sent prefix billed at full price". A well-behaved run with a working
  cache scores near zero even though nearly every byte repeats; a run with a
  stable prefix and a broken cache scores near the prefix's share of input.

The headline sentence in the report pairs this fraction with the simulator's
fixed-vs-billed dollar delta (§5), so the "how much waste" number always
appears next to "and here is what fixing it is worth".

## 5. The cache simulator: scenarios and assumptions

`simulate(run)` prices four counterfactuals of the same run. Dollar amounts
use the versioned pricing table in `tokenbill/pricing.py`; token amounts in
the replayed scenarios derive from each call's billed `total_input` split by
char fractions (§1 rescaling — each call's input total always matches billed).

| Scenario | What it prices | Basis |
| --- | --- | --- |
| **as-billed** | The run exactly as the provider billed it: `usage` × price table. | Exact (ground truth). |
| **no-cache** | Every input token at the full base input rate — cache reads and writes repriced as uncached, no write premium. "What if caching were off entirely." | Exact token counts, counterfactual prices. |
| **optimal-cache** | A replay of the rendered calls — their *actual* bytes — under the documented cache rules with an ideally placed breakpoint (below). "What this run would cost with perfect breakpointing." Write premiums are counted retrospectively (an optimal policy never caches what nothing will read), so for a byte-unstable run this degenerates to no-cache instead of exceeding it; making the bytes stable is fixed-cache's job. Never exceeds no-cache. | Approx (char-fraction token split), documented rules. |
| **fixed-cache** | optimal-cache applied to the breaker-repaired calls from `breakers.repaired_calls` — volatile spans stabilized, tool order restored, missing breakpoints added, content otherwise untouched. When no repairs exist, identical to optimal-cache. | Same as optimal-cache. |

The optimal-cache replay processes calls in timestamp order. A cache entry is
usable for the current call if and only if:

1. it was written under the **same model** (prompt caches are per-model — a
   mid-run model switch always starts cold),
2. it is the longest previously written prefix **byte-identical** to the
   current call's rendered prefix,
3. the entry is alive: written or last read within `CACHE_TTL_SECONDS` of the
   current call's `ts`, and
4. the matched prefix meets the model's `min_cacheable_prefix_tokens`
   (approx-token check — a threshold exposure, §3).

Charging: the matched prefix at the read rate; the remainder at the write
premium **only when the written entry is read by a later call in the replay**
(retrospective accounting — the replay knows the whole run, and an optimal
policy never pays 1.25× for an entry nothing will read back, including the
final call's extension, which nothing can ever read), otherwise at the plain
uncached rate. As a guarantee, the scenario is clamped at the no-cache price:
declining to cache is always available to an optimal policy, so "optimally
caching costs more than doing nothing" can never be reported. The breakpoint
is assumed at the end of messages on every call — the optimal placement under
prefix caching.

Assumptions, in one table (all constants live in `tokenbill/pricing.py` with
source comments):

| Assumption | Value | Status |
| --- | --- | --- |
| Cache TTL | 300 s (the 5-minute cache) | Documented. |
| TTL refresh on read | Yes — a hit slides the expiry window | Documented assumption, flagged here because agent gaps near 300 s make results sensitive to it. |
| Breakpoint policy | One breakpoint, end of messages, every call | Simulator's choice: optimal placement. Providers allow up to `MAX_BREAKPOINTS = 4`; the cap is not binding for this single-breakpoint policy. |
| Minimum cacheable prefix | Per model, 512–4096 tokens (table below) | Documented. |
| Cache write premium | 1.25× base input (5-minute-TTL writes) | Documented. |
| Cache read rate | 0.10× base input | Documented. |
| Prefix matching | Byte-identical canonical rendering, tools → system → messages | Documented render order; rendering is our model (§2). |

Pricing table (verified 2026-07 against
<https://platform.claude.com/docs/en/about-claude/pricing.md>; re-verified before each
release — see `tokenbill/pricing.py`). One known deviation, stated rather than
hidden: claude-sonnet-5 carries *introductory* billing ($2.00/$10.00 per MTok)
through 2026-08-31; the table deliberately uses the standard rates below, so
sonnet-5 dollar figures can overstate real bills during that window — the
report's pricing footnote discloses this:

| model | $/MTok in | $/MTok out | min cacheable prefix |
| --- | --- | --- | --- |
| claude-opus-5 | 5.00 | 25.00 | 512 |
| claude-fable-5 | 10.00 | 50.00 | 512 |
| claude-opus-4-8 | 5.00 | 25.00 | 1024 |
| claude-opus-4-7 | 5.00 | 25.00 | 2048 |
| claude-opus-4-6 | 5.00 | 25.00 | 4096 |
| claude-sonnet-5 | 3.00 | 15.00 | 1024 |
| claude-sonnet-4-6 | 3.00 | 15.00 | 1024 |
| claude-haiku-4-5 | 1.00 | 5.00 | 4096 |

Unknown models never crash the pipeline: dollars become `None` with a warning,
token accounting continues, and the CLI's `--model-price MODEL=IN,OUT` lets
you price them yourself.

## 6. Validation: the demo as calibration certificate

A cost profiler asks for trust; the demo is built so it does not have to. The
four bundled scenarios (`tokenbill/demo_traces.py`) are deterministic
synthetic coding-agent loops with *planted* waste, and their `usage` numbers
are derived from the same `approx_tokens` rendering the analyzer uses — so
every expectation is computable before the pipeline runs:

- `well-behaved` — byte-stable prefix, one breakpoint, usage computed as if
  caching worked. Expected: zero breakers, redundancy near zero, and
  **as-billed ≈ optimal-cache** (cache reads agree within rounding; dollars
  differ only by the final call's write premium, which the real bill pays but
  the retrospective replay does not — nothing can ever read the last write).
- `timestamp` — identical, except the system prompt embeds a per-call
  timestamp. Expected: one volatile-system breaker with the timestamp span in
  evidence, zero cache reads billed, fixed-cache well below as-billed.
- `tool-churn` — the tool tuple's order rotates every 4th call. Expected:
  tool-churn breakers at the rotation calls and *no* volatile-system false
  positive.
- `no-cache` — byte-stable prefix but no breakpoint and everything billed
  uncached. Expected: a missing-breakpoint breaker; fixed-cache ≈
  optimal-cache < as-billed ≈ no-cache.

The flagship test (`tests/test_demo_recovers_planted_waste.py`) asserts all of
this, with expectation arithmetic derived from the traces' documented
construction, plus byte-determinism across two processes.

The `well-behaved` case doubles as the **simulator validation hook**: whenever
a run's billed usage shows real cache activity, the simulator compares billed
cache reads against its own predicted reads and reports the agreement ratio in
the scenario note. On `well-behaved` the reads must agree within rounding — a
test enforces it — which is evidence that the replay rules and the usage
construction implement the *same* documented semantics. (Dollars carry one
derived, deliberate gap: the real bill pays the write premium on the final
call's extension, which the retrospective replay bills uncached.) What this does **not**
prove: that the provider's production cache behaves like the documented rules
on your traffic (§8). On real traces with cache activity, the agreement ratio
is printed precisely so you can see how far reality deviates from the model.

## 7. Breaker classification

A breaker is a divergence between consecutive calls' renderings (or a
cache-defeating configuration), classified by cause in strict priority order:

1. **model-switch** — the `model` field changed; caches are per-model, so
   everything after the switch is cold regardless of bytes. Checked first
   because it explains any byte divergence downstream.
2. **tool-churn** — the diverging segment is the tools segment. The evidence
   reports first-seen vs. current order; the fix is to freeze registration
   order.
3. **volatile-system** — the diverging segment is the system prompt, the
   changed span *strictly* overlaps a volatile-pattern match (ISO dates/times,
   unix timestamps, UUIDs, monotonic counters; the regex list is a tested
   module constant; a match merely touching the span — a stable date next to
   an edited punctuation mark — does not count), and substituting every
   volatile match makes the two systems byte-identical, i.e. the volatile
   values fully explain the divergence. The fix names the span and where to
   move it (e.g. into the latest user message). Any other system change —
   including a mixed volatile-plus-real-edit — is reported as a "system-edit"
   variant with a different fix sentence, because only pinning the system
   text actually repairs it; we do not pretend to know an arbitrary edit is
   automatable waste.
4. **history-rewrite** — the diverging segment is a message that already
   existed in the previous call (index < previous message count): something
   rewrote delivered history in place, invalidating every byte after it.
5. **missing-breakpoint** — no divergence at all, prefix at or above the
   model's cacheable minimum, yet the request carried zero `cache_control`
   markers and billed cache activity is zero. The cheapest fix in the list.

Priority order matters because one root cause can produce several surface
symptoms; attributing a tool-order diff to "system volatility" would point the
user at the wrong fix. Each breaker carries `est_recovered_usd`: the run is
re-simulated with *only that breaker* repaired, and the value is as-billed
minus fixed-cache dollars — positive means the fix recovers money. The
estimate is floored at 0: billed usage can legitimately beat the simulated
single-breakpoint replay (for example, real multi-breakpoint caching), and a
negative "recovery" would claim the fix costs money — the report words that
case explicitly ("no recovery modeled — billed caching already beats the
simulated fix") instead of printing a negative dollar figure. It is
`None` when pricing is unknown, and also for kinds with no mechanical repair
(model-switch, history-rewrite): their repair leaves the calls untouched, so
billed-minus-fixed would price the optimal replay of the still-broken run — a
number the displayed fix could never deliver, so no number is shown. Because
repairs can overlap, per-breaker recoveries are not guaranteed to sum to the
all-repairs fixed-cache delta; the report's headline uses the all-repairs
number.

## 8. Threats to validity

Read these before acting on a report.

- **Attribution error from the char heuristic.** Segment shares and the
  redundancy fraction inherit error proportional to the char-per-token
  density *mismatch* between a call's segments (§3). For agent payloads
  (prose + code + JSON, densities roughly 3–4.5 chars/token) a segment's
  share can plausibly be off by a relative ~10–20% in adversarial mixes;
  totals are unaffected by construction. Every affected number is labeled
  approx.
- **Threshold misclassification.** The `min_cacheable_prefix_tokens` gate is
  an absolute check made with approximate tokens; prefixes near a model's
  minimum (512–4096 depending on model) can be classified wrongly in either
  direction, changing optimal-cache dollars for short-prefix runs. Long-prefix
  agent runs — the interesting ones — sit far from the boundary.
- **The TTL-refresh assumption.** The simulator assumes a cache read slides
  the 5-minute expiry. If provider semantics differ, or your loop's gaps
  hover near 300 s, optimal-cache savings shift; the assumption is a named
  constant (`TTL_REFRESH_ON_READ`) so the sensitivity is one edit away.
- **Documented rules, not server behavior.** The simulator implements the
  provider's published cache semantics. It does not model eviction under
  load, regional topology, concurrent-request races, or any undocumented
  behavior. The agreement hook (§6) surfaces gross divergence — but only on
  traces that show real cache activity to compare against.
- **The single-breakpoint policy is an idealization.** "Optimal-cache"
  assumes a breakpoint at the end of messages on *every* call. Real clients
  set static breakpoints and providers cap them at 4; a real integration may
  not reach the simulated optimum. Treat optimal-cache as the ceiling under
  the documented model, not a promised outcome.
- **`est_recovered_usd` is a counterfactual estimate**, priced under the same
  model — not a refund guarantee. Prices themselves are versioned data that
  can change; the table records its verification date.
- **The rendering is our model of the provider's serialization** (§2).
  Divergence *localization* (which segment changed) is robust; exact byte
  offsets are relative to our canonical rendering, not provider internals.
- **The demo is synthetic.** It certifies that the instruments recover known,
  planted waste; it says nothing about the waste profile of any real agent.
  Your trace is the only evidence about your agent.
- **v0.1 is Anthropic-shaped.** The schema is provider-neutral, but the usage
  fields, cache rules, and pricing model the Anthropic prompt cache;
  applying the simulator to other providers' traces would be category error
  until an adapter maps their semantics.
