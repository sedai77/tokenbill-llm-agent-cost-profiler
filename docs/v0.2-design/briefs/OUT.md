### OUT — Renderers, FOCUS, SARIF, ccusage, showback, allocation, workload (wave 2)

**Goal.** Every output an enterprise consumes, with labels travelling with every number, allowance kept out
of billed columns, and privacy enforced at the rendering boundary: result@2 JSON, terminal (every `RunResult`
slot), single-file HTML, FOCUS 1.4 CSV (per-channel BilledCost rule), SARIF, ccusage JSON, team showback (incl.
per-billing-path list-equivalent distribution), plus the allocation-rules engine and workload classifier (SPEC
§14, R3, R4, R10, D17, D19, D26).

**Owns.** `tokenbill/outputs/{result_json,terminal,html,focus,sarif,ccusage,showback}.py`,
`tokenbill/finops/{allocation,workload}.py`, `tests/v2/outputs/**`, `tests/v2/finops/**`.

**Consumes.** `core.types` (`RunResult` and every slot type, `BillSummary`, `LedgerCostRow`, `ClusterDay`,
`CheckResult`, `PublishedAggregate`), `core.labels`, `core.money`, `core.textsafe`, `core.records`,
`core.kanon.publish`, `core.facts` (FOCUS columns, benchmark anchors with sources), `core.builders`,
`core.testing` (`published_for_tests` for renderer-only tests).

**Provides.** The renderer API of SPEC §14.7 exactly; `finops.allocation.load_rules`, `apply_rules`,
`coverage`; `finops.workload.classify`.

**Build.** §14.1 schema rule (no floats anywhere; every object with an integer leaf has `evidence`; money
only as MONEY objects; `list_equivalent` never under billed keys; `--deterministic` ordering); terminal ≤ 100
columns with label chips and a section for every `RunResult` slot (bill with allowance line, data quality,
calibration, reconciliation per channel, findings, plan with allowance headroom apart, packs, replays, measure
plan, measurements, ab, check, pricing); HTML with CSP, no scripts, table twins with captions, WCAG 2.2 AA
contrast in both themes, size budget; FOCUS columns from facts.json and the `x_` columns of §14.4, per-channel
BilledCost honesty rule (`reconciled_channels`, `channels` filter), allowance rows with `BilledCost = 0`,
`--role enrichment`, chargeback coverage gate, k-anonymous merging via `core.kanon.publish` with
`x_SuppressedUsers`; SARIF 2.1.0 rules; ccusage daily/monthly (numbers rendered from decimal strings, never
Python floats); showback from published aggregates only, with anchors and benchmarks shown with sources and the
per-billing-path list-equivalent distribution; allocation rules and workload classifier per §14.6. Renderers
accept only `PublishedAggregate` for grouped data and only `Figure.is_billed_eligible` values in billed columns
(raise `ContractViolation` otherwise).

**Facts to verify.** FOCUS 1.4 columns come from facts.json; ccusage JSON field names (checklist §19.8 #13).

**Acceptance tests.**
- `validate_result_json` rejects a float, an integer object without `evidence`, a money value not in MONEY
  form, and a `list_equivalent` figure under `bill.exact`; a full `RunResult` fixture with every slot filled
  round-trips deterministically;
- a billed column fed an ESTIMATED or LIST_EQUIVALENT Figure raises; unpriced renders as "unpriced (N
  inferences)"; the terminal renders each slot type (snapshot tests);
- HTML: no external URL, CSP meta present, no `<script>`, every `<svg>` chart followed by a `<table>` with
  `<caption>`, no pseudonym strings; terminal sanitizes ANSI in names;
- FOCUS: required columns present; every `x_` name matches `^x_[A-Z][A-Za-z0-9]{1,48}$`; totals equal the input
  `LedgerCostRow` sums per day and team; a channel not in `reconciled_channels` → refusal unless
  `allow_unreconciled` (then `x_Reconciled=false`) or excluded by `channels`; allowance rows have `BilledCost =
  0` and `x_PriceBasis = list_equivalent`; `role="enrichment"` zeroes Billed/Effective cost; chargeback below
  95% coverage refused; rows under k merged with `x_SuppressedUsers`;
- SARIF has the required 2.1.0 keys and one result per violation; ccusage output parses and matches the
  documented field names; showback contains no individual identifiers, shows the anchors with sources and the
  billing-path distribution only for groups with ≥ k users;
- allocation: first-match semantics, proportional splits sum to the original, `(unallocated)` line, coverage
  KPI; workload classifier ≥ 95% accuracy on a labeled fixture set; CANARY absent from every renderer output;
- gate (`importorskip("tokenbill.store.db")`): FOCUS totals equal `SqliteStore.cost_rows` sums on a small
  ledger; showback built from a real `aggregate` + `core.kanon.publish`.

**Size.** ~3.0k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-8** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to OUT are binding for you.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E47 (print "users unknown" via core.kanon.row_notes).
