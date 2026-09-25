### DETECT-OTHER — Context, premium, model routing, failure, automation, tail detectors (wave 2)

**Goal.** The remaining ten usage-level detector classes: context size and compaction window, the harness
static prefix and tool-definition bloat, carry and config tax, premiums and sticky escalation, model routing
(delegation, same-tier, **default model, default effort**, effort mix, **rebaseline**), failure paths
(**incl. max-tokens truncation**), automation (**incl. CI run cost**) and the runaway tail (SPEC §10.1, the
non-cache rows of §10.2, D14, D26, D34).

**Owns.** `tokenbill/detect/{context,premium,model,failure,automation,tail}.py`, `tests/v2/detect_other/**`.

**Consumes.** `core.transitions`, `core.findings` (helpers incl. `fit_cpt`, `rate_nano`), `core.policy`,
`core.catalog` (`successor`, `retiring_within`, `levers_for_kind`, `ALLOWLIST`), `core.types`, `core.labels`,
`core.money`, `core.evidence` (tokenizer band, tool-search band, max-tokens and code-review benchmarks,
CPT defaults), `core.protocols` (`Detector`), `core.registry.run_detectors`, `core.builders`,
`core.testing` (`FakePricer`, `FakeReplayer`, `assert_detector_conforms`).

**Provides.** Detector classes at the registry paths of SPEC §3.7: `detect.context.{SizeTax,
CompactionWindow, StaticPrefix, Carry}`, `detect.premium.{PremiumModifiers, StickyEscalation}`,
`detect.model.Routing`, `detect.failure.FailurePath`, `detect.automation.Automation`, `detect.tail.Runaway`.

**Build.** Every non-cache row of SPEC §10.2 with the framework rules of §10.1 (cohort confinement, allowance
labeling, `min_usd` and thresholds from `ctx`, allowlisted config patches, deterministic ids): size-tax exact
decomposition; compaction-window grid with guards; static-prefix harness cost from `ctx.static_prefix_floor`
and tool-defs-bloat from fingerprints (reduction band 0.50–0.85, upper bound); carry/config tax with the cpt
fit; premiums EXACT on identical tokens; sticky escalation self vs org (count only when ≥ k); routing kinds
incl. `default-model` (replay `model=claude-sonnet-5@agent_product:claude_code,lane_kind:main` with band,
trade-off, `needs_eval`) and `default-effort` (replay `effort=medium,scale=0.5@…`), same-tier via
`core.catalog.successor` only, `rebaseline` (dominant-model change detection, 14-day before/after means,
tokenizer-family note, thinking-default note, stale-prompt flag at +20% output per call, behavioral, no
recoverable); failure kinds incl. `max-tokens-truncation` (billing rule documented; retry detection within 120
s and `T ≥ 0.95·T_trunc`); automation kinds incl. `ci-run-cost` (per repo/workflow, benchmark with source,
CI vs interactive $); runaway with cohort statistics.

**Acceptance tests.**
- one hand-computed fixture per kind (to the nano); premium detector on SPEC §6.9 case 4 vs case 1 yields an
  EXACT fast premium of $0.068 with `{"min_usd": "0.01"}`; Appendix A.11 truncation ($0.71376 EXACT,
  $0.53532 ESTIMATED upper bound) with `{"min_usd": "0.10"}`;
- size-tax exact decomposition; compaction-window curve with the extra-compactions guard (via `FakeReplayer`);
  static-prefix info with S known and absent; tool-defs-bloat range on a 14k-token non-deferred tools tier
  and none when tools are deferred; carry and config-tax (first listing only; cpt fit with ≥ 30 samples else
  defaults); sticky escalation (self vs org count ≥ k); routing: delegation with band, same-tier exact
  arithmetic labeled ESTIMATED and no same-tier finding for Opus 4.8 (no successor), default-model and
  default-effort on Claude Code main lanes only, rebaseline Δ% on a synthetic migration; failure path (each
  sub-kind, incl. SDK attempts from recorder hooks counting toward retry storms); automation (each sub-kind);
  runaway (break-glass semantics: team only unless `ctx.break_glass`);
- allowance cohort: size-tax figures carry LIST_EQUIVALENT and "Allowance headroom:" titles;
- unmet `requires` → exactly one `missing-capabilities` finding via `run_detectors`; healthy-control lanes → no
  finding with recoverable ≥ `min_usd`; `assert_detector_conforms` for all 10 classes (incl. shard
  invariance); CANARY absent;
- gate (`importorskip` `tokenbill.synth.fleet` and `tokenbill.sim.usage_replay`): on `synth.fleet.generate()`
  the search, infra, data (routing, default-model, default-effort, rebaseline), ops, ci-bots (automation,
  truncation) and agents (tool-defs-bloat) plants are recovered within tolerance and the core team produces no
  finding ≥ `min_usd`.

**Size.** ~3.0k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **(none — see note)** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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

