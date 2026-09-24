# tests/v2/admin — ADMIN acceptance tests

Package ADMIN (wave 2): the provider-side truth as content-free ledger records (SPEC §5.11, §5.13,
D31, D32).

| module | adapters (registry name) |
|---|---|
| `tokenbill/adapters/anthropic_admin.py` | `UsageReportAdapter` (`anthropic-usage-report`), `CostReportAdapter` (`anthropic-cost-report`), `ClaudeCodeAnalyticsAdapter` (`anthropic-cc-analytics`), `EnterpriseAnalyticsAdapter` (`anthropic-enterprise-analytics`); plus the shared page machinery (`ReadContext`, `load_documents`, `classify_page`, exact money and timestamp parsers) |
| `tokenbill/adapters/openai_admin.py` | `OpenAIUsageBucketsAdapter` (`openai-usage-buckets`), `OpenAICostsAdapter` (`openai-costs`) |
| `tokenbill/adapters/cloud_billing.py` | `AwsCurAdapter` (`aws-cur`), `GcpBillingExportAdapter` (`gcp-billing`) — a no-float money module |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/admin` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/admin && uv run --python
3.12 --extra dev coverage report --include='tokenbill/adapters/anthropic_admin.py,tokenbill/adapters/openai_admin.py,tokenbill/adapters/cloud_billing.py'`
(≥ 95% at hand-off). A longer fuzz run: `TB_ADMIN_FUZZ_EXAMPLES=2500 uv run … pytest
tests/v2/admin/test_fuzz.py` (60 examples per property by default; 2,500 ran clean at hand-off).

## Behavior worth knowing (decisions the SPEC left open)

- **Inputs.** One recorded page per file (the documented response object), a JSON array of pages,
  JSONL of pages, a recorded-page wrapper `{"endpoint"|"url"|"path", "fetched_at"|"fetched_ms",
  "response"|"page"|"body"}`, `.gz` of any of these, or a **directory** (every file read in one
  pass; pages of other endpoints in a directory are skipped). Pages are classified by shape (or the
  wrapper's endpoint), so an empty page needs a wrapper to be attributed.
- **Ids.** Workspace, API-key, project and account ids become `h_` pseudonyms under the name key
  unless listed in `opts.name_allowlist` (brief: "api keys and workspaces `h_` under the name key";
  `IngestOptions` has no `--hash-workspaces` flag, see CONTRACT-CHANGE-ADMIN-1 (e)). A name key is
  required (`UsageError` otherwise). `SourceInfo.source_id`/`name_hmac` are HMACs of the file name.
- **People.** Person-level grouping values (`account_id`, `service_account_id`, `user_id`,
  `claude_tag_user_id`) are dropped and their rows summed (`stats["person_dims_dropped"]`). Analytics
  actors (e-mail, API key name, user id) are used only for the `opts.team_map` lookup (exact, then
  case-folded) and the distinct-user count, then discarded. Teams per `(date, team)` go through
  `core.kanon.merge_small_groups` with `opts.k_anonymity`: small groups merge into `(other)`, or are
  dropped with `dq.outcomes_suppressed` (count = groups; no token magnitude, which would
  disclose what the suppression hides; `stats["users_dropped"]` counts the people). Unmapped actors
  count as team `(unmapped)`.
- **Money.** JSON numbers are parsed as exact `Decimal` (never float); every amount is accumulated as
  an exact integer of 10⁻²¹⁰ USD, rows sharing a cost line's identifying fields are summed, and each
  line is rounded half-even once through `core.money.cents_to_nano` / `usd_str_to_nano`. The
  remainders of cost-line amounts are summed in `stats["rounding_remainder_e18"]` (1e-18 USD units,
  each remainder rounded half-even to 1e-18); remainders of aggregate costs (provider estimates,
  team-level Enterprise costs) are in `stats["aggregate_rounding_remainder_e18"]`. Amounts with more
  than 200 fractional digits or |amount| ≥ 10³⁰ USD are quarantined.
- **Finality.** Every source uses the 30-day revision window: a bucket/date is `final` once
  `opts.now_ms ≥ bucket end + 30 days`, else `provisional` (with the default `now_ms = 0` everything
  is provisional). `fetched_ms` comes from the wrapper's fetch time or Enterprise
  `data_refreshed_at`, else 0 (the store keeps one version per `agg_id`/`line_id`: final first, then
  the latest fetch).
- **Ids of records.** `agg_id = stable_id("ag", source_kind, start, end, dims…)` and `line_id =
  stable_id("cl", <every identifying field>)`, so a re-fetched page replaces the earlier version.
- **Enterprise per-user endpoints** produce team-level aggregates of their own source kinds
  `anthropic.enterprise_team_usage` and `anthropic.enterprise_team_cost` (reported cost on basis
  `invoice`, `list_cost_nano` from `list_amount`), never mixed with the organization-level
  `anthropic.enterprise_usage` / `anthropic.enterprise_cost` (CONTRACT-CHANGE-ADMIN-1 (a)).
  Per-user records need `starting_at` (request them with `bucket_width`).
- **OpenAI.** Only `organization.usage.completions.result` rows are token usage (other usage kinds
  are counted in `stats["results_skipped_other_kinds"]`); cache writes are `cache_write_other` with
  TTL 1800 s; `batch: true` → tier `batch`, `default` → `standard`. Cost lines keep `line_item` as
  the description; model/token type are never guessed from it.
- **CUR 2.0.** Bedrock rows only; `Tax` rows skipped; `line_item_line_item_type` becomes
  `cost_type` (`Usage`, `Credit`, …); tokens only from usage-type line items. Tokens per unit come
  from `pricing_unit` when it names tokens (`tokens`, `1K tokens`, `Thousand tokens`, `1M tokens`,
  `Million tokens`, `1,000 tokens`…), else from a verified SKU rule's `unit_tokens`, else none
  (`stats["rows_unit_unknown"]`, cost still kept). Principals → `p_` (identity modes
  `central-ingest`/`install` with a principal key) on the cost lines; team on the token aggregates:
  `opts.team_map` on the raw ARN, then on the role ARN of an `assumed-role` session, then the
  `iamPrincipal/team` tag, else `(unmapped)`. Legacy `lineItem/…` column names are accepted. Parquet
  → `SourceError` asking for the CSV export.
- **GCP.** Claude SKUs only (`sku.description` contains "Claude" or a verified rule matches); `tax`
  rows skipped; `amount = cost + Σ credits`, `list_amount = cost`; tokens only from `regular` rows;
  scope from `location.region` (then `location.location`, then a verified rule; none →
  `dq.scope_unknown`); labels allowlisted in `GCP_LABEL_DIMS` (`team`, `cost_center`/`cost-center`,
  `department`, `environment`, values `[A-Za-z0-9_.:/ -]{1,63}`), others dropped with
  `dq.unknown_fields`.

## Test files

| file | covers |
|---|---|
| `test_usage_cost_report.py` | usage report buckets and dims (`h_` workspaces/keys, default workspace, `not_available` geo, allowlist), person dims summed, unsplit cache creation, revision window vs injected clock, pagination JSONL, edge page (priority, fast, 1M context, web fetch); cost report exact nano, many-decimal strings and JSON numbers (exact remainders), context-window folding, negative adjustments, rejects; **the reconcilable pair priced to the nano by `FakePricer`**; edge-pair token lines equal FakePricer with the geo/fast modifiers |
| `test_cc_analytics.py` | fixture days: `(other)` group of 6 and the dropped 3+1; outcome sums; person-free output (canary e-mail, actor names, org id); team with 3 users merged or dropped; k from options; unmapped and case-folded actors; distinct counting; exact estimated cost; pages split across files read as a directory; window filter and malformed records; property over k |
| `test_enterprise.py` | usage/cost/user_usage/user_cost endpoints; `amount`/`list_amount` exact; `list_amount` = usage at list (FakePricer), `amount` = 80%; provisional vs final around the 30-day window; team rollups and suppression; whole directory; wrapper hint on empty pages |
| `test_openai_admin.py` | usage buckets (disjoint buckets, 30-min write TTL, derived uncached, sum checks, tiers, user rows summed, unix-second buckets); costs exact from JSON numbers, currency, API-key folding; fixture costs = buckets at list (FakePricer) |
| `test_aws_cur.py` | fixture CSV and `.gz` equal; net vs unblended, credits; principals `p_` and teams (role map, tag), central mode; verified-rule mapping of input/output/read/5m/1h/batch in 1K and 1M units; only Bedrock rows; hourly → daily; Parquet refusal; legacy columns; bad rows, strict mode, ragged/oversize CSV lines; export directories; corrupt gzip; unit parser; exact token conversion |
| `test_gcp_billing.py` | JSONL and CSV equal; `cost + credits`; region → scope; labels allowlist; unmapped vs verified SKU rules; units from pricing units or rules; non-Claude/tax/bad rows; flattened CSV headers; strict mode |
| `test_common.py` | `assert_adapter_conforms` on **every fixture** (and with the conformance default options); registry paths; sniff matrix (each fixture claimed only by its adapter) and truncated heads; classification; wrappers/arrays/JSONL/BOM/gzip; directory handling; bad documents; streaming of large files; required name key; window filters; content-free `SourceInfo`; byte-identical output across processes; no float and no network imports in the owned modules; shared parsers |
| `test_fuzz.py` | hypothesis: random JSON documents and mutated fixture pages for all eight adapters, random bytes (`.json`/`.csv`/`.gz`), random CUR rows and GCP rows, sniffing, scalar parsers — only `TokenbillError` escapes, outputs encode, no person dims/canary; cents parsing exact vs `core.money`; k-anonymity property over random teams |
| `test_fixtures_manifest.py` | MANIFEST lists every fixture; `build_fixtures.py` reproduces every file byte-identically; every adapter reproduces the script's closed-form expectations; all fixtures ingest into `MemoryStore` (idempotent, `p_` kept under the org key) |
| `test_gate_recon_pair.py` | **gate** (`importorskip` RATES, RECON): the recorded pair priced to the nano by the real `RateCard`; `recon.reconcile` verdict `reconciled` for `anthropic_api` |

`CONTRACT-CHANGE-ADMIN-1.md` lists the contract gaps found while building.

## Fixtures and provenance (`tests/v2/fixtures/admin/`)

All synthetic, generated by the stand-alone `build_fixtures.py` (no randomness, never imports
`tokenbill`; rates for the reconcilable pair from `tokenbill/core/facts.json`). `MANIFEST.json`
lists every file with its adapter, role (`recon_pair`, `pagination`, `edge`, `k_anonymity`,
`enterprise`, `openai`, `cloud`), provenance and closed-form expectations, plus the options the
expectations assume (`now` 2026-09-23, k = 5, the team map, identity mode) and the reconcilable
pair (`recon_pairs`). RECON's and F-KIT's gate tests read it.

Invoice-side files are price-consistent with their usage files at the `facts.json` list rates, so
any reconciliation over the whole directory is meaningful: the `recon_pair` cost report equals its
usage report at list (nano-exact); the edge cost report's token lines equal the edge usage at list
with the US-geo 1.1× and Opus 5 fast-mode ($10/$50) modifiers (its Priority Tier usage has no cost
line; code execution, session usage and a 4.5e-15 USD sub-nano remainder are the residuals under
test); the Enterprise `list_amount` equals Enterprise usage at list and `amount` is 80% of it plus
a code-execution line; OpenAI costs equal the usage buckets at the gpt-5.6-sol promotional rates
(each aggregate below the 272K long-context band). CUR/GCP amounts are self-consistent (net =
0.9 × unblended; GCP credits 5% of cost) but not tied to rates: their SKU rules are unverified. The content canary is planted in
person/content fields (e-mails, terminal types, actor names, IAM session names, GCP project names
and non-allowlisted labels, CUR line descriptions); provider descriptions that adapters copy are
never planted.

| file(s) | shape source (retrieved 2026-09-23) |
|---|---|
| `anthropic/usage_report_2026-08.json` + `cost_report_2026-08.json` (reconcilable at list), `usage_report_2026-09_pages.jsonl`, `edge/*` | platform.claude.com Usage & Cost API reference (Get Messages Usage Report, Get Cost Report) |
| `anthropic/cc_analytics_*` | platform.claude.com Claude Code Analytics API guide |
| `anthropic/enterprise/*` | platform.claude.com Claude Enterprise Analytics API reference |
| `openai/*` | openai-openapi `UsageResponse`/`UsageTimeBucket`/`UsageCompletionsResult`/`CostsResult` |
| `cloud/cur2_bedrock_2026-09.csv(.gz)` | AWS CUR 2.0 data dictionary (line item columns, `INCLUDE_IAM_PRINCIPAL_DATA`) |
| `cloud/gcp_billing_2026-09.{jsonl,csv}` | GCP standard usage cost export schema |

## Facts: verified and unverified

Verified against the primary documentation on 2026-09-23 (shapes and field names only):
Anthropic usage report (`uncached_input_tokens`, `cache_read_input_tokens`,
`cache_creation.ephemeral_{5m,1h}_input_tokens`, `output_tokens`, `server_tool_use.web_search_requests`,
group-by `api_key_id`/`workspace_id`/`model`/`service_tier`/`context_window`/`inference_geo`/`speed`,
plus the person-level `account_id`/`service_account_id`); cost report (`amount` cents decimal
string, `cost_type` ∈ {`tokens`, `web_search`, `code_execution`, `session_usage`}, `token_type` ∈ the
five token fields, `workspace_id` null = default workspace, Priority Tier excluded); Claude Code
Analytics record shape (`actor.{email_address|api_key_name}`, `core_metrics`, `tool_actions`,
`model_breakdown[].tokens.{input, output, cache_read, cache_creation}`, `estimated_cost.amount` in
cents); Enterprise Analytics endpoints and fields (`amount`/`list_amount` cents strings,
`data_refreshed_at`, 30-day revisions, flat per-user records); OpenAI `input_uncached_tokens`
excludes cache writes, `amount.value` is a JSON number in USD; CUR 2.0 column names including
`line_item_iam_principal` (AWS documents data from 2026-04-08; SPEC §19.4 says 2026-04-17);
GCP export field names, `credits[].amount`, `cost_type` ∈ {regular, tax, adjustment,
rounding_error}.

**Unverified** (shipped as documented guesses, flagged here and in the code):

- Claude Code Analytics token convention (exclusive vs inclusive, SPEC §19.8 #7): implemented as
  exclusive; `cache_creation` has no TTL split → `cache_write_unknown` (`dq.no_ttl_split`).
- `cost_type`/`token_type` values beyond the documented enumerations and the cost-report
  description texts (fixtures invent descriptions such as "Claude Opus 5 Usage - Input Tokens").
- OpenAI endpoint paths (the platform docs refused automated access; field names come from the
  OpenAPI description) and `line_item` texts.
- CUR `pricing_unit` strings for Bedrock usage types (fixtures use `1K tokens` / `1M tokens` / `Units`)
  and every usage-type → bucket rule (all `verified: false` in facts.json, so `map_sku` maps nothing
  and CUR/GCP tokens take the unmapped path; the model of a CUR row is not derivable from its usage
  type).
- GCP `usage.unit` of Claude SKUs (fixtures use `tokens`; `count` is not converted), Claude SKU ids,
  and whether multi-region locations should be `multi_region` (implemented per SPEC: "global" →
  global, anything else → regional).
- The recorded-page wrapper format (RECON's `pull` owns it; the adapters accept the keys above).
- Release gate (SPEC §19.8 #6): no real, redacted usage/cost page pair from an adopting organization
  exists yet.
