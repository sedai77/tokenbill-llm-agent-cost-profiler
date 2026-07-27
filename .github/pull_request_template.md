## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `uv run --python 3.12 --extra dev pytest -q` passes locally (includes the
      flagship planted-waste recovery test).
- [ ] `uv run --python 3.12 --extra dev ruff check .` is clean.
- [ ] No new runtime dependencies (stdlib only; `instrument.py` duck-types the
      SDK client and never imports `anthropic`).
- [ ] Tests added/updated for the change; nothing here hits the network or
      needs an API key — CI has no secrets and never will.
- [ ] Every new number surfaced to users is either derived from billed `usage`
      (exact) or labeled approximate; no approximation is presented as billed.
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for user-visible changes.
- [ ] If a metric definition, cache-rule constant, or pricing entry changed:
      `DESIGN.md`, the report's methodology footnotes, and the flagship test's
      derived expectations are updated together — and pricing changes cite the
      published pricing doc in the source comment.

## Anything reviewers should focus on?

<!-- Subtle spots, e.g. canonical-rendering byte stability, approx-vs-billed
     labeling, TTL edge cases in the simulator, seed scoping. -->
