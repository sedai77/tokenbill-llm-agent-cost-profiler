# SYNTH-FLEET tests

Package: `tokenbill/synth/{fleet,truth,writers}.py` — the deterministic synthetic fleet of SPEC §18
(`tokenbill demo --fleet`, the detectors' and plan's per-plant gate tests, the INTEGRATION flagship).

## What is tested

| file | covers |
|---|---|
| `test_closed_forms.py` | every closed form of `synth.truth` on one-lane plant fixtures: Appendix A.1 ($1.1508), A.2 (+$0.318 at 1h), A.4 keepalive ($0.6924; 4 pings for 20 min; 15 capped pings and cold for 2 h; Claude Code refused), A.5 ($4.00 EXACT / $3.90 premium), A.6 (−$0.569, no change above the lane maximum), A.10 ($0.31 / $0.2576 / K* 37.2), A.11 ($0.71376 / $0.53532); hand-computed restore-caching, model remap, effort scaling, batch, fast/regional premiums, size tax, tool-defs bloat, runaway, rebaseline |
| `test_fleet_world.py` | SPEC §18 acceptance on `generate(seed=7)`: team table (61 devs, tiny = 3 principals), window/today/provisional days, plants and truth for every non-control team, meaningful figures, billing paths (`subscription`, `usage_credits` on the two overage days, `bedrock` regional), the control team produces **no miss event** under `core.transitions`, plant lanes follow the documented billing rules (no ambiguous transition), team-specific shapes, canonical records are content-free, `MemoryStore` round trip vs team spend, provider records vs reconciliation truth, in-process determinism |
| `test_writers.py` | every written file parses (JSONL / JSON / CSV), carries the documented fields, CANARY only in transcripts and headless streams; small independent readers reproduce the canonical totals (message-id de-duplication, fallback iterations, placeholder call, headless residuals, OTLP int64 strings, trace@2 closed key sets and a full `from_json` round trip of every agents request, usage/cost/analytics pages, CUR arithmetic) |
| `test_properties.py` | hypothesis: warm lanes only pay the 1h write premium; cold lanes flip every transition; closed forms add over disjoint lanes; neutral policies change nothing; generator argument fuzz raises only `UsageError` |
| `test_determinism.py` | `generate(seed=7, out_dir=…)` in two processes (different `PYTHONHASHSEED`): identical file bytes, canonical records and truth |
| `test_scale.py` | scale mode: exact count, re-iterable, deterministic, lanes assemble; PR budget 10⁵ requests ≤ 15 s, RSS ≤ 300 MB; `perf`: 10⁶ requests ≤ 60 s with bounded RSS (measured ≈ 33 s, ≈ 50 MB) |
| `test_gate_files_through_adapters.py` | **gate** (`importorskip` CC, TELEM, TRACE, ADMIN adapters): every written family read by the real adapter reproduces the canonical token totals per team; cost-report and CUR invoice totals equal the canonical cost lines |
| `test_gate_truth_vs_oracle.py` | **gate** (`importorskip("tokenbill.synth.oracle")`): each replay-based plant truth equals `ReferenceReplay` on the plant's lanes within 1 nano |

Contract notes: `CONTRACT-CHANGE-SYNTH-FLEET-1.md` (advisory, TRACE §4.2: `lookback_pos` in trace@2
`blocks` records is position-dependent).

## Fixtures and provenance

No fixture files are checked in: every input is generated (seeded, synthetic) by `synth.fleet.generate`
into a temporary directory. Schemas follow SPEC §19.4 and the primary sources below; nothing is copied
from real transcripts or real provider pages. Only transcripts and headless streams carry content, all
of it synthetic filler ending with CANARY (`TB-CANARY-7f3a91`); every other file is content-free.
Keys `FLEET_ORG_KEY`, `FLEET_NAME_KEY`, `FLEET_FP_KEY` are public demo constants (they protect nothing).

## Using the fleet (for DETECT / PLAN / RECON / CLI / INTEGRATION gate tests)

- `world = generate(seed=7, out_dir=tmp)`; `world.lanes(team=None)` assembles lanes;
  `world.ingest_result()` is one `IngestResult` for a store built with `org_key=FLEET_ORG_KEY`,
  `name_key_id=key_id(FLEET_NAME_KEY)`; `fleet_ingest_options(**kw)` reads the written files with the
  same keys. `world.truth.plant(id)`, `.plants_for(team=, detector_id=, kind=)`, `PlantTruth.within(figure,
  value[, low=, high=])` check a recovery against its tolerance; `world.truth.team_map` maps raw actors
  (analytics emails and API-key names, CUR IAM ARNs, device refs) to teams.
- Plant ids: `platform.no-cache`, `payments.ttl-1h`, `search.size-tax`, `search.compaction-window`,
  `mobile.cold-resume.{allowance,overage}`, `infra.fast-premium`, `infra.sticky-escalation`,
  `data.delegation`, `data.same-tier.<lane_kind>.<model>`, `data.default-model`, `data.default-effort`,
  `data.rebaseline`, `ops.regional-premium`, `ops.runaway`, `ci-bots.truncation`,
  `ci-bots.ci-cross-run`, `ci-bots.batch-eligible`, `ci-bots.ci-run-cost`, `agents.keepalive`,
  `agents.edit-churn`, `agents.tool-defs-bloat`. Fleet expectations (`world.truth.expectations`): core
  clean, tiny suppressed, agents without block breakers.
- Window 2026-09-22 … 2026-10-19 (Opus 5.5 is priced from 2026-09-22); `today` = 2026-11-13; the last
  six days are provisional. Overage days 2026-09-30 and 2026-10-01; data migration on 2026-10-06.

### Conventions a consumer must know (documented choices where the SPEC is silent)

- **Seat allowance** (search, mobile outside the overage days) is absent from the usage and cost
  reports (seat plans show only usage credits); reconciliation must treat LIST_EQUIVALENT ledger usage as
  `seat_allowance_unmetered`, not as an over-count. Mobile overage (usage credits) is invoiced at the
  same 15% contract discount.
- **Priority tier**: the data team's Opus 4.8 main traffic on the first two days has
  `service_tier="priority"`: present in the usage report, absent from the cost report
  (`priority_excluded_from_cost_report`).
- **Unpriced model**: two calls on `claude-sonnet-5-5` (announced, not priced) on a provisional day;
  the synthetic invoice prices them like Sonnet 5.
- The placeholder call's provider-billed output is its `output_upper`; the ambiguous refusal (declined
  output 6) is billed by the provider; the pre-output refusal is not.
- `restore_caching` truth writes in the 5m bucket (`τπ` defaults to 300 s); the compaction-window truth
  uses `S_c = 20,283` (every COMPACTION event in the fleet reports that `post_tokens`, so the org median
  equals the default); size-tax `cost_observed` is the X = 200k decomposition (100k/400k in details);
  truncation recovery counts a truncated attempt when the **next** same-lane request starts within 120 s
  with `T ≥ 0.95·T_trunc`; runaway percentiles are nearest-rank over the ops main cohort (p95 of session
  cost, p99 of per-session maximum rolling-1h cost).
- Tool-defs-bloat truth: the synthetic tool search removes 70% of the 14,000 non-deferred tool-definition
  tokens; saved tokens are reads when the request read its prefix, else writes. The truth lies inside the
  detector's [0.50, 0.85] band under both the prefix-position and the proportional read/write-mix
  convention (both totals are in `details`).
- Context edits replace cleared tool results by an 8-token placeholder in place (block positions do not
  move, so a block's `lookback_pos` is a function of its hash in the trace@2 delta encoding).
- Scale mode (`scale_requests=N`) yields canonical requests/events/sessions only: no truth, no files, no
  provider-side records.

## Facts: verified and unverified

Verified against the primary source on 2026-09-23:
- Admin `usage_report/messages` result fields (incl. `service_tier` values `priority`, `speed`,
  `inference_geo`, `context_window`) and `cost_report` result fields (`amount` in cents as a decimal
  string, `cost_type`, `token_type` enumeration) — platform.claude.com API reference;
- Claude Code Analytics response shape (`actor`, `core_metrics`, `tool_actions`, `model_breakdown`,
  integer `estimated_cost.amount` in cents) — platform.claude.com Claude Code Analytics API page.

Prices, multipliers, modifiers, successors and SKU patterns come from `core/facts.json` only.

**Unverified** (synthetic shapes; the corresponding VERIFY items of SPEC §19.8):
- CUR 2.0 CSV column set, `pricing_unit` value `"1M tokens"`, Bedrock usage types (#17; every
  `core.catalog.SKU_RULES` row is `verified: false`, so canonical CUR records follow the unmapped path);
- claude-code-action execution-file container and SDK message keys (#16); `timestamp` fields on SDK
  messages are an addition so timing is available;
- Claude Code OTel event attribute names and encodings (`cost_usd` as `doubleValue`) (#8);
- the Claude Code transcript format (internal, version dependent), `origin.kind`, `quotaLimits`
  semantics (#15);
- the cost-report `description` strings (plausible labels, content-free).
