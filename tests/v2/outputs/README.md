# OUT tests — renderers (`tests/v2/outputs/`)

Package OUT (SPEC §14, R3, R4, R10, D17, D19, D26; Copilot amendment A-8; rulings R-E30, R-E47).
`tokenbill/outputs/ccusage.py` moved to CLI-LEDGER (CORE-AMENDMENTS O-5) and is not part of OUT.

| module | provides |
|---|---|
| `tokenbill/outputs/result_json.py` | `money_json` (= `core.labels.figure_json`, A-8), `to_result_json`, `dumps_result`, `validate_result_json` (SPEC §14.7); shared helpers `require_billed`, `require_allowance`, `require_published`, `display_rows` (R-E30), `rule_violations`, `canonical_dumps`, `date_of`, `extension_slots_present` |
| `tokenbill/outputs/terminal.py` | `render_terminal` (every `RunResult` slot, ≤ *width* columns), `chip`, `usd`, `pct`, `scrub`, `LEGEND` |
| `tokenbill/outputs/html.py` | `render_html` (single file, CSP, no scripts / URLs, table twins, both themes AA), `page`, `table`, `bar_figure`, `money`, `esc`, `PALETTES` |
| `tokenbill/outputs/focus.py` | `write_focus` (FOCUS 1.4 CSV, D19 BilledCost rule), `row_key`, `focus_columns`, `FOCUS_COLUMNS`, `X_COLUMNS` |
| `tokenbill/outputs/sarif.py` | `to_sarif` (SARIF 2.1.0, rules `TB-CACHE-SHARE`, `TB-NEW-BREAKER`, `TB-COST-REGRESSION`, `TB-SERIALIZATION-CHURN`) |
| `tokenbill/outputs/showback.py` | `render_showback` (per-team HTML / CSV / JSON from `PublishedAggregate` only, anchors and benchmarks with sources, per-billing-path distribution) |

## Fixtures and provenance

Everything is **synthetic**; no real transcripts, no network.

- `sample.py` — a `RunResult` with **every slot** filled (hand-built from the `core.types` contracts;
  the `team` breakdown goes through the real `core.kanon.publish` so a users-unknown row and a merged
  `(other: <5 users)` row appear; the rest uses `core.testing.published_for_tests`). The policy pack's
  README and hook text carry `CANARY`; renderers publish only their sha256.
- `ledger.py` — a small ledger (4 teams × 2 days, `anthropic_api` + `bedrock`, one subscription
  lane, one request without principal) ingested into `MemoryStore` (and `SqliteStore` in the gate);
  `CANARY` sits in the request `source` locator, which no renderer reads.
- `extension.py` — a test-only `copilot` section renderer registered with `monkeypatch` on
  `core.registry.EXTENSIONS` (resolved through the F-EXT host), and a minimal `CopilotSummary`.
- `snapshots/terminal_full.txt`, `snapshots/terminal_width80.txt` — terminal snapshots of the full
  fixture (100 and 80 columns). Regenerate: `TOKENBILL_UPDATE_SNAPSHOTS=1 pytest tests/v2/outputs -k snapshot`.

## Acceptance map (brief → test)

| acceptance item | test |
|---|---|
| `validate_result_json` rejects a float, an integer object without `evidence`, money not in MONEY form, `list_equivalent` under `bill.exact`; full fixture round-trips deterministically | `test_result_json.py::test_validate_rejects`, `::test_full_result_conforms_and_round_trips_deterministically`, `::test_deterministic_orders_arrays_by_stable_ids` |
| billed column fed an ESTIMATED / LIST_EQUIVALENT figure raises | `test_result_json.py::test_billed_slots_refuse_non_billed_figures`, `test_terminal.py::test_billed_lines_refuse_estimates_and_allowance`, `test_html.py::test_billed_places_refuse_non_billed_figures` |
| unpriced renders as "unpriced (N inferences)"; terminal renders each slot (snapshots) | `test_terminal.py::test_unpriced_bill_prints_unpriced_never_zero`, `::test_each_slot_renders_its_own_section`, `::test_full_result_snapshot`, `::test_narrow_snapshot` |
| HTML: no external URL, CSP meta, no `<script>`, every `<svg>` followed by `<table>` + `<caption>`, no pseudonyms; WCAG AA both themes; size budget | `test_html.py` |
| terminal sanitizes ANSI in names | `test_terminal.py::test_names_are_sanitized_and_pseudonyms_scrubbed` |
| FOCUS: required columns, `x_` regex, totals per day and team, unreconciled refusal / `allow_unreconciled` / `channels`, allowance `BilledCost = 0`, enrichment, chargeback < 95% refused, k-merge with `x_SuppressedUsers` | `test_focus.py` (incl. the hypothesis property `test_k_merging_preserves_every_days_total`) |
| SARIF required keys, one result per violation | `test_sarif.py` |
| showback: no individuals, anchors with sources, billing-path distribution only for groups ≥ k | `test_showback.py` |
| CANARY absent from every renderer output | `test_store_backed.py::test_canary_is_absent_from_every_renderer_output`, `test_showback.py`, `test_html.py` |
| gate: FOCUS totals equal `SqliteStore.cost_rows` sums; showback from a real `aggregate` + `core.kanon.publish` | `test_gate_store.py` (`gate`, `importorskip("tokenbill.store.db")`; verified locally against a temporary, uncommitted copy of `pkg/STORE` 6bd4f9f: 3 passed, FOCUS CSV byte-identical to MemoryStore) and the same checks on `MemoryStore` in `test_store_backed.py` |
| A-8: `render_sections`, `write_focus(…, extra_rows, owned_channels)`, `figure_json`, BILL line for `PricedTotal.pool` | `test_*::test_extension_*`, `test_focus.py::test_owned_channels_are_replaced_by_extension_rows`, `test_terminal.py::test_allowance_and_pool_are_apart_from_the_bill` |
| R-E47 "users unknown" via `core.kanon.row_notes`; R-E30 folding | `test_terminal.py::test_users_unknown_rows_say_so`, `test_result_json.py::test_users_unknown_cells_finer_than_team_fold_into_the_team_cell` |

Fuzz / property tests: `test_result_json.py::test_validator_never_raises` (arbitrary JSON values),
`::test_every_figure_encodes_to_a_valid_money_object`, `test_focus.py` (k-merge totals, extension
rows only raise `TokenbillError`).

## Interpretations (for consumers: CLI-LEDGER, CLI-SAVINGS, CP-OUT)

- **Schema rule scope.** An integer's nearest enclosing object (through arrays) carries `evidence`;
  counts are grouped into small objects labelled `"exact"` (`counts`, `coverage`, `privacy`, …).
  Two documented exceptions: the `range` of a MONEY object and the root's wall-clock `generated_ms`
  (the SPEC example shows it label-free; `--deterministic` drops it). Money keys are `usd`, `nano`,
  `*_usd`, `*_nano`; billed keys are `exact`, `billed`, `spend`, `invoice`, `joint_saving`,
  `headline_monthly`. A finding's evidence list is `evidence_items` (the key `evidence` is the label).
- **Labels chosen by OUT** where the contract carries raw nano: reconciliation `invoice` = EXACT
  INVOICE, `priced_provider` = EXACT on the rate-card basis, `ledger` / residuals / unexplained =
  ESTIMATED (ledger points include estimated lines); `MeasurePlan.mde_nano` = ESTIMATED; Shapley SE =
  ESTIMATED; `CheckResult` medians = EXACT on the rate-card basis. Policy-pack README and hook texts
  are written by `policy -o`; the result carries their sha256.
- **Extensions.** Sections render only when the extension's `RunResult` slot is set (`copilot`); an
  unavailable renderer adds `dq.extension_unavailable` to `data_quality`; an HTML section with a
  script or an external URL is refused (`ContractViolation`).
- **FOCUS.** `write_focus` keeps the SPEC §14.7 signature and adds keyword-only `extra_rows`,
  `owned_channels` (A-8), `findings` (fills `x_FindingIds` / `x_TopWasteCause`) and `reconciliation`
  (fills `x_ReconciliationDeltaPct` = (ledger − invoice) / invoice per channel). `contracted` and
  `recoverable_by_scope` are keyed by `focus.row_key(row)`; `recoverable_by_scope` also accepts
  `(team, lane_kind)` and `(team,)`. Rows must be grouped by `date` (the exporter needs the charge
  day; CLI-LEDGER: `cost_rows(group_by=("date", "provider", "channel", "model", "team",
  "cost_center", "project", "workspace_id", "lane_kind", "workload_class", "agent_product",
  "billing_path"))`). List-equivalent-only channels never block the export. An identity (all grain
  columns but team / cost center / project) whose users stay below k after `publish` is still
  exported as one org-level `(other: <k users)` row (FOCUS totals keep matching the ledger; no team
  is named). A CONTRACT-priced ledger has no list amount, so `ListCost` repeats the contracted
  amount; pass LIST rows plus `contracted` for a true split. ESTIMATED range amounts only in
  `x_EstimatedCostLow` / `x_EstimatedCostHigh`. Free-text cells starting with `= + - @` get a `'`
  prefix (spreadsheet formula guard). Split rows (`finops.allocation.split_rows`) fill
  `AllocatedMethodId` / `AllocatedMethodDetails`.
- **Showback.** "Overage share" is the share of seat requests on billing path `usage_credits` versus
  `subscription` in the `billing_paths` aggregate (QUOTA_STATE events are not part of published
  aggregates). A developer-month is 487/16 days (365.25 / 12). p50 / p90 per billing path are
  percentiles of cell means weighted by developers, labelled ESTIMATED. Context p50 / p90 are across
  the team's published cells (the mean always prints).

## Unverified facts (display names, not prices)

- FOCUS `ServiceName` / `ServiceProviderName` / `HostProviderName` / `InvoiceIssuerName` per channel
  (`focus._CHANNELS`): Anthropic, AWS, Google are from SPEC §14.4; `claude_platform_aws` (invoice
  issuer AWS), `foundry` (Microsoft), `azure_openai` (Microsoft) and `github_copilot` (GitHub) are
  not verified against a provider invoice.
- SARIF `$schema` URL `https://json.schemastore.org/sarif-2.1.0.json` (the common published
  location; the log is valid SARIF 2.1.0 either way).
- FOCUS column names and feature levels come from `core/facts.json` (`verification: primary`).
