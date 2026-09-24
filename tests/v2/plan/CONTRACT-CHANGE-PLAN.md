# CONTRACT-CHANGE-PLAN — contract gaps found while building PLAN (wave 2b)

PLAN implements against the current contract (`tokenbill/core/*` at f2162e4, SPEC §11 and
Appendix E). Nothing below was worked around silently; each item states what PLAN does today and
the proposed contract change. None blocks a merge.

## 1. `check_effect` needs the rolled-out value (SPEC §11.5)

**What.** `check_effect(lanes_after, *, lever_id, cohort, since_ms, days=7)` cannot know the
compaction window or the effort level that was rolled out ("median `pre_tokens` ≤ window × 1.05",
"at or below the level"), nor the TTL direction or target model when they differ from the SPEC's
examples.

**Today.** PLAN adds an optional keyword `target: str | None = None` and also accepts the value as a
`lever_id` suffix (`"cc.autocompact_window=400000"`, `"cc.max_effort=medium"`). Defaults: TTL levers
`1h` (the SPEC's check), `cc.default_model` the catalog grid model; the window and effort levers
raise `UsageError` without a value. Callers using the SPEC signature keep working.

**Proposed.** SPEC §11.5 signature `check_effect(lanes_after, *, lever_id, cohort, since_ms,
days=7, target=None)`; CLI-SAVINGS: `tokenbill policy check-effect --lever ID [--target VALUE]`
(or `--lever ID=VALUE`), or read the value from the rendered pack.

## 2. Catalog selectors wider than the delivery key (F-KIT, `core.catalog.LEVERS`)

**What.** `cc.fast_mode_opt_in` (`fastModePerSessionOptIn`), `cc.max_effort` (`maxEffortLevel`) and
`model.same_tier_upgrade` (`env.ANTHROPIC_DEFAULT_*_MODEL`) are delivered by Claude Code managed
settings but have selector `all` (their grids `fast=off`, `effort=…` and `model=…@model:<m>` are
unscoped / model-scoped). PLAN replays each lever only on the lanes its selectors match, so any
saving these clauses find on SDK / API lanes (`lane_kind:api_run`, `agent_product:agent_sdk`)
would be credited to a Claude Code setting that cannot reach those lanes. (On the synthetic fleet
`fast=off` saves only on the infra team's Claude Code main lanes, so the credit is right there;
but the `all` selector still joins `cc.fast_mode_opt_in`, `cc.prompt_cache_ttl.main` and `sdk.ttl`
into one interaction group.)

**Proposed.** Selector `agent_product:claude_code` for these three levers (and the grid clauses
scoped the same way, e.g. `effort=high@agent_product:claude_code`); an SDK-side fast-mode lever, if
wanted, as a separate catalog entry delivered by snippet.

## 3. `LeverResult` cannot say "not in the joint set"

**What.** SPEC §11.2 shows trade-off levers "evaluated and shown" and behavioral levers "never
projected", but `LeverResult.shapley` and `.projected_monthly` are required `Figure`s.

**Today.** PLAN sets `group` to `"trade-off"`, `"ceiling-only"`, `"projection-only"` or
`"behavioral"` (joint levers: `"<class>:g<n>"`) and uses ESTIMATED figures with `nano=None` and a
`"unpriced: <reason>"` note where there is no number. Renderers print "unpriced" plus the reason.

**Proposed.** `LeverResult.status: str = "joint"` (`joint | tradeoff | ceiling | projection |
behavioral`) appended with a default, and `shapley` / `projected_monthly` typed `Figure | None`
(None = not computed, never zero).

## 4. `ActionPlan.shapley_se` is keyed by lever id only

**What.** A lever can have a Shapley credit in more than one billing class (billed and allowance).

**Today.** Keys are the lever id for the billed class and `"<class>:<lever id>"` for `allowance` and
`pool`.

**Proposed.** Document that key form in SPEC §3.5 (or key by the group id).

## 5. Block-level levers have no joint engine

**What.** `blocks.breakpoints` (`replay: block`) cannot be combined with usage-level clauses in one
replay: the usage replayer ignores block clauses and the block replayer ignores usage clauses
(both record it in `assumptions`), and `AnalysisContext` carries one replayer.

**Today.** Block levers are listed like `replay: none` levers — projection from the linked
findings' recoverable figures, outside the Shapley plan and the headline.

**Proposed.** Either an `AnalysisContext.block_replayer` and a documented additivity assumption
between the two engines, or keep the current treatment and say so in SPEC §11.2.

## 6. Fleet-level TTL value vs per-cohort heterogeneity

**What.** SPEC §11.2 chooses one grid value per lever for the whole class. On the synthetic fleet
the whole-fleet choice for `cc.prompt_cache_ttl.main` is `5m` (teams billed at 1h lose more than
payments gains), while payments alone chooses `1h` (matching `FleetTruth`).

**Today.** The plan reports the class-level choice; cohort values reach the packs through the
findings' `Fix.config_patch` (cohort packs, forced by `ttl-heterogeneous`).

**Proposed.** Optionally let `build_action_plan` choose per (team, lane kind) cohort for levers
whose delivery is per cohort, or document the current behavior in SPEC §11.2.
