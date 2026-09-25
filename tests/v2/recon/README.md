# tests/v2/recon — RECON acceptance tests

Package RECON (wave 2): the ledger gate (SPEC §12, §10.3, D19, D26, D31, D32) and the Copilot
amendment A-5 (`merge_reports` unions `ReconciliationReport.decisions`).

| module | provides |
|---|---|
| `tokenbill/recon/reconcile.py` | `reconcile(...)` (SPEC §12 signature), `merge_reports(reports)`, `reconciled_channels(report)`, `suggest_contracts(...)` |
| `tokenbill/recon/residuals.py` | `classify(...)` (SPEC §12 signature), `classify_rows(...)` (per-row detail), `RESIDUAL_CODES` |
| `tokenbill/recon/costmap.py` | `COST_TYPE_MAP` (data), `map_line`, `sku_rule_status`, the source-kind / channel tables |
| `tokenbill/recon/pull.py` | `pull(kind, *, key_env, since, until, out_dir, opener=None, sleep=time.sleep)`, `PULL_KINDS` |
| `tokenbill/recon/orgscan.py` | `OrgScan` (registry `aggregate.org-scan`, requires `{"aggregates"}`, `aggregate = True`) |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/recon` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/recon && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/recon/*'` (97% at hand-off). A longer
fuzz run: `TB_RECON_FUZZ_EXAMPLES=800 … pytest tests/v2/recon/test_fuzz.py` (800 ran clean).

## Behavior worth knowing (decisions the SPEC left open)

- **Rows.** One `ReconRow` per `(channel, date, workspace, model, bucket[, service_tier])`, keys
  as ordered `(name, value)` pairs. `workspace` is the provider id, `(default)` for the null-id
  default workspace, or `*` when some ledger record of the channel has no workspace (then every row
  of the channel joins per model-day). Buckets are `UsageBuckets` fields plus `web_search` (per
  workspace, no model), `code_execution` / `session_usage` (cost-report only), `unmapped` (with a
  `cost_type` or `sku` pair), `credits`, and `total` (channel-total mode, per channel-day).
  Informational Claude Code Analytics team-day rows carry an `info` pair and never enter a verdict,
  a residual or a coverage total. `web_search` rows carry no token counts.
- **Invoice sources.** Per channel the first of `INVOICE_PRECEDENCE` present is used (the Admin cost
  report before the Enterprise Analytics cost endpoint; the two cover the same spend and are never
  added) with its paired usage kind (`INVOICE_USAGE_PAIRS`). GCP lines are checked against the gross
  `list_amount` (cost) and the netted credits become a `credits` row (residual `cloud_credits`); CUR
  `Credit` / `Refund` line items are `credits` rows too.
- **Channel-total mode.** A channel whose token lines cannot be mapped to (model, bucket) — an
  unverified or missing SKU rule, a CUR line without a model, Enterprise cost types (no documented
  enumeration), OpenAI line items — is reconciled on channel-day totals: token coverage on totals and
  the ledger's **list-basis** dollars scaled by the channel's own `invoice / list` (the lines' list
  amounts when every line has one, else our price of the provider's usage); contract-basis ledger
  dollars are not discounted again. `mapping_verified = False`.
- **Rate-card check** per closed model-day (web search per day): within `tolerance_pct` or
  `$0.01 × invoice lines`; unpriced provider usage, provider usage without an invoice line and an
  invoice line without provider usage all fail (unknown is never within tolerance).
- **Residuals** are signed parts of the `invoice − ledger` gap (`gap = Σ claims + unexplained` per
  model-day group, property-tested), except two informational magnitudes: Priority Tier usage priced
  by our rate card, and list-equivalent allowance dollars. `unobserved_traffic` is the whole gap of
  a model-day without ledger tokens, else our price of the provider tokens the ledger lacks (per
  bucket, capped at the net token deficit). `implied_discount` is `invoice − our price of the
  provider's tokens` beyond 1 nano per invoice row (rounding noise). Reconstructed ledger usage
  (placeholder output, partial streams, hidden compaction) may hide up to the provider's excess in
  its bucket (`estimated_components`). `cents_rounding` explains up to Σ parse remainders
  (`rounding_remainders`, keyed by adapter name, R-E44); a sub-nano remainder is listed with 0.
  `session_usage` (Managed Agents runtime) is classified with code execution as cost-report-only.
  A row keeps the dominant code of its group.
- **Statuses**: `match`, `explained`, `within_tolerance` (a non-zero rate-card error inside the
  tolerance, or ≤ 1 nano per row of rounding), `unexplained` (unexplained nano, or a model-day
  outside the rate-card tolerance), `over` (over-count), `provisional`, and `under` / `match` by
  tokens on channels without invoice rows.
- **Revision window.** A day is provisional while `today − date ≤ 30` days (final once `today ≥ day
  end + 30 days`, the rule the ADMIN adapters use for `finality`); `closed_only` drops those dates
  from every input. Finality of the report: `provisional` if any row is, else `final` (`n/a` without
  rows). With no rows the window is `(today, today)`.
- **Verdicts.** Per channel: `insufficient_data` without invoice rows; `reconciled` iff every closed
  model-day is within the rate-card tolerance, unexplained ≤ `unexplained_pct` of the invoice per
  closed workspace-month (never below the channel's parse-remainder magnitude), and no over-count;
  else `not_reconciled`. Overall per §12.4 (channels with ledger spend; without any, the channels with
  invoice data). Over-counts are counted per model-day group.
- **Contract suggestion** (§12.2): multipliers `invoice / list` per model over closed dates, rounded
  to 4 decimals; see CONTRACT-CHANGE-RECON-1 (a) for per-channel overlays. The re-run prices each
  ledger record with its channel's overlay pricer **in the same streamed pass** (the overlays come
  from the provider side, before the ledger is read), so the ledger is consumed once.
  `rerun_pricer_factory` defaults to `pricer.with_contract` when the pricer has it.
- **merge_reports**: disjoint channels (else `ContractViolation`), equal tolerances, decisions via
  `core.extensions.recon_decisions_of`; verdict, coverage and finality recomputed over the union
  (an extension report whose rows carry no `channel` dim counts all its channels as having ledger
  spend when its rows do); the merged `rate_card_error` is the maximum of the inputs' triples
  (conservative); a single report is returned unchanged.
- **Org scan.** Provider usage kinds per channel as in reconciliation; scopes `channel`,
  `workspace_id` (`(default)` for the null id; `api_key_id` only with the threshold
  `aggregate.org-scan.scope = api_key` **and** break-glass) and `model`; unpriced scopes are skipped;
  every finding passes `core.findings.min_usd_gate`; `n_users = 0` (no person dimension); category
  `aggregate`. Threshold `aggregate.org-scan.read_share_below` overrides 0.80.
- **Pull.** One owner-only JSONL file per kind and window (`<kind>_<since>_<until>.jsonl`, written
  as `.partial` and renamed; removed on failure) of wrappers `{"endpoint", "fetched_ms",
  "response"}`; `fetched_ms` from the response `Date` header; the body is kept verbatim (numbers are
  never re-encoded) on one line, and a body that echoes the key is redacted. Person-level groupings
  are never requested. Backoff doubles per attempt (1, 2, 4, … ≤ 60 s) unless `retry-after` says
  otherwise; at most 6 retries.

## Test files

| file | covers |
|---|---|
| `test_reconcile.py` | the brief's acceptance tests: exact pricing; 15% discount → 0.15 / overlay 0.85 / re-run reconciled; non-uniform → not a simple multiplier; per-model overrides; two channels (bedrock `insufficient_data`, `reconciled_channels`); CUR channel-total (disabled rules, `mapping_verified = False`, 0.10 discount, bedrock overlay, 3% gap fails), CUR mapped with verified rules; every residual code; over-count; rate-card failures; Enterprise and OpenAI channel totals; CC Analytics info rows; argument errors; determinism; streaming |
| `test_residuals.py` | `classify` in SPEC order, the bridge identity, special rows, channel codes, cents rounding |
| `test_costmap.py` | the documented enumerations, `map_line` on every line kind, disabled vs verified SKU rules |
| `test_merge.py` | `merge_reports`: union, verdict/coverage/finality, decisions (A-5), conflicts, reruns |
| `test_pull.py` | fake opener + fake sleep: pagination, 31-day chunks, per-day Claude Code Analytics, 60 rpm, `retry-after` ≤ 60 s, bounded retries, key never in pages/logs/errors (grep), `UsageError`s, the socket guard; gate: pulled pages read by the ADMIN adapters and reconcile |
| `test_orgscan.py` | cache-read share 0.60 (sources) / 0.90; fast/geo/priority premiums exact to the nano; thrash, TTL mix, batch share, effective discount; min-usd, window, delegated channels, api-key scope; `assert_detector_conforms`; `run_detectors` once per run |
| `test_fuzz.py` | hypothesis: `retry-after` parser, `pull` on random responses, `reconcile` on random worlds (only `TokenbillError`, order-independent), classifier bridge, multiplier recovery |
| `test_guards.py` | no float in `tokenbill/recon` (AST), network imports only in `pull.py`, docstrings, canary |
| `test_gate_ratecard_admin.py` | **gate** (`importorskip` RATES engine/schema and the ADMIN adapters): ADMIN's recorded fixtures reconcile with the real `RateCard` (and, meanwhile, with `FakePricer`): the pair to the nano, the edge pair's residuals, every fixture in one run |
| `test_gate_fleet.py` | **gate** (`importorskip` SYNTH-FLEET): the synthetic fleet recovers `FleetTruth.recon` (multipliers per channel, priority and allowance residuals, provisional dates, re-run reconciled) |

`CONTRACT-CHANGE-RECON-1.md` lists the contract gaps found while building.

## Fixtures and provenance (`tests/v2/fixtures/recon/`)

All synthetic, hand-written. `pull/usage_report_p1.json`, `usage_report_p2.json` (two pages linked
by `next_page`) and `cost_report_p1.json` are Admin API response bodies shaped from the *Get Messages
Usage Report* and *Get Cost Report* references (platform.claude.com, retrieved 2026-09-24); the cost
lines equal the usage at the `facts.json` Opus 5 rates. Every other test builds its records with
`core.builders` (`make_aggregate`, `make_cost_line`, `make_ctx`, `make_usage`) and prices them with
`FakePricer`; the gate tests read ADMIN's checked-in fixtures (`tests/v2/fixtures/admin/`, their
MANIFEST) through ADMIN's public adapters. No real transcripts, pages or keys.

## Facts

Verified against the primary documentation on 2026-09-24:
- Admin cost report `cost_type` ∈ {`tokens`, `web_search`, `code_execution`, `session_usage`},
  `token_type` ∈ {`uncached_input_tokens`, `cache_read_input_tokens`,
  `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`,
  `output_tokens`}, `service_tier` ∈ {`batch`, `standard`} (Priority Tier absent), `workspace_id`
  null for the default workspace (→ `COST_TYPE_MAP`, verified for `anthropic.cost_report` only).
- Usage report `service_tier` ∈ {`batch`, `flex`, `flex_discount`, `priority`, `priority_on_demand`,
  `standard`} (`priority` and `priority_on_demand` are Priority Tier, `PRIORITY_TIERS`); grouping by
  `speed` needs the `fast-mode-2026-02-01` beta header.
- Pagination (§19.8 #10): `has_more` / `next_page` in responses, `page` in requests, for the usage
  and cost reports, Claude Code Analytics and Enterprise Analytics; `limit` ≤ 31 buckets at `1d`;
  Claude Code Analytics `starting_at` is one `YYYY-MM-DD` day, `limit` ≤ 1,000; Enterprise
  Analytics ≤ 31 days per query, 30-day revision window, `amount` / `list_amount`; headers
  `x-api-key` and `anthropic-version: 2023-06-01`.

**Unverified** (shipped disabled or as documented guesses):
- Every CUR usage-type / GCP SKU rule (§19.8 #17): `verified: false` in `facts.json`, so `map_sku`
  maps nothing and Bedrock / Vertex reconcile in channel-total mode (`mapping_verified = False`);
  the CUR model identity is not derivable (CONTRACT-CHANGE-RECON-1 (e)).
- Enterprise Analytics `cost_type` / `token_type` enumerations (no documented list): mapped through
  `COST_TYPE_MAP` but unverified → channel totals.
- The 60 requests-per-minute Enterprise Analytics limit comes from SPEC §19.4 (EAAPI); the fetched
  reference page does not state it. No client-side limit is applied to the Admin usage/cost/Claude
  Code endpoints (none documented). OpenAI admin endpoints are not pulled (paths unverified).
- CUR line item types treated as credits (`Credit`, `Refund`) and GCP `cost_type` values other than
  `regular` (→ `unmapped`).
- Release gate (§19.8 #6): no real, redacted usage/cost page pair from an adopting organization
  exists yet; the CLI must print `dq.recon_schema_unverified` until one does.
