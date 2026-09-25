# Verification: gap-failure-path-spend

Verifier: adversarial fact-check subagent, 2026-09-23.
Report checked: `research/gap-failure-path-spend.md`.

## Method

- **Primary docs, re-downloaded independently.** Anthropic `platform.claude.com/docs/en/*.md` and Claude Code `code.claude.com/docs/en/*.md` pages were fetched fresh with curl into `research/vfp/` and grepped. I did not reuse the author's `fp_docs/` copies.
- **Other sources.** Opened with WebFetch or curl:
  - AWS (Bedrock burndown, prompt caching, error codes)
  - Google (Vertex pricing, Gemini billing, rate limits, troubleshooting)
  - Azure OpenAI FAQ
  - OpenAI (Flex, rate limits, prompt caching, background mode, openai-python README)
  - LiteLLM, Portkey, Cloudflare, Codex config, Datadog blog
- **SDK source.** Read from GitHub with `gh api`:
  - `_base_client.py` and `_constants.py` on main, plus the v1.5.0 and v1.6.0 tags;
  - commit history;
  - the installed SDK versions in other local venvs.
- **OTel.** `gen-ai-spans.md`, the GenAI registry and `http-spans.md` were read with `gh api`.
- **arXiv.** Titles, authors and dates came from the export API. Full HTML text was grepped for Wink, SWE-Effi and MAST.
- **Status feed.** `status.claude.com/api/v2/incidents.json` was re-fetched and the statistics recomputed.
- **Local corpus.** I re-ran `21_failure_paths.py`, `22_nostop_output_gap.py` and `23_error_turns.py`, with outputs in `research/vfp/*.rerun.out`.
  - The corpus is live, so the re-run drifted slightly: 44,999 calls and $11,777, against 44,286 calls and $11,714.
  - All ratios and failure-path dollars reproduced. `refused_detail.py` prices each refused attempt separately.
- **Pricing.** Scenario arithmetic was recomputed from the Anthropic pricing page:
  - Opus 5 and Opus 4.8: $5 input, $6.25 5-minute write, $10 1-hour write, $0.50 cache read, $25 output.
  - Fable 5: $10 input, $12.50 5-minute write, $20 1-hour write, $1 cache read, $50 output.

## Verdicts

| id | verdict |
|---|---|
| fp-billing-matrix | confirmed |
| fp-anth-refusal-iterations | confirmed (with material caveat) |
| fp-fallback-credit | confirmed |
| fp-ttl-from-request-start | corrected |
| fp-sdk-retry-after-uncapped | confirmed |
| fp-cc-retry-semantics | confirmed |
| fp-cc-fallback-chains | confirmed |
| fp-nested-retry-amplification | confirmed |
| fp-incident-duration | confirmed |
| fp-ratelimit-feedback | corrected |
| fp-never-succeeding-400 | corrected |
| fp-cc-missing-final-usage | confirmed |
| fp-local-failure-share | confirmed (with caveat) |
| fp-agent-loop-prevalence | corrected |
| fp-resume-vs-retry | corrected |
| fp-network-aborts | confirmed |
| fp-gateway-abort-metering | corrected |
| fp-provider-ttl-thresholds | confirmed |
| fp-trace-fields-standards | confirmed |
| fp-cross-provider-failover | confirmed |
| fp-recorder-blindspot | confirmed |

## Notes per finding

### fp-billing-matrix: confirmed

- **Vertex / Agent Platform pricing.** The page says: "You're charged only for requests that return a 200 response code. Requests returning any other response codes, such as 4xx and 5xx codes, aren't charged for the input or output."
- **Gemini billing FAQ** (last updated 2026-09-20): "If your request fails with a 400 or 500 error, you won't be charged for the tokens used. However, the request will still count against your quota."
- **Azure OpenAI FAQ** (updated_at 2026-09-11; canonical URL is now `/azure/foundry-classic/openai/faq`). It confirms the 400 content-filter, 408 timeout and 200 `content_filter` charges, and says there is no charge for 401 or 429.
- **OpenAI Flex.** On 429 Resource Unavailable: "You will not be charged when this occurs."
- **Anthropic Batch processing.** Errored, canceled and expired items: "You will not be billed for these requests."
- **Anthropic pricing, web search.** "If an error occurs during web search, the web search will not be billed."
- **Anthropic silence.** I grepped `api/errors`, `streaming`, `rate-limits`, `pricing` and `handling-stop-reasons`, plus the bundled `claude-api` docs. None of them states how client disconnects, mid-stream errors or pre-token 429/529 are billed. The silence claim holds.
- **Caveat on Bedrock.** The "You're only billed for your actual token usage" note is real, but it appears in the quota and burndown context. It is not a failure-billing rule.

### fp-anth-refusal-iterations: confirmed

- **Rules.** `refusals-and-fallback` "Billing and rate limits" states the rules verbatim: pre-output declines are not billed but count against rate limits; attempts with output are billed at their own model's rates; `usage.iterations` is the per-attempt record; top-level `usage` covers the serving attempt only.
- **Informational usage.** `handle-streaming-refusals` says pre-output usage is "informational only".
- **Local re-run.** It reproduces the headline figures: 9 declined `message` entries at $21.05, $9.70 of fallback attempts, and a maximum cache write of 642,494 tokens.
- **Material caveat the finding omits.** The per-attempt pricing (`vfp/refused_detail.py`) shows **$17.88 of the $21.05 (85%) sits in the three attempts with only 4, 6 and 9 output tokens**. Those are the ones that may be free pre-output declines (1-hour writes of 642k and 227k tokens). If they are unbilled, billed refused spend is about $3.17.

### fp-fallback-credit: confirmed

- **Mechanics.** The `fallback-credit` page says caches are per model and the prefix "must be written into the new model's cache from scratch". It also says:
  - "Fallback credit is in beta on the Claude API, Amazon Bedrock, Claude Platform on AWS, Google Cloud, and Microsoft Foundry."
  - "The token expires five minutes after the refusal."
  - The fields that must match exactly are `system`, `messages`, `tools`, `tool_choice`, `thinking` and `cache_control`, among others.
  - The refund shows as `cache_creation` lower and `cache_read` higher by the same amount.
- **Arithmetic.** Opus 4.8: 150k × $6.25/MTok = $0.9375, and 150k × $0.50/MTok = $0.075. The saving is $0.8625, which rounds to $0.86.

### fp-ttl-from-request-start: corrected

**Confirmed:**
- `prompt-caching`: "The lifetime is measured from the start of the request that writes or reads the cache entry … Time spent generating a response counts against the lifetime".
- `prompt-caching`: "a cache entry only becomes available after the first response begins".
- The Python SDK doc gives a 10-minute default timeout, and "requests that time out are retried twice by default".
- Recomputed ratios: success $0.2875; cold 5-minute retry $1.15 (4.0×); cold 1-hour retry $1.72 (5.98×); retries inside the TTL 1.0–1.70×.

**Correction to the "$4.24 worst case" (3 non-streaming timeouts of about 30k output each):**
- The same Python SDK doc says: "The SDK will throw a `ValueError` if a non-streaming request is expected to take longer than approximately 10 minutes." In `_base_client._calculate_nonstreaming_timeout`, `expected = 3600 × max_tokens / 128000`, so any `max_tokens` above about 21,333 raises unless `stream=True` or an explicit timeout is passed.
- So a 30k-output non-streaming request cannot happen with SDK defaults.
- The $4.24 figure also assumes that generation after the client disconnects is billed, which is undocumented.
- The arithmetic is right ($0.8375 + 2 × $1.70 = $4.24), but it is a hypothetical upper bound. It requires overridden SDK settings, or a network drop on streaming, and it is not an outcome of the SDK defaults.

### fp-sdk-retry-after-uncapped: confirmed

- **Current main** (`_base_client.py`, latest commit touching the file 2026-09-19): `if retry_after is not None and retry_after > 0: return min(retry_after, 4_294_967.0)`. The comment reads "just do what it says".
- **Where the change came from.** Commit 2d03ba20 (2026-09-12), "fix(client): honor Retry-After values above 60 seconds".
- **Release history.**
  - The change shipped in the **v1.6.0 release (2026-09-15)** and is in v1.7.0 and v1.8.0.
  - v1.5.0 and the locally installed v1.4.0 (in a different project's venv) still have `0 < retry_after <= 60`.
  - So it is in released versions, not only on main.
- **Constants.** `INITIAL_RETRY_DELAY = 0.5`, `MAX_RETRY_DELAY = 8.0`, and jitter = `1 - 0.25*random()`.
- **Retry conditions.** `_should_retry` obeys `x-should-retry`, then retries 408, 409, 429 and ≥500. Connection errors are retried too.
- **Header.** Every attempt sends `x-stainless-retry-count` (0 on the first attempt).

### fp-cc-retry-semantics: confirmed

- **code.claude.com `errors`:**
  - "retries transient failures up to 10 times";
  - a dropped connection before any part of the response is complete, "including its thinking", is re-issued;
  - after a completed block, Claude Code keeps the output ("could execute the same tool calls twice"). Before v2.1.199 it discarded that output;
  - non-interactive runs continue "up to three times in a row"; subagents are also prompted to continue;
  - on a non-streaming fallback, "Claude Code sends each affected request twice".
- **`env-vars`:**
  - `CLAUDE_CODE_MAX_RETRIES` defaults to 10, capped at 15;
  - the watchdog retries 429/529 **indefinitely**, and other transient errors up to 300 times ("roughly three hours"), backing off up to 5 minutes. The finding's "300" applies only to non-429/529 errors.
- **Local re-run.** It reproduces:
  - 46 events;
  - statuses 529 ×11, 429 ×4, 401 ×8, and none ×23. The 23 are connection codes: StreamSuspended 14, ECONNRESET 4, FailedToOpenSocket 4, ConnectionRefused 1;
  - `maxRetries` = 10 on all 46;
  - 5 of 22 episodes cold, $33.61.
- **Caveat.** The gap between the successes on either side of an error episode has p50 1,154 s, and 4 gaps exceed 1 hour. Some of those cold writes are idle time, not retry-caused.

### fp-cc-fallback-chains: confirmed

- **model-config, availability fallback.** It triggers when the model is "overloaded, unavailable, or returns another non-retryable server error". Rate-limit, auth, billing, size and transport errors "never trigger a switch". "The switch lasts for the current turn only".
- **model-config, content fallback.** Fable 5.1, Fable 5 and Opus 5.5 route bio to Opus 5 and cyber to Opus 4.8. "After a fallback, the session continues on the fallback model."
- **Local.** `04_semantics.out` shows `model_changed` at 11,165,765 missed tokens across 44 events.
- **Caveat.** That total covers every model-change miss, including manual `/model` switches, not only fallbacks.

### fp-nested-retry-amplification: confirmed

- **LiteLLM** routing doc: pins provider SDKs to `max_retries: 0`, because otherwise N retries turn "one request into (1 + N) ** 2 upstream calls".
- **Portkey:** "Up to 5 retry attempts"; a 60-second cumulative wait cap; "Retry attempts aren't logged individually".
- **Cloudflare** (updated 2026-09-14): "a maximum of five retry attempts". Portkey and Cloudflare both say five *retries*, so up to six total attempts; the finding's "5 attempts" is loose.
- **Codex:** `request_max_retries` default 4, `stream_max_retries` default 5, `stream_idle_timeout_ms` 300000. The URL now 308-redirects to learn.chatgpt.com/docs/config-file/config-reference.
- **Gemini SDK:** "up to four times … maximum delay of 60 seconds".
- **OpenAI:** "disable SDK retries or account for them … so nested retry loops don't multiply requests."
- **Arithmetic.** 11 × 4 = 44.

### fp-incident-duration: confirmed

- **Recomputed from the live feed.** It holds 50 incidents (2026-07-22..2026-09-22). 33 carry the "Claude API (api.anthropic.com)" component, spanning 2026-07-24..2026-09-22 (59 days).
- **Durations (created to resolved):**
  - median 1.07 h;
  - p90 3.4 h by nearest rank (3.3–3.9 h depending on the quantile method);
  - max 7.15 h, min 0.25 h;
  - 33 of 33 longer than 5 minutes, 18 longer than 1 hour;
  - sum 50.5 h.
- **Chu et al., arXiv 2501.12469** (2025-01-21). The abstract says ChatGPT failures "take longer to resolve but occur less frequently" than Claude's, with "strong weekly and monthly periodicity".
- **FAILS, arXiv 2503.12185** (2025-03-15) exists as described.
- **Caveat.** These are status-page windows. Most are partial "Elevated errors" (22 of 49), not full outages, so only retry chains that actually span the window go cold.

### fp-ratelimit-feedback: corrected

**Confirmed:**
- **Anthropic rate limits:**
  - `cache_read_input_tokens` "Do NOT count toward ITPM"; `cache_creation_input_tokens` do count;
  - `max_tokens` does not factor into OTPM;
  - the spend-cap 429 "has no `retry-after` header. Retrying, including the SDKs' automatic retries, fails until access resumes."
- **Bedrock burndown:**
  - `input + max_tokens` is deducted at the start;
  - output burndown is 15× for 4.8, 10× for Opus 5.5 / Sonnet 5 / Opus 5 / Fable 5.1, and 5× for ≤4.7;
  - the worked example deducts 36,000 up front against a final 9,000.
- **OpenAI:** "unsuccessful requests contribute to your per-minute limit".

**Corrections:**
1. **Gemini source.** The Gemini rate-limits page (updated 2026-09-02) does not say failed requests use quota; I grepped both the live page and the local copy. The statement is in the Gemini billing FAQ (2026-09-20).
2. **HiveMind attribution** (arXiv 2604.17111, 2026-04-18). The 72–100% → 0–18% failures, and 48–100% less wasted compute, belong to the full five-primitive proxy: admission control, rate-limit tracking, AIMD with circuit breaking, token budgets and priority queues. The ablation says "transparent retry – not admission control – is the single most critical primitive". Crediting "admission control plus transparent retry" misdescribes the system.

### fp-never-succeeding-400: corrected

**Confirmed:**
- The public `api/errors` page documents the binding 400 for new accounts created on or after August 31, 2026, and the `drop_block` fix.
- `context-windows`: "prompt is too long" is a 400 "on every model".
- Spend-cap 429 is terminal (no `retry-after`).
- Claude apps gateway 429: `billing_error` with `x-should-retry: false`.
- Claude Code AUP: the check "evaluates the full conversation … usually re-triggers the same refusal".

**Correction.** The quoted sentence "Retrying the same body never clears it; `count_tokens` returns the same 400" is **not on the public `api/errors` page**. It comes from the bundled `claude-api` skill `shared/error-codes.md` (2.1.280), and the source should be cited as that. The substance is consistent with the public page.

### fp-cc-missing-final-usage: confirmed

**Re-run (live corpus drifted slightly):**
- 4,508 no-stop calls, 10.0%, $510.61;
- 4,347 followed by `tool_result` (96.4%), 27 by an interrupt, 1 in a main thread;
- output p50 3 vs 471 for complete calls; visible characters 410 vs 402;
- v2.1.260 at 35%;
- missing output estimated at $112.39–$245.63, 0.96–2.10% of spend.

**Docs:**
- `handling-stop-reasons`: `stop_reason` is "`null` in the initial `message_start` event … Provided in the `message_delta` event".
- `streaming`: the `message_start` examples show `output_tokens` of 2–3, and `message_delta` usage is "cumulative".

The dollar figure is a model-based estimate, as the report states.

### fp-local-failure-share: confirmed

- **Re-run reproduces the components:**
  - $33.61 cold first success after an error episode;
  - $21.05 refused attempts;
  - $15.40 across 88 interrupts, 87 of 87 with cache writes;
  - $8.32 of loop calls.
- **Totals.** Direct spend is about $78.38, or 0.67% of $11,714. Error-consuming turns cost $283 (2.41%).
- **Tool errors.** 2.2% overall and 8% for MCP tools.
- **Span.** 133.7 days, from `05_analyze.out`.
- **Caveats:**
  - $17.88 of the $21.05 refusal line may be free (see fp-anth-refusal-iterations), so direct spend could be about $60 (0.5%).
  - The 3.1% upper bound adds overlapping categories. The loop calls sit inside the $283.
  - The corpus is one developer.

### fp-agent-loop-prevalence: corrected

**Confirmed from the full texts:**
- **Wink** (Meta Platforms; arXiv 2602.17037, 2026-02-19), Table 1 over "42,807" production trajectories: Loops 5.21%, DNF 15.95%, UC 6.62%, Tool Call Failure 14.02%, total 29.2%.
- **Wink A/B test:** tool-call failure rate 5.29% → 5.07%.
- **MAST** (arXiv 2503.13657): FM-1.3 step repetitions 15.7%.
- **SWE-Effi** (arXiv 2509.09853): 8.8M vs 1.8M tokens, "over 4 times".
- **TraceProbe** (arXiv 2607.06184): "search loops the most stable".
- **Datadog blog** (2026-06-09): "Retry loops occur when an agent repeatedly retries the same failing operation."

**Corrections:**
1. **"Tool-call failures and loops in 14–30%" conflates categories.** 29.2% is *all* misbehaviour, including instruction drift (15.95%) and unrequested changes (6.62%). Tool-call failures are 14.02% and loops 5.21% of trajectories.
2. **"Failed runs cost about 4× more" is one configuration.** It holds for SWE-agent + GPT-4o-mini. The paper shows other ratios elsewhere, for example AutoCodeRover failures take "nearly three times as long".
3. **Wink's token effect is quantified.** "Token Usage per Session decreased by 5.3%". The report's "magnitude not public" (F15) is wrong.

### fp-resume-vs-retry: corrected

**Confirmed:**
- `streaming` Error recovery: ≤4.5 models use an assistant prefill; 4.6+ models use "a user message that instructs the model to continue".
- "Tool use and extended thinking blocks cannot be partially recovered".
- The page never mentions billing.
- OpenAI background mode: the response "continues running", and you resume with `starting_after=<sequence_number>`.
- openai-python README: "Stream consumption is not automatically retried, because replaying a request could duplicate output already delivered".
- The per-token claim holds: ($25 − 1.25 × $5) / $25 = 75%.

**Correction.** The "retry leg −50% ($0.276 → $0.139), episode 1.87× → 1.39×" figures require the resumed call to generate only about 2k output. That means assuming the 5k of thinking is **not** regenerated, which contradicts the finding's own point that thinking cannot be salvaged.
- If the continuation re-thinks (5k) and writes the remaining 1k of text, the retry leg is $0.2385. That is −14%, and the episode is 1.74×.
- The salvaged 2k of text alone is worth only about $0.0375.

### fp-network-aborts: confirmed

- **AWS Bedrock troubleshooting:** "NAT Gateways, interface VPC endpoints, and Network Load Balancers have a fixed idle connection timeout of 350 seconds … dropped without notifying the client".
- **The fix needs two settings:** boto3 `tcp_keepalive=True`, and a kernel `tcp_keepalive_time` below 350 s. The Linux default is 7200.
- **Claude Code `network-config`:**
  - first-byte and byte watchdogs: 180 s direct, 300 s elsewhere;
  - event watchdog: about 5 minutes;
  - body idle timeout: 5 minutes;
  - so that a dead connection "fails and retries".

### fp-gateway-abort-metering: corrected

**Confirmed quotes:**
- "Client aborts are billed too … a floor estimate of about four characters per output token … so aborting requests early doesn't evade a cap."
- The unknown-model tier is $5/$25.
- The spend-cap 429 carries `x-should-retry: false`.
- "a circuit breaker rather than an invoice".

**Correction to the interpretation.** The page describes the gateway's own per-developer spend-cap meter and explicitly says it is not an invoice ("reconcile against your provider's usage reporting"). It is **not** evidence of how the Anthropic API bills aborts. Calling it the "strongest signal that partial output on aborts is meant to be charged" overreaches. It is only a useful template for estimating abort cost.

### fp-provider-ttl-thresholds: confirmed

- **OpenAI prompt caching:**
  - GPT-5.6+ prefixes remain eligible "for 30 minutes after its most recent write or reuse", with `"30m"` the only value and writes at 1.25×;
  - `in_memory` entries last "around 5 to 10 minutes of inactivity, up to one hour";
  - "Organizations without Zero Data Retention enabled default to `24h`".
- **Bedrock:** Claude models support "5 minutes, 1 hour", and the TTL "resets with each successful cache hit".
- **Anthropic:** lifetime runs from request start (see above).

### fp-trace-fields-standards: confirmed

- **semantic-conventions-genai `gen-ai-spans.md`** (latest commit 2026-09-16):
  - `error.type` is Stable, "Conditionally Required If the operation ended in an error";
  - `gen_ai.response.finish_reasons` and `gen_ai.response.time_to_first_chunk` are at Development status;
  - the output-tokens note says "SHOULD report the billed count".
- **No retry or attempt attribute** exists in the spans doc or the GenAI registry. The only "attempt" hits are about memory records.
- **HTTP spans:** `http.request.resend_count` is Stable, "Recommended if and only if request was retried", and updated "regardless of what was the cause".
- **Claude Code OTel:** carries `attempt`, `success`, `stop_reason` and `client_request_id`, with "remains available for failures such as timeouts". That field is first-party only.

### fp-cross-provider-failover: confirmed

ContinuityBench (arXiv 2607.15899, 2026-07-17; Pandey & Singh):
- N = 750 failover events;
- 99.20% continuity preservation for history-forwarding against "near-0%" for stateless failover;
- "asynchronous exponential backoff with jitter to prevent cascading retry storms against strict-limit fallback APIs".

The uncached-prefix cost is correctly labelled as inference.

### fp-recorder-blindspot: confirmed

- **`tokenbill/instrument.py` docstring:** "Failed calls (exceptions) propagate untouched and record nothing."
- **`_RecordingStreamManager.__exit__`** records only when `exc_type is None`, through `get_final_message()`.
- **Raw `create(stream=True)`** has no usage and is skipped with a warning.
- **SDK retries** loop inside `_base_client` (`remaining_retries`), below the patched `messages.create`, so N attempts collapse into at most one record.
- **`api/errors`:** SDKs retry "twice by default".
