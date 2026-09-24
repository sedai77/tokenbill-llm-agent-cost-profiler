# tests/v2/telemetry — TELEM (wave 2)

OTLP/JSON, OpenAI, Bedrock and Anthropic-response adapters and the non-Anthropic usage conventions
(SPEC §5.1, §5.2, §5.9, §5.10, D23, D43). Owned modules:
`tokenbill/adapters/{conventions_ext,otel,openai,bedrock,anthropic_responses}.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/telemetry` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/telemetry && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/adapters/conventions_ext.py,tokenbill/adapters/otel.py,tokenbill/adapters/openai.py,tokenbill/adapters/bedrock.py,tokenbill/adapters/anthropic_responses.py'`
(98% at hand-off).

| file | covers |
|---|---|
| `helpers.py` | area-local: fixture paths, `central()` ingest options (org key, fixture team map), `p_of`, `blob` (leak checks), OTLP/JSON builders (`attr`, `event`, `logs`, `span`, `spans`, `metrics`, `point`) |
| `test_conventions.py` | one golden sum-check case (or more) per convention from `conventions_golden.json` (OpenAI Responses 10,000 / 6,000 / 2,000 → 2,000 / 6,000 / 2,000 at 1,800 s; Chat reasoning subset and 5.6 writes; GenAI new and legacy names; read + write > input → exclusive with `dq.convention_mismatch`; OpenInference span; CrewAI 17 vs 17,119 → `dq.sum_check_failed`; Bedrock `cacheDetails` with residual → unknown, no details, exact split; Claude Code OTel); registration and idempotence; `SumCheckError` quarantine path; `bad_usage` table; hypothesis fuzz (only `TokenbillError` escapes) and total-preservation properties; the shared parsers (`parse_iso_ms`, `to_int`, `usd_to_nano` without binary-float digits, `clean_label`, `canonical_usage_json`, `cache_scope`, identity modes, team map, names, error table, `head_record`, strict mode) |
| `test_otel.py` | brief acceptance: 3 `api_request` + 1 `api_error` + metrics → 3 requests, 1 failed attempt + API_ERROR event, aggregates never requests; `intValue` strings parsed; `cost_usd` only `provider_estimate` (pricing identical with and without it); `user.email` absent everywhere, `p_` under the org key; team from the team map; resource attributes → attribution; raw-body events counted and ignored; beta `llm_request` span → `ttft_ms` and exact subagent lanes; without spans a shared inexact subagent lane (`dq.lanes_inferred`); trace-only `llm_request` spans; joins by `client_request_id`; duplicates; subscription/API-key/cloud TTL hints and channels; tool results on the tool span's lane; cumulative metric temporality; `.zst` on Python < 3.14 → `SourceError` naming `compression: none`; `.gz`; timestamps from `eventName` / `event.timestamp` / numeric encodings; quarantine and strict mode; since/until; central collector modes; GenAI (new, legacy, mismatch, conversation lanes, `msg_` ids, served tier); OpenInference AGENT wrapping two LLM spans → exactly two requests; span errors; sniffing; `AnyValue` decoding |
| `test_openai.py` | served `service_tier`; Responses and Chat buckets; truncation → `max_tokens`; every diagnostics reason (verified `*_changed` labels and the SPEC's short labels) → canonical reason with `provider_reason` verbatim; `cache_missed_tokens` never priced (FakePricer); Azure → `sub:` + `h_` scope; `previous_response_id` chains (parent after child) and conversation lanes; `request_meta` overrides and the attribution allowlist; failed / cancelled / batch-error responses; quarantines; sniffing |
| `test_bedrock.py` | `cacheDetails` split with residual → unknown; `global.` → scope global, geo/in-region → regional, inference-profile ARNs; InvokeModel bodies via `anthropic.messages` (iterations, tier, speed, geo); `identity.arn` → team map (role form) → `p_`, raw ARN dropped; `requestMetadata` allowlist and lanes; error codes; Converse and Anthropic streams; OpenAI models on Bedrock; unknown models unpriced; sniffing |
| `test_anthropic_responses.py` | response fields (message-id request ids, TTL split, diagnostics, applied edits, attribution); batch results carry `service_tier="batch"` and unbilled result types are skipped; a Vertex `request_meta` supplies `model_raw` and `endpoint_scope`; iterations and the refusal rule; error responses → failed attempts; split-entry de-duplication; quarantines; sniffing |
| `test_conformance.py` | `core.testing.assert_adapter_conforms` for all four adapters on all six fixtures, in install and central-ingest mode; protocol surface, registry (`get_adapter`, `sniff_adapter`), each fixture claimed by exactly one TELEM sniffer; lazy convention loading in a fresh process; byte-identical output across processes and `PYTHONHASHSEED`; every fixture into `MemoryStore` (idempotent, no nulled names/principals); a GenAI span and a recorded response merge by message id; the checked-in fixtures equal the builder's output |
| `test_fuzz.py` | hypothesis fuzz per parser: random bytes, random JSON, structured OTLP noise (logs, spans, metrics with hostile `AnyValue`s and timestamps) and mutated fixture records, lenient and strict; only `TokenbillError` escapes, output serializes, no canary |
| `test_gate.py` | `@pytest.mark.gate` (merge gate 1): fixtures priced by the real `RateCard([load_builtin()])` equal `FakePricer` on its rows and provider estimates never change a price; all fixtures ingest into the real `SqliteStore` idempotently |
| `CONTRACT-CHANGE-TELEM-1.md` | `AppendedItem` has no place for the beta `claude_code.tool` span's `result_tokens` |

## Fixtures and provenance (`tests/v2/fixtures/telemetry/`)

All synthetic, written by `build_fixtures.py` (run it as a script from the repository root; it is
deterministic and `test_checked_in_fixtures_match_their_builder` pins the bytes). Field names follow
SPEC §19.4 and the primary sources below; no real transcripts, telemetry or provider pages are used.
Content-bearing fields (prompts, tool parameters, message text, raw API bodies, emails, IAM session
names, repo URLs, batch `custom_id`s) carry `core.builders.CANARY` / `CANARY_EMAIL`; every test
asserts the canary never reaches an output.

| file | content |
|---|---|
| `otlp_claude_code.jsonl` | one Claude Code session exported by the collector file exporter: logs (`user_prompt`, 3 `api_request`, 1 `api_error`, 2 `tool_result`, 2 raw-body events), beta traces (`llm_request` spans with `ttft_ms` / `agent_id`, a `tool` span with `result_tokens`), delta metrics (`claude_code.token.usage` by type, `claude_code.cost.usage`, one unrelated metric) and one truncated line |
| `otlp_genai.jsonl` | GenAI spans: semconv ≥ 1.42 names (OpenAI, served tier `flex`), legacy `cache_creation` names (Anthropic), a read + write > input span under an `invoke_agent` span, an `embeddings` span |
| `otlp_openinference.jsonl` | an AGENT span wrapping two LLM spans (one under a CHAIN), a TOOL span, and a CrewAI-style LLM span (17 prompt tokens, 17,102 cache reads, total 367) |
| `openai_usage.jsonl` | Responses (diagnostics `tools_changed`, 30-minute writes, reasoning, chained by `previous_response_id`, a truncated one, a `comparison_response_not_found`), a Chat completion, an Azure `{request_meta, response}` pair, a Batch API output line, a negative-residual record |
| `bedrock_invocations.jsonl` | invocation logs (Converse with `cacheDetails` and a residual, InvokeModel with an Anthropic body, a throttled call, a ConverseStream body under an inference-profile ARN) and a Converse `{request_meta, response}` pair |
| `anthropic_responses.jsonl` | `{request_meta, response}` pairs: a message with a 1h write, diagnostics and applied edits; a batch result; a Vertex response without a model; a refusal/fallback with iterations; an expired batch result; a bare message without a timestamp; an error |
| `conventions_golden.json` | the golden sum-check cases of `test_conventions.py` (raw usage, expected buckets and codes, provider total) |

## Facts verified against primary sources (2026-09-23)

* Claude Code OTel (<https://code.claude.com/docs/en/monitoring-usage>): events
  `claude_code.{api_request, api_error, tool_result, user_prompt, api_request_body,
  api_response_body}` with `event.name`; `api_request` attributes `request_id`,
  `client_request_id`, `model`, `query_source` (`main` / `subagent` / `auxiliary`), `speed`,
  `effort`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`, `cost_usd`, `attempt`, `stop_reason`; `api_error` `status_code`,
  `attempt`, `error`; standard attributes `session.id`, `user.id` (random), `user.account_uuid`,
  `user.account_id`, `user.email`, `organization.id`, `app.version`, `app.entrypoint`, `vcs.*`;
  metrics `claude_code.token.usage{type=input|output|cacheRead|cacheCreation}` and
  `claude_code.cost.usage`; beta spans `claude_code.llm_request` (`ttft_ms`, `agent_id`,
  `parent_agent_id`, `request_id`, token counts) and `claude_code.tool` (`result_tokens`,
  `tool_use_id`, `agent_id`).
* OTLP/JSON encoding (<https://opentelemetry.io/docs/specs/otlp/>): 64-bit integers are decimal
  strings (numbers accepted when decoding), trace/span ids hex, enums integers, unknown fields
  ignored.
* OTel GenAI (<https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/> and the
  `semantic-conventions-genai` repository): `gen_ai.usage.input_tokens` SHOULD include cached
  tokens; `cache_read.input_tokens`, `cache_creation.input_tokens` (1.40–1.41) and
  `cache_write.input_tokens` (≥ 1.42); `reasoning.output_tokens`; `gen_ai.system` deprecated for
  `gen_ai.provider.name`; `prompt_tokens` / `completion_tokens` deprecated.
* OpenInference (<https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md>):
  `openinference.span.kind` values; `llm.token_count.{prompt, completion, total}`;
  `prompt_details.{cache_read, cache_write}` are sub-counts of `prompt`;
  `completion_details.reasoning`.
* OpenAI prompt caching (<https://developers.openai.com/api/docs/guides/prompt-caching>):
  `usage.input_tokens_details.{cached_tokens, cache_write_tokens}`; `prompt_cache_options.ttl`
  only `30m`. Diagnostics (<https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics>):
  top-level `prompt_cache_diagnostics.{type, reason, comparison_reusable_tokens,
  cache_missed_tokens}`, types `cache_hit | cache_miss | comparison_response_not_found |
  unavailable`, request `prompt_cache_options.comparison_response_id`, Responses API, GPT-5.6+.
  Batch output lines (<https://developers.openai.com/api/docs/guides/batch>):
  `{id, custom_id, response: {status_code, request_id, body}, error}`.
* Bedrock (<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>,
  `API_runtime_TokenUsage`, `API_runtime_CacheDetail`, `API_runtime_Converse`,
  `model-invocation-logging`): `inputTokens` is only the non-cached input (total = input +
  cacheRead + cacheWrite); `cacheDetails[{ttl: 5m|1h, inputTokens}]`; Converse response
  `serviceTier.type`, `metrics.latencyMs`, `stopReason` values; the log entry format
  (`schemaType`, `timestamp`, `accountId`, `requestId`, `operation`, `modelId`, `identity.arn`,
  `requestMetadata`, `output.outputBodyJson`); Converse error codes and HTTP statuses.
* Message Batches (<https://platform.claude.com/docs/en/build-with-claude/batch-processing>): result
  types `succeeded | errored | canceled | expired`; only `succeeded` is billed.

### SPEC errata found while verifying (implemented both ways, no contract impact)

* OpenAI miss reasons are spelled with a `_changed` suffix (`model_changed`,
  `prompt_cache_key_changed`, `service_tier_changed`, `tools_changed`, `text_format_changed`,
  `reasoning_effort_changed`, `verbosity_changed`, `input_changed`) plus `context_compacted`; the
  status field is `type`. SPEC §5.10 lists the short labels; `DIAGNOSTIC_REASONS` accepts both and
  keeps the provider label verbatim.
* `claude_code.tool_result` no longer documents `tool_result_size_bytes` — see
  `CONTRACT-CHANGE-TELEM-1.md`.

## Unverified (shipped conservatively; for the release notes)

* `ts_start_ms = time − duration_ms` for `api_request` (the event is emitted at completion): SPEC
  §5.9 **VERIFY**; no recorded Claude Code export was available. Beta `llm_request` spans give the
  start directly when present.
* Whether Claude Code encodes numeric event attributes as `intValue`/`doubleValue` or as strings at a
  given version: both are accepted (§19.8 #8).
* `tool_result_size_bytes` (see above) and the join of `tool_result` events to lanes through
  `claude_code.tool` spans (`tool_use_id`).
* Bedrock invocation-log `errorCode` for failed calls (not in the documented entry format; used only
  when present) and `serviceTier.type` values other than `default`/`priority`/`flex` (→ `unknown`).
* The Anthropic `diagnostics.cache_miss_reason` location in a Messages response (top-level
  `diagnostics`, as in transcripts); unknown types map to `unavailable` with the label kept.
* Chat Completions `prompt_tokens_details.cache_write_tokens` on GPT-5.6+ (SPEC §5.2; the verified
  pages show it for Responses); read when present.

## Design notes

* **No double counting.** Metrics become `UsageAggregate(source_kind="otel.metric")` only; per-user
  metric series are summed per `(bucket, channel, model, product, speed, team)` so no identity
  survives; cumulative series keep their latest point. Raw-body events are never decoded.
* **Identity.** Raw identities (`user.*`, `identity.arn`, `request_meta.attribution.principal`) are
  looked up in `opts.team_map` (the admin's map outranks caller-supplied team metadata), then
  pseudonymized under `opts.principal_key` (emails lower-cased first) and dropped.
  `SourceInfo.principal_key_id` is set only when a `p_`/`c_` value was emitted; `name_key_id` is
  always set (`SourceInfo.name_hmac` is an `h_` value).
* **Lanes.** Claude Code: `session.id` + `query_source`, exact per `agent_id` when spans exist.
  GenAI: conversation id, else the nearest `invoke_agent` span. OpenInference: nearest AGENT span.
  OpenAI: `request_meta`, conversation, `previous_response_id` chain. Bedrock: `requestMetadata`
  `session`/`lane`. Anthropic responses: `request_meta`. Anything else is one request per lane with
  `lane_exact=False` and `dq.lanes_inferred`.
* **Duplicates.** A record exported twice (same request id within a file) collapses to the copy with
  the larger output (the split-entry rule), counted in `stats["duplicate_records"]`. Across files the
  store merges by message id or provider request id (§7.3); OTLP logs and traces exported to separate
  files therefore join in the store, not in the adapter.
* **Memory.** The OTLP adapter joins events, spans and metrics within one file, so it holds that
  file's decoded records in memory (bounded by the file, not the fleet); rotate exporter files.
* **Billing path.** `Attribution.billing_path` mirrors the pricing context's billing path when the
  source implies one (Bedrock, Vertex, OpenAI, Azure …) and stays unset when it is unknown.
* **Timestamps.** Anthropic and Converse response objects carry none; without `request_meta.ts_ms`
  (or `ts`, or for boto3 Converse responses the `ResponseMetadata.HTTPHeaders.date`) such records are
  quarantined (`missing:request_meta.ts_ms`) rather than dated by the ingest clock.
* **Channels and billing paths** are table-driven (`CHANNEL_BY_BILLING_PATH`,
  `BILLING_PATH_BY_CHANNEL`, `SCOPE_PREFIX_BY_CHANNEL`, `TTL_HINT_BY_BILLING_PATH`, provider tables)
  so additive values (e.g. the Copilot addendum, R-E4/R-E15) need data, not code.
