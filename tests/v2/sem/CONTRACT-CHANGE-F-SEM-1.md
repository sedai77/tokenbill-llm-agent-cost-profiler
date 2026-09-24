# CONTRACT-CHANGE-F-SEM-1 — clarifications, one verified-fact correction, additive names

Raised by F-SEM (wave 1) under SPEC §21 #3. Nothing here renames or removes a SPEC field or
signature; the code implements the current contract plus the interpretations below. The contract
owner decides whether to fold them into SPEC Appendix E.

## 1. Fact correction (implemented): per-message effort beta is not on Foundry

SPEC §3.14 / D28 (b) lists channels `{"anthropic_api", "claude_platform_aws", "foundry", "vertex"}`
for `PER_MESSAGE_EFFORT_BETA`. The primary source (Anthropic, *Mid-conversation system messages*,
<https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages>, checked
2026-09-23) says per-message effort is "available on Claude API and Google Cloud only (not Amazon
Bedrock or Microsoft Foundry)"; beta header `mid-conversation-output-config-2026-07-01` and the model
list (Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5) match the SPEC.

Implemented: `core.cache_rules.PER_MESSAGE_EFFORT_CHANNELS = {"anthropic_api", "claude_platform_aws",
"vertex"}` (Foundry removed; Claude Platform on AWS kept because it is served by the Claude API and
the source does not exclude it — unverified). Appendix A.13 is unaffected (all its rows use
`anthropic_api`). Proposed SPEC text: "(b) … and channel in {anthropic_api, claude_platform_aws,
vertex}".

Related, not implemented (would diverge from the block engine's tier salts): the Claude Code docs
say that in Claude Code only the *first* fast-mode enable of a conversation breaks the cache (later
toggles vary a non-key speed setting). §3.15 rule 4 counts every served-speed change as
`fast-toggle`; a refinement for `agent_product == "claude_code"` would need the same change in
`sim.block_replay`.

## 2. Policy grammar (§9.5) interpretations

- **`Policy.name`**: the grammar has no name clause, so `parse_policy` names a policy by its
  canonical spec (`"observed"` for the empty policy) and `combine` does the same. Hence
  `parse_policy(to_spec(p)) == p` holds for every *canonical* policy (name = canonical spec,
  canonical selectors, normalized scales), which is what the hypothesis test generates;
  `to_spec(parse_policy(to_spec(p))) == to_spec(p)` holds for every expressible policy.
- **Defaults of omitted options**: `keepalive … max=` → 3600 s (`KEEPALIVE_MAX_IDLE_S`),
  `cold-resume … min=` → 200000 (§9.3.4), `effort … scale=` → `0.5` (the value shown in the
  grammar), `compact-window … post=` absent → `None` (org median). Canonical output prints every
  option except an absent `post`, e.g. `effort=high` → `effort=high,scale=0.5`.
- **Durations** accept `s`, `m`, `h` or a bare number of seconds; canonical form is seconds with
  `s`. Every count/duration is an int in `(0, 2**53]`.
- **Block-level repairs**: §9.7 routes "repairs prefixed `block:`" to the block engine, so
  `repair=block:<id>` (`[a-z0-9][a-z0-9_-]{0,63}`) is accepted besides the five usage-level ids.
- **Selectors**: a repeated key inside one selector (`team:a,team:b`) is a `UsageError` (AND of the
  same key is empty or redundant). Free values (team, agent type, agent product, model) may contain
  spaces and `:` but not `; , @ =` or control characters. `billing_path` values are checked
  against `core.records.BILLING_PATHS` at call time (so additive paths, R-E4, work without a code
  change). `model:` terms compare normalized model ids.
- **Conflicts**: the same selector with two different values for `ttl`, `model` or `effort`, or
  two different scalar clauses, is a `UsageError` in `parse_policy` and a `ContractViolation` in
  `combine`; identical repeats are de-duplicated. `parse_policy("observed")` is the observed policy.
- `to_spec` raises `ContractViolation` for a `Policy` the grammar cannot express.

## 3. Shards (§3.21)

- **Unattributed shard filter**: `shard_where(ShardKey(team=None, …))` returns `{"team": ""}`
  (`core.shards.UNATTRIBUTED_TEAM`). `LedgerStore.iter_lanes(where=…)` has no NULL encoding, so
  stores (MemoryStore, SqliteStore) must read `team == ""` as "team IS NULL". An empty team string
  is treated as unattributed everywhere in F-SEM (`plan_shards`, `shard_of_lanes`, `cohort_key`).
- `merge_findings` concatenates and then sorts in the `run_detectors` order
  (`−recoverable point, detector_id, finding_id`), so the merged list does not depend on shard
  order or size.
- `merge_replay` concatenates `per_lane`, `lanes_skipped` and `outcomes` in part order
  (`outcomes` is None if any part dropped them), de-duplicates assumptions in first-seen order and
  combines calibration (UNCALIBRATED dominates).

## 4. Conventions (§3.11)

- **Quarantine signal**: malformed usage raises `core.conventions.BadUsageError`, a subclass of
  `UsageError`, message `"bad_usage: <field>"`.
- **Iterations invariant, "other shapes"**: §19.2 says the top-level usage excludes compaction and
  advisor iterations, while §3.11 says "Σ elements == top-level". Both are accepted: Σ of all
  elements **or** Σ of the MESSAGE elements equals the top level. Comparison uses uncached, read,
  total-write and output tokens (the TTL split and the reasoning subset are ignored).
- An absent `cache_creation_input_tokens` means "the split is the total" (no negative-residual
  note); top-level `server_tool_use` counts that no element reports are attached to the serving
  inference (otherwise web-search requests would be lost when the top level is not priced).
- **Advisor without a model**: an `Inference` has no field for an unpriced reason, so the advisor
  inference gets `model = model_raw = ""` (unpriced by every pricer) and the note
  `dq.unpriced_model`.
- An unknown `iterations[].type` becomes an OTHER inference (billable).

## 5. Transitions (§3.15)

- `Transition.index` is the position among the lane's requests **that have a serving inference**
  (the `i` of §3.15); join on `request_id` when iterating all requests.
- "Differs" for effort, thinking, client version and cwd key means both requests report a value and
  the values differ (`None` = not observed, never a change). Served speed is always known.
- Availability fallback: `status == 529` or `error_type in {"overloaded", "overloaded_error"}` in
  the window, plus a MODEL_FALLBACK event with trigger `availability` in the window. Plan toggle:
  the switch itself is between the Opus and Sonnet families.
- `classify_transitions` does not need `rules` or `static_prefix_floor`; both are accepted for
  symmetry. An unknown `min_cacheable_tokens` counts as 0 in the prediction.
- `static_prefix_floor` with an even count uses `floor((a + b) / 2)` of the two middle values.

## 6. Shapley and findings helpers

- `shapley_exact` / `shapley_mc` make credits sum to `value(all) − value(∅)` (= `value(all)` for
  savings games). `scale_credits` splits the target equally when the credits sum to 0; an empty
  mapping scales only to 0.
- `build_finding` also enforces: every figure on one basis; `PROVIDER_ESTIMATE` only on
  data-quality findings (R4), in any cohort (a provider estimate is neither billed nor
  list-equivalent, so the D26 rule below does not apply to it); otherwise `LIST_EQUIVALENT` exactly
  when the scope carries `billing_class=allowance` (D26, and it keeps allowance and billed finding
  ids distinct); the SPEC value sets of category, lever_class, audience and confidence;
  evidence ≤ 20.
- `top_evidence` magnitude = the first int among the `magnitude`, `nano`, `tokens` attrs, else 0.
- `rate_nano` raises `PricingError` for an unpriceable context (unknown is never zero);
  `cache_write_unknown` is priced at the point rate (hint, else 5m) through `price_usage`.
- `fit_cpt(lanes, family)` expects the lanes of one tokenizer family (the caller partitions with
  `Pricer.tokenizer_family`); the sample definition is in its docstring and the README.

## 6a. Robustness and determinism (adversarial review)

- `effort_change_keeps_cache(betas=…)` matches beta values exactly: a raw `anthropic-beta` header
  string is split on commas (never a substring test) and `None` counts as no betas.
- `parse_version` returns None (unknown) when a numeric component has more than 18 digits; a
  model id whose `gpt-` version is not a short dotted number has no OpenAI TTL option. Client
  versions and model ids come from transcripts and provider payloads, and `int()` of a digit run
  above `sys.get_int_max_str_digits()` raises `ValueError`, which must never escape.
- `plan_shards`, `stratified_sample` and `shapley_mc` use the plain-str values of lane kinds,
  billing classes and players (a `TBEnum` member and its value plan, group and seed the RNG
  identically), so a store that fills `LaneIndexRow.lane_kind` with `LaneKind` members gets the
  same shards and the same seeded sample as one that stores strings. `ShardKey.lane_kind` is always
  a plain str.

## 7. Additive public names (beyond the SPEC signatures)

`conventions`: `BadUsageError`, `normalize_anthropic_messages`, `registered_conventions`,
`ANTHROPIC_MESSAGES`, `CODEX_ROLLOUT`, `REFUSAL_PRE_OUTPUT`, `REFUSAL_AMBIGUOUS`,
`REFUSAL_MID_STREAM`. `cache_rules`: `CLAUDE_CODE_EFFORT_MIN_VERSION`,
`EFFORT_KEEPS_CACHE_EXCLUDED_CHANNELS`, `PER_MESSAGE_EFFORT_CHANNELS`, `ANTHROPIC_CHANNELS`,
`VERIFIED_ON`, `parse_version`, `version_at_least`, `RulesTable.channels()`. `transitions`:
`is_miss_event`, `HIT`, `CAUSES`, `MODEL_SWITCH_SUB_CAUSES`, `PARAM_CHANGE_SUB_CAUSES`,
`DIAG_CAUSES`. `shapley`: `largest_remainder`, `MAX_EXACT_PLAYERS`. `policy`: `CLAUSE_ORDER`,
`SELECTOR_KEYS`, `EFFORT_LEVELS`, `REPAIRS`, `BREAKPOINT_POLICIES`, `COLD_RESUME_MIN_DEFAULT`,
`EFFORT_SCALE_DEFAULT`, `KEEPALIVE_MAX_DEFAULT_S`. `shards`: `finding_sort_key`,
`UNATTRIBUTED_TEAM`. `findings`: `evidence_magnitude`, `MAX_TITLE`, `MAX_SUMMARY`, `MAX_EVIDENCE`,
`CPT_MIN_SAMPLES`, `CATEGORIES`, `LEVER_CLASSES`, `AUDIENCES`, `CONFIDENCES`.
