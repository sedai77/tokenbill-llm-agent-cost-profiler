### TELEM — OTLP/JSON, OpenAI, Bedrock, Anthropic-response adapters; conventions (wave 2)

**Goal.** Normalize every non-transcript usage source into disjoint buckets without double counting, keep
OpenAI's cache diagnostics as ground truth, and refuse to treat telemetry cost estimates as bills (SPEC §5.2,
§5.9, §5.10, D23, D43, R4).

**Owns.** `tokenbill/adapters/{conventions_ext,otel,openai,bedrock,anthropic_responses}.py`,
`tests/v2/telemetry/**`, `tests/v2/fixtures/telemetry/**`.

**Consumes.** `core.conventions` (`register_convention`, `Convention`, `sum_check`, `anthropic_inferences`),
`core.records`, `core.types`, `core.ids`, `core.jsonl` (`open_text`, zstd guard), `core.models`,
`core.builders`, `core.testing`.

**Provides.** `adapters/conventions_ext.py` registering on import `bedrock.converse`, `openai.responses`,
`openai.chat`, `otel.genai`, `otel.genai.legacy`, `openinference`, `claude_code.otel` (mappings §5.2);
`adapters.otel.OtlpJsonAdapter` (`otlp`), `adapters.openai.OpenAIUsageAdapter` (`openai`),
`adapters.bedrock.BedrockAdapter` (`bedrock`), `adapters.anthropic_responses.AnthropicResponsesAdapter`
(`anthropic-responses`).

**Build.** Per §5.9–§5.10: OTLP file-exporter JSON lines (logs, metrics, traces; `.gz`; `.zst` only on Python ≥
3.14 else a clear `SourceError`), int64 values as strings parsed to int, attribute arrays; Claude Code
`api_request` → requests with fidelity NO_TTL_SPLIT, writes → `cache_write_unknown`, `cost_usd` →
`provider_reported_cost_nano` basis `provider_estimate`, `dq.no_ttl_split`, billing path from `opts`;
identities (`user.email`, `user.id`, `user.account_uuid`) mapped to team via `opts.team_map`, pseudonymized with
`opts.principal_key` (org key; `central-ingest`) and dropped; names with `opts.name_key`; resource attributes →
attribution; lanes from `session.id` + `query_source` (+ `agent_id` from beta spans, which also give
`ttft_ms`); `api_error` → event + failed attempt; `tool_result` → appended item; metrics →
`UsageAggregate(source_kind="otel.metric")` never in the ledger; raw-body events ignored with
`dq.raw_bodies_ignored`; GenAI spans and OpenInference LLM-kind spans only. OpenAI: served tier;
`incomplete_details.reason == "max_output_tokens"` → `stop_reason = "max_tokens"`; `prompt_cache_diagnostics` →
`CacheDiagnostic` with the canonical-reason mapping of §5.10; `azure_openai` channel → `sub:` cache scope.
Bedrock invocation logs (`identity.arn` → team map → `p_`, allowlisted `requestMetadata`, usage from the logged
body, `normalize_model` for Bedrock ids, billing path `bedrock`). Anthropic responses and batch results
(`service_tier` batch; `{request_meta, response}` pairs with `channel`, `endpoint_scope`, `model_raw`,
`billing_path`).

**Facts to verify.** §19.4 OTel/OpenAI/Bedrock field names; checklist §19.8 #8 (incl. OpenAI diagnostics paths).

**Acceptance tests.**
- a golden sum-check fixture per convention: OpenAI Responses `input 10000, cached 6000, cache_write 2000` →
  uncached 2,000, read 6,000, `cache_write_other` 2,000 with TTL 1,800 s; Chat with reasoning subset; OTel
  GenAI new and legacy names; OpenInference trace with one AGENT span wrapping two LLM spans → exactly two
  requests; Bedrock `cacheDetails` split with residual → unknown; a CrewAI-style span (17 input tokens with
  implied 17,119) → `dq.sum_check_failed`; a GenAI span with `read + write > input` → treated exclusive with
  `dq.convention_mismatch`;
- OTLP: 3 `api_request` events + 1 `api_error` + token metrics → 3 requests, 1 failed attempt/API_ERROR event,
  metric aggregates not in `requests`; `intValue` strings parsed; `cost_usd` only as `provider_estimate`;
  `user.email` absent from every output and replaced by a `p_` pseudonym under the principal key; team from the
  team map; raw-body events counted and ignored; beta `llm_request` span sets `ttft_ms` and exact lanes; a
  `.zst` file on Python < 3.14 → `SourceError` naming `compression: none`;
- OpenAI: served `service_tier`; each diagnostics reason maps to its canonical reason and keeps
  `provider_reason`; `cache_missed_tokens` never priced; truncated response → `stop_reason = "max_tokens"`;
  Azure channel → `sub:` scope; Anthropic batch results carry `service_tier="batch"`; a Vertex `request_meta`
  supplies `model_raw` and `endpoint_scope`; Bedrock `global.` id → scope global;
- `assert_adapter_conforms` for all four adapters; CANARY absent; hypothesis fuzz per parser.

**Size.** ~2.6k LOC including tests.
