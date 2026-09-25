# CP-DET-LANES tests — `copilot.lanes` and the generic detectors on Copilot lanes

Package CP-DET-LANES owns `tokenbill/detect/copilot_lanes.py` (`CopilotLanes`, id `copilot.lanes`)
and this directory. Brief: `docs/v0.2-design/copilot-briefs/CP-DET-LANES.md`; design: addendum
§9.3, §10.0, §10.3, §10.4, Appendix C.G5 / G5b / G10.

## Files

| file | what it covers |
|---|---|
| `helpers.py` | area-local builders: Copilot requests / lanes / COMPACTION and SESSION_META events on channel `github_copilot`, an `AnalysisContext` over `FakePricer`, finding filters |
| `test_long_context.py` | `band_premium` and `long-context-band`: **C.G5** (300,000 input, tier unknown → premium 300,000,000 nano EXACT = 660,000,000 band − 360,000,000 default), **C.G5b** (270,000 input, `context_tier=long_context` → ESTIMATED [0; 285,000,000], point 0), the reverse disagreement, Auto ×0.9, folded writes, non-band models, one finding per model (C.G10 rates), the VS Code extract lane yields the kind, the events-only CLI lane is skipped for it, reach evidence |
| `test_compaction.py` | `compaction-cost`: forced compactions counted and priced exactly on an events-only CLI lane, trigger join (±60 s, nearest, ties earlier), events without inferences counted without money, COMPACTION lanes, unbillable / unpriced / billing-uncertain compactions |
| `test_static_overhead.py` | `static-overhead`: a lane with 30k tool-definition tokens → the recoverable range [161,250,000; 274,125,000] contains the hand value 225,750,000 (0.70 × the tool carry); prefix-order carry (read, write, uncached); latest static report; floor fallback without recoverable; lanes without input skipped |
| `test_subagent_ci.py` | `subagent-share` by `agent_type` (free-text names reported as `custom`, canary absent); `ci-uncapped`: CLI CI sessions without `credit_limit_nano` counted (p50/p90, uncapped share, suggested cap), gh-aw runs compared with the 1,000 AIC default cap; events-only CLI and gh-aw lanes yield `compaction-cost` and `ci-uncapped` through `run_detectors` without `usage_sequence` |
| `test_conformance.py` | `assert_detector_conforms` (incl. shard invariance) on a mixed world; Claude Code lanes of the same team never included; registry gating (`ext:copilot`, `requires={"credits"}`, per-shard call); self view; unattributed team; allowance and non-pool lanes; canary; determinism |
| `test_properties.py` | hypothesis: random Copilot + Claude Code worlds conform, are lane-order independent and family-filtered; `0 ≤ low ≤ point ≤ high` for every band premium |
| `test_gate_generic.py` | **gate** (`importorskip` DETECT-CACHE / DETECT-OTHER / CP-DET-USAGE / REPLAY — all on disk, all pass): `cache.miss-by-cause model-switch` carries the Copilot fix (`target github-copilot`, no `CLAUDE_CODE_*` key); `ttl-expiry` only with a known τ; `premium.modifiers fast-premium`, `automation ci-run-cost`, `context.static-prefix static-prefix` produce findings when called directly but none through `run_detectors` (`FAMILY_EXCLUSIONS`) while `copilot.org-scan fast-mode` (C.G8), `copilot.lanes ci-uncapped` and `static-overhead` do; `cache.ttl-advisor` never sees a Copilot lane; the real `UsageReplayer` on Copilot lanes |
| `test_gate_synth.py` | **gate** (`importorskip("tokenbill.synth.copilot_world")`, CP-SYNTH, not on disk: skipped): the synthetic world's vscode / agents plants through `copilot.lanes` and the real generic detectors + replayer; `model-switch` recovered, excluded kinds absent, control team `core` clean; truth attribute names read defensively |

## Fixtures and provenance

All fixtures are synthetic records built in code with `core.builders` (no files, no real traces),
dated 2026-09-10, priced by `core.testing.FakePricer` from `core/facts.json` (`copilot.rates`).
Hand values come from Appendix C (G5, G5b, G8, G10) or are computed in the test docstrings.

## Interpretations (brief and addendum)

- **Gating.** The brief's `requires={"credits"}` wins over addendum §10.0's `requires=frozenset()`
  with `KIND_REQUIRES` (revision 3.1 precedence: briefs win). Each kind checks its own per-lane
  input (`KIND_INPUTS`); a lane without it is skipped for that kind only. No skipped-kinds
  data-quality finding: whether one shard holds a kind's input is not a data-quality fact, a lane
  detector must stay shard-invariant, and `COUNT_SOURCE` lists none for `copilot.lanes`.
- **`long-context-band` recoverable** is the premium itself (the standalone counterfactual "every
  banded request at the default tier"), ESTIMATED `upper_bound`, with the premium's range. The
  delivery reach of `copilot.context_default` (CLI only, trusted directories, trusted share
  unknown; §9.3) is evidence (`reach:copilot.context_default`), not a factor of the finding:
  §9.3 multiplies *projections* by reach, and scaling the finding would make a VS Code cohort's
  finding disappear when one small CLI lane joins it (`min_usd_gate` compares the recoverable
  point). The same holds for `copilot.mcp_trim` (`reach:copilot.mcp_trim`).
- **Premium arithmetic.** Resolved side = `Pricer.price_inference` lines (A, or the A/B range);
  default side = the same line quantities at `Pricer.unit_rates` (base rates on both engines;
  unknown-TTL writes keep their [5m, 1h] range and hint point, so non-band ranges cancel). Only
  billable inferences with final or estimated usage are analysed (a [0, full] billing-uncertain
  range is not a band). With the default `min_usd` a pure A/B range (point 0, e.g. C.G5b) is
  dropped; tests pass `min_usd="0"` for it.
- **`compaction-cost`** joins COMPACTION inferences to COMPACTION events of the same lane by
  nearest timestamp within ±60 s (CP-LOCAL emits both from one `session.compaction_complete`);
  trigger = `copilot_trigger`, else `trigger`, else `unknown`. Category `lever` (brief), no lever
  in `COPILOT_LEVERS`.
- **`static-overhead`** carries the static prefix in prompt order: read first (`min(S, R)` at the
  request's read price), then written, then uncached — the addendum's "`static × r` per warm
  request + `× w` per miss" generalized to partial hits; each class at the request's own priced
  lines. The tool-definition carry puts the tools tier first. Point 0.70 of the reduction band is
  SPEC §10.2's.
- **`subagent-share`** is computed inside the SUBAGENT cohort (shard invariance forbids a share of
  the team's MAIN spend); **`ci-uncapped`** splits by `agent_product` (scope dims `workload_class=ci`,
  `agent_product`), percentiles nearest-rank, suggested cap `ceil(p99 × 1.5)` credits; gh-aw runs
  without a SESSION_META `credit_limit_nano` are compared with the default cap. It links the
  behavioural levers `copilot.session_limits` (CLI) / `copilot.agentic_workflow_caps` (gh-aw).
- **Names** (agent types, triggers, models) reach a finding only when identifier-like; free text is
  reported as `custom` (the canary test plants it in `agent_type` and a trigger).
- **Self view** reads the self principal's lanes and lanes without a principal (as DETECT-OTHER).

## Unverified facts (inherited, not transcribed here)

- The 1,000 AIC default cap per gh-aw run (`facts.copilot.aic_default_cap_per_run`, research).
- `contextTier` reaches only the CLI in trusted directories; managed MCP lists reach CLI, VS Code,
  JetBrains and the Copilot app (§9.3). The agent-product ids `copilot_jetbrains` / `copilot_app`
  are the addendum's naming for products no adapter emits yet (**VERIFY** when CP-OTEL maps them).
- CLI COMPACTION events and COMPACTION inferences share a timestamp (CP-LOCAL, events schema).
- Forced triggers = `context_limit_retry`, `memory_pressure` (addendum §10.3, SDK event domain).

## Limitations

- A lane without requests (e.g. a CLI session covered by a VS Code extract, which CP-LOCAL emits as
  LaneEvents only) has product family `default` and never reaches the detector; its compaction
  triggers are not counted (see `CONTRACT-CHANGE-CP-DET-LANES.md`).
- Events-only CLI lanes price only output-only requests and compactions; their rollup aggregates
  (input tokens) are not lane data, so `ci-uncapped` session spend is a lower bound there.
