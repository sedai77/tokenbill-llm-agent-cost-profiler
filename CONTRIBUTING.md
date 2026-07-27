# Contributing to Token Bill

Thanks for helping make agent bills explainable. This document covers the
mechanics; [docs/SPEC.md](docs/SPEC.md) is the authoritative contract for the
internals — when this file and the spec disagree, the spec wins.

## Ground rules

- **Zero runtime dependencies is a feature, not an accident.** The package
  must import and run on a bare Python 3.10+ install — no exceptions, not
  even optional extras. The recorder (`tokenbill/instrument.py`) duck-types
  the Anthropic SDK client and must never import `anthropic`. PRs that add a
  runtime dependency will be declined.
- **The honesty rules are product law.** Every dollar total comes from real
  billed `usage` fields — exact. Character-based tokenization
  (`approx_tokens`, chars ÷ 3.7) is used *only* for proportional attribution
  and divergence localization, is always rescaled so segments sum to the
  billed total, and is labeled "approx" everywhere it surfaces. A PR that
  presents an approximate number as billed will be declined regardless of how
  useful the number is.
- **Determinism is load-bearing.** Every stochastic step takes an explicit
  `seed` and derives its stream via `common.rng(seed, *scope)`. Never call
  `random` module-level functions or `hash()` for anything reproducible.
- **The flagship test is the contract.**
  `tests/test_demo_recovers_planted_waste.py` asserts the analyzer, simulator,
  and breaker detector recover the demo traces' planted waste, with
  expectations derived from the documented usage construction. If your change
  breaks it, either the instruments are wrong or the traces changed — either
  way, that is a design conversation, not a tolerance bump.

## Development setup

We use [uv](https://docs.astral.sh/uv/). No install step is needed beyond
cloning:

```bash
git clone https://github.com/sedai77/tokenbill-llm-agent-cost-profiler
cd tokenbill-llm-agent-cost-profiler

# Run the full offline pipeline
uv run tokenbill demo -o report.html

# Tests (any supported interpreter; CI runs 3.10 and 3.13)
uv run --python 3.12 --extra dev pytest -q

# Lint
uv run --python 3.12 --extra dev ruff check .
```

Or via the Makefile: `make demo`, `make test`, `make lint`.

## Tests

Everything passes offline — no network, no API keys, ever. CI has no secrets
and never will. Tests for the recorder use a tiny fake client double (a few
lines), never the real SDK. Tests that need trace input generate it from
`tokenbill/demo_traces.py` or build `Call` objects inline — do not add tests
that download anything or check in large trace fixtures containing real
prompts.

## Style

- `from __future__ import annotations`, full type hints, frozen dataclasses
  for value types.
- Loggers are named `logging.getLogger("tokenbill.<module>")`.
- Docstrings on public functions; comments only for invariants the code
  cannot express.
- `ruff check .` must pass (config in `pyproject.toml`; line length 100).

## Submitting changes

1. Fork, branch, and keep the change focused.
2. Add or update tests next to the code you touched.
3. Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.
4. Confirm `pytest -q` and `ruff check .` are clean.
5. Open the PR — the template walks through the checklist.

Metric definitions, cache-rule constants, and pricing entries are public API:
changing one changes what a dollar figure means. Such changes need a matching
update to [DESIGN.md](DESIGN.md), the report's methodology footnotes, and the
flagship test's derived expectations — together, in the same PR. Pricing and
cache-rule edits must cite the published pricing doc in the entry's source
comment; the values are re-verified before each release.

## Releasing (maintainers)

1. Re-verify the pricing table against the published pricing doc (see the
   comment in `tokenbill/pricing.py`).
2. Move `[Unreleased]` entries into a new dated section in `CHANGELOG.md`.
3. Bump `__version__` in `tokenbill/__init__.py`.
4. Tag `v<version>` and push the tag. `release.yml` builds, verifies the tag
   matches the package version, publishes to PyPI via Trusted Publishing (no
   tokens), and creates a GitHub release with the changelog section as notes.
