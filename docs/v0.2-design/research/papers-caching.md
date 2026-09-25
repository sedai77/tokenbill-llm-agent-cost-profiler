# Track: papers-caching — Prompt/KV caching, prefix reuse, and what Token Bill must model

Research date: 2026-09-23. Scope: prompt/KV caching papers, provider-cache audits, 2025-2026 agent-caching studies, semantic caching, and the concrete rules Token Bill's simulator and breaker detector should implement.

Evidence grading used below:
- **strong**: provider primary documentation, peer-reviewed venue, or a primary measurement with a clear method.
- **moderate**: single arXiv preprint or single-vendor study with a stated method, not yet replicated. GitHub issues/PRs with measured numbers.
- **weak**: blog, secondary summary, or only indirectly supported.

Every source listed was opened during this research (URL + publication/updated date, or "accessed 2026-09-23" when the page shows no date). Numbers from PDFs were checked against the extracted paper text, not just summaries. Numbers labelled **derived** are my arithmetic on sourced formulas and prices.

---

## 0. Executive summary

1. **Caching dominates the agent bill, so the cache model is the product.** Anthropic's own measurements: agent loops read a **median 84%** of input from cache over a day of real traffic. The **top 10% of harnesses read ≥94%**. Caching took Claude Fable 5.1 from **$37.94 to $7.12 per task** on DeepResearch Bench II. One outside study of 5,493 billed Claude Code executions found cache reads + writes were **~87% of reconstructed cost**, while tool-output tokens were **3.3%**. Any Token Bill number that isn't cache-aware will be wrong for agents.
2. **Token Bill's simulator matches the 2024 cache model, not the 2026 one.** It is missing:
   - the **1-hour TTL** (2x write price)
   - TTL measured from the **request start**, with generation time counted against it
   - the **20-block lookback window**
   - **concurrency**: an entry can't be read until the first response starts streaming
   - the **invalidation hierarchy**: thinking, effort, tool_choice, images, speed, web-search and citations toggles, and `disable_parallel_tool_use` all break the cache without changing any rendered byte
   - **workspace-scoped, fleet-wide reuse** across sessions
   - **new prices**: Opus 5.5 at $4/$20 with 0.05x reads; the 1.1x `inference_geo` multiplier; fast mode; batch discounts stacking with cache multipliers
   - the **~30% larger tokenizer** on 4.7+ models, which breaks a single chars/3.7 constant
3. **Real caches fall short of the documented model, so the simulator needs calibration.** Examples:
   - Sonnet 4.6 cumulative hit rate plateaued at **ρ ≈ 0.83 for ~2k-token prefixes** and reached 1.0 only above roughly 3.5k tokens.
   - Providers route to **multiple servers with separate caches**, so a prefix is warm only after enough writes (seen by Gu et al. and by CAPC).
   - Tool arrays may be **cached without any cache_control marker**.
   - Cross-region Bedrock inference **"may lead to increased cache writes"**.

   Token Bill should report "documented-rules optimum" and "calibrated expectation" separately, fitting ρ(prefix size, gap) from the customer's own billed traces.
4. **The biggest enterprise-scale breakers are infrastructure and configuration, not prompt text:**
   - LLM gateways that strip `cache_control`, which makes every turn fully uncached
   - gateways that drop the `anthropic-beta` header, which loses the 1h TTL
   - custom base URLs that disable tool search, so every MCP reconnect invalidates the cache
   - traffic split across workspaces or API keys, which fragments the cache
   - Claude Code on API keys defaulting to the **5-minute** TTL for the main conversation
   - nondeterministic tool ordering from HashMap-backed MCP registries. Codex saw hit rate fall **below 1%**. In Claude Code issue #49038, sorting the tools cut cache creation from **56,370 to 32 tokens**.
5. **TTL and keepalive economics can now be computed.** A keepalive pays off up to an idle of I_max = τ(w/r − 1): about **46 min** at standard Anthropic prices with a 4-minute ping. Derived from the same formula: **~96 min on Opus 5.5** and **~196 min on Fable 5.1**. Anthropic's rule of thumb: use the 1h TTL when **more than ~1 in 20 gaps fall between 5 and 60 minutes**. When nothing pauses, the 5-minute TTL cost **15% less (Sonnet 5)** and **11% less (Opus 5)**. Token Bill can turn each user's gap histogram directly into a TTL/keepalive policy with a dollar figure.
6. **"Token reduction is not cost reduction."** Changes that edit the prefix can raise the bill:
   - A compression layer cut tool-output tokens **38.4%** but raised billed cost **6.8%**. One proxy raised cost **48.4%**.
   - Query-aware prompt compression was **+40.1%** vs vanilla on τ-bench.
   - Context editing cost **74% more** on one Anthropic run.
   - A 25-token status line at the front of a system prompt raised cost from **$0.59 to $4.24 per run**.

   Token Bill should price every optimization *net of cache effects*, and could be the only tool that does.
7. **Semantic caching is risky and rarely useful for coding agents.** Static similarity thresholds give unreliable error rates, and attacks have been shown to hijack cache keys. Agent outputs depend on environment state. Token Bill should measure *exact* and *near-duplicate* request rates to size that opportunity honestly. It should also flag semantic caches as a correctness and security risk, and point to verified or plan-level caching (APC: −50% cost) for repeated agent tasks.

---

## 1. Findings

### A. Provider cache semantics the simulator must model (Anthropic, authoritative)

#### F1 — Invalidation hierarchy: request parameters outside the rendered bytes break caching (strong)
- The cache has three tiers: tools → system → messages. A change invalidates its own tier and everything after it.
  - Tool definitions invalidate everything.
  - Toggling web search or citations invalidates system and messages. Web fetch toggles do the same.
  - `speed` (fast mode) invalidates system and messages. Claude Code notes the fast-mode header "is part of the cache key", so the first fast turn re-reads the whole history.
  - `tool_choice`, `disable_parallel_tool_use`, and adding or removing images invalidate messages only.
  - Thinking parameters and `output_config.effort` always invalidate messages. They also invalidate tools and system "on models that render the configuration ahead of them". Setting effort explicitly to the model's default does not invalidate.
- On earlier Opus/Sonnet models and **all Haiku models through Haiku 4.5**, previously cached thinking blocks are stripped when a non-tool-result user message follows. Every message after the first stripped block falls out of cache.
- Model switches have no escape hatch because caches are per model.
- Cache-preserving alternatives now exist:
  - mid-conversation `role:"system"` messages instead of editing the system prompt (Opus 5, Opus 4.8, Fable 5/5.1, Mythos; not Sonnet 5)
  - `tool_addition`/`tool_removal` blocks (beta `mid-conversation-tool-changes-2026-07-01`)
  - per-message effort (beta, Fable 5.1 / Opus 5)
  - `clear_at:"next_user_message"` reminders
- **Token Bill gap:** the canonical rendering covers only tools/system/messages. A thinking, effort, tool_choice, speed or image toggle produces a billed miss that Token Bill can't explain today.
- Sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching — accessed 2026-09-23
  - https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23
  - bundled claude-api skill `shared/prompt-caching.md` (v2.1.280)

#### F2 — TTL semantics: lifetime counts from request *start*, reads refresh it, 1h TTL costs 2x, and mixed TTLs have ordering and billing rules (strong)
- Quoted rule: the lifetime "is measured from the start of the request that writes or reads the cache entry, not from the end of its response". A 4-minute generation leaves about 1 minute before a 5-minute entry expires.
- The cache is refreshed for no additional cost each time it is used.
- Prices: 1h writes are 2x base input; 5m writes are 1.25x; reads are 0.1x, with 0.05x on Opus 5.5 and 0.025x on Fable 5.1 / Mythos 5.1.
- Break-even: 5m pays off after one read; 1h after two reads.
- Mixing TTLs: longer-TTL entries must come before shorter ones. Billing uses positions A (highest hit), B (highest 1h block after A) and C (last breakpoint).
- `usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` split writes by TTL.
- Server tools automatically insert a 5m breakpoint after their results. These appear as 5m writes even when every marker you set is 1h.
- **Token Bill gap:**
  - The trace has one `ts` described as "unix seconds at request time". There is no end time or first-token time.
  - The simulator has one TTL (300 s) and only the 1.25x write multiplier.
  - It can't model 1h writes, mixed TTLs, or generation time eating into the TTL.
- Sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - https://platform.claude.com/docs/en/about-claude/pricing.md — accessed 2026-09-23
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching — accessed 2026-09-23

#### F3 — 20-block lookback window: the real API does not match "any earlier prefix" (strong)
- "The system checks at most 20 positions per breakpoint, counting the breakpoint itself as the first."
- On the Claude API, a run of consecutive `tool_use` blocks counts as one position, and so does a run of `tool_result` blocks.
- Documented example: the turn-2 entry at block 15 is missed from a breakpoint at block 35. Adding a second breakpoint at block 15 fixes it.
- Bedrock's "simplified cache management" has the same ~20 content-block lookback.
- Symptom: every request rewrites the whole conversation even though payloads are byte-identical.
- **Token Bill gap:** `optimal-cache` matches the longest previously written prefix wherever it is. For a real client with one moving breakpoint, that **overstates** achievable reads whenever a turn appends more than 20 positions.
- Sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html — accessed 2026-09-23

#### F4 — Concurrency: an entry can't be read until the first response begins, so parallel fan-outs pay N times (strong)
- "A cache entry only becomes available after the first response begins."
- N parallel requests with identical prefixes all pay full price.
- Documented fix: send one request, wait for its first streamed token, then fire the other N−1.
- Claude Code implements this for workflow fan-outs. It holds all but the first same-prefix agent "for up to 5 seconds by default".
- Parallel workers that build slightly different prompts over the same context write N separate entries.
- **Token Bill gap:** the replay treats an entry as readable at the writer's `ts`. That overstates reads for subagent swarms and batch fan-outs. There is also no detector for the pattern.
- Sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23

#### F5 — Cache scope: workspace-isolated on the Claude API; per-machine and per-directory prefixes in Claude Code (strong)
- "Caches are isolated per workspace" on the Claude API, Claude Platform on AWS and Microsoft Foundry. Bedrock and Google Cloud isolate at the organization level. Different organizations never share caches.
- The same prompt split across workspaces writes and reads separate entries.
- In Claude Code, "the cache is effectively scoped to one machine and directory":
  - The system prompt carries the working directory, platform, shell, OS version, auto-memory paths, and a git-status snapshot.
  - Worktrees of the same repo miss each other's cache.
  - Parallel sessions in the same directory *do* share it.
  - Agent SDK fleets can suppress the per-machine sections to share cache across machines.
- **Token Bill gap:** simulation is per run. Enterprises have thousands of developers sending near-identical tools+system prefixes. The fleet-level question is how much of that prefix is shared across sessions in one workspace, and how much is lost to workspace or key fragmentation and per-machine sections.
- Sources:
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23

#### F6 — 2026 pricing and tokenizer facts the pricing module is missing (strong)
- Model prices ($/MTok):

  | model | input | 5m write | 1h write | cache read | output |
  |---|---|---|---|---|---|
  | Opus 5.5 | $4 | $5 | $8 | $0.20 (0.05x) | $20 |
  | Fable 5.1 / Mythos 5.1 | $10 | $12.50 | $20 | $0.25 (0.025x) | $50 |
  | Fable 5 / Mythos 5 | $10 | — | — | $1 (0.1x) | $50 |
  | Sonnet 5 | $2 | — | — | — | $10 (now standard pricing) |
  | Haiku 4.5 | $1 | — | — | — | $5 |

- Other price modifiers:
  - Batch is 50% off and **"multipliers stack with other pricing modifiers, including the Batch API discount and data residency"**.
  - `inference_geo:"us"` applies **1.1x to every token category**, including cache reads and writes, on 4.6+ models.
  - Fast mode: Opus 5.5 $8/$40; Opus 5 and 4.8 $10/$50. Cache multipliers apply on top.
  - Full 1M context is billed at standard rates for 4.6+ models.
- Tokenizer: "Claude 4.7 and later models … use a newer tokenizer … approximately 30% more tokens for the same text."
- Minimum cacheable prefix is non-monotonic across generations: 512 on the newest models, 4096 on Opus 4.6/4.5 and Haiku 4.5.
- **Token Bill gap:**
  - `pricing.py` has no Opus 5.5, Mythos 5.1, Opus 4.5 or Sonnet 4.5.
  - It has no 1h write multiplier and no batch, geo or fast-mode modifiers.
  - The single `CHARS_PER_TOKEN = 3.7` ignores a roughly 1.3x tokenizer difference between model families, which matters most for the min-prefix gate.
- Sources:
  - https://platform.claude.com/docs/en/about-claude/pricing.md — accessed 2026-09-23
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23

#### F7 — Fleet benchmarks and measured lever sizes from Anthropic (strong)
- "Over a full day of real traffic, agent loops read a median 84% of their input from the cache, and the top 10% of harnesses, coding or not, read 94% or more. Deep in a task, a well-built loop pays full price on under 1% of its input."
- With caching, Fable 5.1 fell from $37.94 to $7.12 per task on DeepResearch Bench II, and Sonnet 5 from $3.20 to $1.20.
- On an issue-triage agent, "a 25-token status line at the front of the system prompt cost $4.24 per run instead of $0.59, more than running with caching off."
- 1h TTL rule: use it if "more than about 1 gap in 20 falls between 5 minutes and an hour". When turns arrive seconds apart, the 5-minute default "cost 15% less than the 1-hour setting on Claude Sonnet 5 and 11% less on Claude Opus 5."
- The bundled cost guide cites a caching effect of "a factor of 2.5 to 3.7, at 81% to 90% hit rates". It also gives a Poisson estimate hit ≈ 1 − e^(−λ·TTL) for sizing without traces.
- **Opportunity:** Token Bill can grade each harness, team or repo against a published fleet distribution (p50 84%, p90 ≥94%) and put a dollar value on the gap.
- Sources:
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md — accessed 2026-09-23
  - bundled `shared/cost-optimization.md` (v2.1.280)

#### F8 — Cache diagnostics: server-side ground truth for "why did this miss" (strong; beta)
- Beta header `cache-diagnosis-2026-04-07`, available on the Claude API only (not Bedrock, Vertex, Foundry or Claude Platform on AWS).
- Pass the previous response `id` as `diagnostics.previous_message_id`. The response then returns `cache_miss_reason.type`, one of:
  - `model_changed`, `system_changed`, `tools_changed`, `messages_changed`
  - `previous_message_not_found`
  - `unavailable`: this covers diffs in `tool_choice`, `thinking`, `context_management`, `output_config`, `output_format`, or the active beta headers
- Each `*_changed` result also carries `cache_missed_input_tokens`.
- A diagnostics result of `null` combined with low reads means "requests match but the entry expired". That is a TTL problem, not a prompt problem.
- Fingerprints are hashes only (ZDR-eligible), short-lived, and scoped to the workspace. The header must be sent on **every** request.
- **Opportunity:**
  - Token Bill's recorder should opt in by default on first-party traffic and store `diagnostics` per call.
  - The breaker classifier can then be validated against provider ground truth on real traffic, not only on synthetic demos.
  - The expired-vs-changed split feeds directly into the TTL advisor.
- Source: https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics — accessed 2026-09-23

#### F9 — The "healthy-loop signature", and misses no payload diff can explain (strong)
- In a warmed loop, reads cover the whole prior prefix and writes are "roughly the previous assistant output plus the newly appended input".
- If `cache_creation_input_tokens` is close to the full conversation size on every request, one of these is happening:
  1. the prefix is being rewritten upstream
  2. thinking blocks are being stripped (older models)
  3. a turn appended more than 20 positions (the lookback)
- Cases 2 and 3 show nothing in a payload diff.
- `input_tokens` is only the uncached remainder. Total prompt size is the sum of all three fields.
- **Opportunity:** a cheap "signature" classifier that needs only billed usage:
  - write/turn-delta ratio
  - read-collapse events
  - write-without-read chains

  It can run on Admin-API aggregates or OTel data where full payloads aren't available.
- Source: bundled `shared/prompt-caching.md` § Verifying cache hits (v2.1.280). Consistent with https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23.

### B. Audits of real provider caches

#### F10 — Gu et al., "Auditing Prompt Caching in Language Model APIs" (ICML 2025) (strong)
- Statistical timing audits of 17 API providers found **prompt caching in 8** and **global cross-user cache sharing in 7**, including OpenAI's `text-embedding-3-small`, which was patched after disclosure.
- Anthropic (Claude 3 Haiku) and OpenAI (GPT-4o mini) showed per-organization sharing, matching their docs at the time.
- Most APIs needed only 1 victim request to detect caching. OpenAI and Azure `text-embedding-3-small` needed 25. That is consistent with **multiple servers holding separate caches and random routing**.
- At least five providers changed something after the October 2024 disclosure.
- The audit also inferred that OpenAI's embedding model is decoder-only.
- **Implications for Token Bill:**
  1. Provider caches are not a single global store. Hit probability depends on routing, so ρ=1 is an idealization.
  2. Cache scope is a security property enterprises will ask about. Token Bill's isolation report (F24) should reference this.
- Sources:
  - https://arxiv.org/abs/2502.07776 — v1 2025-02-11, v2 2025-07-13
  - https://proceedings.mlr.press/v267/gu25b.html
  - code: https://github.com/chenchenygu/auditing-prompt-caching

#### F11 — Measured Anthropic cache behaviour: hit-rate plateau for small prefixes, token-strict keys, implicit tools caching (moderate)
- Source: Song (PayPal), "Cache-Aware Prompt Compression: A Two-Tier Cost Model for LLM API Caching", arXiv 2607.15516, 2026-07-17.
- On Sonnet 4.6 with 5-minute ephemeral caching, cumulative hit rate ρ(N) at a 2.4k-token prefix rose from **0.47 at N=5** to **0.89 at N=50**.
- By prefix size:

  | prefix size | ρ(N=5) | ρ(N=10) | ρ(N=30) |
  |---|---|---|---|
  | 2,053 tokens | 0.53 | 0.63 | 0.83 |
  | ≥4,096 tokens | 1.00 | 1.00 | 1.00 |

  The authors model the threshold at T ≈ 3,500 tokens.
- A 94k-token prefix "requires multiple write events for the cache to propagate across Anthropic's routing pool".
- Cache keys are token-strict, but a leading/trailing whitespace change still **hit** (normalization).
- Billing reconciled with published rates to **<1%**.
- In a production assistant with a 94k-token prefix (287 MCP tools), a "vanilla" run with no `cache_control` still showed cache_read ≈ 106k per call. The authors call this "implicit tools caching" and treat it as unexplained.
- Crossover formula for when caching alone beats query-aware compression: ρ_cross(r) = (α − 1/r)/(α − β), with α = write/input and β = read/input. For Sonnet 4.6: r=2 → 0.652, r=3 → 0.797, r=6 → 0.942.
- CAPC (query-agnostic compression + explicit cache_control):
  - 16/16 LongBench-v2 wins
  - mean −49% vs cache-only
  - −51.7% on the 94k-prefix assistant; −45.5% end-to-end
  - on τ-bench, the same reward as vanilla (36/50), while query-aware compression was **+40.1%** vs vanilla
- **Token Bill implications:**
  - The simulator's ρ=1 assumption overstates savings for small prefixes.
  - Ship a calibrated ρ(prefix size, N) fitted from the customer's own billed reads.
  - Treat cache activity without markers as possible implicit caching, not a data error.
- Source: https://arxiv.org/pdf/2607.15516 — 2026-07-17

### C. Agents: hit rates, what breaks caching, cache-friendly layout

#### F12 — "Don't Break the Cache": 41–80% cost reduction on long-horizon agents, and naive full-context caching can hurt (moderate→strong)
- Method: over 500 DeepResearch Bench sessions, 10k-token system prompts, GPT-5.2 / Claude Sonnet 4.5 / Gemini 2.5 Pro / GPT-4o.
- Best-strategy cost savings: **79.6% (GPT-5.2), 78.5% (Sonnet 4.5), 41.4% (Gemini 2.5 Pro), 45.9% (GPT-4o)**.
- TTFT improved 13.0%, 22.9%, 6.1% and 30.9% respectively.
- Full-context caching regressed TTFT on GPT-4o (−8.8%) and on Gemini under exclude-tool-results (−2.9%).
- Savings scale with prompt size: 10–45% at 500 tokens, 54–89% at 50k tokens. They stay within about 10 points across 3–50 tool calls.
- Below the provider minimum (500 tokens), TTFT got 10–18% worse.
- Rules the paper recommends:
  - put dynamic content at the end of the system prompt
  - avoid dynamic traditional function calling
  - exclude dynamic tool results from cached blocks
- Source: https://arxiv.org/abs/2601.06007 — v1 2026-01-09, v2 2026-01-31 (PwC)

#### F13 — Production practice: KV-cache hit rate is the primary SLO; append-only, deterministic serialization, and tools as state (strong as practice, weak as measurement)
- Manus (2025-07-18) calls "the KV-cache hit rate … the single most important metric for a production-stage AI agent". Their numbers: input:output ≈ 100:1; Sonnet cached $0.30 vs uncached $3/MTok.
- Manus rules:
  - no timestamps at the start of the system prompt
  - append-only context
  - deterministic serialization
  - explicit breakpoints that include the end of the system prompt
  - session IDs for routing on vLLM
  - **mask tools rather than remove them**
- Anthropic's Claude Code team (Shihipar, 2026-04-30): "We run alerts on our prompt cache hit rate and declare SEVs if they're too low."
  - Put static content first and dynamic content last.
  - Use `<system-reminder>` in the next message instead of editing the system prompt.
  - Never switch models mid-session; use subagents instead.
  - Never add or remove tools; model state as tools (EnterPlanMode/ExitPlanMode), and use `defer_loading` stubs.
  - Cache-safe compaction forks reuse the parent's exact system, tools and context.
- **Opportunity:** Token Bill should ship hit-rate SLOs, alerts and a CI "prefix-stability" gate, not only after-the-fact reports.
- Sources:
  - https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus — 2025-07-18
  - https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ — 2026-04-30; redirects from claude.com/blog

#### F14 — Claude Code-specific invalidators (strong)
Actions that invalidate the cache:
- **Model switches:**
  - `/model`
  - `opusplan`, where each plan-mode toggle is a switch
  - automatic model fallback on Fable, Opus 5.5 and Opus 5
  - skill frontmatter `model:`
- **Effort changes.** These keep the cache only on Opus 5.5 / Fable 5.1 with an API key or subscription. They still invalidate on Bedrock, Vertex, the apps gateway, or with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`.
- **Fast mode:** the first turn with it on.
- **MCP servers connecting or disconnecting, or dynamic tool updates.** This only invalidates when tools are loaded into the prefix: tool search disabled, a custom `ANTHROPIC_BASE_URL` gateway, `alwaysLoad`, or threshold loading. It can happen silently when a stdio server exits or reconnects.
- **Plugins that provide MCP servers.**
- **Bare tool deny rules,** when tool search is off.
- **`/compact`.**
- **Image eviction** (in batches).
- **Upgrades**, which change the system prompt.

Actions that keep the cache: editing files; editing CLAUDE.md (it doesn't apply until `/clear`, `/compact` or restart); permission modes; output style; skills and commands; `/recap`; `/rewind`; subagents; forks.

Monitoring: `/usage` shows a "Prompt cache (main)" line with hit ratio, misses and "likely cause" (v2.1.260+).

- **Opportunity:** a Claude Code breaker taxonomy mapped to these causes, attributed per user and repo. This is the core of an enterprise Claude Code deployment.
- Source: https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23

#### F15 — Claude Code TTL defaults and fleet-wide policy (strong)
- With an **API key, usage credits or a cloud provider, the main conversation defaults to a 5-minute TTL**.
- With a subscription within plan usage it gets 1 hour. Subagents, workflows, forks and compaction default to 5 minutes.
- Controls:
  - `promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL`
  - `subagentPromptCacheTtl`
  - `ENABLE_PROMPT_CACHING_1H`
  - `FORCE_PROMPT_CACHING_5M`

  These can be pushed to every developer via **managed settings**.
- Through a gateway, the 1h request travels in the `anthropic-beta` header, which must be forwarded.
- The 1h TTL is not available via the Claude apps gateway.
- Verify with `usage.cache_creation.ephemeral_1h_input_tokens`.
- **Opportunity:**
  - Token Bill computes each org's gap histogram (F7/F18) and emits a ready-to-deploy managed-settings snippet with projected dollars.
  - It checks the gateway forwards the beta header.
  - Note: humans cause most >5-minute gaps. CacheWise found tool-initiated requests 20x more frequent than user-initiated ones (F25).
- Source: https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23

#### F16 — Gateways can silently disable caching (strong)
Claude Code docs describe three gateway behaviours:
1. The gateway **forwards markers**. Caching works normally.
2. The gateway **rejects with a 400** naming `cache_control`. Claude Code moves the marker to the last message; the block is billed uncached.
3. The gateway **removes the markers while returning success**. "your entire conversation history bills as uncached input on every turn". Converting block-form system content to a plain string drops the marker the same way.

Also: a custom `ANTHROPIC_BASE_URL` gateway disables tool search, so every MCP change invalidates the prefix.
- **Token Bill should build:**
  - a `gateway-strip` detector: a known harness that normally sends markers, zero cache activity across long sessions, and the base URL / gateway label in metadata
  - a `beta-header-dropped` detector: 1h TTL requested but only `ephemeral_5m` writes returned
  - a gateway conformance probe script
- Source: https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23

#### F17 — Nondeterministic tool and description serialization is common and very costly (moderate; multiple independent incidents)
- OpenAI Codex PR #2611 (opened 2025-08-23, merged 2025-08-25): "MCP servers are stored in a HashMap … the tool order changes across turns, effectively breaking prompt caching". One session's hit rate fell "below 1%". Fixed by sorting tools by name.
- Claude Code issue #49038 (2026-04-16, closed as not planned):
  - The Agent tool (tools[0]) listed sub-agent types in nondeterministic order, so every resumed session missed the whole prefix.
  - Measured: 56,370 → 32 cache-creation tokens after a deterministic sort.
- Anthropic's cache diagnostics names "tool `input_schema` JSON was serialized non-deterministically" as a cause of `tools_changed`.
- **Token Bill gap:** tool-churn detection compares tuple *order*. It should also catch:
  - a tool *description* that changes, for example an enumerated sub-agent list
  - schema key-order drift, which `canonical_json` sorting currently *hides*. The provider sees raw bytes, so Token Bill should diff raw bytes when the recorder has them.
- Sources:
  - https://github.com/openai/codex/pull/2611 — 2025-08-23/25
  - https://github.com/anthropics/claude-code/issues/49038 — 2026-04-16
  - https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics — accessed 2026-09-23

### D. TTL / keepalive economics

#### F18 — Keepalive economics: when re-sending the prefix during idle pays off (moderate; formula strong)
- Source: Khailo, arXiv 2607.19214 (submitted 2026-07-21).
- Keeping a prefix warm through an idle period I costs (I/τ + 1)·r per input token. Letting it expire costs w once.
- Break-even I_max = τ(w/r − 1): **≈46 min for Anthropic** (r=0.1, w=1.25, τ≈4 min) and ≈36 min for OpenAI and DeepSeek.
- The cheapest ping interval is just under the TTL (~4 min, not the common 30 s). A 30 s ping costs 8x more: ~$3.60/h vs ~$0.45/h for a 100k-token prefix.
- Measured at a 600 s idle: Anthropic baseline **0/48 warm vs 40/40 with keepalive**. The post-pause request cost up to **12.5x less**. DeepSeek held 42/42 vs 4/48. OpenAI and Google were "sticky" (baseline mostly warm).
- **Derived** with the same formula and current prices (τ = 240 s, w = 1.25):
  - Opus 5.5 (r = 0.05): I_max ≈ 96 min
  - Fable 5.1 (r = 0.025): I_max ≈ 196 min
- Anthropic's docs agree that for Fable 5.1 a `max_tokens:0` keepalive "is usually cheaper than the 1-hour TTL". The docs also say `max_tokens:0` is rejected with `stream:true`, structured outputs and Batches.
- Pre-warming with `max_tokens:0` returns no output tokens and bills the normal write. Only do it when latency is user-visible and there is a moment before traffic arrives.
- Sources:
  - https://arxiv.org/abs/2607.19214 and PDF — 2026-07-21
  - bundled `shared/prompt-caching.md` § Choosing the TTL / Pre-warming
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23

### E. Interplay with compression and context management

#### F19 — "Token Reduction Is Not Cost Reduction" (moderate; vendor-authored)
- Source: Weinberger & Hozez (PointFive), arXiv 2607.12161, July 2026. Method: 5,493 provider-billed executions, 7 repos, 3 model tiers, 103 tasks, with Claude Code as the baseline.
- Findings:
  - Cache creation + reads ≈ **87% of reconstructed four-component cost** (≈80% of the actual bill).
  - Tool outputs ≈ **3.3% of cost**, so the upper bound on visible-token compression savings is ≈5%.
  - End-to-end: RTK −2.7%; RTK-ML **+6.8%** despite **38.4% fewer delivered tool-output tokens**; Headroom proxy **+48.4%**.
  - Per-task correlation between token reduction and cost change: r = 0.154.
- Caveat: the authors' employer is a FinOps vendor.
- Source: https://arxiv.org/html/2607.12161 — July 2026 (v5)

#### F20 — Context editing, compaction, and history edits carry a cache cost (strong)
- Anthropic measured:
  - "On the 20-issue run they saved nothing, and context editing cost 74% more."
  - On the long run, "the prune saved 39% and compaction 32%, while context editing changed nothing."
- The bundled cost guide calls context editing "a context-window tool, not a savings lever": every clearing pass rewrites the cached conversation.
- Guidance:
  - Clear in a few large batches.
  - Prune at natural boundaries so each prune is one cold miss.
  - Prefer `/rewind`, which truncates to an already-cached prefix, over `/compact` mid-task.
- Leyline (arXiv 2606.01065, 2026-05-31) notes that production harnesses "fall back to re-prefill on every edit". Serving-side splicing lifted replay cache hits by +11.2 pp. This isn't available on commercial APIs.
- **Token Bill:** price each history-rewrite event as (tokens after the edit point) × (write − read) rates. Recommend batching or trigger thresholds.
- Sources:
  - https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md — accessed 2026-09-23
  - https://code.claude.com/docs/en/prompt-caching — accessed 2026-09-23
  - https://arxiv.org/abs/2606.01065 — 2026-05-31

#### F21 — Cache writes that are never read are pure surcharge (strong + moderate)
- Parsing the Stream (Salesforce, arXiv 2609.01466, 2026-09-01): "a cache breakpoint on a rebuilt monolithic prefix produced zero cache reads and cost more than no caching ($2.06 vs $2.03)" (512,521 write tokens at the 1.25x premium). The append-only form fell from $2.03 to $0.374 (5.4x).
- Anthropic docs: automatic caching on a prompt that ends in unique per-request content "pays the write premium on bytes that are never read back — a pure surcharge". The signature is writes on every request while reads never cover the full shared prefix.
- CAPC observed explicit markers adding ~1.1% write tax on small prefixes.
- OpenAI GPT-5.6 now charges 1.25x for writes and advises monitoring `cache_write_tokens` against later reads (F22).
- **Token Bill:** add an "unread-write premium $" metric: Σ unread writes × (write multiplier − 1) × input price. Add a `write-without-read` detector. Today the retrospective accounting hides this waste inside the optimal scenario instead of surfacing it as a finding.
- Sources:
  - https://arxiv.org/html/2609.01466v1 and PDF — 2026-09-01
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — accessed 2026-09-23
  - bundled `shared/prompt-caching.md` § Automatic vs explicit
  - https://arxiv.org/pdf/2607.15516 — 2026-07-17

### F. Multi-provider rules (an enterprise will not be Anthropic-only)

#### F22 — OpenAI (2026): explicit breakpoints, write premium, 30-minute TTL, routing keys (strong)
- **GPT-5.6 and later:**
  - `prompt_cache_options.mode` can be `implicit` (default: automatic breakpoint on the latest message plus any explicit ones) or `explicit`.
  - `prompt_cache_breakpoint` blocks; up to 4 per request (per the Bedrock-hosted GPT-5.6 docs).
  - "cache writes cost 1.25× the standard, uncached input-token rate"; reads 0.1x.
  - `prompt_cache_options.ttl` is `30m`, which is the only value and the default.
  - `usage.input_tokens_details.cached_tokens` and `cache_write_tokens`.
  - Minimum 1,024 visible tokens.
- **Earlier models:**
  - `cached_tokens` rounded down to multiples of 128.
  - `prompt_cache_retention` of `in_memory` ("around 5 to 10 minutes of inactivity, up to one hour") or `24h`. The default is `24h` for non-ZDR orgs.
- Routing hashes the initial tokens. "Traffic above 15 requests per minute can lead to overflow routing."
- `prompt_cache_key` improves stickiness. One customer went from a 60% to 87% hit rate. Responses API with `previous_response_id` gives "40-80% better cache utilization" than Chat Completions for reasoning models.
- Caches aren't shared across organizations or regional processing boundaries.
- Sources:
  - https://developers.openai.com/api/docs/guides/prompt-caching — accessed 2026-09-23
  - https://developers.openai.com/cookbook/examples/prompt_caching_201 — accessed 2026-09-23, undated
  - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html — accessed 2026-09-23

#### F23 — Gemini, Bedrock, DeepSeek: implicit best-effort caches, storage-priced explicit caches, platform-specific minimums (strong)
- **Gemini:**
  - Implicit caching is on by default for 2.5+ models.
  - Minimums: 2,048 tokens (2.5), 4,096 (3.x Flash / 3.1 Pro).
  - Explicit context caching bills **storage per token-hour**: 2.5 Pro caching $0.125/MTok (≤200k) + $4.50/MTok/h storage; 2.5 Flash $0.03 + $1.00/MTok/h; 3.x Flash $0.075 + $0.50/MTok/h through 2026-12-31.
  - The explicit-cache break-even therefore depends on reads per hour, not per TTL.
- **Bedrock:**
  - Implicit caching (best effort) plus explicit checkpoints, max 4. The minimum applies cumulatively across tools+system+messages.
  - 1h TTL on current Claude models.
  - "Cross-region inference … may lead to increased cache writes."
  - Caching isn't supported with batch inference.
  - The Bedrock table lists **Opus 4.7 at 4,096 tokens/checkpoint**, while Anthropic's docs say 2,048 on every platform. Unresolved.
  - Usage fields: `cacheReadInputTokens` / `cacheWriteInputTokens` / `cacheDetails`.
- **DeepSeek:**
  - Disk cache, on by default and best effort.
  - A hit requires a full match of a "cache prefix unit".
  - Construction takes seconds; unused entries are cleared in hours to days.
  - `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
- Sources:
  - https://ai.google.dev/gemini-api/docs/caching — last updated 2026-09-02
  - https://ai.google.dev/gemini-api/docs/pricing — updated 2026-09-23
  - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html — accessed 2026-09-23
  - https://api-docs.deepseek.com/guides/kv_cache — accessed 2026-09-23

### G. Enterprise topology: isolation vs reuse

#### F24 — Gateways and credential pooling collapse cache isolation; the fix costs ~2% (moderate→strong)
- **KeyPooling** (JHU/CUHK, arXiv 2608.17485 v2, 2026-08-23):
  - Five open-source gateways were tested against OpenAI and Anthropic. **None bound customers to upstream credentials by default.** Under a shared credential, all five exposed cross-customer cache reads.
  - OpenRouter: cross-account reads for 12 of 28 labels carrying 33.7% of tested volume.
  - "All 17 public Claude Code, Cursor, and OpenCode prefixes … exceed 1,024 tokens", so agent traffic clears cache minimums before private content begins.
  - Defense: a namespace derived from the authenticated identity, placed *after* reusable public prefixes, "preserved most modeled reuse at a 1.7–2.5% cost increase."
- **CacheProbe** (Northeastern, arXiv 2605.30613, 2026-05-28): OpenRouter's default shared-credential mode showed cross-account hits (Groq, Fireworks, OpenAI 4.8%). BYOK restored isolation.
- For an enterprise, cross-developer reuse inside one workspace is where the savings come from. It is also where timing-based leakage between teams would happen (F10).
- **Token Bill:**
  - produce a report on isolation boundaries vs cache reuse: workspaces, keys, gateway namespaces
  - price the cost of each split
  - recommend "shared public prefix, namespaced private suffix"
- Sources:
  - https://arxiv.org/pdf/2608.17485 — v2 2026-08-23
  - https://arxiv.org/html/2605.30613v1 — 2026-05-28

### H. Serving side (self-hosted fleets and how providers behave)

#### F25 — Coding-agent traffic patterns and cache-aware scheduling (moderate→strong)
- **CacheWise** (UW/UVA, arXiv 2606.16824, 2026-06-15), on real **Claude Code** traces:
  - Tool-completion requests are **20x more frequent than user-initiated** requests at the median.
  - Session length: **36 min median**, >2.6 h at the tail.
  - About **21x higher prefill:decode ratio** than chat.
  - Tool durations are long-tailed and predictable from tool type/args (ls 49 ms; pytest 83 s; WebFetch 40 s).
  - Reuse-aware eviction gave 2–2.6x fewer evictions and up to 3.5x faster sessions.
- **Continuum** (arXiv 2511.02230, v7 2026-09-08): KV "time-to-live" pinning across tool calls, >8x average job completion time on SWE-Bench/BFCL/OpenHands.
- **KVFlow** (arXiv 2507.07400, 2025-07-10): workflow-aware eviction, 1.83–2.19x.
- **Preble** (arXiv 2407.00023): distributed prompt-sharing scheduling, 1.5–14.5x average latency.
- **llm-d** (2025-09-24): precise prefix-cache-aware routing, P90 TTFT 0.542 s vs 31 s approximate (57x), 2x throughput.
- **SGLang RadixAttention** (arXiv 2312.07104): up to 6.4x throughput.
- **Token Bill:**
  - For self-hosted fleets, ingest `vllm:prefix_cache_queries` / `vllm:prefix_cache_hits` and `kv_block_reuse_gap_seconds`.
  - Audit session-affinity routing.
  - For API users, the tool-call gap distribution (mostly seconds) and human gaps (minutes) calibrate the TTL model.
- Sources:
  - https://arxiv.org/abs/2606.16824 — 2026-06-15
  - https://arxiv.org/abs/2511.02230 — 2025-11-04 / v7 2026-09-08
  - https://arxiv.org/abs/2507.07400 — 2025-07-10
  - https://arxiv.org/abs/2407.00023 — 2024-05-08 / rev 2024-10-03
  - https://llm-d.ai/blog/kvcache-wins-you-can-see — 2025-09-24
  - https://arxiv.org/abs/2312.07104 — v2 2024-06-06
  - https://docs.vllm.ai/en/latest/design/metrics.html — accessed 2026-09-23
  - https://docs.vllm.ai/en/latest/design/prefix_caching.html — accessed 2026-09-23

#### F26 — Reuse beyond exact prefixes exists in research but not on commercial APIs (strong)
- **Prompt Cache** (Gim et al., MLSys 2024): modular, schema-defined reuse of attention states, 8x (GPU) / 60x (CPU) TTFT.
- **CacheBlend** (arXiv 2405.16444): reuses non-prefix chunk KV with selective recompute, 2.2–3.3x TTFT, 2.8–5x throughput.
- **EPIC** (arXiv 2410.15332): position-independent caching, up to 8x TTFT / 7x throughput.
- **CacheGen** (SIGCOMM'24): KV compression, 3.5–4.3x smaller.
- **RAGCache** (arXiv 2404.12457): up to 4x TTFT.
- vLLM's hash includes the parent block hash, so it is strictly prefix-chained. It supports `cache_salt` for isolation.
- Commercial APIs (Anthropic, OpenAI, Gemini, Bedrock, DeepSeek) all document exact-prefix matching.
- **Token Bill rules:**
  1. Never credit non-prefix reuse on API traces.
  2. For RAG and document agents, detect **retrieved-chunk order churn**: the same documents in a different order is a cache miss. Recommend a stable canonical ordering.
  3. Optionally add a self-host counterfactual.
- Sources:
  - https://arxiv.org/abs/2311.04934 — 2023-11-07 / rev 2024-04-25
  - https://arxiv.org/abs/2405.16444 — v3 2025-04-03
  - https://arxiv.org/abs/2410.15332 — rev 2025-05-27
  - https://arxiv.org/abs/2310.07240 — rev 2024-07-19
  - https://arxiv.org/abs/2404.12457 — 2024-04-18
  - https://docs.vllm.ai/en/latest/design/prefix_caching.html — accessed 2026-09-23

### I. Semantic caching, response caching, plan caching

#### F27 — Semantic caching: real risks, and low value for agents (strong on the risks)
- **GPTCache** (NLP-OSS 2023) reports 2–10x speedups on hits.
- **vCache** (ICLR 2026): "static thresholds do not give formal correctness guarantees, result in unexpected error rates". Per-prompt learned thresholds gave up to 12.5x higher hit rate and 26x lower error.
- **Category-aware caching** (arXiv 2510.26835): high-repetition categories hit 40–60%, volatile ones 5–15%. Code queries cluster densely, so fixed thresholds produce false positives.
- **Key-collision attacks** (arXiv 2601.23088): 86% hit / 81% injection success against GPTCache-style caches. Agent tool-invocation hijacking reached a 90.6% hit rate. Defenses: salting (hit rate −10.8 to −21.0 pp), per-user isolation.
- **LaCache** (arXiv 2608.01718): response-side verification cut attack hit rate from 0.95 to 0.01 while keeping 0.93 benign hits.
- **Redis/NYU** (EMNLP 2026 industry): PR-AUC-based model selection "leads to systematically poor deployment choices". The paper cites ~33% of chat logs being repeated or semantically similar.
- **Plan caching** (APC, NeurIPS 2025): reusing plan templates across similar agent tasks cut cost 50.31% and latency 27.28% on average. It works because agent outputs depend on environment state, which defeats whole-response caching.
- **Token Bill:**
  - Measure exact-duplicate request share and near-duplicate *task* share across the fleet.
  - Recommend exact caching for deterministic calls (classifiers, embeddings) and plan or verified caches for repeated agent tasks.
  - Warn against static-threshold semantic caches on code or agent traffic.
- Sources:
  - https://aclanthology.org/2023.nlposs-1.24/ — Dec 2023
  - https://arxiv.org/abs/2502.03771 — v5 2026-02-21
  - https://arxiv.org/abs/2510.26835 — 2025-10-29
  - https://arxiv.org/html/2601.23088v1 — 2026-01-30
  - https://arxiv.org/html/2608.01718v1 — 2026-08-03
  - https://arxiv.org/pdf/2606.19719 — v4 2026-09-01
  - https://arxiv.org/abs/2506.14852 — v2 2026-01-26

#### F28 — Batch and cache discounts stack, but fan-out cache hits inside a batch are best effort (strong)
- The Batch API is 50% off every token, cache reads and writes included. The discounts stack.
- Cache hits inside a concurrent batch are best effort.
- Bedrock doesn't support prompt caching with batch inference.
- OpenAI's cookbook reports Flex processing gave "an 8.5% increase in cache hit rate compared to the Batch job", which translated into a 23% reduction in input cost.
- **Token Bill:** add a "batchable + cacheable" counterfactual that models the concurrent-write effect (F4), not a naive 50% × cached price.
- Sources:
  - https://platform.claude.com/docs/en/about-claude/pricing.md — accessed 2026-09-23
  - bundled `shared/cost-optimization.md` § 2.5
  - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
  - https://developers.openai.com/cookbook/examples/prompt_caching_201

---

## 2. Rule table for the simulator (current status vs needed)

| # | Rule | Source | Token Bill v0.1.2 | Change |
|---|---|---|---|---|
| R1 | Exact prefix match, tools→system→messages | Anthropic docs | yes (canonical JSON) | Also diff **raw** bytes when the recorder has them; canonical sorting hides key-order drift (F17) |
| R2 | Per-model caches | docs | yes | ok |
| R3 | 5m TTL, sliding refresh on read | docs | yes (300 s) | Measure TTL from **request start**; add `ts_start`, `ts_first_token`, `ts_end` to the trace (F2) |
| R4 | 1h TTL at 2x write; mixed-TTL ordering; A/B/C billing positions | docs | no | Add TTL per breakpoint and the `cache_creation` 5m/1h split. Add scenarios `ttl-1h` and `keepalive` (F2, F18) |
| R5 | Min cacheable prefix per model and platform | docs, Bedrock | partial | Add a platform dimension (Bedrock Opus 4.7 = 4,096?). Use a per-tokenizer-family chars/token ratio (4.7+ ≈1.3x more tokens) (F6, F23) |
| R6 | ≤4 breakpoints, 20-position lookback, tool_use/tool_result runs = 1 position | docs | no | Add a `realistic-cache` scenario that replays the **observed** breakpoint positions with the lookback (F3) |
| R7 | Entry readable only after the first response starts streaming | docs | no | Gate reads on the writer's `ts_first_token`. Add a fan-out detector (F4) |
| R8 | Invalidation hierarchy for thinking, effort, tool_choice, disable_parallel_tool_use, images, speed, web search/citations | docs | no | Record these parameters in the trace and add tier-aware invalidation (F1) |
| R9 | Thinking-block stripping on older models, and dropped thinking blocks across models | docs | no | `thinking-strip` breaker (F1, F9) |
| R10 | Workspace-scoped (1P/AWS/Foundry) or org-scoped (Bedrock/Vertex) caches, shared across sessions | docs | no (per-run only) | **Fleet replay** keyed by (workspace, model) across runs (F5) |
| R11 | Cache read multipliers 0.1 / 0.05 (Opus 5.5) / 0.025 (Fable 5.1); batch 0.5x; geo 1.1x; fast mode | pricing | partial | Update the pricing table and add a modifier stack (F6) |
| R12 | Real hit rate < 1 for small prefixes; multi-server propagation; implicit tools caching | CAPC, Gu | no | Add a `calibrated` scenario with ρ(prefix size, reads, gap) fitted from billed traces (F10, F11) |
| R13 | Server tools auto-insert 5m breakpoints | docs | no | Don't flag the resulting writes as breakers (F2) |
| R14 | OpenAI: implicit/explicit modes, 1.25x writes (5.6+), 30m TTL, 128-token increments on older models, 15 RPM/key overflow | OpenAI docs | no | Provider adapter plus rule set (F22) |
| R15 | Gemini explicit caches billed per token-hour of storage; implicit best effort | Google docs | no | Storage-aware break-even (F23) |

## 3. New breaker and detector catalogue (proposed)

| Detector | Signal | Fix text | Evidence |
|---|---|---|---|
| `param-churn` | thinking, effort, tool_choice, speed, image or web-search toggle between consecutive calls, with a read collapse | Pin per route. Use per-message effort / mid-conversation system messages where supported | F1 |
| `thinking-strip` | Haiku 4.5 / older model, thinking on, plain user message after tool use, write spike | Keep tool_result turns, or move to 4.6+/Opus 4.5+ | F1, F9 |
| `lookback-overflow` | >20 non-collapsed positions appended since the last written entry; byte-identical overlap; write ≈ whole conversation | Add an intermediate breakpoint every ~15 positions | F3 |
| `cold-fanout` | ≥2 same-prefix requests starting before the first one's first token | Stagger: fire 1, await first token, then N−1 (Claude Code holds 5 s) | F4 |
| `ttl-expiry` | Diagnostics null / bytes identical, gap > TTL, full rewrite | 1h TTL if >1/20 gaps are 5–60 min; keepalive if r is small (Fable 5.1, Opus 5.5) | F7, F8, F18 |
| `write-without-read` | Writes never read before expiry (unique tails, final turns, short subagents) | Move the breakpoint to the end of the shared portion; switch automatic → explicit | F21 |
| `gateway-strip` | Known harness, zero cache activity across long sessions, gateway base URL | Configure the gateway to forward `cache_control` and `anthropic-beta` | F16 |
| `beta-header-dropped` | 1h TTL configured but only `ephemeral_5m` writes | Forward the header | F15, F16 |
| `tool-desc-nondeterminism` | Tools segment differs only in description or enum order | Sort sub-agent / MCP lists deterministically | F17 |
| `mcp-reconnect` | tools_changed with an MCP server set delta; tool search off | Enable tool search / defer_loading; avoid custom base URL gateways | F14 |
| `workspace-fragmentation` | Same tools+system prefix hash written in ≥2 workspaces or keys within one TTL window | Consolidate workspaces, or accept the isolation cost (priced) | F5, F24 |
| `per-machine-prefix` | Claude Code / Agent SDK prefixes differ only in cwd, OS or git-status sections | Agent SDK: suppress per-machine sections; standardize the dev env | F5 |
| `compression-breaks-cache` | A compression or proxy layer mutates the cached region each call | Query-agnostic compression before the breakpoint; keep the rest append-only | F11, F19 |
| `history-edit-cost` | Context-edit or compaction event; price = tokens after the edit × (write − read) | Batch edits; raise the trigger; prefer rewind/prune at boundaries | F20 |
| `rag-order-churn` | Same retrieved docs in a different order | Canonical ordering (e.g., doc-id) before the breakpoint | F26 |
| `model-switch` (extend) | Include `opusplan`, fallback, skill-frontmatter model, fast-mode first turn | Use subagents for other models | F14 |

## 4. What Token Bill should build (prioritized)

**P0: correctness and credibility (makes the numbers enterprise-grade)**
1. **Trace schema v2.** Add:
   - `ts_start`, `ts_first_token`, `ts_end`
   - `provider`/`platform`, `workspace_id`, `api_key_id`, `gateway`
   - `thinking`, `effort`, `tool_choice`, `disable_parallel_tool_use`, `speed`, `inference_geo`, `service_tier` (batch/priority)
   - per-breakpoint `{position, ttl}`
   - `usage.cache_creation.{ephemeral_5m,ephemeral_1h}`
   - raw request bytes hash (in addition to canonical)
   - `diagnostics.cache_miss_reason`

   Recorder: send the `cache-diagnosis-2026-04-07` header by default on first-party traffic. (F1, F2, F8)
2. **Pricing and rules module refresh.**
   - Add Opus 5.5, Mythos 5.1, Opus 4.5, Sonnet 4.5.
   - Add the 1h write multiplier and a modifier stack: batch 0.5x, geo 1.1x, fast mode.
   - Add per-platform minimums and per-tokenizer chars/token.
   - Version it as data with source URLs and a verification date, plus a CI job that re-fetches the pricing page and fails on drift. (F6)
3. **Replay engine v2**, with three honest scenarios:
   - `documented-optimum`: today's optimal, plus the TTL-from-start rule
   - `realistic`: observed breakpoints, lookback, concurrency gating, invalidation hierarchy
   - `calibrated`: ρ fitted per (model, prefix-size bucket, gap bucket) from the customer's own billed reads

   Always report billed-vs-simulated agreement per bucket. (F3, F4, F11)
4. **Fleet mode.** Replay across all runs in a workspace, keyed by (workspace, model, prefix hash). Report:
   - shared-prefix reuse
   - fragmentation loss from workspaces, keys and gateways
   - per-machine prefix loss

   This is the thousand-developer view no per-run tool gives. (F5, F24)

**P1: the biggest dollar levers**

5. **TTL/keepalive optimizer.**
   - Input: per user/harness start-to-start gap histograms.
   - Output: the policy (5m / 1h / keepalive at τ ≈ TTL − margin, bounded by I_max = τ(w/r−1)) and projected $.
   - For Claude Code, also emit a managed-settings snippet (`promptCacheTtl`, `subagentPromptCacheTtl`). (F7, F15, F18)
6. **Breaker catalogue v2** (Section 3). Each breaker carries a fix, $ recovered, and a confidence label. On 1P traffic, confirm with provider diagnostics.
7. **Claude Code enterprise connector.**
   - Ingest OTel `claude_code.token.usage` (type cacheRead/cacheCreation, `query_source` main/subagent/auxiliary, `effort`, `speed`, `mcp_server.name`, `skill.name`) and `claude_code.api_request` events.
   - Ingest the Admin Usage API (1m/1h/1d buckets, group_by workspace, api_key, model, speed, inference_geo, context window) and the Claude Code Analytics API.
   - Build per-user/repo/team hit-rate leaderboards against the Anthropic fleet benchmark (p50 84%, p90 ≥94%).
   - Sources: https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23); https://platform.claude.com/docs/en/manage-claude/usage-cost-api.md (accessed 2026-09-23).
8. **Gateway conformance probe.** A scripted two-request probe per gateway route that checks:
   - `cache_control` is forwarded
   - the `anthropic-beta` header is forwarded
   - block-form system survives
   - reads > 0

   Plus the `gateway-strip` detector. (F16)

**P2: moat features**

9. **Cache-hit-rate SLOs and alerts** (Claude Code's own practice), and a **CI prefix-stability gate**: render the prompt twice from the same state, diff raw bytes, fail on drift; optionally a paid 2-request cache probe. (F13, F17)
10. **Net-of-cache optimization evaluator.** For any proposed change (compression, context editing, proxy, model routing), run a paired counterfactual through the replay engine and report *billed* $ change, not tokens removed. Include the CAPC crossover calculator ρ_cross(r) = (α − 1/r)/(α − β). (F11, F19, F20)
11. **Multi-provider adapters:** OpenAI (implicit/explicit, 30m, 1.25x writes, `prompt_cache_key`, 15 RPM overflow), Gemini (implicit; explicit with storage-hour billing), Bedrock (platform minimums; cross-region write inflation), DeepSeek, and vLLM/SGLang metrics for self-hosted fleets. (F22, F23, F25)
12. **Isolation vs reuse report** for security review: current boundaries (org / workspace / key / gateway namespace), timing-side-channel exposure, and the priced cost of recommended splits (KeyPooling: ~1.7–2.5%). (F10, F24)
13. **Duplicate and plan-reuse sizing:** exact-duplicate call share, near-duplicate task share, APC-style plan-cache opportunity. Explicit warnings against static-threshold semantic caching on agent and code traffic. (F27)

**Validation plan (to earn enterprise trust)**
- Reproduce three published results as regression fixtures:
  1. Anthropic's 25-token-status-line case: $0.59 → $4.24.
  2. "Don't Break the Cache" strategy ranking.
  3. CAPC's small-prefix ρ plateau (via the calibrated scenario).
- Track the agreement ratio between billed and simulated reads per bucket on every customer trace. Refuse to print recovered-$ claims where agreement < a threshold.

---

## 5. Open questions

1. **Small-prefix hit rate on current models.** Does the ρ<1 plateau CAPC measured on Sonnet 4.6 (May 2026) hold for Opus 5 / 5.5 / Sonnet 5 at the new 512-token minimums? It needs calibration on the enterprise's own traces.
2. **"Implicit tools caching."** Cache reads with no cache_control on large tool arrays were seen in one study only. It could be undocumented provider behaviour or a harness artifact. Confirm before modelling.
3. **Opus 4.7 minimum on Bedrock.** Bedrock's table says 4,096 tokens; Anthropic's docs say 2,048 on every platform. Which does billing follow?
4. **Trace timestamps.** Does the enterprise's trace source record request start, first token and end? Without them, TTL-from-start and concurrency rules can only be approximated from `duration_ms` (OTel has it).
5. **Claude Code access path.** Does the enterprise use subscriptions (1h default for the main conversation) or API keys / Bedrock / Vertex (5m default)? This decides the size of the TTL lever.
6. **Gateway topology.** Is an LLM gateway (LiteLLM, Portkey, internal proxy) in the path? Does it forward `cache_control`, block-form system and `anthropic-beta`? Does it disable tool search?
7. **Workspace layout.** How many workspaces and API keys split the same harness traffic? What is the security team's stance on cross-team cache sharing within a workspace, given timing-side-channel research?
8. **OpenAI retention.** How do OpenAI's `24h` default retention and the GPT-5.6 `30m` TTL interact in practice? Official statements are terse. Measure with keepalive-style probes.
9. **Cache diagnostics coverage.** Will diagnostics become available on Bedrock, Vertex and Foundry? Until then, breaker ground truth exists only for first-party API traffic.
10. **Measured semantic-cache opportunity.** What are the real exact-duplicate and near-duplicate rates in enterprise coding-agent traffic? Likely low; measure before recommending.

---

## 6. Source list (opened during this research)

**Papers**
- Gu, Li, Kuditipudi, Liang, Hashimoto. Auditing Prompt Caching in Language Model APIs. ICML 2025. https://arxiv.org/abs/2502.07776 (v1 2025-02-11; v2 2025-07-13); https://proceedings.mlr.press/v267/gu25b.html
- Lumer et al. (PwC). Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks. https://arxiv.org/abs/2601.06007 (v1 2026-01-09; v2 2026-01-31)
- Song (PayPal). Cache-Aware Prompt Compression: A Two-Tier Cost Model for LLM API Caching. https://arxiv.org/pdf/2607.15516 (2026-07-17)
- Khailo. Keeping the Cache Warm Pays: Keepalive Economics for Agentic Workloads. https://arxiv.org/abs/2607.19214 (2026-07-21)
- Weinberger, Hozez (PointFive). Token Reduction Is Not Cost Reduction. https://arxiv.org/html/2607.12161 (July 2026, v5)
- Sun et al. KeyPooling: Measuring Where LLM API Relay Paths Collapse Prompt Cache Isolation. https://arxiv.org/pdf/2608.17485 (v2 2026-08-23)
- Fahey. CacheProbe: Auditing Prompt Cache Isolation in Gateway APIs. https://arxiv.org/html/2605.30613v1 (2026-05-28)
- Pakhomov, Nijkamp (Salesforce). Parsing the Stream: A Live Trace Model for Long-Horizon Agents. https://arxiv.org/html/2609.01466v1 (2026-09-01)
- Ma, Eitzinger, Köstler. Leyline: KV Cache Directives for Agentic Inference. https://arxiv.org/abs/2606.01065 (2026-05-31)
- Tiwari et al. CacheWise: … Serving LLM Coding Agents. https://arxiv.org/abs/2606.16824 (2026-06-15)
- Li et al. Continuum: Multi-Turn LLM Agent Scheduling with KV Cache Time-to-Live. https://arxiv.org/abs/2511.02230 (2025-11-04; v7 2026-09-08)
- Pan et al. KVFlow. https://arxiv.org/abs/2507.07400 (2025-07-10)
- Srivatsa et al. Preble. https://arxiv.org/abs/2407.00023 (2024-05-08; rev 2024-10-03)
- Zheng et al. SGLang / RadixAttention. https://arxiv.org/abs/2312.07104 (v2 2024-06-06)
- Gim et al. Prompt Cache: Modular Attention Reuse. MLSys 2024. https://arxiv.org/abs/2311.04934 (2023-11-07; rev 2024-04-25)
- Yao et al. CacheBlend. https://arxiv.org/abs/2405.16444 (v3 2025-04-03)
- Liu et al. CacheGen. SIGCOMM'24. https://arxiv.org/abs/2310.07240 (rev 2024-07-19)
- Hu et al. EPIC. https://arxiv.org/abs/2410.15332 (rev 2025-05-27)
- Jin et al. RAGCache. https://arxiv.org/abs/2404.12457 (2024-04-18)
- Schroeder et al. vCache: Verified Semantic Prompt Caching. ICLR 2026. https://arxiv.org/abs/2502.03771 (v5 2026-02-21)
- Bang. GPTCache. NLP-OSS 2023. https://aclanthology.org/2023.nlposs-1.24/ (Dec 2023)
- Wang et al. Category-Aware Semantic Caching. https://arxiv.org/abs/2510.26835 (2025-10-29)
- Zhang et al. Key Collision Attack on LLM Semantic Caching. https://arxiv.org/html/2601.23088v1 (2026-01-30)
- Liang et al. LaCache. https://arxiv.org/html/2608.01718v1 (2026-08-03)
- Baral et al. Closing the Operational Gap in Semantic Caching. EMNLP 2026 Industry. https://arxiv.org/pdf/2606.19719 (v4 2026-09-01)
- Zhang et al. Agentic Plan Caching. NeurIPS 2025. https://arxiv.org/abs/2506.14852 (v2 2026-01-26)

**Provider documentation (accessed 2026-09-23 unless dated)**
- Anthropic prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing.md
- Anthropic cache diagnostics: https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- Anthropic tool use with prompt caching: https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching
- Anthropic optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
- Anthropic Usage & Cost Admin API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api.md
- Claude Code: How Claude Code uses prompt caching: https://code.claude.com/docs/en/prompt-caching
- Claude Code monitoring (OTel): https://code.claude.com/docs/en/monitoring-usage
- Claude blog, Lessons from building Claude Code: Prompt caching is everything (2026-04-30): https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/
- OpenAI prompt caching guide: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI Prompt Caching 201 cookbook: https://developers.openai.com/cookbook/examples/prompt_caching_201
- Google Gemini caching (updated 2026-09-02): https://ai.google.dev/gemini-api/docs/caching ; pricing (updated 2026-09-23): https://ai.google.dev/gemini-api/docs/pricing
- AWS Bedrock prompt caching: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
- DeepSeek context caching: https://api-docs.deepseek.com/guides/kv_cache
- vLLM prefix caching design: https://docs.vllm.ai/en/latest/design/prefix_caching.html ; metrics: https://docs.vllm.ai/en/latest/design/metrics.html
- Bundled Anthropic claude-api skill v2.1.280 (local): `shared/prompt-caching.md`, `shared/cost-optimization.md`, `shared/agent-design.md`, `shared/tool-use-concepts.md`

**Engineering and practice**
- Manus, Context Engineering for AI Agents (2025-07-18): https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- llm-d, KV-Cache Wins You Can See (2025-09-24): https://llm-d.ai/blog/kvcache-wins-you-can-see
- openai/codex PR #2611 (2025-08-23/25): https://github.com/openai/codex/pull/2611
- anthropics/claude-code issue #49038 (2026-04-16): https://github.com/anthropics/claude-code/issues/49038
