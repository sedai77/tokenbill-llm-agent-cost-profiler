### F-SEM-C — Product families, Copilot finding bases and fixes, cache-rules row, transitions (wave 1.5b)

**Goal.** Make shared detector semantics product-aware so every existing detector works on Copilot lanes
without Copilot code, and so Copilot detectors can publish invoice dollars beside list-equivalent credits
without breaking the ratified finding rules: product families, the Copilot basis rule (R-E20), pool labeling,
Copilot fix substitution, a Copilot cache-rules row whose unknown TTL semantics are explicit, a context-tier
cause, and convention loading that reports missing modules. Read SPEC §3.11, §3.14, §3.15, §3.22, §10.1,
Appendix E (E.2 R-E8); addendum DC14, §3.6, §10.4, §19.3 #26, §19.5 #14; `CORE-AMENDMENTS.md` items S-1 … S-5
and rulings R-E20.

**Owns.** Edits within `tokenbill/core/{findings,cache_rules,transitions,conventions}.py`;
`tests/v2/sem/test_copilot_*.py`. Ownership check `--package F-SEM`.

**Consumes.** F-CORE-C (billing class `pool`, `PricingContext.context_tier`, `Finding.headroom`, `Scope` dims,
`Fix.target` domain, `DataQualityNote`). `core.catalog.fix_for` is read lazily (F-KIT-C, same sub-wave): a
missing accessor means "no Copilot fix known".

**Provides.**
- `findings.product_family(lane) -> str` (`"copilot"` iff `lane.billing_class == "pool"` or the first
  request's `pricing.channel == "github_copilot"`, else `"default"`).
- **`cohort_key` unchanged** (the 3-tuple `(team, lane_kind, billing_class)` that
  `tests/v2/sem/test_findings.py::test_cohort_key` pins): billing class `pool` already separates Copilot from
  Claude Code cohorts, because only the two Copilot paths map to it.
- `make_scope(**dims)` adds `product="copilot"` when `billing_class == "pool"` and no `product` is given.
- `build_finding` / `_validate` with the R-E20 per-field basis domains for scopes carrying `("product",
  "copilot")` or `("billing_class", "pool")`: `cost_observed` ∈ {LIST_EQUIVALENT, LIST, INVOICE};
  `recoverable`, `recoverable_shapley`, `projected_monthly` ∈ {LIST, LIST_EQUIVALENT}; `headroom` =
  LIST_EQUIVALENT; CONTRACT never; PROVIDER_ESTIMATE only in data-quality findings (R4). Every other scope keeps
  today's checks exactly (one basis, the D26 allowance rule, R4) and must have `headroom is None`. Pool cohorts
  from generic detectors get the title prefix `"Copilot credits: "` and the summary phrase "list-equivalent
  AI-credit value" when absent (the prefixed title is cut to 120 chars, the summary to 400). For
  `product=copilot` scopes `fix` is replaced by `fix_for(detector_id, kind, "copilot")` when one exists, else the text is kept, every `config_patch` whose target is not
  `github-copilot` is dropped, `target=None`, and " (no Copilot setting known)" is appended.
- `findings.min_usd_gate(finding, ctx) -> bool`.
- `cache_rules.CacheRules.ttl_semantics_known: bool = True` (appended) and the `github_copilot` row returned by
  `RulesTable._build` for `channel == "github_copilot"` (values in S-4; `ttl_options_s=()`,
  `ttl_semantics_known=False`); `RulesTable.channels()` unchanged (pinned by `test_cache_rules.py`).
- `transitions`: the unknown-TTL rule and the `param-change` sub-cause `context-tier-change` (S-4).
- `conventions.load_notes() -> tuple[DataQualityNote, ...]` with `dq.convention_module_unavailable` per
  skipped `CONVENTION_MODULES` entry (S-5).

**Build.** Exactly `CORE-AMENDMENTS.md` S-1 … S-5. Addendum revision 3 already keeps the 3-tuple `cohort_key`
and non-optional `CacheRules` fields; where it differs from S-1 … S-5 (its `CONSERVATIVE_TTL_CHANNELS` constant
instead of `CacheRules.ttl_semantics_known`, `context_tier` in the `system` instead of the `messages` tier,
`is_copilot_scope`, "one basis B" for the dollar fields), S-1 … S-5 win (CORE-AMENDMENTS "Precedence").

**Acceptance tests.**
- Two lanes of one team, one Claude Code (`api_key`) and one Copilot (`copilot_pool`), never share a cohort;
  `product_family` of each; `make_scope(billing_class="pool", team="t")` has `product: copilot`.
- R-E20: a `product=copilot` finding with `cost_observed` LIST_EQUIVALENT, `recoverable` ESTIMATED LIST and
  `headroom` LIST_EQUIVALENT is valid; the same with `headroom` on LIST, with a CONTRACT figure, or with
  PROVIDER_ESTIMATE outside data-quality → `ContractViolation`; an INVOICE `cost_observed` on an entity scope
  (no `billing_class` dim) is valid; `headroom` on a Claude finding → `ContractViolation`; every existing
  `tests/v2/sem/test_findings.py` case unchanged and green.
- A finding on a Copilot scope whose fix carries `env.CLAUDE_CODE_*` keys comes out with the Copilot fix from a
  stub `fix_for`, or with the Claude keys stripped and "(no Copilot setting known)" when none exists.
- Transitions on a Copilot lane: τ known (300 s hint) → a gap of τ + prev_duration + 11 s is `ttl-expiry`; a
  gap within ± (prev_duration + 10 s) of τ is `ambiguous`; τ unknown → never `ttl-expiry`; a context-tier
  change between requests → `param-change/context-tier-change`; a Claude lane's causes are unchanged.
- `normalize` with a `CONVENTION_MODULES` entry pointing at a missing module still normalizes with the other
  conventions and `load_notes()` names the module.
- All F-SEM tests of wave 1 unchanged and green (incl. `test_cohort_key`, `channels()`).

**Size.** ~0.6k LOC including tests.
