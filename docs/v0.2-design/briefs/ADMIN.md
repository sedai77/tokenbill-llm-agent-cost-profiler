### ADMIN — Admin/Analytics page adapters and cloud billing exports (wave 2)

**Goal.** Parse the provider-side truth — Anthropic usage/cost/Analytics pages, OpenAI organization usage and
costs, AWS CUR 2.0 and the GCP billing export — into `UsageAggregate`, `CostLine` and team-level
`OutcomeAggregate` records, exactly, content-free and never per person (SPEC §5.11, §5.13, D31, D32).

**Owns.** `tokenbill/adapters/{anthropic_admin,openai_admin,cloud_billing}.py`, `tests/v2/admin/**`,
`tests/v2/fixtures/admin/**`.

**Consumes.** `core.records` (`UsageAggregate`, `CostLine`, `OutcomeAggregate`), `core.types`
(`IngestOptions`, `IngestResult`), `core.money` (`cents_to_nano`, `usd_str_to_nano`), `core.ids`, `core.jsonl`,
`core.kanon.merge_small_groups`, `core.models`, `core.catalog` (`map_sku`, `SKU_RULES`), `core.builders`,
`core.testing` (`assert_adapter_conforms`). Map CUR usage types / GCP SKUs only through
`core.catalog.map_sku` (verified rules only); unmapped rows keep `sku`, leave `model`/`token_type` empty and
count in `dq.unmapped_sku` — never guess.

**Provides.** Adapters `UsageReportAdapter`, `CostReportAdapter`, `ClaudeCodeAnalyticsAdapter`,
`EnterpriseAnalyticsAdapter`, `OpenAIUsageBucketsAdapter`, `OpenAICostsAdapter`, `AwsCurAdapter`,
`GcpBillingExportAdapter` at the registry paths of SPEC §3.7; checked-in recorded-page fixtures with a
`MANIFEST.json` (RECON's and F-KIT's gate tests read them).

**Build.** Page adapters per §5.11: cents decimal strings parsed exactly with remainders recorded in
`IngestResult.stats["rounding_remainder_e18"]` (the remainder in 1e-18 USD units as an int); provisional within
the 30-day revision window relative to `opts.now_ms`; Claude Code Analytics and Enterprise user-level endpoints
**aggregated to (date, team) at ingest** through `opts.team_map` with `opts.k_anonymity` (`core.kanon`), groups
below k merged into `(other)` or dropped with `dq.outcomes_suppressed`; actor refs never leave the adapter.
Cloud billing per §5.13: CUR 2.0 CSV/CSV.gz (Bedrock product codes only; `line_item_iam_principal` → team map →
`p_` with the principal key; account ids → `h_` with the name key; `pricing_unit` 1K vs 1M normalization;
net vs unblended cost), GCP billing export CSV/JSONL (Claude SKUs; credits; labels allowlist; region → scope).
Channel set on every aggregate and cost line.

**Facts to verify.** §19.4 usage/cost/analytics field names and CUR/GCP columns; checklist §19.8 #6, #7
(field names; the map itself is RECON's), #17 (column names only). Build fixtures shaped from the documented
responses; mark unverified names in your README.

**Acceptance tests.**
- usage report pages → aggregates with the right buckets and dims (api keys and workspaces `h_` under the name
  key); cost report → cost lines with exact nano and remainders; many-decimal cents strings parsed without
  float (no-float lint clean on `adapters/cloud_billing.py`);
- Claude Code Analytics per-user rows become `(date, team)` aggregates and outcome rows; a team with 3 users
  is merged into `(other)` or dropped; no output row carries a principal, actor id or email;
- Enterprise Analytics `amount`/`list_amount` parsed; dates within 30 days of the injected clock are
  provisional;
- OpenAI usage buckets and costs parsed with channel `openai_api`;
- CUR: a synthetic CUR 2.0 CSV (and its `.gz`) with Bedrock input/output/cache rows in 1K and 1M units →
  token aggregates and cost lines; IAM principals become `p_` and teams; non-Bedrock rows skipped; Parquet →
  `SourceError` naming the CSV export; GCP: Claude SKU rows with credits → cost lines with `amount = cost +
  credits`, labels allowlisted, region → endpoint scope;
- `assert_adapter_conforms` on every adapter; CANARY absent; hypothesis fuzz of every parser.

**Size.** ~2.5k LOC including tests.
