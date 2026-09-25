# Gap track: failure-path spend (failed, retried, aborted, truncated and refused work)

Track: `gap-failure-path-spend`. Date: 2026-09-23. Author: research subagent for Token Bill.

Scope: what failed, retried, aborted, truncated or refused LLM work actually costs, per provider. Also how retries and fallbacks destroy prompt-cache value, and how often this happens. The deliverables are a billing-semantics matrix, detector specs, new trace fields, and recommended client configurations.

Method:
- **Primary docs.** Downloaded as Markdown or HTML on 2026-09-23 and grepped locally. Raw copies are in `research/fp_docs/`. `WebSearch` was exhausted for this session, so every source below was opened directly by URL.
- **arXiv papers.** Found through the arXiv export API and read from abstract or full HTML.
- **SDK source.** Read on GitHub, plus the installed package.
- **Local corpus.** Three new scripts ran over the local Claude Code corpus (1.2 GB, 43–44k billed calls, about $11.7k at list price):
  - `research/21_failure_paths.py`
  - `research/22_nostop_output_gap.py`
  - `research/23_error_turns.py`

  Their outputs are in the matching `.out` files. The scripts print aggregates only: no prompt text or tool text.

Evidence labels used throughout:
- **[P]** primary statement.
- **[I]** inference from primary statements.
- **[L]** measured in the local corpus.
- **[W]** weak: community evidence, or the docs are silent.

---

## 0. Executive summary

1. **The docs are explicit about failure billing only in a few places, and those places matter.**
   - **Google Vertex:** "You're charged only for requests that return a 200 response code."
   - **Gemini API:** 400/500 responses are not charged but still count against quota.
   - **Azure OpenAI:** you are charged whenever "the service performs processing", including 400 content-filter errors and 408 timeouts.
   - **OpenAI Flex:** 429 "Resource Unavailable" is not charged.
   - **Anthropic, refusals:** a decline before any output is not billed but counts against rate limits. A mid-stream decline bills input plus the output already streamed. Per-attempt billing is in `usage.iterations`.
   - **Anthropic, other:** Message Batches items that error, are cancelled or expire are not billed.
   - **Anthropic docs are silent** on client disconnects, mid-stream `overloaded_error`, and pre-token 429/529 (though an error body carries no `usage`).
   - **Anthropic's own Claude apps gateway** meters client aborts at a floor of about 4 characters per output token "so aborting requests early doesn't evade a cap".
2. **The expensive part of a failure is usually the lost cache, not the failed attempt.**
   - Anthropic measures cache lifetime "from the start of the request", and generation time counts against it.
   - A cache entry "only becomes available after the first response begins".
   - So any retry that starts more than 5 minutes (or 1 hour) after the original request start re-writes the whole prefix.
   - Example: a 150k-token Opus 5 prefix. A cold retry costs **4.0×** the successful call with a 5-minute write, or **6.0×** with a 1-hour write. A retry inside the TTL costs 1.0–1.7×.
3. **Retry machinery now routinely produces waits longer than the TTL.**
   - The Anthropic Python SDK now honours any positive `retry-after` with no 60-second cap (commit 2026-09-19). Its default timeout is 10 minutes with 2 retries, so a timed-out non-streaming request is retried after the 5-minute TTL has already lapsed.
   - Claude Code's `CLAUDE_CODE_RETRY_WATCHDOG` backs off "up to 5 minutes between attempts", for up to 300 attempts (about 3 hours).
   - Anthropic's status feed shows **33 Claude API incidents in 59 days**. All lasted longer than 5 minutes, 18 lasted longer than 1 hour, and the median was 1.07 hours.
4. **Nested retries multiply attempts.**
   - LiteLLM documents that double-applied retries turn one request into (1+N)² upstream calls.
   - Claude Code (10 retries) behind a gateway that also retries (N=3) can make about 44 upstream attempts per logical request.
5. **Local corpus result: a correction to an earlier claim.** The earlier empirical report said 10.4% of Claude Code responses were "interrupted" because they had no `stop_reason`. That reading is wrong.
   - 96.4% of those 4,499 calls are followed by a `tool_result`, so the turn completed.
   - Their recorded `output_tokens` (p50 = 3) is the `message_start` placeholder. The final `message_delta` usage was never written to the transcript.
   - The artefact is concentrated in subagent and workflow sidechains and in some CC versions (v2.1.260: 35%).
   - Consequence: output spend is **under-counted** by about $112–$246, or **1.0–2.1% of the corpus**. Token Bill must reconstruct final usage for these calls, not label them as aborts.
6. **Measured failure-path spend in this corpus is small.** It is about 0.7% directly attributable, with an upper bound of about 3.1%.
   - Cold first success after an API-error episode: $33.61 excess.
   - Refused-then-fallback attempts: $21.05. These are reported in `iterations`, but may not be billed if pre-output.
   - Calls interrupted by the user: $15.40.
   - Loops of three or more consecutive tool errors: $8.32.
   - Upper bound: turns that consume an errored tool result, $283 (2.4%).
   - The reason it is small here: the main thread uses the 1-hour TTL and there is no gateway, Bedrock NAT or nested retry.
   - Enterprise fleets have all of those risk factors, and production agent studies report that 14–30% of trajectories contain tool-call failures or loops (Meta's Wink study, 42.8k production trajectories).
7. **What Token Bill should build.** Treat every logical request as a chain of attempts:
   - price each attempt with a provider-specific billing rule;
   - flag six failure-path detectors: retry-storm, cold-retry-after-backoff, abort-waste, never-succeeding-400, fallback-cache-loss, tool-error-loop;
   - add an output-undercount correctness check;
   - emit config recommendations: one retry owner, backoff capped below TTL minus generation time, resume from partial, and fallback credit.

---

## 1. Billing-semantics matrix (failure mode × provider)

Legend:
- **In** = billed input (incl. cache read/write).
- **Out** = billed partial output.
- **Cache** = is a cache entry written, reusable by a retry.
- ✔ = yes, ✘ = no, ? = undocumented.
- Tags: [P] primary, [I] inferred, [W] weak or community.
- **RL** = counts against rate limits or quota.

| Failure mode | Anthropic 1P / Claude Platform on AWS | Bedrock (Claude) | Vertex / Google Cloud Agent Platform (Claude) | Foundry (Claude) | OpenAI | Gemini API | Azure OpenAI (for Foundry context) |
|---|---|---|---|---|---|---|---|
| **429 / 529 / 5xx before first token** | In ✘ [I: error body has no `usage`] · Out ✘ · Cache ✘ [P: "cache entry only becomes available after the first response begins"] · CC: a 529 "doesn't count against your quota" (plan quota) [P] | In ✘ [I: "You're only billed for your actual token usage"] · quota: `input + max_tokens` deducted at request start, then trued up [P] | In ✘ Out ✘ [P: "Requests returning any other response codes, such as 4xx and 5xx codes, aren't charged"] | ? [W] | Flex 429: ✘ [P "You will not be charged"]. Other 429/5xx: ? · RL ✔ [P "unsuccessful requests contribute to your per-minute limit"] | In ✘ Out ✘ [P: 400/500 "won't be charged"] · RL ✔ [P] | 429 ✘ [P: "If the service doesn't perform processing, you won't be charged … 429"] |
| **Error event mid-stream (e.g. `overloaded_error` SSE after 200)** | In ? Out ? — docs silent. By analogy with refusals (mid-stream = input + streamed output billed) assume In ✔ Out ✔ (partial) [I/W]. Cache ✔ (response began) [I] | ? [W] | ? (HTTP status was 200) [W] | ? | ? [W] | ? [W] | ✔ if processing happened [P general rule] |
| **Client disconnect / cancel / timeout** | ? on the API [W]. Anthropic's Claude apps gateway **meters** aborts at ~4 chars/output token [P, gateway meter, not invoice]. Cache ✔ once the response began [I]. SDK: non-streaming timeout ⇒ "client terminating the connection and retrying" (2 retries) [P] | ? [W]. NAT/VPCE/NLB 350 s idle timeout silently drops long streams [P] | ? [W] | ? | Background mode: the response "continues running" after disconnect and can be resumed from `sequence_number`; cancel endpoint exists [P]. Billing of cancelled work: ? | ? | 408 timeout **charged** [P] |
| **User interrupt (Esc in Claude Code)** | Same as client abort. Local: 87/87 interrupted calls had already recorded cache writes (1h TTL) [L] | same | same | same | n/a | n/a | n/a |
| **400 that can never succeed** (binding 400, prompt too long, invalid param) | In ✘ [I] · repeated sends waste latency and RL. Binding 400: "Retrying the same body never clears it" [P, bundled docs]. `count_tokens` returns the same 400 for free [P] | In ✘ [I] | ✘ [P] | ? | ? | ✘ [P] | **400 content-filter: charged** [P] |
| **`max_tokens` truncation** | ✔ full input + output (HTTP 200) [P]. `max_tokens` does not factor into OTPM [P] | ✔; `max_tokens` reserved against TPM at request start; output burndown 5× (≤4.7), 10× (Opus 5/5.5, Sonnet 5, Fable 5.1), 15× (4.8) [P] | ✔ [P, 200] | ✔ | ✔ | ✔ | ✔ |
| **`model_context_window_exceeded`** (input + `max_tokens` > window, 4.5+) | ✔ billed as a normal truncated response [P] | ✔ | ✔ | ✔ | n/a | n/a | n/a |
| **Input alone exceeds context** | 400 "prompt is too long" → In ✘ [I]. CC compacts (billed) or retries with smaller `max_tokens` [P] | "Input is too long for requested model" (CC treats it the same) [P] | ✘ [P] | ? | ? | ✘ [P] | ? |
| **Refusal, pre-output** | **Not billed**; usage informational; **counts against rate limits** [P] | Fallback credit available; pre-output billing not stated [P/W] | same as Bedrock | same | ? | safety block ? | prompt-filter 400 **charged** [P] |
| **Refusal, mid-stream** | Input + streamed output billed [P] | ? | ? | ? | ? | ? | completion filtered (200, `content_filter`) **charged** [P] |
| **Refusal + server-side fallback** | Declined-before-output attempts reported in `usage.iterations`, not charged. Each attempt that produced output billed at its own model's rates. `iterations` is the per-attempt record; top-level `usage` = serving attempt only [P]. Server-side fallback only on 1P and Claude Platform on AWS [P] | Client middleware + fallback credit (beta) [P] | same | same | n/a | n/a | n/a |
| **Refusal + manual retry on another model** | Cold write on the new model (caches are per-model) unless `fallback_credit_token` is redeemed within 5 min with an exactly matching body [P] | credit ✔ [P] | credit ✔ [P] | credit ✔ [P] | n/a | n/a | n/a |
| **Batch item errored / cancelled / expired** | **Not billed** [P] | — | — | — | — | — | — |
| **Server-tool error** (web search) | "If an error occurs during web search, the web search will not be billed" [P] | | | | | | |
| **Tool `is_error` result loops** | Every turn billed normally; each repeat re-reads the whole prefix and appends the error text [P mechanics / L] | same | same | same | same | same | same |

Main gaps in the documentation:
- Anthropic 1P never says in public docs whether client disconnects or mid-stream `overloaded_error` bill partial output.
- The closest primary signals:
  - the refusal rule, which bills partial output mid-stream;
  - the Claude apps gateway, which meters aborts;
  - `message_start` already carrying input and cache usage.
- Token Bill should therefore:
  - default to "billed: input + tokens streamed" for mid-stream failures and aborts on Anthropic;
  - label that estimate `billing_rule=anthropic_abort_assumed`;
  - reconcile against the Usage & Cost API or invoice.

---

## 2. Findings

### F1. Anthropic refusal billing is precise and per attempt, so failed attempts must be priced from `usage.iterations` (`fp-anth-refusal-iterations`)
- **Pre-output decline.** "You are not billed for a refusal that arrives before any output. `content` is empty, and token counts appear in `usage` but are not charged. The request still counts against your rate limits."
- **Mid-stream decline.** It "bills the input tokens and the output already streamed at normal rates."
- **Server-side fallback.** "`usage.iterations` array is the per-attempt record of what you're billed. The top-level `usage` counts describe only the attempt that produced the returned message."
- **Fallback exposure.** If the fallback model is rate-limited or overloaded, "the fallback attempt is not made and the preceding refusal is returned." Fallback triggers only on classifier declines, never on 429, 529 or 5xx.
- **Local [L].** The 9 refused Fable 5 attempts in the corpus show:
  - output of 4–2,127 tokens and cache writes up to 642k tokens per attempt;
  - $21.05 if priced as billed, plus $9.70 for the fallback attempts.
- **Ambiguity.** Attempts with 4–9 output tokens may be "pre-output" declines that are reported but not charged. Token Bill cannot tell from the transcript, so it should show them as a range.
- **Sources:**
  - https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/handle-streaming-refusals (accessed 2026-09-23)
  - bundled `shared/model-migration.md` (claude-api skill 2.1.280)
  - local `22_nostop_output_gap.out`
- **Evidence:** strong.
- **Token Bill:**
  - Price every `iterations[]` entry at its own model.
  - Zero-price entries of type `message` that have `output_tokens == 0`, or that come with an empty `content` and a refusal.
  - Flag ambiguous entries with small non-zero output.
  - Report "refusal RL burn": declined attempts that consumed rate limit but no dollars.
- **Savings:** accuracy fix, <1% of spend. Prevents both over- and under-count.
- **Effort:** S.

### F2. Fallback credit removes the cold cache rewrite on refusal retries; manual retries without it pay 4× (`fp-fallback-credit`)
- Caches are per model. "Prompt caches are per-model… the conversation prefix already cached for the first model must be written into the new model's cache from scratch."
- **Fallback credit** (beta; available on the Claude API, Claude Platform on AWS, Bedrock, Google Cloud and Foundry). With it, the retry "is billed as though the conversation had been on the new model all along".
- **Redemption rules:**
  - the token expires 5 minutes after the refusal;
  - the retry body must match exactly on `system`, `messages`, `tools`, `tool_choice`, `thinking`, `cache_control` and more;
  - the refund shows up as lower `cache_creation_input_tokens` and equally higher `cache_read_input_tokens`;
  - a tokenless retry after server tools ran "re-runs and re-bills those tools".
- **Server-side fallback** (1P and Claude Platform on AWS only) and the SDK middleware apply credit automatically.
- **Quantified [I]:** 150k-token prefix, Opus 4.8 fallback: cold 5-minute write $0.9375 against credited read $0.075, a **$0.86 saving per fallback**. The whole call is $1.15 against $0.29.
- **Sources:**
  - https://platform.claude.com/docs/en/build-with-claude/fallback-credit (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback (accessed 2026-09-23)
- **Evidence:** strong.
- **Token Bill:**
  - **fallback-cache-loss detector:** a model switch where the first call on the new model has `cache_creation` of at least 80% of the prefix, and no credit shift is visible.
  - Recommend server-side `fallbacks: "default"` on 1P, and `BetaRefusalFallbackMiddleware` on Bedrock, Vertex and Foundry.
  - Measure "credit redemption rate" as a hygiene KPI.
- **Savings:** up to about 75% of each refusal-retry call; fleet impact scales with refusal rate (cyber and bio-adjacent teams).
- **Effort:** S.

### F3. Cache lifetime runs from request start, and entries appear only after the response begins. That makes every long backoff and every timeout retry a cold rewrite (`fp-ttl-from-request-start`)
- **Primary statements [P]:**
  - "The lifetime is measured from the start of the request that writes or reads the cache entry, not from the end of its response. Time spent generating a response counts against the lifetime."
  - "A cache entry only becomes available after the first response begins."
  - Cache reads "refresh" the entry at no extra cost.
- **Implications [I]:**
  - A failure **before the first token** writes no cache, so the retry pays the write.
  - A failure **after the response begins** leaves a warm entry, so a quick retry reads it.
  - A retry that starts later than the TTL after the failed attempt's **start** is cold. With a 5-minute TTL, a 4-minute generation that fails leaves about 1 minute to retry warm.
- **SDK default [P]:** timeout 10 minutes, `max_retries=2`, and "requests that time out are retried twice by default". An over-long non-streaming request makes "the client terminating the connection and retrying without receiving a response".
  - So every SDK timeout retry of a 5-minute-TTL prefix is cold by construction.
- **Worst case [I, upper bound]:** Opus 5, 150k prefix, about 30k output per 10-minute attempt, if server-side generation after disconnect is billed. Three attempts cost **$4.24 with nothing delivered**, against $0.84 for one streamed success.
- **Scenario table [I]:** Opus 5 list prices. Prefix 150k cached, 2k new input, 8k output of which 5k is thinking.

| Scenario | Cost | × success |
|---|---|---|
| Success | $0.2875 | 1.00× |
| Pre-token 529, retry within TTL | $0.2875 | 1.00× (latency only) |
| Pre-token 529, retry after 5m TTL (5m write) | $1.15 | **4.0×** |
| Same, 1h-TTL write (Claude Code main thread) | $1.72 | **6.0×** |
| Mid-stream failure after thinking, naive retry within TTL | $0.4885 | 1.70× |
| Failure after 5k thinking + 2k text; naive retry | $0.5385 | 1.87× |
| Same; resume-from-partial (text salvaged) | $0.401 | 1.39× |

- **Sources:**
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23)
- **Evidence:** strong (mechanics); scenario numbers are computed.
- **Token Bill:**
  - **cold-retry-after-backoff detector.** Logic:
    - consecutive attempts share a prefix hash;
    - `retry_start − first_attempt_start > TTL`, where TTL is the requested TTL for that breakpoint;
    - the retry shows `cache_creation ≥ 50%` of the prefix.
  - Dollars = prefix × (write_rate − read_rate).
  - Also flag **non-streaming requests with high `max_tokens`**, which are timeout-retry risks.
- **Savings:** 3–5× on each affected retry. Fleet share depends on incident frequency (F10).
- **Effort:** M.

### F4. The Anthropic SDK now honours any `retry-after`, however long, and tags every attempt with `x-stainless-retry-count` (`fp-sdk-retry-behavior`)
- **Current `main`** (`_base_client.py`, last commit 2026-09-19):
  - "If the API asks us to wait a certain amount of time, just do what it says". It uses any `retry_after > 0` up to about 49.7 days.
  - Earlier releases capped this at 60 seconds. The installed v1.4.0 checks `0 < retry_after <= 60`.
- **Default backoff:** 0.5 s × 2ⁿ, capped at 8 s, with jitter.
- **Retry conditions:** retries connection errors, 408, 409, 429 and ≥500. Obeys `x-should-retry: true|false`.
- **Retry header:** every attempt carries `x-stainless-retry-count: <n>`.
- **Implications:**
  - A 429 with a long `retry-after` (for example, acceleration limits) now makes the SDK sleep past a 5-minute TTL silently, then pay a cold write.
  - A proxy can read `x-stainless-retry-count` to record attempt numbers that are otherwise invisible. Token Bill's current recorder wraps `messages.create`, so SDK-internal retries collapse into one call.
- **Sources:**
  - https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_base_client.py (commit 2026-09-19)
  - https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_constants.py
  - https://platform.claude.com/docs/en/api/errors (accessed 2026-09-23): "retry … twice by default, honoring the `retry-after` header when present"
- **Evidence:** strong (source code).
- **Token Bill:**
  - The recording proxy should log `x-stainless-retry-count`, `retry-after(-ms)` and `x-should-retry`.
  - The SDK recorder should hook the HTTP transport (httpx2 event hooks) so each attempt is its own record.
  - Recommend `max_retries` plus a wrapper that refuses any `retry-after` longer than the remaining TTL, and defers instead.
- **Savings:** prevents cold rewrites (F3).
- **Effort:** S–M.

### F5. Claude Code's retry semantics, verbatim: up to 10 retries (300 under the watchdog), full re-send before any completed block, partial kept after one (`fp-cc-retry-semantics`)
- **Retry budget:**
  - "Claude Code retries transient failures up to 10 times with exponential backoff." `CLAUDE_CODE_MAX_RETRIES` defaults to 10, capped at 15.
  - `CLAUDE_CODE_RETRY_WATCHDOG=1` retries 429 and 529 indefinitely and raises the budget for other transient errors to 300, "roughly three hours of backoff". It "backs off up to 5 minutes between attempts".
- **Dropped connections:**
  - Before any block is complete, "including its thinking", Claude Code **re-issues the request**. All thinking produced so far is regenerated.
  - After thinking but before text, it re-issues up to 2 times.
- **Mid-stream failures after a completed text or tool block:** Claude Code does not re-run, "because that could execute the same tool calls twice". It keeps the partial output (v2.1.199+).
- **Continuations:**
  - Non-interactive `-p` sessions and subagents auto-continue text-only cut-offs up to 3 times.
  - Stalled streams are re-issued once, outside the budget.
- **Context-limit rejects** ("input plus `max_tokens` exceeds the context limit") are retried with a reduced `max_tokens`. Before v2.1.218, this could loop until the budget ran out.
- **Streaming ended before any complete data:** Claude Code "re-sent the request without streaming… Claude Code sends each affected request twice". This is a gateway or proxy misconfiguration that **doubles input spend**.
- **Watchdogs:**
  - first-byte deadline: 180 s direct, 300 s elsewhere, plus 1 s per 32 KB of body;
  - byte watchdog: 180 s direct, 300 s elsewhere;
  - event watchdog: 300 s;
  - body idle timeout: 5 minutes on non-1P providers.
- **Telemetry:**
  - OTel `api_request` carries `attempt`, `success`, `stop_reason`, `request_id` and `client_request_id`.
  - `api_error` carries `status_code`, `attempt`, `retry`, `duration_ms` and `client_request_id`.
  - Transcripts carry `system.api_error{error.status, retryAttempt, maxRetries, retryInMs}`.
- **Local [L]:**
  - 46 `api_error` events in 13 main threads.
  - Statuses: 529 ×11, 429 ×4, 401 ×8, none ×23. The status-less events had connection codes: `StreamSuspended` ×14, `ECONNRESET` ×4, `FailedToOpenSocket` ×4.
  - `retryAttempt` reached 10. `maxRetries` = 10 on all 46.
  - `retryInMs`: p50 613 ms, max 36.6 s.
  - Error → next billed call: p50 62 s; 15 of 46 took more than 300 s and 10 more than 3,600 s. The long gaps include human or quota waits.
  - 5 of 22 episodes' first success was cold, $33.61 excess.
  - There were also 92 unbilled synthetic error turns, 55 of them 429 quota rejections.
- **Sources:**
  - https://code.claude.com/docs/en/errors (accessed 2026-09-23)
  - https://code.claude.com/docs/en/env-vars (accessed 2026-09-23)
  - https://code.claude.com/docs/en/network-config (accessed 2026-09-23)
  - https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23)
  - local `21_failure_paths.out`, `22_nostop_output_gap.out`
- **Evidence:** strong.
- **Token Bill:**
  - Ingest `system.api_error` and OTel `api_error` / `api_request.attempt` into attempt chains.
  - Flag:
    - `CLAUDE_CODE_RETRY_WATCHDOG` with 5-minute-TTL subagents, a guaranteed cold rewrite per long backoff;
    - "Retrying without streaming" double-sends;
    - repeated re-issues that regenerate thinking.
- **Savings:** prevents the 4–6× cold-retry multiplier; removes 2× double-sends.
- **Effort:** M.

### F6. Claude Code fallback chains switch models for one turn only, which can ping-pong caches; content fallbacks are sticky (`fp-cc-fallback-chains`)
- **Availability fallback** (`--fallback-model`, `fallbackModel`):
  - triggers when the primary is "overloaded, unavailable, or returns another non-retryable server error";
  - rate-limit, auth, billing, size and transport errors "never trigger a switch";
  - "The switch lasts for the current turn only, so your next message tries the primary model first again."
- **Content fallback** (classifier flags):
  - Fable and Opus 5.5 → Opus 4.8 (cyber) or Opus 5 (bio);
  - "After a fallback, the session continues on the fallback model."
- **Cost model [I].** Each switch A→B writes B's cache from scratch (per-model caches), unless fallback credit applies (content fallbacks only). A→B→A within the TTL returns to a still-warm A. After a long overload episode, both are cold.
- **Local [L]:** 9 `model_refusal_fallback` events (Fable 5 → Opus 4.8). `diagnostics.cache_miss_reason = model_changed` accounted for 11.2M missed tokens.
- **Sources:**
  - https://code.claude.com/docs/en/model-config (accessed 2026-09-23)
  - https://code.claude.com/docs/en/env-vars (accessed 2026-09-23)
  - local `04_semantics.out`
- **Evidence:** strong.
- **Token Bill:**
  - **fallback-cache-loss** detector (F2), including A→B→A ping-pong within one TTL.
  - Report $ per fallback trigger class (overload vs refusal).
  - Recommend same-family fallbacks and 1-hour TTL on long sessions.
- **Savings:** (write − read) × prefix per switch.
- **Effort:** S.

### F7. Other providers state failure billing explicitly, and the rules differ enough that a multi-provider fleet needs rule-based pricing (`fp-provider-failure-rules`)
- **Google Vertex / Agent Platform:** "You're charged only for requests that return a 200 response code. Requests returning any other response codes, such as 4xx and 5xx codes, aren't charged for the input or output."
- **Gemini API** (billing FAQ, updated 2026-09-20): "If your request fails with a 400 or 500 error, you won't be charged for the tokens used. However, the request will still count against your quota."
- **Azure OpenAI** (FAQ, updated 2026-09-11):
  - "If the service performs processing, you'll be charged even if the status code isn't successful (not 200). Common examples … a 400 error due to a content filter, or a 408 error due to a time-out."
  - A 200 with `finish_reason: content_filter` is also charged.
  - A 401 or 429 is not charged.
- **OpenAI Flex:** 429 Resource Unavailable, "You will not be charged when this occurs".
- **OpenAI rate limits:** "unsuccessful requests contribute to your per-minute limit". Also: "disable SDK retries or account for them … so nested retry loops don't multiply requests."
- **Bedrock:** "You're only billed for your actual token usage". Quota deducts `input + max_tokens` at request start, then trues up.
- **Anthropic Batches:** errored, cancelled or expired items: "You will not be billed". Web search errors are not billed.
- **Sources:**
  - https://cloud.google.com/vertex-ai/generative-ai/pricing (accessed 2026-09-23)
  - https://ai.google.dev/gemini-api/docs/billing (updated 2026-09-20)
  - https://learn.microsoft.com/en-us/azure/ai-foundry/openai/faq (updated 2026-09-11)
  - https://developers.openai.com/api/docs/guides/flex-processing (accessed 2026-09-23)
  - https://developers.openai.com/api/docs/guides/rate-limits (accessed 2026-09-23)
  - https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/build-with-claude/batch-processing (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23)
- **Evidence:** strong (for the stated rules).
- **Token Bill:**
  - Ship a versioned `billing_rules.yaml` keyed by (provider, failure_mode) with a confidence tag. The matrix in §1 is the seed.
  - Price failed attempts from it rather than assuming "every attempt billed" or "no attempt billed".
  - Show "assumed" rules separately in the report.
  - Azure fleets: surface content-filter 400s and 408 timeouts as **billed** waste.
- **Savings:** accuracy. Azure content-filter and timeout waste is directly recoverable.
- **Effort:** M.

### F8. Resume-from-partial pays off only for salvaged text; the bigger lever is staying inside the TTL (`fp-resume-vs-retry`)
- **Anthropic streaming "Error recovery":**
  - For ≤4.5 models, prefill the partial as an assistant message.
  - For 4.6+, "add a user message that instructs the model to continue".
  - "Tool use and extended thinking blocks cannot be partially recovered. You can resume streaming from the most recent text block."
  - The docs say nothing about billing.
- **Cost model [I]:** the salvaged k text tokens re-enter as input (5-minute write ≈ 1.25× input) instead of being regenerated as output.
  - Saving = k × (out − 1.25·in) = k × $18.75/MTok on Opus 5, about 75% on the salvaged span.
  - In the §F3 scenario (5k thinking + 2k text before failure), the retry leg drops from $0.276 to $0.139 (−50%), and the episode from 1.87× to 1.39× of success.
  - Where thinking dominates (thinking was 50.5% of output where reported locally), nothing can be salvaged.
- **Claude Code already does this:** it keeps completed blocks and auto-continues in non-interactive runs and subagents (F5).
- **OpenAI equivalent:** background mode plus `stream: true`. The response "continues running" after a dropped connection, and you resume with `starting_after=<sequence_number>`, so nothing is regenerated.
- **OpenAI SDK caution:** "Stream consumption is not automatically retried, because replaying a request could duplicate output."
- **Sources:**
  - https://platform.claude.com/docs/en/build-with-claude/streaming (accessed 2026-09-23)
  - https://developers.openai.com/api/docs/guides/background (accessed 2026-09-23)
  - https://github.com/openai/openai-python (README, accessed 2026-09-23)
- **Evidence:** moderate (mechanics primary; savings computed).
- **Token Bill:**
  - **abort-waste detector** computes a "salvageable text tokens" column.
  - Recommend resume where salvageable text > 1k tokens.
  - For OpenAI long jobs, recommend background mode.
- **Savings:** 30–50% of the retry leg when text is salvageable; ~0 when thinking dominates.
- **Effort:** S (recommendation), M (simulation).

### F9. Nested retries multiply attempts: (1+N)² at one layer, and more across agent → gateway → SDK (`fp-nested-retry-amplification`)
- **LiteLLM.** The Router pins provider SDK `max_retries: 0` because otherwise a deployment `num_retries: N` is "applied twice and turning one request into (1 + N) ** 2 upstream calls". It also has:
  - `allowed_fails` and `cooldown_time` circuit breaking;
  - `RetryPolicy` per error type;
  - `context_window_fallbacks` and `content_policy_fallbacks`.
- **Portkey:**
  - up to 5 attempts;
  - "cumulative retry wait time for a single request is capped at 60 seconds";
  - a `retry-after` over 60 s fails immediately;
  - "Retry attempts aren't logged individually", which is an observability gap Token Bill can fill;
  - fallbacks trigger on any non-2xx by default.
- **Cloudflare AI Gateway:** up to 5 retries (`cf-aig-max-attempts`, backoff constant, linear or exponential). Dynamic Routing switches model on Rate Limit or Budget Limit nodes.
- **Coding agents:**
  - Claude Code: 10 retries (F5).
  - Codex: `request_max_retries` default 4, `stream_max_retries` default 5, `stream_idle_timeout_ms` 300000.
  - Gemini Python SDK: "up to four times … maximum delay of 60 seconds".
- **Example [I]:** Claude Code (1+10) behind LiteLLM with `num_retries=3` (1+3) makes up to 44 upstream attempts per logical request. Each 529 before the first token is free on most providers, but each mid-stream failure or timeout (billed on Azure; probably on Anthropic) is paid again.
- **Sources:**
  - https://docs.litellm.ai/docs/routing (accessed 2026-09-23)
  - https://docs.litellm.ai/docs/proxy/reliability (accessed 2026-09-23)
  - https://portkey.ai/docs/product/ai-gateway/automatic-retries (accessed 2026-09-23)
  - https://portkey.ai/docs/product/ai-gateway/fallbacks (accessed 2026-09-23)
  - https://developers.cloudflare.com/ai-gateway/configuration/request-handling/ (updated 2026-09-14)
  - https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/ (updated 2026-08-07)
  - https://developers.openai.com/codex/config-reference (accessed 2026-09-23)
  - https://ai.google.dev/gemini-api/docs/troubleshooting (updated 2026-09-20)
  - https://developers.openai.com/api/docs/guides/rate-limits (accessed 2026-09-23)
- **Evidence:** strong (documented defaults); amplification is computed.
- **Token Bill:**
  - **retry-storm detector:** more than R attempts per logical request, or attempts across two or more layers (agent `attempt` > 1 and gateway `x-portkey-retry-attempt-count`, or LiteLLM retries, or `x-stainless-retry-count` > 0).
  - Emit a "single retry owner" config recommendation per stack.
- **Savings:** removes duplicate billed attempts; reduces 429 feedback (F11).
- **Effort:** M.

### F10. Provider incidents routinely outlast every TTL, so retry-through-incident is a structural cold-cache generator (`fp-incident-duration`)
- **Anthropic status feed.** 50 incidents from 2026-07-24 to 2026-09-22. 33 touched "Claude API (api.anthropic.com)":
  - "Elevated errors" was the most common title (23 of 50 overall);
  - API incident duration: p50 **1.07 h**, p90 3.4 h, max 7.15 h;
  - **33/33 lasted longer than 5 minutes; 18/33 longer than 1 hour**;
  - about 50.5 API incident-hours in 59 days, or 3.6% of wall-clock time with some API incident open.
- **OpenAI status feed** (25 recent incidents): p50 0.91 h, p90 9.6 h.
- **Academic characterisation** (Chu et al.): Claude failures "occur more frequently" but resolve faster than ChatGPT's, with "strong weekly and monthly periodicity". The FAILS framework offers MTTR and MTBF tooling.
- **Sources:**
  - https://status.claude.com/api/v2/incidents.json (accessed 2026-09-23)
  - https://status.openai.com/api/v2/incidents.json (accessed 2026-09-23)
  - arXiv 2501.12469 (2025-01-21), https://arxiv.org/abs/2501.12469
  - arXiv 2503.12185 (2025-03-15), https://arxiv.org/abs/2503.12185
- **Evidence:** strong (feed data); moderate for fleet impact.
- **Token Bill:**
  - Join the incident feeds with the fleet's attempt chains to attribute "incident tax": cold rewrites and retries during incident windows.
  - Recommend 1-hour TTL, or deferral instead of retry, during declared incidents.
  - Alert when a fleet's own error rate spikes without a posted incident (a local network or proxy issue).
- **Savings:** unknown until measured; per event, see F3 (4–6×).
- **Effort:** M.

### F11. Failures feed back into rate limits: cache misses consume ITPM, failed requests consume quota, and Bedrock reserves `max_tokens` up front (`fp-ratelimit-feedback`)
- **Anthropic:**
  - `cache_read_input_tokens` "Do NOT count toward ITPM" for most models, but cache writes and uncached input do. A cold retry therefore burns ITPM that a warm one wouldn't, raising the chance of the next 429.
  - OTPM counts only actual output; "`max_tokens` does not factor".
  - A tier **spend-cap 429** "has no `retry-after` header. Retrying, including the SDKs' automatic retries, fails until access resumes."
- **Bedrock:**
  - deducts `input + max_tokens` at the **start**;
  - output burns down at 5× for ≤4.7, 10× for Opus 5 / 5.5 / Sonnet 5 / Fable 5.1, 15× for 4.8;
  - `CacheReadInputTokenCount` is not counted;
  - AWS's example: `max_tokens` 32,000 reserves 36,000 against a final 9,000. Oversized `max_tokens` creates throttling on its own.
- **Gemini:** failed requests "count against your quota". Spend-based rate limits run on a rolling 10-minute window.
- **HiveMind:** uncoordinated parallel agents under contention fail at 72–100%. A proxy with admission control, AIMD, circuit breaking and transparent retry cut failures to 0–18% and eliminated 48–100% of wasted compute. Motivating case: 3 of 11 agents died, wasting 135k tokens. [moderate: small-team preprint]
- **Sources:**
  - https://platform.claude.com/docs/en/api/rate-limits (accessed 2026-09-23)
  - https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html (accessed 2026-09-23)
  - https://ai.google.dev/gemini-api/docs/rate-limits (updated 2026-09-02)
  - arXiv 2604.17111 (2026-04-18), https://arxiv.org/abs/2604.17111
- **Evidence:** strong for the mechanics; moderate for the HiveMind magnitudes.
- **Token Bill:**
  - Report "rate-limit headroom lost to failures".
  - Bedrock `max_tokens` right-sizing: p99 observed output against the configured `max_tokens`.
  - Classify spend-cap 429s as terminal (never-succeeding).
  - Recommend a shared admission controller and retry coordinator for parallel agent fleets.
- **Savings:** indirect (fewer 429s → fewer cold retries); HiveMind reports 48–100% less wasted compute under contention.
- **Effort:** M.

### F12. Some 400s can never succeed, and blind retries or resends only burn latency, rate limit and sometimes compaction spend (`fp-never-succeeding-400`)
- **Preserved-thinking binding 400** (Fable 5.1, Mythos 5.1, Opus 5.5):
  - applies to accounts created on or after 2026-08-31, or to any request with `prefix_mismatch_behavior: "error"`;
  - "Retrying the same body never clears it; `count_tokens` returns the same 400";
  - fix: strip the block and later thinking blocks, or set `drop_block` under beta `thinking-binding-controls-2026-08-01`.
- **Other terminal errors:**
  - "prompt is too long" (input alone exceeds the window) is a 400 on every model;
  - a spend-cap 429 has no `retry-after`;
  - the gateway spend-limit 429 carries `x-should-retry: false`.
- **Claude Code:**
  - an AUP refusal "evaluates the full conversation… sending a new message in the same session usually re-triggers the same refusal";
  - before v2.1.218, context-limit retries could loop to exhaustion;
  - Claude Code no longer retries TLS certificate failures or policy denials.
- **Sources:**
  - https://platform.claude.com/docs/en/api/errors (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/build-with-claude/context-windows (accessed 2026-09-23)
  - bundled `shared/error-codes.md` (claude-api skill 2.1.280)
  - https://code.claude.com/docs/en/errors (accessed 2026-09-23)
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed 2026-09-23)
- **Evidence:** strong.
- **Token Bill:**
  - **never-succeeding-400 detector:** the same (error.type, message class, body-prefix hash) seen at least twice, or any retry of an error marked `x-should-retry: false` or spend-cap 429.
  - Dollars: mostly $0 billed. Report latency, RL and developer time, plus any billed compaction or refusal-loop turns.
  - Fix text per class: strip-and-retry-once, `drop_block`, preflight `count_tokens`, `/rewind`.
- **Savings:** small in dollars on 1P, larger on Azure where 400s are billed.
- **Effort:** S.

### F13. Correction: most "no `stop_reason`" Claude Code calls are complete calls whose final usage was never logged, so output is under-counted (`fp-cc-missing-final-usage`)
- **Local [L]:** 4,499 of 44,286 billed calls (10.2%) have no `stop_reason` on any entry ($510, 4.4% of spend).
  - 4,338 (96.4%) are followed directly by a `tool_result`, so the tool ran and the turn completed.
  - Only 27 are followed by a user interrupt, 5 by a synthetic error, and 117 are the last event in their thread.
  - 1 is in a main thread; 4,498 are in subagent or workflow sidechains.
  - Recorded output: p50 = 3 tokens (p90 20), against p50 470 for complete `tool_use` calls. Yet visible content is p50 410 characters against 397 for complete calls.
  - Concentrated by CC version: 2.1.260 at 35%, 2.1.187 at 19%, most others 4–11%.
- **Explanation [P+I].** In streaming, `stop_reason` is "`null` in the initial `message_start` event" and "Provided in the `message_delta` event". The `message_start` usage example shows `output_tokens: 1–3`. Transcript entries written before `message_delta` therefore carry placeholder output.
- **Estimated under-count [L/I]:** 6.49M output tokens ≈ **$245.55** using per-model output/visible-char ratios, or **$112.30** assuming same-kind median output. That is 1.0–2.1% of total spend. Thinking tokens are invisible, so the true figure may be higher.
- **Sources:**
  - https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/build-with-claude/streaming (accessed 2026-09-23)
  - local `21_failure_paths.out`, `22_nostop_output_gap.out`
- **Evidence:** strong (local measurement plus documented mechanics).
- **Token Bill:**
  - An importer rule for `usage_source ∈ {final, message_start_only, estimated}`.
  - For `message_start_only` with a following `tool_result`: complete the output estimate from content, calibrated per model. Reconcile with OTel `api_request.output_tokens` or the Admin API where available.
  - Do **not** count these as aborts.
  - Genuine aborts are the no-stop calls followed by an interrupt, an error, or thread end.
- **Savings:** accuracy. Prevents a 1–2% under-count, and a 10% false "abort" rate.
- **Effort:** S.

### F14. In this corpus, measured failure-path spend is 0.7% direct and ≤3.1% upper bound, dominated by turns that consume tool errors (`fp-local-failure-share`)
Local [L]. Total $11,714 over 44,286 calls and 134 days:

| Category | $ | Share | Note |
|---|---:|---:|---|
| Cold first success after API-error episode | 33.61 | 0.29% | excess write-over-read, 5 of 22 episodes |
| Refused-then-fallback attempts | ≤21.05 | ≤0.18% | per `iterations`; pre-output ones may be free |
| Calls cut by user interrupt (88 markers) | 15.40 | 0.13% | 87/87 had cache writes |
| Loops (≥3 consecutive errored tool results, 21 runs) | 8.32 | 0.07% | 15 same-tool runs, 12 of them Bash |
| Identical failing tool input re-issued | — | — | 14 occurrences |
| **Direct subtotal** | **≈78** | **≈0.7%** | |
| Turns consuming ≥1 errored tool_result (1,316 calls) | 283 | 2.4% | upper bound; includes needed recovery work |

- Tool error rate: 2.2% of 61,473 tool results. By tool: MCP 8%, Bash 2%, WebFetch 2%, Read 1%.
- **Sources:** local `21_failure_paths.out`, `23_error_turns.out`.
- **Evidence:** strong (for this corpus); single developer, 1P API, 1-hour main-thread TTL, no gateway.
- **Token Bill:**
  - Ship these as default report rows.
  - Use this corpus as a "healthy baseline" fixture. Enterprise fleets with gateways, Bedrock NAT, nested retries or 5-minute TTL should be benchmarked against it.
- **Savings:** here, ≤3%. Fleets with the risk factors in F3/F5/F9/F16 are expected to be higher (unmeasured).
- **Effort:** S.

### F15. Production agent studies: tool-call failures and loops appear in 14–30% of trajectories, failed runs cost 4× more, and intervention cuts tokens (`fp-agent-loop-prevalence`)
- **Wink** (Meta, production IDE agent):
  - 42,807 production trajectories sampled;
  - misbehaviour in **29.2%**: loops **5.21%**, did-not-follow-instructions 15.95%, unrequested changes 6.62%, tool-call failures **14.02%**;
  - infinite loops: 2.18% on Sonnet 4.5 against 3.59% on Opus 4.5;
  - a self-intervention observer resolved 90.9% of single-intervention cases;
  - live A/B: tool-call failure rate 5.29% → 5.07% (−4.2%), with stat-sig reductions in tokens per session and in engineer interventions.
- **MAST** (1,600+ multi-agent traces): step repetition FM-1.3 = 15.7% of failures; not recognising completion FM-1.5 = 12.4%.
- **SWE-Effi:** "an unresolved attempt consumes on average over 4 times more resources than a successful one" (SWE-agent + GPT-4o-mini: 8.8M against 1.8M tokens).
- **Token use varies widely:** runs on the same task differ by up to 30× in tokens (arXiv 2604.22750).
- **TraceProbe** (2,500 SWE-bench trajectories): "search loops" is the most stable anti-pattern.
- **Datadog Agent Console** detects "retry loops… when an agent repeatedly retries the same failing operation" and ships PreToolUse fixes.
- **Sources:**
  - arXiv 2602.17037 (2026-02-19), https://arxiv.org/abs/2602.17037
  - arXiv 2503.13657 (2025-03-17), https://arxiv.org/abs/2503.13657
  - arXiv 2509.09853 (2025-09-11), https://arxiv.org/abs/2509.09853
  - arXiv 2604.22750 (2026-04-24), https://arxiv.org/abs/2604.22750
  - arXiv 2607.06184 (2026-07-07), https://arxiv.org/abs/2607.06184
  - https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09)
- **Evidence:** strong (Wink production data); moderate for transferability.
- **Token Bill:**
  - **tool-error-loop detector:**
    - trigger: ≥3 consecutive `is_error` results, or the same `(tool, input_hash)` failing twice;
    - classify each error (test failure, command not found, permission denied, timeout, MCP error);
    - dollars = cost of the calls consuming those errors, plus the lingering context tax of the error text.
  - Recommend hooks (test-output filters, "stop after 2 identical failures"), turn caps, and Wink-style observers.
- **Savings:** a Wink-like intervention reduced tokens per session (magnitude not public). Loop spend here is 0.07–2.4%; in fleets with 5% loop prevalence it is unknown.
- **Effort:** M.

### F16. Enterprise networks cause silent aborts. Bedrock via NAT, VPC endpoints or NLB drops idle streams at 350 s, and Claude Code watchdogs abort at 180–300 s (`fp-network-aborts`)
- **AWS:** "NAT Gateways, interface VPC endpoints, and Network Load Balancers have a fixed idle connection timeout of 350 seconds." Streaming, extended thinking and large responses fail with resets.
  - The fix requires both `tcp_keepalive=True` in the SDK and a kernel keepalive below 350 s. The Linux default is 7,200 s.
- **Claude Code watchdogs** abort silent streams (F5). On gateways, the byte watchdog is reset by SSE pings, but before v2.1.222 CC reported spurious stalls.
- **Anthropic:** the SDKs set TCP keep-alive. The docs warn that "Some networks may drop idle connections".
- **Sources:**
  - https://docs.aws.amazon.com/bedrock/latest/userguide/troubleshooting-api-error-codes.html (accessed 2026-09-23)
  - https://code.claude.com/docs/en/network-config (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/api/errors (accessed 2026-09-23)
- **Evidence:** strong.
- **Token Bill:**
  - **abort-waste detector** with a network sub-cause: aborts clustered at 300–360 s of silence, or at the watchdog thresholds.
  - Emit a keepalive and watchdog config recommendation.
- **Savings:** removes re-sends on long generations (each re-send ≥1× the call).
- **Effort:** S.

### F17. Anthropic's Claude apps gateway already meters aborts and blocks retries of hard caps, a template for Token Bill's abort accounting (`fp-gateway-abort-metering`)
- "Client aborts are billed too. When a stream ends without the upstream's final usage frame, the meter bills a floor estimate of about four characters per output token for the text already sent to the client."
- The meter uses overrides, then list price, then an unknown-model fallback of $5/$25 per MTok.
- Spend-cap 429 (`billing_error`) carries `x-should-retry: false` and a `retry-after` equal to the time until reset.
- The meter is "a circuit breaker rather than an invoice… reconcile against your provider's usage reporting".
- **Sources:**
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed 2026-09-23)
- **Evidence:** strong (for gateway behaviour); weak as evidence of provider billing.
- **Token Bill:**
  - Adopt the same floor for aborts without a final usage frame: 4 chars per token for text, and `input + cache` from `message_start`.
  - Label it `estimated`.
  - Ingest gateway OTLP.
  - Show the gap between the gateway-metered total and the invoice.
- **Savings:** accuracy.
- **Effort:** S.

### F18. OpenAI's and others' cache lifetimes set the cold-retry threshold per provider (`fp-provider-ttl-thresholds`)
- **OpenAI GPT-5.6 and later:** `prompt_cache_options.ttl` = 30m (the only value), with a cache write at 1.25× input. The prefix stays eligible "30 minutes after its most recent write or reuse".
- **Earlier OpenAI models:** `in_memory` "around 5 to 10 minutes of inactivity, up to one hour", or `24h` extended retention. The default is 24h for non-ZDR organisations.
- **Bedrock Claude models:** 5 minutes and 1 hour. The Bedrock TTL "resets with each successful cache hit".
- **Anthropic:** 5 minutes or 1 hour, measured from request start (F3).
- **Sources:**
  - https://developers.openai.com/api/docs/guides/prompt-caching (accessed 2026-09-23)
  - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html (accessed 2026-09-23)
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed 2026-09-23)
- **Evidence:** strong.
- **Token Bill:** a per-provider and per-model TTL table drives the cold-retry-after-backoff detector and the "max safe backoff" recommendation.
- **Savings:** enables F3.
- **Effort:** S.

### F19. OTel GenAI has `error.type`, `finish_reasons` and TTFC but no attempt attribute; reuse HTTP's `http.request.resend_count` (`fp-trace-fields-standards`)
- **GenAI spans** (semantic-conventions-genai, 2026-09-16):
  - `error.type` (Stable, "Conditionally Required If the operation ended in an error");
  - `gen_ai.response.finish_reasons`, `gen_ai.response.id`;
  - `gen_ai.response.time_to_first_chunk`;
  - `gen_ai.usage.output_tokens`, which "SHOULD report the billed count".
- **HTTP spans:** `http.request.resend_count` (Stable, "Recommended if and only if request was retried"), updated "each time an HTTP request gets resent… regardless of what was the cause".
- **Claude Code OTel:**
  - `api_request.attempt`, `api_request.success`, `api_request.stop_reason`;
  - `api_error.retry`, `api_error.attempt`;
  - `client_request_id`, which survives failures with no server `request_id`.
- **Sources:**
  - https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md (commit 2026-09-16)
  - https://github.com/open-telemetry/semantic-conventions/blob/main/docs/http/http-spans.md (accessed 2026-09-23)
  - https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23)
- **Evidence:** strong.
- **Token Bill:** the new trace fields in §4 map onto these names so an OTel collector can feed Token Bill directly.
- **Savings:** enabler.
- **Effort:** S.

### F20. Stateful failover across providers loses the conversation or the cache, and retry storms against strict fallback limits cascade (`fp-cross-provider-failover`)
- **ContinuityBench** (750 failover events): naive stateless failover preserves "near-0%" of conversational context. History-forwarding reaches 99.2% continuity preservation.
- It notes "the critical necessity of asynchronous exponential backoff with jitter to prevent cascading retry storms against strict-limit fallback APIs".
- **[I]** History-forwarding to a different provider means **a full uncached prefix write** on the fallback (no cross-provider cache). Every cross-provider failover costs about prefix × fallback input rate (× 1.25 if written).
- **Sources:**
  - arXiv 2607.15899 (2026-07-17), https://arxiv.org/abs/2607.15899
- **Evidence:** moderate (preprint).
- **Token Bill:**
  - **fallback-cache-loss** also covers cross-provider hops.
  - Report the failover prefix-write cost against the downtime avoided.
  - Recommend a same-provider different-region hop (Bedrock cross-region inference) before a cross-provider hop.
- **Savings:** unknown (depends on failover frequency).
- **Effort:** S.

### F21. Token Bill's recorder currently drops failed and aborted attempts, so the failure path is invisible (`fp-recorder-blindspot`)
- `tokenbill/instrument.py`:
  - "Failed calls (exceptions) propagate untouched and record nothing."
  - The stream wrapper records only on clean exit (`get_final_message()`).
  - Raw `create(stream=True)` is not recorded.
- The SDK's internal retries happen inside the wrapped `messages.create`, so N HTTP attempts collapse into ≤1 record.
- The prior codebase report (`codebase.md` §open question 2) left abort billing open. This track answers it in §1.
- **Sources:**
  - local repo `tokenbill/instrument.py` (v0.1.2, read-only)
  - https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_base_client.py
- **Evidence:** strong.
- **Token Bill:**
  - Record every attempt, including exceptions, with `message_start` usage when available, `tokens_streamed` and the error class.
  - Hook httpx2 event hooks, or ship the recording proxy, to see SDK retries.
  - Never let recording failures raise into the app.
- **Savings:** enabler for all detectors.
- **Effort:** M.

---

## 3. Detector specs

Shared definitions:
- **Logical request** = consecutive attempts sharing `client_request_id` (Claude Code), or the same (thread, prefix_hash, new-input hash) within 15 minutes.
- **Prefix hash** = hash of (model, system, tools, messages[:-1]) as Token Bill already computes for cache simulation.
- **TTL(bp)** = requested TTL for the last cache breakpoint: 300 s or 3,600 s on Anthropic, and the table in F18 elsewhere.
- **Billing rule** = the entry in `billing_rules.yaml` for (provider, failure_mode), with confidence.

| # | Detector | Trigger (all measurable from proposed fields) | $ attribution | Fix text |
|---|---|---|---|---|
| D1 | **retry-storm** | attempts per logical request > 3; OR attempts at ≥2 layers (agent `attempt`>1 AND `x-stainless-retry-count`>0 / gateway retry header); OR ≥5 `api_error` per thread per 10 min; OR ≥N concurrent agents all 429 within 60 s (thundering herd) | Σ billed failed attempts per billing rule + Σ ITPM of cold retries (RL headroom) | "One retry owner: disable SDK retries behind a retrying gateway (LiteLLM pins `max_retries=0`); cap total attempts ≤3; add jitter; put a shared admission controller in front of parallel agents" |
| D2 | **cold-retry-after-backoff** | same prefix_hash; `retry.start_ts − first.start_ts > TTL(bp)`; retry `cache_creation ≥ 0.5 × prefix_tokens` | prefix_tokens × (write_rate − read_rate); also flag when `retry_after_received > TTL − elapsed` | "Cap retry wall-clock below TTL − expected generation time; honour long `retry-after` by deferring (queue) rather than sleeping; use 1h TTL for flows that run through incidents or CI watchdogs; stream long generations instead of non-streaming + timeout retry" |
| D3 | **abort-waste** | `outcome ∈ {client_abort, timeout, stream_error}` OR (`usage_source = message_start_only` AND next event ∉ {tool_result}) OR interrupt marker follows; sub-causes: network (silence ≥ 300–360 s), watchdog, user Esc, SDK timeout (≥ 600 s) | input+cache per billing rule + `tokens_streamed_before_abort` (or 4 chars/token floor) + the re-send's cost if a retry follows; show `salvageable_text_tokens` | "Stream; set TCP keepalive (Bedrock NAT 350 s); tune watchdogs; resume from partial text (4.6+: continue via user message) instead of full re-send; move long batch-able work to Batches (errored/cancelled items unbilled)" |
| D4 | **never-succeeding-400** | same (error.type, error-class, body hash) ≥2; OR retry after `x-should-retry:false`; OR retry after spend-cap 429 (no `retry-after`); classes: `thinking_binding`, `prompt_too_long`, `invalid_param`, `aup_refusal_repeat`, `spend_cap` | usually $0 billed (Anthropic/Vertex/Gemini) but Azure 400 content-filter is billed; plus billed compaction or refusal-loop turns; plus latency | "Classify as terminal; binding 400 → strip named thinking block (and later ones) once or `prefix_mismatch_behavior: drop_block`; prompt too long → preflight free `count_tokens`; AUP → `/rewind`; spend cap → alert admin, don't retry" |
| D5 | **fallback-cache-loss** | model switch (availability fallback, refusal fallback, gateway fallback, cross-provider hop) where first call on the new model has `cache_creation ≥ 0.8 × prefix` and no credit shift; ALSO A→B→A ping-pong within one TTL | prefix × (write_rate_B − read_rate_B) per switch; for ping-pong also the return write on A if A's TTL lapsed | "Redeem `fallback_credit_token` (5-min expiry, exact body) or use server-side `fallbacks: "default"` / SDK middleware; prefer same-family fallbacks; make overload fallbacks sticky for the TTL instead of per-turn; prefer cross-region over cross-provider" |
| D6 | **tool-error-loop** | ≥3 consecutive `is_error` tool results in a thread; OR same (tool, input_hash) failing ≥2; OR repeated permission-denied for the same tool; OR the same test command failing ≥3 without an intervening edit | Σ cost of calls consuming those errors + error-text context tax (chars/3.7 × read rate × remaining turns) | "PreToolUse hook: stop after 2 identical failures, filter test output to failures, pre-approve or deny permissions in settings; add turn caps; Wink-style observer nudge" |
| D7 | **missing-final-usage** (correctness) | `usage_source = message_start_only` AND followed by `tool_result` | estimated missing output = visible chars × per-model tokens/char − logged output | "Not waste: accounting fix; reconcile with OTel `api_request` or Admin API" |
| D8 | **double-send (non-streaming fallback)** | Claude Code "Retrying without streaming" warning, or two attempts with the same body where the first has 0 events | the first attempt's billed input | "Configure proxy/gateway to pass SSE bodies and headers unmodified" |
| D9 | **quota-reservation throttling** (Bedrock) | 429 ThrottlingException AND `max_tokens` ≥ 4 × p99 observed output for that route | indirect: ITPM/TPM lost; retries | "Right-size `max_tokens` (Bedrock deducts input + `max_tokens` at request start; output burndown 5–15×)" |

---

## 4. New trace fields (JSONL schema v2 additions)

| Field | Type | Source | Why |
|---|---|---|---|
| `logical_request_id` | str | CC `client_request_id` / `x-client-request-id`; recorder UUID | groups attempts |
| `attempt` | int (1-based) | CC OTel `attempt`; `x-stainless-retry-count`+1; OTel `http.request.resend_count`+1 | D1, D2 |
| `retry_layer` | enum `sdk|agent|gateway|app` | recorder or proxy | D1 amplification |
| `outcome` | enum `success|http_error|stream_error|client_abort|timeout|refusal|max_tokens|context_exceeded|pause_turn` | response or exception | billing-rule lookup |
| `error.type` | str | OTel stable attr; API `error.type` (`overloaded_error`, `rate_limit_error`, `invalid_request_error`…) | D4 |
| `http_status` | int | response | rule lookup |
| `error_class` | enum (`thinking_binding`, `prompt_too_long`, `spend_cap`, `acceleration`, `network_reset`, `watchdog`, `nat_idle`…) | message classifier (no text stored) | D4, D3 sub-cause |
| `x_should_retry`, `retry_after_s`, `retry_after_ms` | bool/float | response headers | D2, D4 |
| `ts_request_start` | float | client | TTL arithmetic (TTL runs from request start) |
| `ts_headers` / `ts_first_token` / `ts_last_event` / `ts_error` | float | client or proxy | abort sub-cause, TTFB, stall detection |
| `usage_source` | enum `final|message_start_only|estimated|none` | recorder | D7, and so aborts aren't mistaken for complete calls |
| `tokens_streamed_before_abort` | int | count output deltas (or chars/4 floor) | D3 $ |
| `chars_streamed_text`, `salvageable_text_tokens` | int | deltas | resume ROI |
| `thinking_streamed_tokens` | int | thinking deltas | "unsalvageable" share |
| `resumed_from` / `continuation_of` | call id | recorder | links resume chains |
| `iterations[]` | list{type, model, usage} | response `usage.iterations` | F1 per-attempt billing |
| `fallback.{from_model,to_model,trigger}` | obj | `fallback` content block, CC `model_refusal_fallback`, gateway fallback header | D5 |
| `fallback_credit.{minted, redeemed, shift_tokens}` | obj | `stop_details.fallback_credit_token`, retry usage shift | D5 |
| `prefix_hash`, `cache_ttl_requested` | str/int | recorder | D2, D5 |
| `billing_rule_id`, `billing_confidence` | str | Token Bill rules table | honest reporting |
| tool results: `is_error`, `tool_input_hash`, `tool_error_class` | bool/str/enum | transcript or hook | D6 |
| `provider`, `platform`, `service_tier` | str | config/response | rule lookup |

---

## 5. Recommended client configurations (what Token Bill should emit as fix text)

1. **Exactly one retry owner per stack.** With a retrying gateway (LiteLLM, Portkey, Cloudflare), set `max_retries=0` in the provider SDK, as LiteLLM already does. Otherwise, leave the SDK at 2 retries and let the app layer not retry. Cap total attempts at 3 for interactive traffic.
2. **Backoff budget < TTL − expected generation time.**
   - 5-minute TTL: total retry wall-clock of 60–120 s at most (Portkey's 60 s cap is a good default).
   - If `retry-after` exceeds the remaining TTL, **defer** the job to a queue and accept a cold start, or pre-warm. Don't sleep through it in a request thread.
   - The latest Anthropic SDK obeys any `retry-after`, so wrap it.
3. **Use 1-hour TTL** for long-running agent sessions and for `CLAUDE_CODE_RETRY_WATCHDOG` / CI runs. The watchdog's up-to-5-minute backoff defeats a 5-minute cache every time. Also use it for anything likely to retry through incidents: the p50 API incident lasts 1.07 h.
4. **Stream all long generations.**
   - Never rely on non-streaming plus a 10-minute timeout plus 2 retries. That is up to 3 attempts, each after the TTL.
   - Enable TCP keepalive. On Bedrock behind NAT, VPC endpoints or NLB, also set the kernel keepalive below 350 s.
5. **Resume from partial, don't re-send.**
   - SDK users implement the documented recovery: 4.6+ puts the partial in a user message; ≤4.5 uses an assistant prefill.
   - Salvage text blocks only; thinking and tool_use can't be recovered.
   - OpenAI long jobs: `background: true` with `stream: true` and resume by `sequence_number`.
   - Claude Code does this automatically (v2.1.199+).
6. **Refusals.**
   - 1P / Claude Platform on AWS: server-side `fallbacks: "default"`.
   - Bedrock, Vertex, Foundry: `BetaRefusalFallbackMiddleware`.
   - Hand-rolled: always redeem `fallback_credit_token` within 5 minutes with an exactly matching body.
   - Size fallback-model rate limits, or fallbacks degrade to refusals.
7. **Terminal errors are terminal.** Don't retry `x-should-retry: false`, spend-cap 429s (no `retry-after`), binding 400s (strip once), or prompt-too-long (compact or preflight with free `count_tokens`).
8. **Parallel agent fleets.** Put a shared admission controller in front of the provider: AIMD concurrency, circuit breaker, centralised jittered retry. HiveMind reports 72–100% → 0–18% failures under contention.
9. **Bedrock:** right-size `max_tokens`, since it is reserved against TPM at request start.
10. **Non-urgent work → Message Batches.** Errored, cancelled and expired items are not billed, and you get 50% off.
11. **Tool loops:** PreToolUse hooks that stop after 2 identical failures and filter test output to failures; turn caps per subagent (`maxTurns`); pre-approved permissions to avoid permission-denied retry loops.
12. **Claude Code specifics:**
    - keep `CLAUDE_CODE_MAX_RETRIES` at its default or lower in CI;
    - set `fallbackModel` to the same family;
    - fix proxies that trigger "Retrying without streaming";
    - upgrade past v2.1.218 (context-limit retry loop fix) and v2.1.199 (partial-output keep).

---

## 6. What Token Bill should build (prioritised)

**P0 (correctness first, S–M effort)**
1. **Attempt-level data model and recorder** (F21):
   - record every attempt, including exceptions, aborts and SDK-internal retries (via httpx2 hooks or the recording proxy);
   - add the §4 fields;
   - read `x-stainless-retry-count`, `retry-after(-ms)` and `x-should-retry` in the proxy.
2. **`billing_rules.yaml`** (F7, §1): (provider, failure_mode) → billed input / partial output / cache written, with confidence and source URL. Report "assumed" dollars separately from documented ones.
3. **Claude Code importer fixes:**
   - `usage_source` with `message_start_only` output reconstruction (F13), worth 1–2% of spend;
   - `iterations[]` pricing with zero-priced pre-output declines (F1);
   - ingest `system.api_error` into attempt chains (F5).

**P1 (the six failure-path detectors, M effort)**
4. D1 retry-storm, D2 cold-retry-after-backoff, D3 abort-waste, D4 never-succeeding-400, D5 fallback-cache-loss, D6 tool-error-loop. Add D7–D9 as cheap extras.
5. **Retry and resume simulator.** Extend the 4-scenario replay with "as-billed with failures" against "optimal retry policy":
   - backoff capped below TTL;
   - resume-from-partial;
   - credit redeemed;
   - single retry owner.

   The output is dollars recoverable per policy change, using the §F3 scenario math with real prefix sizes.
6. **Incident-tax join** (F10): pull status feeds (statuspage JSON) and attribute cold rewrites and retries to incident windows.

**P2 (fleet features)**
7. **Config linter:** scan `settings.json`, env vars, LiteLLM, Portkey and Cloudflare configs and SDK construction for:
   - nested retries;
   - watchdog with 5-minute TTL;
   - non-streaming with large `max_tokens`;
   - missing keepalive;
   - per-turn fallbacks.

   Emit PR-able fixes, as Datadog's Fix Library does.
8. **Failure-path KPIs:**
   - failure-path share of spend;
   - attempts per logical request;
   - cold-retry rate;
   - fallback credit redemption rate;
   - tool-error-loop rate;
   - loop spend per 1k sessions.

   Benchmark against the local healthy baseline (0.7% direct) and Wink's production prevalence (loops 5.2%, tool failures 14%).
9. **Gateway reconciliation:** compare gateway-metered abort floors (4 chars/token), CC OTel `cost_usd` and invoice / Usage & Cost API to quantify the "failure accounting gap".

---

## 7. Sources (all opened 2026-09-23 unless dated)

Anthropic / Claude:
- https://platform.claude.com/docs/en/build-with-claude/streaming — Error events; Error recovery (4.5 and earlier vs 4.6+); `message_start` / `message_delta` usage
- https://platform.claude.com/docs/en/api/errors — HTTP errors; SDK auto-retry twice honouring `retry-after`; mid-stream errors after 200; Long requests
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching — lifetime from request start; entries available after the first response begins
- https://platform.claude.com/docs/en/api/rate-limits — cache reads not in ITPM; OTPM ignores `max_tokens`; spend-cap 429 without `retry-after`
- https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons — stop reasons; `stop_reason` null in `message_start`
- https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback — refusal billing; iterations; sticky routing; RL
- https://platform.claude.com/docs/en/build-with-claude/fallback-credit — credit token, 5-minute expiry, exact-match rules, platforms
- https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/handle-streaming-refusals — pre-output refusals not billed
- https://platform.claude.com/docs/en/build-with-claude/batch-processing — errored, cancelled and expired items not billed
- https://platform.claude.com/docs/en/build-with-claude/context-windows — prompt too long 400; `model_context_window_exceeded`
- https://platform.claude.com/docs/en/about-claude/pricing — rates; web search errors not billed
- https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python — retries, 10-minute timeout, timeout retry
- https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_base_client.py (commit 2026-09-19) and `_constants.py`
- Bundled claude-api skill 2.1.280: `shared/error-codes.md`, `shared/model-migration.md`, `python/claude-api/README.md`
- https://code.claude.com/docs/en/errors — Automatic retries; stalls; incomplete-response; non-streaming double-send; Usage Policy refusal
- https://code.claude.com/docs/en/env-vars — `CLAUDE_CODE_MAX_RETRIES`, `CLAUDE_CODE_RETRY_WATCHDOG`, `FALLBACK_FOR_ALL_PRIMARY_MODELS`, timeouts
- https://code.claude.com/docs/en/network-config — streaming idle watchdogs
- https://code.claude.com/docs/en/model-config — fallback chains (turn-only); automatic content fallback
- https://code.claude.com/docs/en/monitoring-usage — OTel `api_request` / `api_error` attributes
- https://code.claude.com/docs/en/sub-agents — API errors in subagents
- https://code.claude.com/docs/en/claude-apps-gateway-spend-limits — abort floor metering; `x-should-retry: false`
- https://status.claude.com/api/v2/incidents.json — incidents 2026-07-24..2026-09-22

Other providers and gateways:
- https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/troubleshooting-api-error-codes.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
- https://cloud.google.com/vertex-ai/generative-ai/pricing
- https://ai.google.dev/gemini-api/docs/billing (updated 2026-09-20)
- https://ai.google.dev/gemini-api/docs/troubleshooting (updated 2026-09-20)
- https://ai.google.dev/gemini-api/docs/rate-limits (updated 2026-09-02)
- https://learn.microsoft.com/en-us/azure/ai-foundry/openai/faq (updated 2026-09-11)
- https://developers.openai.com/api/docs/guides/flex-processing
- https://developers.openai.com/api/docs/guides/rate-limits
- https://developers.openai.com/api/docs/guides/error-codes
- https://developers.openai.com/api/docs/guides/background
- https://developers.openai.com/api/docs/guides/prompt-caching
- https://github.com/openai/openai-python (README: retries; stream consumption not retried)
- https://developers.openai.com/codex/config-reference
- https://status.openai.com/api/v2/incidents.json
- https://docs.litellm.ai/docs/routing
- https://docs.litellm.ai/docs/proxy/reliability
- https://portkey.ai/docs/product/ai-gateway/automatic-retries
- https://portkey.ai/docs/product/ai-gateway/fallbacks
- https://developers.cloudflare.com/ai-gateway/configuration/request-handling/ (updated 2026-09-14)
- https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/ (updated 2026-08-07)
- https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09)
- https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md (commit 2026-09-16)
- https://github.com/open-telemetry/semantic-conventions/blob/main/docs/http/http-spans.md

Papers:
- Chu et al., "An Empirical Characterization of Outages and Incidents in Public Services for Large Language Models", arXiv 2501.12469 (2025-01-21)
- Battaglini-Fischer et al., "FAILS", arXiv 2503.12185 (2025-03-15)
- Cemri et al., "Why Do Multi-Agent LLM Systems Fail?", arXiv 2503.13657 (2025-03-17)
- Nanda et al. (Meta), "Wink: Recovering from Misbehaviors in Coding Agents", arXiv 2602.17037 (2026-02-19)
- Owusu Agyemang et al., "HiveMind", arXiv 2604.17111 (2026-04-18)
- Pandey & Singh, "ContinuityBench", arXiv 2607.15899 (2026-07-17)
- Fan et al., "SWE-Effi", arXiv 2509.09853 (2025-09-11)
- Bai et al., "How Do AI Agents Spend Your Money?", arXiv 2604.22750 (2026-04-24)
- Shu et al., "What Resolve Rate Hides (TraceProbe)", arXiv 2607.06184 (2026-07-07)

Local artefacts:
- `research/21_failure_paths.py/.out`
- `research/22_nostop_output_gap.py/.out`
- `research/23_error_turns.py/.out`
- `research/fp_docs/` (raw doc copies)

---

## 8. Open questions

1. **Anthropic 1P abort billing.** Does a client disconnect or a mid-stream `overloaded_error` bill input, cache write and partial output? Is server-side generation after a disconnect billed? This needs a controlled probe: stream, abort at N tokens, then read the Usage & Cost API per `request_id`.
2. **Pre-token 429/529 on 1P and Bedrock.** Confirm $0 via the Usage & Cost API. The docs imply it but never state it.
3. **Bedrock, Vertex and Foundry refusal billing.** Is a pre-output decline also free there? Only fallback-credit availability is documented.
4. **Claude in Foundry.** Does the Azure OpenAI "charged if processing happened" rule (400 filter, 408 timeout) apply to Claude CCU billing?
5. **Ambiguous iterations.** For refused attempts with small non-zero `output_tokens` (4–9), are they "pre-output" (free) or mid-stream (billed)?
6. **Fleet prevalence.** What are retry, abort and error rates in the target enterprise (gateways, Bedrock NAT, CI watchdog use)? The local corpus is one developer on 1P with a 1-hour TTL.
7. **OpenAI.** Are cancelled streaming or background responses billed for generated tokens? Are generic 429/5xx ever billed outside Flex?
