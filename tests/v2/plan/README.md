# tests/v2/plan — PLAN acceptance tests

Package PLAN (SPEC §11, D15, D16, D26, D30, D34; Copilot amendment A-7): the action plan, the
realization-rate projections, the policy packs, the LiteLLM fragment, the SessionStart hook
template and the post-rollout effectiveness checks.

| module | public API |
|---|---|
| `tokenbill/plan/action_plan.py` | `build_action_plan(...)` (SPEC §11.2 signature), `ShardJob`, `lever_selectors`, `touches`, `DEFAULT_LEVERS` |
| `tokenbill/plan/realization.py` | `PRIORS`, `project(...)`, `observed_rr(receipts)`, `parse_observed_rr`, `prior_note` |
| `tokenbill/plan/policy_pack.py` | `build_policy_packs(...)`, `render_pack(pack, out_dir)`, `apply_merge_patch`, `make_merge_patch`, `canonical_json`, `snippet` |
| `tokenbill/plan/litellm.py` | `render_injection_points(models, *, ttl=None)`, `validate_litellm_fragment(text)`, `parse_yaml_subset` |
| `tokenbill/plan/effectiveness.py` | `check_effect(lanes_after, *, lever_id, cohort, since_ms, days=7, target=None)` |
| `tokenbill/plan/templates/` | `tokenbill_session_start.py` (hook), `snippets/<lever_id>.md` (R-E12) |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/plan` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/plan && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/plan/*.py,tokenbill/plan/templates/*.py'`
— 97% at hand-off (action_plan 99%, realization 100%, effectiveness 99%, litellm 96%,
policy_pack 95%, hook template 95%; the lines left are defensive branches).

| file | covers |
|---|---|
| `test_shapley_plan.py` | Appendix A.7 through `FakeReplayer` (exact credits 8 / 18 / 4, Σ = joint = 30, standalone 10 / 20 / 5); the standalone sum (35) appears in no plan field; a weighted game on 12 lanes sampled to 5 is scaled to the shard-merged full-scope joint (Σφ to the nano, proportions within 1 nano, standalone values × the same factor, "sample-scaled"); disjoint groups add; two groups keep their *sample* proportions when scaled; 7 interacting levers → `shapley-mc` with SE, 6 stay exact; 1 vs 4 shards (and a reordering `map_shards`) give identical plans; delivery scope (a lever never replays lanes it is not delivered to); argument errors |
| `test_plan_levers.py` | grid choice (largest documented saving, levers without a saving dropped); trade-offs (`cc.max_effort`, `cc.default_model`, `cc.default_effort`) evaluated and shown but out of the joint set and the headline unless `include_tradeoffs`; interacting trade-offs (credits 450 / 250); behavioral and ceiling-only levers never projected; allowance levers only in `allowance_headroom_monthly` (LIST_EQUIVALENT); Copilot `pool` lanes → `pool_headroom_monthly`, aggregate levers skipped (A-7); projection-only levers (`cc.tool_search`, `blocks.breakpoints`) from findings; observed RR after 3 receipts; calibrated mode only after a passing model gate (grid choice stays documented); compaction guards (min window, extra compactions per session) and `post=` from the org median (R-E24, R-E40); unpriced replays make credits and the headline unpriced (R2, R-E26); no replayer; empty scope; finding ids per billing class |
| `test_realization.py` | the prior table (from `core.catalog.RR_PRIORS`), cache-transform and trajectory projections (range crossing zero, labeled with the design-judgment note), negative credits, behavioral → None, unpriced credits, ranged inputs, half-even rounding, observed RR from receipts (≥ 3 per class) |
| `test_policy_pack.py` | compaction → `autoCompactWindow` + `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`; TTL → `promptCacheTtl` in JSON (verified in `facts.json`) and only as a README comment when the flag is monkeypatched to unverified; `cc.default_model` → `model` as a README comment without `include_tradeoffs`, `availableModels` + `enforceAvailableModels` only with it; unknown keys, non-Claude-Code keys and out-of-domain values raise; finding fixes delivered; merge patch against `current` keeps unrelated keys and the rollback patch restores `current` exactly (also for a fresh install); RFC 7386 helpers; per-cohort packs forced by a `ttl-heterogeneous` finding (team and MDM group); OTEL arm/wave tags; the hook shipped with the cold-resume levers; LiteLLM fragment validated; SDK snippets; `modelPricing` through the emitter; minimum client version notes; rendering (paths, JSON, executable hook, unsafe paths refused); CANARY absent from every pack field and rendered file |
| `test_litellm.py` | renderer ↔ validator round trip, the YAML subset accepted and refused, structural checks; hypothesis fuzz of arbitrary text, line soups and model names (only `TokenbillError` escapes) |
| `test_hook.py` | subprocess runs with a temporary `HOME`: expired + $3.20 → `systemMessage` containing "$3.20"; below threshold / not expired / wrong types → no output; malformed input (incl. deep nesting and bad bytes) → no output, exit 0; per-session and weekly caps; state file 0600 without the session id; unwritable state → silent; in-process runs for the remaining branches |
| `test_effectiveness.py` | TTL 1h share 95% → None, 40% → `setting-not-effective`, exactly 90% → finding, window / cohort (team, MDM group) / target; subagent and SDK TTL levers; autocompact median (even and odd counts, 1.05 slack); default-model share; effort shares (unknown efforts left out); argument errors |
| `test_properties.py` | hypothesis: for random fleets (kinds, teams, billing classes), sample sizes and seeds, sharded == unsharded and Σφ = joint; headline / headroom = Σ projections; merge + rollback round-trip random `current` settings; `check_effect` never raises on valid arguments |
| `test_hygiene.py` | no `float(` / float literals in the owned modules and the hook, stdlib-only imports, no network modules, docstrings on the public API, CANARY planted in every free-text attribution field never reaches a plan or its packs |
| `test_gate_replay.py` | **gate** (`importorskip("tokenbill.sim.usage_replay")`): the real `UsageReplayer` on Appendix A.1 — the TTL lever's Shapley credit equals its standalone saving, $1.1508; A.2 keeps 5m; A.2b moves to 5m ($0.318); A.1 on a subscription lane → allowance headroom only; A.6's negative compaction window is never chosen; sharded == unsharded; packs canary-free |
| `test_gate_fleet.py` | **gate** (`importorskip` `tokenbill.synth.fleet` and the replayer): the payments team's `cc.prompt_cache_ttl.main` Shapley credit is within ±5% of `FleetTruth` (`payments.ttl-1h`; exact at hand-off) and equals its standalone value; three teams together: Σφ = joint, sharded == unsharded |

The gate tests run against the modules merged into `v0.2` at f2162e4 (`sim.usage_replay`,
`synth.fleet`): all green. On the whole synthetic fleet (2,070 lanes, 26k requests) a plan with
the real replayer takes ≈ 6 s single-process.

## Fixtures and provenance

No fixture files. Every lane is synthetic, built in code with `core.builders` in `helpers.py`
(SPEC Appendix A lanes A.1, A.2, A.2b, A.6 in its `(ts_s, R, W5, W1, U, O)` notation; small
lanes for the fake replays), dated from 2026-09-23. Replays come from `core.testing.FakeReplayer`
(functions of the policy) except in the gate tests. No real transcripts, pages or keys. Prices:
`core.builders.FlatRates` (fake tests) and `core.testing.FakePricer` (`core/facts.json`, gates).

## Facts and interpretations

PLAN transcribes no priced fact. The allowlist (`verified` flags, minimum versions, domains) is
read from `core.catalog.ALLOWLIST` / `core/facts.json`, which overrides SPEC §11.4 prose (Appendix
E.1): every §11.4 key is `verified: true` there as of 2026-09-23, so `promptCacheTtl`, `model` and
`effortLevel` go into JSON; the VERIFY paths are tested by monkeypatching the flags. `effortLevel`
accepts `low|medium|high|xhigh` (no `max`, facts.json). Realization priors come from
`core.catalog.RR_PRIORS`.

Unverified (shipped as guidance, never as data):
- the LiteLLM key for a TTL on injected breakpoints (written only as a commented line);
- `fallback.credit`'s request option (the snippet says VERIFY);
- the hook's install path: managed settings run `python3 ~/.claude/hooks/tokenbill_session_start.py`
  (MDM copies the file there); adjust `HOOK_COMMAND` in the pack if your fleet uses another path.

Interpretations where the SPEC is silent (see also `CONTRACT-CHANGE-PLAN.md`):
- **Delivery scope.** A lever replays only the lanes it is delivered to: its keyed clauses'
  selectors (`ttl`, `model`, `effort`, `keepalive`), else the catalog selector. Replays run per
  partition of lanes by the set of levers touching them and add (per request, R-E24), so the
  sample, joint and shard replays agree and sharded plans equal unsharded ones.
- **Joint replay when the sample is the scope.** The joint is the sample replay of the selected
  set (no shard pass, no scaling); otherwise it is the shard-merged full-scope replay.
- **Levers outside the joint set** keep `LeverResult` fields with an explicit group:
  `"trade-off"` (evaluated, sample-scaled standalone, standalone projection, no credit),
  `"ceiling-only"` (`cc.compact_on_resume`: ceiling shown, never projected), `"projection-only"`
  (`replay` `none`/`block`: projection from the linked findings' recoverable figures of the same
  basis) and `"behavioral"` (never projected). "No value" figures are ESTIMATED with `nano=None` and
  a `"unpriced: <reason>"` note. None of them enters `joint_saving` or a headline.
- **Billing classes.** One `LeverResult` per (lever, class); group ids are `"<class>:g<n>"`;
  `shapley_se` keys are the lever id for billed levers and `"<class>:<lever id>"` otherwise.
  `allowance_headroom_monthly` / `pool_headroom_monthly` are None when the class has no lanes and
  no linked finding.
- **Packs.** One value per key and pack: cohort findings' fix values win in cohort packs, plan
  values in the `all` pack (conflicts listed in the README); `render_pack` writes into
  `out_dir/<cohort>/` (unsafe characters replaced). Targets `litellm` and `sdk` give one `all`
  pack.
- **Effectiveness.** `check_effect` takes the rolled-out value as `target=` or a `=<value>` suffix
  of `lever_id`; TTL levers default to `1h`, `cc.default_model` to the catalog grid model.
