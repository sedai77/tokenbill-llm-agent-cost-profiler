# Changelog

All notable changes to Token Bill are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-26

### Added

- Trace schema `tokenbill/trace@1`: JSONL, one API call per line (full request
  payload + real billed usage), with strict validation and precise
  `TraceError` messages, plus canonical rendering (tools → system → messages)
  as the byte-level substrate for prefix comparison.
- Per-call and per-run **token waterfalls** computed from real billed `usage`:
  cache reads / cache writes / uncached input / output, in tokens and dollars.
- **Redundancy analysis**: the share of cumulative billed input spend that was
  re-sending byte-identical prefix the model had already seen and that was
  *not* served from cache — labeled approximate, since it splits exact billed
  totals by character fractions.
- **Cache simulator** replaying each run under the provider's documented
  prompt-caching rules in four priced scenarios: as-billed, no-cache,
  optimal-cache, and fixed-cache (after repairing detected breakers), with a
  validation hook comparing predicted vs. billed cache reads whenever the
  trace shows real cache activity.
- **Cache-breaker detection** with classified causes — volatile system prompt,
  tool churn, history rewrite, model switch, missing breakpoint — each with
  the evidence span, a one-sentence concrete fix, and the estimated dollars
  recovered.
- `tokenbill demo`: four deterministic synthetic coding-agent scenarios with
  *planted* waste patterns — zero keys, zero network. The flagship test
  (`tests/test_demo_recovers_planted_waste.py`) asserts the analyzer recovers
  exactly the waste the traces plant, so the demo doubles as the correctness
  certificate.
- `tokenbill analyze`: aligned terminal summary plus a single self-contained
  HTML report (inline CSS, inline SVG waterfalls and scenario bars,
  dark/light via `prefers-color-scheme`, zero external resources);
  `--model-price MODEL=IN,OUT` for pricing unknown or self-hosted models.
- `tokenbill.instrument.Recorder`: records real traces by duck-type-wrapping
  an Anthropic-SDK-shaped client (`messages.create` and `messages.stream`,
  streaming via `get_final_message()`), appending one crash-safe JSONL line
  per completed call — without ever importing the SDK.
- Versioned pricing and cache-rule tables with per-entry source comments
  (verified 2026-07 against the published pricing doc).
- Zero runtime dependencies (pure standard library); Python 3.10+; typed
  (PEP 561 `py.typed`).

[Unreleased]: https://github.com/sedai77/tokenbill-llm-agent-cost-profiler/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sedai77/tokenbill-llm-agent-cost-profiler/releases/tag/v0.1.0
