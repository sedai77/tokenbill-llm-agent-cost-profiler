### PLAN — Action plan, realization intervals, policy packs, hook, effectiveness (wave 2)

**Goal.** Turn findings into a ranked, overlap-aware plan (Shapley on a sample scaled to one full-scope joint
replay, realization-rate intervals, a headline that never sums standalone ceilings, allowance headroom kept
apart) and deployable, per-cohort, never-auto-applied patch packs with a SessionStart hook and post-rollout
effectiveness checks (SPEC §11, D15, D16, D26, D30, D34).

**Owns.** `tokenbill/plan/{action_plan,realization,policy_pack,litellm,effectiveness}.py`,
`tokenbill/plan/templates/**` (incl. `tokenbill_session_start.py`), `tests/v2/plan/**`.

**Consumes.** `core.catalog` (`LEVERS`, `LeverDef`, `ALLOWLIST`, `allowed`, `lever`), `core.policy`
(`parse_policy`, `combine`, `lane_matches`, `to_spec`), `core.shapley` (`shapley_exact`, `shapley_mc`,
`scale_credits`), `core.shards` (`stratified_sample`, `merge_replay`), `core.types` (`Finding`, `ActionPlan`,
`LeverResult`, `PolicyPack`, `PolicyEntry`, `Policy`, `AnalysisContext`, `ContractOverlay`, `LaneIndexRow`,
`ShardKey`), `core.labels`, `core.evidence`, `core.protocols` (`Replayer`), `core.builders`, `core.testing`
(`FakeReplayer`). `rates.contract.to_model_pricing` is used only through the injected `model_pricing_emitter`.

**Provides.** `plan.action_plan.build_action_plan(...)` (SPEC §11.2 signature); `plan.realization.PRIORS`,
`project(...)`; `plan.policy_pack.build_policy_packs(...)`, `render_pack(...)`; `plan.litellm.
render_injection_points(...)`, `validate_litellm_fragment(...)`; `plan.effectiveness.check_effect(...)`.

**Build.** Exactly §11: candidates; billed and allowance classes planned separately; grid choice on a seeded
stratified sample (`core.shards.stratified_sample`); interaction groups; Shapley on the sample (exact k ≤ 6,
MC above with SE); one full-scope joint replay via `map_shards` + `merge_replay` and `scale_credits` so Σφ
equals it; monthly normalization; priors (rate 1/1/1; cache 0.8/0.9/1.0; trajectory −0.2/0.5/1.0; behavioral
not projected); headline Σ φ·RR with comonotone range and calibration inheritance; `allowance_headroom_monthly`
apart; observed RR beside priors when ≥ 3 receipts; trade-offs (incl. `cc.default_model`,
`cc.default_effort`) only with `include_tradeoffs`. Packs: allowlist enforcement (unknown key raises; VERIFY
keys only as README comments), RFC 7386 merge patch vs `current`, rollback patch, `autoCompactWindow` + env
pairing, OTEL arm/wave tags, per-cohort packs, org-wide rollout note pointing to `measure plan --design its`,
LiteLLM fragment, hook template (stdlib only; threshold; frequency caps; never blocks; exit 0), README.
Effectiveness checks for TTL, autocompact, default model and effort levers.

**Facts to verify.** Nothing beyond `core.facts` (the allowlist's verified flags come from there); report any
key you believe is wrong via a contract-change note.

**Acceptance tests.**
- Shapley on the Appendix A.7 value function via `FakeReplayer` (credits 8/18/4; Σ = joint); scaling to a
  larger full-scope joint saving preserves Σφ to the nano and the group proportions; disjoint groups add;
  k = 7 uses MC with SE; standalone sums never appear in any `ActionPlan` field; an in-memory `load_lanes`
  with 1 vs 4 shards gives identical plans;
- realization: a trajectory lever's projection range crosses zero and is labeled; headline = Σ φ·RR(p50) with
  p10/p90; behavioral levers absent from the headline; trade-offs excluded unless requested; allowance levers
  appear only in `allowance_headroom_monthly`; observed RR shown after 3 receipts;
- packs: compaction lever → JSON patch with `autoCompactWindow` and `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`; TTL
  lever → `promptCacheTtl` only as a README comment while its value format is unverified in facts.json (and in
  JSON once verified); `cc.default_model` → `model` as a README comment (VERIFY) plus `availableModels` only
  with `include_tradeoffs`; merge patch against `current` keeps unrelated keys, and applying the rollback patch
  restores the original; unknown key raises; per-cohort packs for a heterogeneity finding; OTEL tags present;
  LiteLLM fragment passes the mini-parser;
- hook (subprocess, temporary HOME): expired + $3.20 → JSON `systemMessage` containing "$3.20"; below
  threshold → no output; malformed input → no output, exit 0; frequency caps enforced;
- effectiveness: 1h write share 95% → no finding; 40% → `setting-not-effective`; autocompact median check;
  default-model share check;
- CANARY absent from packs; no network;
- gate (`importorskip("tokenbill.sim.usage_replay")`): with the real replayer on the Appendix A.1 lane, the TTL
  lever's Shapley credit equals its standalone saving; gate (`importorskip("tokenbill.synth.fleet")`): the
  payments team's TTL lever Shapley credit is within ±5% of `FleetTruth`.

**Size.** ~2.5k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-7** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
`core.pool`, Copilot catalog tables) are merged into `v0.2` before you start — read SPEC Appendix E.3 and
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/briefs/CORE-AMENDMENTS.md` (§2–§5) for the exact contracts, and the Copilot addendum
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/SPEC-v0.2-COPILOT.md` only as background. Do not implement Copilot adapters/detectors yourself
(the CP-* packages do); implement only your amendment item so the extension points work.

#### 5. Amendments to SPEC packages (A-; paste into briefs not yet started, late change requests otherwise)

| id | package | status @99ba098 | amendment | LOC |
|---|---|---|---|---|
| A-1 | RATES | not started | addendum §21.4 row, reading Copilot rows through `core.facts.copilot_rates()` / `core.extensions.extension_rate_files()` (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool` (`allowance` = subscription lines only); `pricing verify` runs `core.extensions.rate_verifiers()` | +120 |
| A-2 | STORE | not started | addendum §7.1 DDL and §21.4 row **plus** `SqliteStore(…, adopt_key_ids: bool = False)` (R-E21 replaces the §7.1 "Key-id adoption" paragraph: with or without its own org key the store adopts the principal and name key ids of the first `copilot-export` bundle only, one adopted id, `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode` + an `audit` row, a second bundle key id → `UsageError`), `count_users(source="cost_lines")`, `source_stats` (implements `core.protocols.LedgerStats`), `purge(principal=…)` also for adopted-key rows, `ReconciliationReport` untouched (not stored) | +190, total 2,990 |
| A-3 | TRACE | not started | addendum §21.4 row; closed key sets derived with `core.records.record_fields` (C-4) and `core.records.RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC`, replacing the hand lists (net-neutral, O-5) | ≤ 0 net |
| A-4 | TELEM | **started** | late change request (R-E23): `otlp` skips resources for which `core.models.is_copilot_resource` is true and sets `stats["defer:copilot-otel"]`; rebase onto the gate-F' core first | +40 |
| A-5 | RECON | not started | addendum §21.4 row; `merge_reports` also unions `ReconciliationReport.decisions` (conflicting values → `ContractViolation`) | +90 |
| A-6 | REPLAY | **started** | late change request (R-E23): accept billing class `pool` like `allowance` (LIST_EQUIVALENT; mixed classes still raise) | +10 |
| A-7 | PLAN | not started | addendum §21.4 row; skip `replay == "aggregate"` levers; lever lookup via `core.catalog.lever` (searches both tables) | +40 |
| A-8 | OUT | not started | addendum §21.4 row (`render_sections`, `write_focus(…, extra_rows, owned_channels)`, `money_json` → `figure_json`, BILL line for `PricedTotal.pool`); `outputs/ccusage.py` moves to CLI-LEDGER (O-5) | +40 net, total 2,890 |
| A-9 | WIRING | not started | addendum §21.4 row plus `open_store(…, adopt_key_ids=False)` passed to `SqliteStore`; `ingest_paths` persists records **after** the ledger ingest (so an adopted key id exists before `persist`) and re-reads deferred files | +90 |
| A-10 | CLI-LEDGER (wave 3) | not started | addendum §21.4 row; `reconcile` prints the merged decisions (`recon_decisions_of(reports)`) with its verdicts; nothing is persisted (C-17) | +45 |
| A-11 | CLI-SAVINGS (wave 3) | not started | addendum §21.4 row; because decisions are not persisted, `findings` / `report` (and `scan` on Copilot data) first run `core.extensions.run_reconcilers` + RECON's reconcile over the same window, then call `enrich(…, reconciled_channels=…, recon_decisions=recon_decisions_of(reports))` (without reconcilers, e.g. on a store without Copilot data, `recon_decisions=()`: `excl` convention and unclassified discounts, both labelled) | +65, total 2,965 |
| A-12 | INTEGRATION (wave 4) | not started | `docs/COPILOT.md` (fleet recipe: managed OTel incl. the JetBrains caveat, **VS Code `agent-traces.db` opt-in + daily `copilot collect --source vscode`**, CI post-step, gh-aw artifacts, auth table, privacy); **`docs/COPILOT-ADMIN.md` generated verbatim from `tokenbill/copilot/handoff_data/admin_guide.md`** with `tests/v2/e2e/test_copilot_admin_doc.py` asserting byte equality; flagship §18 incl. the handoff and plan-unknown paths; Copilot release gates (below); weekly YAML check | docs/tests |
| — | CC, ADMIN, BLOCK, VERIFY, DETECT-CACHE, DETECT-OTHER, SYNTH-ORACLE, SYNTH-FLEET | mixed | none | 0 |



---
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to PLAN are binding for you.
