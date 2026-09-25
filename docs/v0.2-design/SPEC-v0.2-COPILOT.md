# Token Bill v0.2 — GitHub Copilot addendum (authoritative for Copilot)

Status: build contract addendum, **revision 3.1**, 2026-09-23 (revision 1 and 2 the same day; revision 3
integrates the product owner's answers of 2026-09-23 and the revision-2 audit; revision 3.1 is the final
consistency pass with the package briefs). It extends
`design/SPEC-v0.2.md` (the SPEC) with first-class GitHub Copilot support. Section numbers mirror the SPEC;
§22 (admin handoff kit) is new. Contract changes to the frozen core are listed once in §3 (CA-1 … CA-40,
plus CA-41 … CA-48 added in revision 3) and are applied in **wave 1.5** (§2.3, §21), after merge gate F and
before wave 2, as SPEC rulings R-E4 and R-E15 require. Behavioral amendments to SPEC packages are listed in
§21.4: as **brief text** for packages not started yet, as **late change requests** for packages that R-E15
let start on the gate-F core (§21.4a). Everything else is new scope owned by the packages of §21. SPEC §21
(build protocol) binds every Copilot package. Ruling ids of this addendum are **R-E16 … R-E23** (revision 2
used R-E5 … R-E8, which collide with SPEC Appendix E.2; every reference is renumbered).

**Revision 3.1 (final consistency pass, 2026-09-23) — precedence.** Revision 3 of this addendum and the
package briefs (`copilot/briefs/`, incl. `CORE-AMENDMENTS.md`) were revised in parallel and diverged in detail.
The binding rule: **`CORE-AMENDMENTS.md` and the package briefs win** for every wave-1.5 contract (signatures,
fields, vocabularies, registry entries, gate F'), for every package boundary, file ownership and size, and for
the rulings R-E16 … R-E23 (§3.10 now mirrors CORE-AMENDMENTS O-2 verbatim; §21.1–§21.3 carry the briefs'
rows). This document stays the design background and the source of every fact (§19), worked example
(Appendix C) and product rule (§0, §1); where the revision-3 text of §3, §5.13–§5.18, §7, §15, §18 or §22
differs from a brief, the brief wins. The divergences, each decided for the brief:

| topic | revision-3 text of this document | binding (brief / CORE-AMENDMENTS) |
|---|---|---|
| Ruling ids | R-E16 exemption + R-E21 amending R-E9; R-E22 adoption | R-E16 covers the exemption **and** the R-E9 amendment; R-E21 = key-id adoption; R-E22 = plan never assumed (the E-ruling form of R17) |
| Key-id adoption | explicit key-id sets (`IngestOptions.adopt_key_ids`, `SqliteStore` / `MemoryStore` `adopt_key_ids: Collection[str]`), `core.keys.accepted_key_id`, `--adopt-key-id`, `--allow-mixed-keys` | R-E21: boolean `adopt_key_ids`; the first `copilot-export` bundle's key ids are adopted automatically, with or without the store's own org key (so collector files with `r_` / `c_` principals and a bundle share one store, joined by team only); one adopted key id per store; a second bundle key id → exit 2 |
| Reconciler decisions | `ChannelDecision`, `AnalysisContext.channel_decisions`, persisted by `put_decisions` in a `copilot_decisions` table | string pairs `ReconciliationReport.decisions` → `AnalysisContext.recon_decisions` (C-14, C-17); not persisted — every command that enriches runs the reconcilers first (A-10, A-11) |
| Span mapping | new core file `core/spans.py`; CP-VSCODE owns `adapters/copilot_vscode.py` | no `core/spans.py`; CP-OTEL owns `copilot_otel.py`, `copilot_vscode.py`, `gh_aw.py` and the `github_copilot.otel` convention; CP-VSCODE owns only the collector |
| VS Code collector output (§5.18) | `IngestResult`s written as trace@2 by TRACE's writer; dq `copilot_vscode_retention_gap` / `_collect_stale` | content-free SQLite (VS Code DDL) and OTel-JS JSON-lines extracts read by `copilot-vscode-traces` / `copilot-otel`; dq `dq.copilot_vscode_gap` / `dq.copilot_vscode_retention_risk` (CP-VSCODE brief) |
| Team maps | `copilot/teammap.py` owned by CP-HANDOFF | owned by CP-ORGDATA (`teammap.build_maps`) |
| Registry (CA-21, CA-44) | `copilot-export` inserted first; a `copilot-admin-answers` adapter; `tokenbill.core.spans` in `CONVENTION_MODULES` | C-24: `copilot-export` and `github-copilot-activity-report` appended; answers read via `--answers` by `admin_answers.parse_answers`; `CONVENTION_MODULES` = conventions_ext, copilot_conventions, copilot_otel, github_billing, gh_aw |
| Vocabularies (CA-41) | `editor_family()` and `PLAN_SOURCES` in `core.records`; `EDITOR_FAMILIES` spelled `visualstudio`, `github_web`, …; `record_fields()` | C-9 spellings (`visual_studio`, `github_com`, `mobile`, …); `editor_family()` in `core.catalog` (K-3); `record_fields()` kept (C-4) |
| Experimental flags | `copilot-ai-usage-quota`, `copilot-dashboard-ndjson` | `copilot-report-quota` (quota evidence as `plan_quota` config rows, CP-BILL); the dashboard NDJSON export is accepted with dq codes, no flag |
| Plan detection API (CA-48) | `detect_plans(…, aggregates, *, month) -> dict`, `plan_scenarios()`, `PlanEvidence.hint`, `CopilotSummary.scenarios` | P-1: `detect_plans(cost_lines, licenses, config, *, month, entity_mode) -> list[PlanEvidence]`; the hint is rendered by CP-OUT; scenarios are the keys of `plans_by_scenario`; `copilot_allowance("unknown")` raises `UsageError` |
| Cache rules (CA-32) | `CONSERVATIVE_TTL_CHANNELS`; `context_tier` salted into the `system` tier | S-4: `CacheRules.ttl_semantics_known: bool = True` (False on the `github_copilot` row); `messages` tier |
| Figure bases (CA-45) | the dollar fields share one basis incl. INVOICE | S-2: dollar fields ∈ {LIST, LIST_EQUIVALENT}; INVOICE only in `cost_observed` |
| Aggregate-only bundles (§22.5) | seat counts as `pool_seats` run flags; seat kinds unavailable | `seat_counts` / `activity_counts` config rows (C-9); `idle-seat` per team from counts (CP-HANDOFF, CP-DET-SEATS) |
| Handoff API (§22.5, §5.16–§5.17) | `export_bundle(…)`, `pseudonym(login, *, key_file)`, the §22.5 manifest keys and boundary-matching leak rule, `--hash-workspaces`, the §5.17 answers schema | CP-HANDOFF brief: `export_from_files(…, exclude_logins, key_rotated)`, `pseudonym_of(login, *, key)`, its manifest keys, leak rule and answers schema (incl. `seat_policy`); no `--hash-workspaces` (org and cost-center names are organizational data) |
| Existing tests | "wave-0/1 tests green unedited" | exactly three declared edits T-1 … T-3 (`test_registry.py`, `test_types.py`, `test_records.py`): the pinned registry list and type-field names make them unavoidable |
| §18 world | 127 users (vscode 10, jetbrains 10, agents 5, core 10) | CP-SYNTH brief: 146 users (vscode 15, jetbrains 15, agents 4, core 20) and variants `volume`, `slack`, `plan_unknown`, `plan_conflict`, `plan_quota` |
| Pricing YAML history | "42 dated revisions" | 38 (the fact-check's count; `raw/yml/commits.txt` has 38 lines) — corrected in place |
| Appendix C.P14 / P15 | as written below | binding as written (flag name per this table); F-POOL adds P14b / P15b |

Evidence base: four research tracks written and adversarially fact-checked on 2026-09-23
(`copilot/copilot-billing.md` + `.verify.md`; `copilot-data-apis`; `copilot-client-telemetry`;
`copilot-cost-levers`). Corrected versions are used. For revision 2 the lead architect re-opened, on
2026-09-23, the local copies of the GHEC OpenAPI description (`github/rest-api-description`
`descriptions/ghec/ghec.json`, info.version 1.1.4: endpoint descriptions, auth scopes, enums), the
billing-reports reference, the cost-centers and billing-cycles concept pages, the code-review concept page,
the about-github-agentic-workflows page, the CLI configuration-directory and command references, the
changelogs of 2026-06-25, 2026-07-01, 2026-08-26, 2026-09-14 and 2026-09-23, GitHub's `github/copilot-sdk`
`rpc.ts` / `session-events.ts` (@075f027), `microsoft/vscode` `extensions/copilot` sources
(`otelSqliteStore.ts`, `otelConfig.ts`, `genAiAttributes.ts`, `package.json`), `github/gh-aw` (@358fbf2,
2026-09-23: `pkg/cli/token_usage_types.go`, `actions/setup/js/ai_credits_context.cjs`, fixture
`awf-v0.28.7-aic-token-usage.jsonl`) and the 38 dated revisions of the `github/docs` pricing YAML
(`copilot/raw/yml/<date>_<sha>.yml`, map in `copilot/raw/yml/commits.txt`). Facts with sources and dates are
in §19. Every unsettled item is marked **VERIFY** and ships disabled, as a labelled assumption with a dq or
residual code, or as commented guidance.

**Premise corrections (unchanged from revision 1).** (1) Copilot has a managed-settings file
(`.github-private` repo `copilot/managed-settings.json`, team files, MDM, system files), but it is
client-enforced and covers only a few cost-relevant keys (`model` incl. `"auto"`, `telemetry`,
`allowedMcpServers` / `deniedMcpServers`). Model enablement, budgets, seats, paid-usage policy and code-review
effort are server-side settings (UI, some REST). The Copilot policy pack is therefore a managed-settings patch
plus an admin checklist plus REST request files that Token Bill never executes (§11.3). (2) `ai_credits_used`
(metrics API) is a per-user daily consumption signal, not a bill. The billed truth is the AI usage report and
the billing usage REST API (§5, §12).

### What revision 3 changes (owner answers and audit → where fixed)

Product owner answers (2026-09-23): (1) the product owner has **no GitHub admin access**; a colleague with
enterprise/org owner or billing-manager rights runs the data pull; (2) it is **not known** whether the
company is on Copilot Business or Enterprise; (3) developers use **VS Code and IntelliJ (JetBrains)**.

| item | fix |
|---|---|
| Answer 1: admin handoff | DC26, DC31; new §22 (admin guide, minimal permissions, no-token UI path, `copilot-export@1` bundle, product-owner analysis); new package CP-HANDOFF (§21); adapters `github-copilot-activity-report`, `copilot-export`, admin answers (§5.15–§5.17); commands `copilot admin-guide / export / inspect / pseudonym`, `pull --out *.tbx`, `scan --from *.tbx` (§15); key-id adoption R-E21 (§7.1, CA-47; revision 3.1) |
| Answer 2: plan unknown | DC27, R17; `core.pool.detect_plans` + scenario pool months (CA-40, CA-48); `PlanEvidence`, `PoolMonth.plan_*`, `seats.unknown_plan` (CA-14); `plan-status` kind and per-scenario findings (§10.1); two-column plan and bill (§11.2, §14); `--plan` statement, never a default (§15); Appendix C.P13–P15 |
| Answer 3: VS Code + JetBrains | DC28–DC30; org-data path primary and release-blocking; client priority VS Code > JetBrains > CLI (§2.1, §5 intro); VS Code `agent-traces.db` promoted to a supported fleet/volunteer source with a collector (new package CP-VSCODE, §5.13, §5.18); JetBrains via org data + experimental OTel (§5.10); Auto reach and JetBrains-heavy teams (§9.3); release gate (2) switched to a real VS Code `agent-traces.db` (§21.6) |
| Ruling-id collision with SPEC E.2 | R-E5…R-E8 → R-E16…R-E19; new R-E20…R-E23 (§3.10) |
| Pool findings rejected by ratified R-E8 (`_validate`) | R-E20 figure-basis rule for Copilot scopes, declared as an amendment of R-E8 (CA-31, CA-34, CA-45) |
| `cohort_key` 4-tuple breaks `test_cohort_key` | 3-tuple kept; `product_family()` separate (CA-31, DC14) |
| `kanon._exempt` exempts team-scoped `aggregate` findings | R-E16 (incl. the R-E9 amendment; revision 3.1 numbering); `_exempt`, re-scoping chain, headroom summing and the R-E10 `publish()` fix assigned to F-KIT-C (CA-37, CA-46) |
| Merged F-KIT tests pin `LEVERS`, `ALLOWLIST` (31), `PROMOTIONS`, facts settings keys and modifier keys | Copilot data in separate tables `COPILOT_LEVERS`, `COPILOT_ALLOWLIST`, `COPILOT_PROMOTIONS` and `facts.copilot.*`; no wave-1 test changes (CA-35, CA-36, CA-25) |
| `CacheRules` fields are non-optional; DETECT-CACHE already started | CA-32 uses `ttl_options_s=()` and a conservative-channel set; no type widening |
| `long_context.threshold_input_tokens` vs merged `threshold` | §6.1 uses `threshold` |
| `PricedTotal.allowance` semantic narrowing | declared as a contract change with unchanged behaviour for existing data (CA-12) |
| `normalize_model(provider_hint="github")` drops routing/speed/pseudo | adapters call `normalize_copilot_model`; delegation maps pseudo labels to `reason` (CA-20) |
| No carrier for the reconciler's decided convention and `gross_is_list` | `ChannelDecision`, `ReconciliationReport.decisions`, `AnalysisContext.channel_decisions` (CA-11, CA-23, CA-39) |
| `AnalysisContext` lacks `outcomes` (cloud-agent-cost) | CA-11 `outcomes` |
| Detector gating too strict | per-kind capability gates; `requires` only the extension gate (§10.0) |
| CA-8 validators need catalog vocabularies defined later (1.5b) | vocabularies move to `core.records` (CA-8, CA-41) |
| CP-OTEL (2.6k) cannot absorb a collector; two wave-2 packages must not import each other | CP-VSCODE split out; the chat-span extractor, the attribute allowlist and the `github_copilot.otel` convention move to the new core file `core/spans.py` (CA-42) |
| CP-PULL's recorded directory holds logins and must not be the handoff artifact | `--out *.tbx`: private 0700 temp dir → export → delete (§5.12) |
| Critic 0.20(c): CA-8 comment still placed the zero-user budget's `p_` inside attrs | comment fixed: team / cost center only (CA-8) |
| Reconciler decisions lost between runs on a persistent store | `ExtRecordStore.put_decisions` / `decisions()`; `copilot_decisions` table (CA-23, §7.2) |
| TELEM and REPLAY already started (R-E15) | late change requests R-E23 (§21.4a); `is_copilot_resource` lands in wave 1.5 before they merge |
| CA-39 "all functions take notes"; §11.3 entry point; §12 numbering; no-float list; provenance class | fixed in place (CA-39, §11.3, §12.1, CA-28, §18) |
| Critic 0.16 (TRACE 3,015, OUT 3,040) | §21.5 binding: `ccusage.py` moves to CLI-LEDGER; TRACE's key lists derived from `core.records.record_fields` (CA-41), net ≤ 0 |
| Design state `_copilot_state.json` still revision 1 | regenerated by the orchestrator from §21 of this revision (not part of this document) |

### What revision 2 changes (critic findings → where fixed)

| finding | fix |
|---|---|
| Wave sequencing (gate 0 closed; R-E4) | wave 1.5 with four amendment packages + F-EXT + F-POOL and gate F' (§2.3, §21) |
| CostLine lacks routing/speed/pseudo; closed cost_type vocabulary | CA-4 (fields, `GITHUB_COST_TYPES`), CA-5 (`pseudo` dim) |
| count_users over requests only; Scope dims; entity findings | CA-24, CA-13, CA-36 (`COUNT_SOURCE`), CA-37, ruling R-E16 (§8.2) |
| `copilot_compliance` extra key; Attribution.extra is pairs | CA-2 |
| FakePricer subset too small | CA-25, CA-38: every Copilot row of §19.2 in facts and FakePricer |
| CP-SYNTH plants unpriceable | §18: window 2026-07-01 → 09-22, Opus 4.8 before 09-22 |
| CLI flag collisions, no argparse hooks | argv aliases (CA-22, CA-39, §15): `scan --copilot` → `copilot scan`; `--github-org` |
| Mixed OTLP files lose Claude spans | disjoint resource filters + deferral (§5.10, §21.4 TELEM/WIRING) |
| Registry imports missing wave-2 modules | skip-with-dq rule (CA-21, CA-33, CA-39, ruling R-E18) |
| Aggregate levers break gate F grammar | `AGGREGATE_GRIDS` + `ADMIN_ACTIONS` (CA-35) |
| ClusterDay.pool_nano; WIRING amendment | CA-12, §21.4 |
| Closed value lists (Fix.target, PolicyPack.target, Scope, Promotion, ConfigSnapshot kinds) | CA-13, CA-8 (`run_flags`), CA-36 (three Gemini promotions), CA-37 (parent chain) |
| Reconciler inputs unbuildable | COST_STATE events for OTel roots (CA-7); `source_stats` (CA-24); `rounding_remainders` in `ChannelReconciler` (CA-23) |
| Store/trace amendments incomplete; revision; NULL keys | natural ids (CA-19), latest-fetch-wins, record store (§7), TRACE (§21.4) |
| Double counting (FOCUS, duplicate kinds) | extension channels own their FOCUS rows (CA-39, §14.3); `FAMILY_EXCLUSIONS` (CA-36, §10.4) |
| Aggregate detectors per shard | `Detector.aggregate` + `run_detectors(aggregates_only=…)` (CA-21, R-E17) |
| Packages over 3k LOC | 22 packages ≤ 3k; SPEC-package increments ≤ 3k (§21) |
| Ownership files, SHA→date map, seat_breakdown owner, merge logic in core, rate_files | CA-30, CP-RATES brief, §4, RECON `merge_reports` (§21.4), `extension_rate_files` (CA-39) |
| No-float list | CA-28 |
| Circular recon tests, facts verification, self-authored fixtures | §12.1 (was §12.6), §18, CA-25 + R-E19 (`verification: research`), experimental flags (§5.9, §5.10) |
| Unit rename, COMPACTION trigger domain, zero-user budgets | `ActivityDay.reported_cost_nano` (CA-8), `copilot_trigger` attr (CA-7), team/entity counts (§10.1) |
| Agentic workflows | DC21, §5.2, §5.14, §10.2 `agentic-workflow-cost`, lever `copilot.agentic_workflow_caps` |
| Seat auto-assignment; team seats | DC20, `seat-auto-assign`, seat reclaim split (§10.1, §11) |
| Volume / subscription billing | DC19, `billing_mode` on `PoolMonth`, §11.3 |
| Code-review cost drivers | §10.2 `review-*`, levers, checklist, DC15 event 2026-06-25 |
| Auto tiers; lever reach | `copilot.auto_tier` lever; reach factors (§9.3) |
| Live-pull auth | token file + resume, per-endpoint auth table, user-token agent-task pull (§5.12, §19.4) |
| VS Code deferral reason | opt-in `copilot-vscode-traces` adapter with key allowlist (§5.13) |
| CA-27 (rev 1) cache rules unverified | rules row with unknown fields (CA-32) |
| One write rate per model contradicted | DC6 replaced: writes are ranges [published, 2 × input] for Claude models (§6.2) |
| Commit dates presented as effective dates | date-source column K/C/D/B, `copilot_rate_boundary` residual (§6.1, §12) |
| Report grouping keys assumed | §19.5 #1; natural keys; per-product fallback in L3 |
| Cap block/continue hard-coded | `capped_policy` input (F-POOL, P7) |
| Auth scopes over-generalized | §19.4 table from the OAS |
| `service.name` hard-coded | configurable Copilot resource predicate (§5.10) |
| Metrics LOC field names | `loc_*_sum` names (§5.5) |
| Session-store inclusivity evidence | third-party, VERIFY (§5.11) |
| Long-context band alternative | session-tier hypothesis → range (§6.2) |
| Labels: writes, seats, open months, discount, compliance, promo, larger runner, direct usage, totals | R16 label discipline (§1.2, §10.0, §14) |

---

## 0. Decision log (Copilot)

| # | Decision | Why |
|---|---|---|
| DC1 | New provider `github`; channel **`github_copilot`** for AI-credit usage; invoice-side channels **`github_actions`** (Actions minutes of Copilot workloads) and **`github_sandbox`** (cloud sandbox meters). Copilot rate rows are their own channel, never derived from Anthropic/OpenAI rows. | GitHub publishes its own per-model table; its GPT-5.6 Sol history differs from OpenAI's (SPEC D36). |
| DC2 | New billing paths **`copilot_pool`** (the user's seat or pooled included credits) and **`copilot_direct`** (metered straight to the organization: `GITHUB_TOKEN` CLI in Actions, agentic workflows, unlicensed/bot code review). **Both map to the new billing class `pool`**: per request they are priced on basis `LIST_EQUIVALENT`, never billed-eligible. | Which request is "the overage", and whether direct usage draws on the pool first (§19.5 #7), are artefacts of GitHub's time-ordered drawdown. Per-request invoice attribution would be dishonest. |
| DC3 | **The pool rule (R11).** Invoice dollars for Copilot exist only at the pool-entity × month level (seats + overage + direct-org + adjacent meters). A credit saving converts to invoice dollars as `min(saving, overage)`; a seat change saves `fee − Δoverage`. Every Copilot saving carries its regime (`slack`, `overage`, `straddling`, `unknown`). Implemented once in `core/pool.py`. | Seat price equals included credits at $0.01, so seat and credit levers substitute depending on the regime. |
| DC4 | **Truth hierarchy.** AI usage report rows are invoice-grade; REST `usage/summary` per SKU is the invoice proxy; client per-request data (CLI, OTel, VS Code traces, gh-aw) is ledger priced by us; `ai_credits_used`, runtime nano-AIU, budget user-states, agent-task `usage.amount` and gh-aw AIC are provider or tool estimates (R12). | Research billing F19/F20, data-apis F11, client-telemetry F1. |
| DC5 | **Rates**: effective-dated rows replayed from the `github/docs` pricing YAML history; published absolute prices; long-context bands; modifiers `github.auto` ×0.9 and `github.compliance` ×1.1 (`stacking: assumed`); fast mode as `speed=fast` with a `replace_base` modifier; promotions in `core.catalog.PROMOTIONS`. **`effective_from` carries a date source** (B billing start, C changelog, D doc text, K docs commit = VERIFY). | Commit dates are merge dates, not billing effective dates (critic 1). |
| DC6 | **Cache writes stay ranges (SPEC R5, unamended).** Claude-model rows carry the published write price as the 5m rate and **2 × input** as a possible 1h rate (**VERIFY**: SDK `cacheWrite1hPrice`, VS Code comment "2x write premium"); other rows carry one write price in both classes (zero-width range, still ESTIMATED). Runtime `cacheTtlSeconds` sets `write_ttl_hint` (the point). | GitHub's SDK defines a separate 1-hour write price; revision 1's "one write rate" claim is withdrawn. |
| DC7 | **Per-user joins, team-only outputs.** Seat, activity and per-user credit rows are stored only under `p_` pseudonyms (org key, or the admin's export key adopted per DC31) so seats × activity × credits can be joined; every output is a k ≥ 5 team aggregate or an entity-level statement (R-E16). No names, no rankings; for seat clean-up the admin gets a GitHub query, not a list. | SPEC §8.4/§8.5. |
| DC8 | **Content-free.** `/copilot/usage-records` (raw bodies) is refused by a registered refusal adapter; `events.jsonl` content fields are dropped at parse time; VS Code `agent-traces.db` is read through a key allowlist; gh-aw artifacts are read only from `token-usage.jsonl`; managed OTel is recommended with `captureContent: false`, `lockCaptureContent: true`. | SPEC D22/D23. |
| DC9 | **Channel extensions through one host.** `core.registry.EXTENSIONS` (typed `ExtensionSpec`) plus the host module `core/extensions.py` (F-EXT) let generic commands, OUT, WIRING and the CLI call Copilot code with one-line calls; missing extension modules degrade to a dq note. | SPEC §3.7 string-map principle; keeps SPEC-package increments small (≤ 3k LOC rule). |
| DC10 | **Two Copilot plans, never summed.** Lane levers on Copilot lanes go through PLAN as billing class `pool` (`ActionPlan.pool_headroom_monthly`). Org levers are planned on the **cell table** by the Copilot aggregate plan (exact Shapley, pool-converted) and returned in `RunResult.copilot`. | The aggregate plan covers 100% of billed usage; lane levers only collector users. |
| DC11 | **Model identity** by `core.models.normalize_copilot_model` (display names, id forms, `Auto:` prefix, `(fast mode)`, pseudo labels). | Billing rows use display names in GitHub's fixtures but `claude-sonnet-4` in the docs example. |
| DC12 | Published per-activity ranges (code review $0.05–$1 Lite / $0.25–$5 Balanced) are quoted context only, never projected (R13). | SPEC honesty rule 6. |
| DC13 | **No enforcement.** REST calls (budgets, cost centers, seats, policies) are emitted as request files; nothing is executed. Live pulls are read-only GETs plus the report-export POST (which only creates a download). | SPEC §1.3. |
| DC14 | **Product families.** `core.findings.cohort_key` keeps its merged 3-tuple `(team, lane_kind, billing_class)` (revision 3: the revision-2 4-tuple would break `tests/v2/sem/test_cohort_key`); Copilot lanes are already separated because only Copilot uses billing class `pool`. A separate `core.findings.product_family(lane)` (`copilot` for channel `github_copilot`) drives detector family filters and the `product: copilot` scope dim; Copilot and Claude Code lanes never share a finding; `core.findings.build_finding` swaps in the Copilot fix from `core.catalog.fix_for` and strips Claude Code config patches; `FAMILY_EXCLUSIONS` turns off generic kinds that Copilot kinds replace (§10.4). | No Claude Code key may appear in a Copilot fix; no double counting. |
| DC15 | **Verification** on team × day panels from the AI usage report repriced at the baseline card (R8). Exogenous events (ITS covariates / placebo dates): 2026-06-25 code-review efficiency (~20% lower review cost), 2026-07-01 enterprise Auto default, 2026-09-01 promo end, 2026-09-14 Auto tiers, 2026-09-23 personal review settings and enterprise default effort, 2026-09-28 review default Balanced, 2026-10-01 upfront seat charges, 2026-10-02 / 2026-10-19 retirements, and every rate change. | SPEC §13, D15. |
| DC16 | **Live pulls** read a token from an env var or a private token file (re-read on 401; GitHub App installation tokens expire after one hour), resume by unit, send `X-GitHub-Api-Version: 2026-03-10`, record no auth headers or signed URLs, and are tested only through an injected opener under the socket guard. Agent-task pulls need a separate user token and enumerate listed repositories. | data-apis F25 (corrected); OAS agent-task descriptions. |
| DC17 | **Package plan.** Wave 1.5 = F-CORE-C → (F-SEM-C ∥ F-KIT-C ∥ F-EXT ∥ F-POOL) → gate F'. Wave 2 adds **19** Copilot packages (revision 3: + CP-HANDOFF, + CP-VSCODE split from CP-OTEL) beside the 17 SPEC packages; all depend only on core; runtime composition only through registry / extension dotted paths, cross-package checks are gate tests. | R-E4, R-E15; PLAN ≤ 3k rule. |
| DC18 | **Local self-view.** `tokenbill copilot me` (alias `me --copilot`) reads the developer's VS Code `agent-traces.db` by default when present (revision 3: VS Code first), then `~/.copilot` when present; JetBrains users are told that no local per-request data exists and that their numbers come from org data; labelled list-equivalent credits. | SPEC D18; owner answer 3. |
| DC19 | **Billing mode per entity** (`metered` \| `volume` \| `azure` \| `unknown`), from seat SKU lines or `--billing-mode`. Cost centers apply only to metered usage; volume licences may bill on the subscription anniversary. Seat savings, deadlines and cost-center budget advice depend on it. | Cost-centers and billing-cycles concept pages (2026-09-23). |
| DC20 | **Seat assignment modes.** `seat_management_setting` (`assign_all` \| `assign_selected` \| `disabled` \| `unconfigured`) and `assigned_via_team` decide which seat removals a request file can actually perform; only direct assignments in `assign_selected` orgs get a projected saving. | OAS `DELETE …/selected_users`: "unless they retain access through team membership". |
| DC21 | **Agentic workflows** (gh-aw) are a Copilot workload: Actions minutes on compiled `.lock.yml` workflows (path mapping **VERIFY**) plus org-billed AI credits (`copilot-requests: write`; default cap 1,000 AIC per run). Covered by a workload, an adapter for gh-aw `token-usage.jsonl`, a finding and a lever. | about-github-agentic-workflows (2026-09-23). |
| DC22 | **Data-driven discount classes.** `discount_amount` is "discount (unclassified)" until L1 shows gross = tokens × list for the row set and L3 shows Σ discount ≤ pool; only then is it labelled pool-included. Whether direct rows draw on the pool is read from their own discounts. | billing-reports reference: discount covers included usage and other discounts. |
| DC23 | **CLI aliases, not shared flags.** `scan --copilot`, `me --copilot` and `collect copilot-cli` are rewritten to the `copilot` command group before argparse (`core.extensions.rewrite_argv`); no Copilot flag is added to another package's parser. | Ownership; avoids the `scan --org` collision. |
| DC24 | **Extension record store.** `LicenseSnapshot`, `ActivityDay`, `ConfigSnapshot` persist in Copilot-owned tables in the same SQLite file through `core.protocols.ExtRecordStore` (CP-STORE); STORE's schema changes stay small. | STORE ≤ 3k LOC; ownership. |
| DC25 | **Label discipline (R16).** INVOICE requires: invoice-side amount (net of a report row, a seat SKU line, an Actions/sandbox line) × final rows × closed month × reconciled channel. Everything else is LIST, LIST_EQUIVALENT or ESTIMATED. Totals take the weakest component label. | Critic 1 (c) items. |
| DC26 | **Admin handoff (revision 3).** The analyst (product owner) needs no GitHub rights. An admin (enterprise owner, org owner or billing manager) runs one offline command, `tokenbill copilot export` (or `copilot pull … --out export.tbx`), which turns the raw GitHub files into a content-free, pseudonymized, k-safe bundle `tokenbill/copilot-export@1` (`*.tbx`, §22.5). The product owner runs `tokenbill copilot scan --from export.tbx` with no token and no network. A no-token path exists (admin downloads report CSVs in the UI, §22.4). Raw GitHub files never leave the admin machine except as a documented last resort. | Owner answer 1; SPEC §8 (pseudonymized data is still personal data). |
| DC27 | **Plan is detected, never assumed (R17).** The Copilot plan of each billing entity (Business, Enterprise, mixed) comes from data in a fixed precedence (seat SKU lines > seats API `plan_type` > org billing `plan_type` > report `total_monthly_quota` (experimental) > admin statement). While any seat's plan is unknown, every pool-dependent figure is computed for **both** scenarios (unknown seats all Business / all Enterprise) and shown side by side, labelled ESTIMATED; no scenario is picked, and plan-change advice is off. | Owner answer 2; `core.pool.detect_plans` (CA-48). |
| DC28 | **Client priority (revision 3).** The org-data path (CP-BILL, CP-ORGDATA, CP-HANDOFF) is primary and release-blocking: it covers 100% of billed usage with no laptop collection. Per-developer client data is ranked VS Code > JetBrains > Copilot CLI; CP-LOCAL (CLI) is non-blocking and kept for CI / `GITHUB_TOKEN` runs. | Owner answer 3. |
| DC29 | **VS Code `agent-traces.db` is a supported source** (revision 2 kept it self-view only). The fact-check confirmed that `span_attributes` carries `copilot_chat.copilot_usage_nano_aiu` and `gen_ai.usage.cache_creation.input_tokens` (`otelSqliteStore.ts`, `chatMLFetcher.ts`), so the file gives per-request tokens incl. cache writes plus a runtime credit estimate. It is read through a key allowlist for the self-view, and collected (opt-in per developer; the setting is user-level only) by `tokenbill copilot collect --source vscode` for fleet or volunteer panels. `chatSessions/*.jsonl` and Agent Debug Logs stay deferred. | Owner answer 3; client-telemetry fact-check correction 1. |
| DC30 | **JetBrains through org data.** No local JetBrains scraping (its stores hold content and no tokens). JetBrains usage is covered by the AI usage report (per user), metrics `totals_by_ide` (`ide:intellij`) and seat / activity-report editor strings (`editor_family = "jetbrains"`, VERIFY). JetBrains OTel ships only behind `IngestOptions.experimental ⊇ {"copilot-jetbrains-otel"}` until a real file is a fixture; managed `model` does not reach JetBrains, so Auto reach is reduced and JetBrains-heavy teams get server-side model policies (§9.3). | Owner answer 3; client-telemetry F19 and fact-check (managed-telemetry support for JetBrains is contradictory in GitHub's docs). |
| DC31 | **Key-id adoption, not re-keying.** Bundle pseudonyms are HMACs under an **export key** that stays on the admin machine and is reused every month (joinable months). The product owner's store *adopts* the bundle's key ids (R-E21): it accepts those `p_` / `h_` values, never computes new ones, and never joins them with values under another key id. The product owner therefore cannot link a pseudonym to a login; erasure requests go through the admin (`copilot pseudonym`, §22.7). | Keeps `CostLine` / `LicenseSnapshot` / `ActivityDay` at `p_` only (no `c_` widening in core). |

---

## 1. Scope

### 1.1 How Copilot is billed (calculator contract, verified 2026-09-23)

```
UNIT         1 AI credit = $0.01. Budgets in USD, usage shown in credits.
LINE PRICE   credits = Σ_bucket tokens × rate_usd_per_Mtok(model, tier, bucket, date) / 10,000
             buckets: input (uncached), cached input (read), cache write, output (reasoning inside output)
TIER         long-context band when the request's input exceeds 272K (GPT-5.4/5.5/5.6 Sol/Terra, GPT-6
             Astra/Luna/Sol) or 200K (GPT-5.6 Luna, Grok 4.5–4.7), band rates for the whole request.
             Alternative reading: the session's context tier (SDK per-tier maxPromptTokens; contextTier
             default|long_context) decides → ranges where the readings disagree (§6.2, VERIFY)
MODIFIERS    × 0.90 Auto model selection on paid plans (Chat, CLI, Copilot app, cloud agent; every Auto tier)
             × 1.10 when a restrict-to-compliant-models policy (data residency / FedRAMP) is on
             (stacking and rounding undocumented → assumed multiplicative, rounded once per line)
WRITES       Claude models: published write price (5m class); a 1-hour write price exists in GitHub's SDK
             (value unpublished → possible 2 × input, VERIFY). Others: one published write price
FREE         code completions, next edit suggestions, utility models, local BYOK
POOL         per billing entity: Σ seats × {Business 1,900 | Enterprise 3,900} credits per month (promo for
             existing customers 2026-06-01 → 2026-09-01: {3,000 | 7,000}); resets 00:00 UTC on the 1st; no
             carry-over; adds raise the pool at once (proration VERIFY); removals shrink it next cycle
PLAN         detected per entity from data (seat SKU lines > seats API plan_type > org billing plan_type >
             report total_monthly_quota > admin statement; §3.9 CA-48); unknown seats → two scenarios
             (all Business | all Enterprise) computed side by side, never one assumed (R17)
ORDER        user-level budget (always a hard stop) → pool → metered at $0.01/credit ("AI credits paid usage"
             ON by default) → cost-center / org / enterprise budget (hard stop only with "Stop usage…").
             Cost centers may cap their pool draw at their licences' allowance; at the cap the admin
             chooses block or continue-as-overage (REST exposes only ai_credit_pool_enabled)
DIRECT       GITHUB_TOKEN CLI in Actions, agentic workflows, unlicensed/bot code review: metered to the org;
             user budgets not considered; pool draw read from the rows' discounts (VERIFY)
SEATS        Business $19, Enterprise $39 list per seat-month; metered: adds prorated, removals billed to
             cycle end; volume licences: annual, possibly billed on the subscription anniversary
ADJACENT     Actions minutes (code review, cloud agent, third-party agents, agentic workflows) beyond
             included minutes (larger runners never use included minutes; public-repo standard runners
             free); cloud sandbox meters; Code Quality licences (its AI credits draw on the pool)
INVOICE      seats + max(0, pooled − pool) × $0.01 (per entity, cap policy applied) + direct-org net
             + Actions + sandboxes + Code Quality   (contract / volume discounts: not modeled, residual)
```

Consequence (R11): while an entity has slack, a saved credit saves $0 now and a removed idle Business seat
saves $19; in overage a saved credit saves $0.01 and a removed seat saves ≈ $0. Appendix C.P1–P15 are the
binding worked examples (P13–P15: unknown plan, conflicting plan evidence, plan from report quota).

### 1.2 Honesty rules added for Copilot (binding with SPEC §1.2)

- **R11 Pool rule.** Copilot usage on either Copilot billing path is list-equivalent per request. Invoice
  dollars exist only at pool-entity × month level. Conversions go only through
  `core.pool.realize_credit_saving` / `realize_seat_change`; the non-converted part is **pool headroom**
  (LIST_EQUIVALENT), shown apart and never added to invoice savings.
- **R12 Provider and tool estimates.** `ai_credits_used`, runtime nano-AIU (`totalNanoAiu`,
  `github.copilot.nano_aiu`, `copilot_chat.copilot_usage_nano_aiu`), budget `user-states.consumed_amount`,
  agent-task `usage.amount`, gh-aw `ai_credits_*` (priced by gh-aw from third-party pricing) and
  `github.copilot.cost` (a multiplier) are never billed numbers; they feed parity checks and forecasts only.
- **R13 Published ranges are context.** Quoted with source and date, never computed into projections.
- **R14 People.** No output lists, ranks or counts individuals below k; seat findings publish team counts
  (k ≥ 5) and the GitHub API filter an admin runs themselves; per-user budgets are published only as counts
  at team (k ≥ 5) or entity level.
- **R15 Dated facts expire.** Findings tied to dated events carry the date and are suppressed after
  `expires`.
- **R16 Invoice label discipline.** A Copilot figure is INVOICE only when it is the sum of invoice-side
  amounts (report-row net, seat-SKU net, Actions/sandbox net) over final rows of a closed month on a
  reconciled channel. Open or provisional months are LIST (observed so far) plus ESTIMATED forecasts;
  counts × list prices, forecasts, counterfactuals and conversions are ESTIMATED; totals take the weakest
  label of their components and name the non-invoice components in the note.
- **R17 Plan honesty (revision 3).** Token Bill never assumes a Copilot plan. A plan is either detected
  from data with its evidence named (`PlanEvidence`, §3.2), stated by an admin (`--plan`, answers file) and
  labelled as a statement, or unknown. While unknown, pool-dependent figures (pool, overage, seat fees,
  realized savings, the invoice total, the aggregate plan) are shown for both scenarios side by side, each
  ESTIMATED, with a "how to find out" hint (§14.2); no single number combines them and no Business-vs-Enterprise
  advice is given. Data beats an admin statement; a conflict is shown, not hidden.
- **R18 Handoff safety (revision 3).** A handoff bundle contains no login, email, numeric user id, raw repo
  name, workflow path, token, signed URL or content; only `p_` / `h_` pseudonyms under the admin's export key,
  team labels of teams with ≥ k seated users (others merged into `(other)` before writing), organizational
  names (org logins and cost-center names; `--hash-workspaces` hashes them) and numbers. A leak gate scans
  every bundle member before it is written; any hit aborts (§22.5).

### 1.3 Non-goals (Copilot)

No proxy; no enforcement; no prompt/response bodies; no per-developer plan recommendations; no Visual
Studio / Xcode / Eclipse / JetBrains local scraping; no individual-plan (Pro/Pro+/Max) optimizer (ingestion
only); no LLM calls; no execution of user-supplied token commands; no minting or refreshing of GitHub App
tokens (stdlib has no RS256; the admin's own tooling writes the token file); no step that requires the
analyst to hold GitHub admin rights.

### 1.4 Deployment this revision is built for (owner answers, 2026-09-23)

| fact | consequence |
|---|---|
| The analyst (product owner) has no GitHub admin access; a colleague with enterprise/org owner or billing-manager rights does the setup | admin handoff kit (§22): guide, one offline export command, a no-token UI path, a pseudonymized bundle the analyst scans without rights or network |
| Business vs Enterprise is unknown | plan detection and side-by-side scenarios (R17, CA-48, §10.1, §11.2, §14) |
| Developers use VS Code and IntelliJ | org data is the primary path; VS Code `agent-traces.db` is the per-developer source; JetBrains via org data; Copilot CLI demoted (DC28–DC30) |
| Works council / privacy | team level only, k ≥ 5, no names (R14, R18) |

---

## 2. Architecture

### 2.1 Layers (Copilot path)

```
 SOURCES (files; recorded pages; live GETs only with --live)          priority: ① org data (primary, release-
                                                                      blocking) ② VS Code ③ JetBrains ④ CLI
  ① GitHub billing: AI usage report CSV | detailed/summarized usage CSV | REST ai_credit/usage, usage/summary,
                  usage, premium_request/usage                                              [CP-BILL]
  ① GitHub org data: budgets (+ user-states) | cost centers | org Copilot billing settings | usage-metrics
                  NDJSON (API or dashboard export) | seats pages | cloud-agent tasks | usage-records (refused)
                                                                                           [CP-ORGDATA]
  ① Handoff:      UI activity-report CSV | admin answers JSON | copilot-export@1 bundle (*.tbx) written on the
                  admin machine, read on the analyst's                                     [CP-HANDOFF]
  ① Live pulls:   report exports, REST pages, metrics downloads (recorded to files; --out *.tbx) [CP-PULL]
  ② VS Code:      agent-traces.db (opt-in per developer) + otel outfile → collect / self-view  [CP-VSCODE]
  ②③ Fleet OTel:  Copilot OTel files (VS Code, CLI; JetBrains experimental) | gh-aw token-usage.jsonl [CP-OTEL]
  ④ CLI / CI:     ~/.copilot session-state events (+ session-store.db, experimental) → collect  [CP-LOCAL]
        │  adapters: conventions, pseudonymization, team/cost-center maps at ingest (admin side for bundles)
        ▼
  CANONICAL LEDGER (STORE) requests/inferences (channel github_copilot, billing class pool), UsageAggregate,
                  CostLine (+ routing/speed/pseudo/…), OutcomeAggregate (+ extra), COST_STATE events
  EXT RECORDS (CP-STORE) LicenseSnapshot, ActivityDay, ConfigSnapshot in copilot_* tables, same SQLite file
        ▼
  PRICING  RateCard ⊕ tokenbill/copilot/data/github_copilot.json (core.extensions.extension_rate_files) [CP-RATES]
        ├──► COPILOT RECONCILER github_copilot / github_actions / github_sandbox, 4 layers   [CP-RECON]
        ├──► POOL LEDGER core.pool: cells, pool months, forecast, regime, realization     [F-POOL]
        ├──► ENRICH ctx: licenses, activity, config, outcomes, plans (both scenarios while
        │               unknown), pools, reconciled channels, reconciler decisions         [CP-STORE]
        ├──► DETECT: copilot.seats-budgets ∥ copilot.org-scan ∥ copilot.lanes ∥ generic   [CP-DET-*]
        ├──► PLAN: lane levers (PLAN, class pool) ∥ Copilot aggregate plan             [CP-PLAN]
        ├──► POLICY: managed-settings patch, admin checklist, REST files, budget design [CP-POLICY]
        └──► VERIFY: Copilot team × day panels → VERIFY estimators                     [CP-RECON]
  OUTPUTS  RunResult.copilot → sections, showback, FOCUS rows (through core.extensions)   [CP-OUT]
  CLI      `tokenbill copilot …`; aliases scan --copilot / me --copilot / collect copilot-cli|copilot-vscode
                                                                                                 [CP-WIRE]
```

### 2.2 Data flows

**Handoff (the deployment of §1.4; primary):** the admin reads `tokenbill copilot admin-guide`, obtains the
GitHub files either with a read-only token (`copilot pull … --out export.tbx`) or by downloading UI reports
(§22.4), fills `admin_answers.json`, and runs `tokenbill copilot export --in … --out export.tbx` offline →
`copilot inspect export.tbx` shows counts only → the file is handed over → the product owner runs
`tokenbill copilot scan --from export.tbx` (no token, no network, temporary or `--db` store that adopts the
bundle's key ids, R-E21) → reconciliation → plan detection (both scenarios while unknown) → pool months →
findings → aggregate plan → policy pack (for the admin to apply) → report. Monthly bundles made with the same
export key join across months.

**Day one, admin analyses directly (`tokenbill copilot scan`):** recorded or `--live` AI usage report + usage
summary + detailed usage + seats + org settings + metrics + budgets/cost centers → temporary store → the
same pipeline. No collectors needed; covers 100% of billed Copilot usage.

**Fleet / volunteers (optional, per developer):** VS Code developers who opt in enable the SQLite span
exporter (a user setting) and run `tokenbill copilot collect --source vscode` daily (MDM or a scheduled
task); the trace@2 files are ingested into the analyst's persistent store, which pseudonymizes them with its
own org key (SPEC §7.2) while it adopts the bundle's key ids for the org data (R-E21). The two key spaces are
joined only by team, never per person (§7.1); managed OTel through the org collector (VS Code, CLI; JetBrains
experimental); CI post-steps (`collect --source cli`) and gh-aw artifacts → cause-level findings on covered
users.

**Self-view:** `tokenbill copilot me` reads VS Code `agent-traces.db` (when the user enabled
`github.copilot.chat.otel.dbSpanExporter.enabled`) and `~/.copilot` into a temporary store with the install
key → own lanes, causes, list-equivalent credits; no pool data, so headroom only. JetBrains users get a note
that no local per-request data exists.

### 2.3 Build sequencing (binding; replaces revision 1 §3 preamble)

```
wave 0  F-CORE ────────────── gate 0 (closed at b0ba621)
wave 1  F-SEM ∥ F-KIT ─────── gate F (core frozen for wave-1 contracts)
wave 1.5a  F-CORE-C   (contract owner applies CA-1…CA-30, CA-41…CA-44 to F-CORE files; ownership check as
                       F-CORE; defines every vocabulary its own validators use, CA-41)
wave 1.5b  F-SEM-C ∥ F-KIT-C ∥ F-EXT ∥ F-POOL   (depend on F-CORE-C only; F-EXT/F-POOL new core files)
        ─────────────────── gate F' : Copilot smoke-on-fakes green; core/* frozen again
wave 2  17 SPEC packages ∥ 19 Copilot packages (§21.2). SPEC packages already started on the gate-F core
        (R-E15: ADMIN, BLOCK, CC, DETECT-CACHE, REPLAY, SYNTH-FLEET, SYNTH-ORACLE, TELEM, VERIFY at 99ba098)
        rebase onto the gate-F' core before merging and take the late change requests of §21.4a; packages
        not yet started get the §21.4 rows in their briefs
        ─────────────────── gate 1 (+ Copilot gate tests)
wave 3  CLI-LEDGER ∥ CLI-SAVINGS (with §21.4 amendments) ── gate 2
wave 4  INTEGRATION (+ Copilot release gates §21.6)
```

SPEC rulings R-E4 and R-E15 are satisfied: every Copilot core addition is additive with defaults and lands
between gate F and the first wave-2 merge; packages that started on the gate-F core keep compiling against it
(no rename, no removed field) and only the two late change requests of §21.4a (TELEM, REPLAY) need code in a
started package. The contract owner (F-KIT agent) runs F-CORE-C and F-KIT-C; F-SEM-C runs as the F-SEM agent or a
fresh session with the F-SEM brief; F-EXT and F-POOL are fresh sessions. The orchestrator records the
one-time F-CORE file grant for F-CORE-C in `tests/v2/kit/RULINGS.md`.

**Tests that pin merged contracts (binding for wave 1.5; none may be edited).** `tests/v2/sem/test_findings.py::
test_cohort_key` (3-tuple); `tests/v2/kit/test_catalog.py` (`LEVERS` == the 24 SPEC levers in order; replay ∈
{usage, block, none}; non-empty grids unless replay none; the needs_eval / trade-off table; `patch_keys ⊆
ALLOWLIST`; `ALLOWLIST` == `facts.settings_keys`, length 31; `PROMOTIONS == (openai.gpt-5.6-sol.2026-08,)`);
`tests/v2/core/test_facts_evidence.py` (settings-key set; modifier `when` keys; top-level rate rows).
Revision 3.1: `tests/v2/core/test_registry.py` (adapter list, 20 detectors, `CONVENTION_MODULES`) and
`tests/v2/core/test_types.py` (`PricedTotal` field names, 21 `RunResult` fields) also pin merged contracts that
the registry entries and the `pool` / `copilot` fields must extend, so exactly three existing tests are edited as
declared contract changes (CORE-AMENDMENTS T-1 … T-3: those two plus a `billing_class` assert in
`test_records.py`); no other existing test changes. Hence
every Copilot lever, key, promotion, modifier and rate row lives in separate tables (`COPILOT_LEVERS`,
`COPILOT_ALLOWLIST`, `COPILOT_PROMOTIONS`, `facts.copilot.*`), and generic lookups (`lever()`,
`promotion_for()`, `allowed()`) search the Copilot tables second (additive).

### 2.4 The extension host (`core/extensions.py`, F-EXT)

Every generic caller uses host functions, never `EXTENSIONS` directly. Each function resolves the dotted
paths of every `ExtensionSpec` lazily; a missing module or resource adds one `DataQualityNote`
(`dq.extension_unavailable`, detail `"<extension>:<hook>"`) to the caller's `notes` list and continues. The
API is fixed in CA-39 (§3.8).

---

## 3. Core changes (wave 1.5; applied after gate F, frozen at gate F')

All additions have defaults; no field is renamed or removed; existing constructors, fixtures and tests keep
working (the pinned tests of §2.3 are not edited). Two changes alter the meaning of a merged rule and are
therefore declared contract changes with their own rulings: the Copilot figure-basis rule in `build_finding`
(R-E20, amends ratified R-E8) and the k-exemption of `product`-scoped findings (R-E16, which amends R-E9).
Owners: **F-CORE-C** (CA-1 … CA-30 and CA-41 … CA-44, edits F-CORE files plus the new F-CORE file
`core/spans.py`, ownership check `--package F-CORE`), **F-SEM-C** (CA-31 … CA-34, CA-45, `--package F-SEM`),
**F-KIT-C** (CA-35 … CA-38, CA-46, CA-47, `--package F-KIT`), **F-EXT** (CA-39, new file
`core/extensions.py`), **F-POOL** (CA-40, CA-48, new file `core/pool.py`). Rulings R-E16 … R-E23 are added to
SPEC Appendix E (as E.3) by the orchestrator (§3.10). **Revision 3.1:** the binding, code-checked form of this
section is `copilot/briefs/CORE-AMENDMENTS.md` (items C-, S-, K-, E-, P-, T-); the CA text below is its design
background, and its "Addendum revision 3 → this list" paragraph says which revision-3 items (CA-41 … CA-48 and
in-place edits) are carried, changed or withdrawn (e.g. no `core/spans.py`, no `core.keys.accepted_key_id`).

### 3.1 `core/records.py` (F-CORE-C)

- **CA-1** `BILLING_PATHS` += `"copilot_pool"`, `"copilot_direct"`. `billing_class(path)` returns
  `"allowance"` iff `subscription`, **`"pool"` iff `copilot_pool` or `copilot_direct`**, else `"billed"`.
  New constant `BILLING_CLASSES = ("billed", "allowance", "pool")`; `COPILOT_BILLING_PATHS`. `Lane.billing_class`
  follows.
- **CA-2** `PricingContext` += `routing: str = "direct"` (`"direct" | "auto" | "unknown"`),
  `compliance: str | None = None` (`"data_residency" | "fedramp"`), `context_tier: str | None = None`
  (`"default" | "long_context"`; Copilot session tier). `provider` gains `"github"`; `channel` gains
  `"github_copilot"`. `EXTRA_KEYS` += `"copilot_compliance"` (value `data_residency|fedramp|none`; readers
  use `dict(attribution.extra).get(...)`, extra stays sorted pairs).
- **CA-3** raw-usage allowlist (the SPEC §4.2 "numbers and allowlisted enum strings", kept as
  `core.records.RAW_USAGE_ENUMS: Mapping[str, frozenset[str]]`; create it if F-CORE kept it elsewhere) gains
  the Copilot keys `tokenType` (`input|cache_read|cache_write|output`), `initiator`, `interactionType`,
  `contextTier`; numeric Copilot keys (`totalNanoAiu`, `batchSize`, `costPerBatch`, `tokenCount`) are numbers.
- **CA-4** `CostLine` += `quantity: str | None = None` (decimal string), `unit: str | None = None`,
  `cost_center: str | None = None`, `team: str | None = None`, `repo: str | None = None` (`h_`),
  `workload: str | None = None`, `workflow: str | None = None` (`h_` of an agentic-workflow path; other
  workflow paths are never stored), `routing: str | None = None`, `speed: str | None = None`,
  `pseudo: str | None = None`. New constants:
  `GITHUB_COST_TYPES = ("ai_credit.user", "ai_credit.direct", "ai_credit.legacy_pru", "seat", "actions",
  "sandbox", "code_quality.license", "metered.ai_credit", "rest.ai_credit", "rest.summary", "rest.usage",
  "other")` (closed; F-POOL, CP-BILL, CP-RECON, CP-OUT agree on these strings);
  `COPILOT_WORKLOADS = ("copilot_code_review", "copilot_cloud_agent", "agentic_workflow", "code_quality")`;
  `COPILOT_PSEUDO = ("code_review", "cloud_agent", "auto_unattributed", "unknown")`.
  New `source_kind` values `"github.ai_usage_report"`, `"github.metered_usage"`, `"github.billing_api"`
  (records re-read from a handoff bundle keep their original `source_kind`; the bundle is visible only as
  `SourceInfo.adapter == "copilot-export"`);
  `channel` values `"github_copilot"`, `"github_actions"`, `"github_sandbox"`.
- **CA-5** `UsageAggregate`: new `source_kind` values `"github.ai_usage_report"`,
  `"github.ai_usage_report.coverage"` (one per file × day: row count and totals, for revision checks),
  `"github.ai_usage_report.quota"` (revision 3, experimental `copilot-ai-usage-quota`: one per month × org ×
  `total_monthly_quota` value, `n_users` = distinct usernames with that quota; plan evidence only, CA-48),
  `"github.agent_tasks"`, `"copilot.cli_rollup"`, `"gh_aw.run"`; allowed `dims` keys += `cost_center`,
  `sku`, `routing`, `pseudo`, `organization`, `convention`, `state`, `artifact`, `repo`, `workflow` (`h_`),
  `source` (`s_`), `quota`, `editor_family`. (`speed`, `team`, `model`, `channel` already exist.)
- **CA-6** `OutcomeAggregate` += `extra: tuple[tuple[str, int], ...] = ()` with keys in
  `OUTCOME_EXTRA_KEYS = ("prs_merged", "prs_created_by_copilot", "prs_merged_created_by_copilot",
  "prs_reviewed_by_copilot", "copilot_suggestions", "copilot_applied_suggestions")`; new `source_kind`
  `"github.copilot_metrics"`.
- **CA-7** `EVENT_ATTRS` (E.1) extended, all new attrs optional (absent = None):
  COMPACTION `copilot_trigger: str|None` ∈ {`threshold`, `manual`, `context_limit_retry`,
  `memory_pressure`, `model_switch`}, `system_tokens: int|None`, `tool_definitions_tokens: int|None` — the
  existing `trigger` domain stays {`auto`, `manual`} (Copilot `manual` → `manual`, every other Copilot
  trigger → `auto`), so DETECT-CACHE's auto/manual branching is unchanged; SESSION_META
  `credit_limit_nano: int|None` (`maxAiCredits` × $0.01), `routing_mode: str|None`, `context_tier: str|None`;
  CONTEXT_EDIT `edit_type` value `"copilot_truncation"`; COST_STATE `reporter` values
  `"copilot.cli.checkpoint"`, `"copilot.otel.invoke_agent"` (root-span nano-AIU total of one conversation),
  `"copilot.vscode.turn"`, `"gh_aw.run_total"`.
- **CA-8** New records (frozen, slots, `to_json`/`from_json` round trip, validators). Every vocabulary the
  validators use (`ACTIVITY_KEYS`, `ACTIVITY_FLAGS`, `CONFIG_KINDS`, `CONFIG_KEYS`, `EDITOR_FAMILIES`,
  `editor_family()`, `PLAN_SOURCES`) is defined in `core.records` by F-CORE-C itself (CA-41); `core.catalog`
  re-exports them (CA-36). Revision 2 referenced catalog names that F-KIT-C adds only in wave 1.5b.

```python
@dataclass(frozen=True, slots=True)
class LicenseSnapshot:
    """One seat of a seat-licensed AI product at a snapshot date. Stored per p_ principal (org key, or an
    adopted export key, R-E21); never exported in clear; published only as k-anonymous counts (R14)."""
    snapshot_date: str                 # YYYY-MM-DD (UTC) of the pull / report_time
    product: str                       # "github_copilot"
    plan: str                          # "business" | "enterprise" | "unknown" (activity report: always unknown)
    principal: str                     # ^p_[0-9a-f]{20}$
    team: str | None
    cost_center: str | None
    org: str | None                    # org login (clear) or h_ with --hash-workspaces; None for
                                       # enterprise-level activity reports
    seat_created: str | None
    pending_cancellation: str | None
    last_activity_bucket: str          # "0-7" | "8-30" | "31-90" | "none_90d"
    last_activity_surface: str | None  # core.records.editor_family value
    last_authenticated_bucket: str
    assigned_via_team: bool | None     # revision 3: None = unknown (activity report has no assignment data)
    fetched_ms: int = 0
    source_kind: str = "github.copilot_seats"   # | "github.copilot_activity_report" (revision 3)

@dataclass(frozen=True, slots=True)
class ActivityDay:
    """Per principal per day activity of a seat-licensed AI product (metrics users-1-day)."""
    date_utc: str
    product: str
    principal: str                     # p_
    team: str | None
    cost_center: str | None
    reported_cost_nano: int | None     # NANO-USD: ai_credits_used × $0.01 (12.5 credits → 125,000,000);
                                       # provider estimate (R12), never billed
    counts: tuple[tuple[str, int], ...]   # keys ∈ core.records.ACTIVITY_KEYS (or "ide:*", "model:*",
                                          #  "feature:*" patterns), sorted
    flags: tuple[str, ...]             # ⊆ ACTIVITY_FLAGS, sorted
    fetched_ms: int = 0
    source_kind: str = "github.copilot_metrics"

@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Content-free configuration state of a billing entity or of this run."""
    snapshot_ms: int
    source_kind: str      # "github.budgets" | "github.cost_centers" | "github.org_copilot_settings"
                          # | "tokenbill.cli" | "tokenbill.admin_answers" (revision 3, §5.17)
    kind: str             # CONFIG_KINDS = ("budget", "budget_users", "cost_center", "org_settings",
                          #  "run_flags")
    entity_id: str        # "enterprise" | "org:<login>" | "cc:<name>" | "budget:<id>" | "run"
                          # (user-scope budgets: entity "budget:<id>"; attrs carry only the user's team /
                          #  cost center from the maps — never the user, a login or a p_ (R14, §5.4))
    attrs: tuple[tuple[str, str | int | bool | None], ...]   # keys ∈ core.records.CONFIG_KEYS[kind], sorted
    fetched_ms: int = 0

def record_key(rec: LicenseSnapshot | ActivityDay | ConfigSnapshot) -> str
    # natural key with "" for None parts (no NULLs in keys):
    # license: (snapshot_date, product, principal, org or ""); activity: (date_utc, product, principal);
    # config: (kind, entity_id, date of snapshot_ms)
```
  `run_flags` attrs (source `tokenbill.cli` or `tokenbill.admin_answers`): `promo_eligible` (bool),
  `compliance` (`none|data_residency|fedramp`), `billing_mode.<entity>` (`metered|volume|azure`),
  `renewal_date.<entity>`, `capped_policy.<cost center>` (`block|continue`), `pool_seats.<entity>.<plan>`
  (int), and (revision 3) `plan.<entity>` (`business|enterprise|mixed`; an **admin statement**, lowest plan
  precedence, never a default, R17), `stop_usage.<budget scope>` (bool; answers-file copy of the "Stop usage"
  flag when budgets were not pulled). A `run_flags` snapshot from `tokenbill.admin_answers` and one from
  `tokenbill.cli` merge key by key, CLI last.

### 3.2 `core/types.py` (F-CORE-C)

- **CA-9** `IngestResult` += `licenses`, `activity`, `config` (lists, `field(default_factory=list)`).
- **CA-10** `IngestOptions` += `cost_center_map: tuple[tuple[str, str], ...] = ()`,
  `otel_service_names: tuple[str, ...] = ()` (extra Copilot `service.name` values, §5.10; revision 3: each
  entry is `NAME` or `NAME=<agent_product>` with `agent_product` ∈ {`copilot_jetbrains`, `copilot_other`};
  `is_copilot_resource` uses the `NAME` part only),
  `experimental: frozenset[str] = frozenset()` (feature flags: `"copilot-store"`, `"copilot-cli-otel-file"`,
  and revision 3 `"copilot-jetbrains-otel"`, `"copilot-ai-usage-quota"`, `"copilot-dashboard-ndjson"`),
  and revision 3 `adopt_key_ids: frozenset[str] = frozenset()` (key ids a handoff import may carry, R-E21;
  read by the `copilot-export` adapter to refuse a bundle whose key id the caller did not name).
- **CA-11** `AnalysisContext` += `licenses: tuple[LicenseSnapshot, ...] = ()`, `activity: tuple[ActivityDay,
  ...] = ()`, `config: tuple[ConfigSnapshot, ...] = ()`, `pools: tuple[PoolMonth, ...] = ()`,
  `reconciled_channels: frozenset[str] = frozenset()`, and revision 3 `outcomes: tuple[OutcomeAggregate,
  ...] = ()` (the §10.2 `cloud-agent-cost` denominator), `channel_decisions: tuple[ChannelDecision, ...] =
  ()` (the reconciler's per-file convention and `gross_is_list`, CA-23), `plans: tuple[PlanEvidence, ...] =
  ()` (CA-14, CA-48).
- **CA-12** `PricedTotal` += `pool: Figure | None = None` (Σ LIST_EQUIVALENT lines on Copilot billing paths).
  **Declared contract change (revision 3):** the merged docstring of `allowance` ("Σ every line on basis
  LIST_EQUIVALENT") becomes "Σ LIST_EQUIVALENT lines on the `subscription` billing path". Before this
  addendum the subscription path was the only LIST_EQUIVALENT path, so every existing input gives the same
  number; F-CORE-C edits the docstring, F-KIT-C's `FakePricer` / `MemoryStore` route pool lines to `pool` and
  add a test that a mixed subscription + `copilot_pool` input splits correctly. `ClusterDay` += `pool_nano:
  int = 0`.
- **CA-13** `Finding` += `headroom: Figure | None = None`. `ActionPlan` += `pool_headroom_monthly: Figure |
  None = None`. `Fix.target` domain += `"github-copilot"`. `PolicyPack.target` domain += `"github-copilot"`.
  `Scope.dims` documented keys += `product`, `entity`, `cost_center`, `org`, `plan`, `bucket`, `surface`,
  `workload`, and revision 3 `plan_scenario` (`business` | `enterprise`; present only while a plan is unknown,
  R17) and `editor_family`.
- **CA-14** New result types (revision 3 adds `PlanEvidence`, the `plan_*` fields, `CopilotBillLine.scenario`,
  `CopilotSummary.plan_status / scenarios / plans_by_scenario` and the bill line `seats.unknown_plan`):

```python
@dataclass(frozen=True, slots=True)
class PlanEvidence:
    """What the data says about one billing entity's Copilot plan in one month (R17)."""
    entity_id: str
    month: str
    plan: str                         # "business" | "enterprise" | "mixed" | "unknown"
    source: str                       # ∈ core.records.PLAN_SOURCES: "seat_lines" | "seats_api" |
                                      #  "org_settings" | "report_quota" | "admin_statement" | "none"
    seats: tuple[tuple[str, str], ...]   # plan → seats (decimal string), incl. "unknown"
    conflict: bool = False            # a lower-precedence source disagreed (the higher one won)
    evidence: tuple[str, ...] = ()    # content-free notes, e.g. "seat_lines: copilot_enterprise 200"
    hint: str | None = None           # how to find out (unknown / conflict only), §14.2

@dataclass(frozen=True, slots=True)
class PoolMonth:
    entity_id: str                    # "enterprise" | "org:<login>" | "cc:<name>" (capped cost center)
    month: str                        # "YYYY-MM" (UTC calendar month; the pool resets on the 1st)
    billing_mode: str                 # "metered" | "volume" | "azure" | "unknown"
    seats: tuple[tuple[str, str], ...]          # plan → seat-months (decimal string); in a scenario the
                                                # unknown seats are counted under the scenario's plan
    seats_source: str                 # "seat_lines" | "run_flags" | "licenses" | "none"
    pool_credits: str; pool_nano: int
    promo: str | None
    consumed_report_nano: int         # Σ credits × $0.01 of pooled report rows (final + provisional)
    consumed_estimate_nano: int       # ai_credits_used for days not yet in the report (R12)
    pool_draw_nano: int | None        # Σ discount classified pool-included (None: unclassified)
    discount_other_nano: int          # classified non-pool discounts (Auto, contract) when separable
    discount_unclassified_nano: int
    overage_observed_nano: int        # Σ net of pooled report rows so far
    direct_net_nano: int              # Σ net of direct-org rows
    direct_draws_pool: str            # "yes" | "no" | "unknown" (from the direct rows' discounts)
    capped_policy: str | None         # cc entities: "block" | "continue" | "unknown"
    days_final: int; days_provisional: int; days_in_month: int
    finality: str                     # "closed" (month ended and every day final) | "open"
    forecast: Figure | None           # ESTIMATED month-end consumption, p10–p90 (open months)
    overage_forecast: Figure | None   # ESTIMATED (open months; range when capped_policy unknown)
    regime: str                       # "slack" | "overage" | "straddling" | "unknown"
    notes: tuple[str, ...] = ()
    plan_source: str = "none"         # revision 3: PlanEvidence.source of this entity × month
    plan_scenario: str | None = None  # None when every seat's plan is known; else "business" | "enterprise"
    plan_conflict: bool = False

BILL_LINES = ("seats.business", "seats.enterprise", "seats.unknown_plan", "ai_credits.gross",
              "ai_credits.discount_pool", "ai_credits.discount_other", "ai_credits.discount_unclassified",
              "ai_credits.overage", "ai_credits.direct_org", "code_quality.licenses",
              "code_quality.ai_credits", "actions.code_review", "actions.cloud_agent",
              "actions.agentic_workflow", "sandbox", "total.invoice")

@dataclass(frozen=True, slots=True)
class CopilotBillLine:
    month: str; entity_id: str
    line: str                          # ∈ BILL_LINES
    quantity: str | None; unit: str | None
    amount: Figure                     # label per R16 (§14.1)
    components: tuple[str, ...] = ()   # total.invoice: the lines it sums
    scenario: str | None = None        # revision 3: "business" | "enterprise" for pool-dependent lines
                                       # while the plan is unknown (seats.unknown_plan, overage, total)

@dataclass(frozen=True, slots=True)
class AdminAction:
    action_id: str; lever_id: str | None
    admin_action: str                  # key of core.catalog.ADMIN_ACTIONS
    where: str                         # "enterprise settings" | "organization settings" | "managed-settings.json"
                                       # | "repository settings" | "personal settings (communicate)" | "REST"
                                       # | "workflow frontmatter"
    what: str                          # ≤ 400 chars, content-free
    doc_url: str
    rest_file: str | None
    auth_note: str | None              # the role / scope the REST call needs (§19.4)
    reach: str | None                  # decimal string: share of the affected credits the delivery reaches
    projection: Figure | None
    deadline: str | None
    needs_eval: bool; tradeoff: bool

@dataclass(frozen=True, slots=True)
class CopilotSummary:
    window: tuple[str, str]
    lines: tuple[CopilotBillLine, ...]
    pools: tuple[PoolMonth, ...]
    teams: PublishedAggregate | None
    seat_counts: tuple[tuple[str, str, int], ...]   # (team or "(other)", "plan:bucket", count), k-anonymous
    plan: ActionPlan | None
    actions: tuple[AdminAction, ...]
    channel_verdicts: tuple[tuple[str, str], ...]   # channel → verdict
    notes: tuple[str, ...] = ()
    plan_status: tuple[PlanEvidence, ...] = ()      # revision 3: one per entity (latest month)
    scenarios: tuple[str, ...] = ()                 # () when every plan is known; else ("business",
                                                    #  "enterprise"); then `plan` is None and
    plans_by_scenario: tuple[tuple[str, ActionPlan], ...] = ()   # holds one plan per scenario
    editor_split: PublishedAggregate | None = None  # revision 3: credits by team × editor_family (k ≥ 5)

@dataclass(frozen=True, slots=True)
class ChannelDecision:
    """What a channel reconciler decided about one source file (revision 3; carried to the enricher)."""
    channel: str
    source_id: str | None
    convention: str | None            # e.g. "github.ai_usage_report.excl" | ".incl"; None = undecidable
    gross_is_list: bool | None        # DC22 (None = not tested / not decidable)
    plan_inferred: str | None = None  # diagnostic only (§12 `copilot_plan_inferred`); never used for labels
    note: str | None = None

@dataclass(frozen=True, slots=True)
class FocusRow:
    """One FOCUS 1.4 row from a channel extension; values already formatted (money as decimal strings)."""
    columns: tuple[tuple[str, str], ...]   # standard columns + x_ columns matching ^x_[A-Z][A-Za-z0-9]{1,48}$
    channel: str
    reconciled: bool
```
- **CA-15** `RunResult` += `copilot: CopilotSummary | None = None`.

### 3.3 `core/money.py`, `labels.py`, `jsonl.py`, `ids.py`, `models.py` (F-CORE-C)

- **CA-16** `money.py`: `NANO_USD_PER_CREDIT = 10_000_000`, `NANO_AIU_PER_CREDIT = 10**9`;
  `credits_str_to_nano(value: str) -> tuple[int, Decimal]`; `nano_aiu_to_nano(n: int) -> tuple[int, Decimal]`
  (÷ 100, half-even); `nano_to_credits_str(nano: int) -> str`. Tests:
  `credits_str_to_nano("42.726213") == (427262130, 0)`; `nano_aiu_to_nano(23_284_800_000) == (232_848_000, 0)`.
- **CA-17** `labels.py`: `figure_json(fig) -> dict[str, object]` (canonical MONEY encoder, SPEC §14.1;
  OUT's `money_json` delegates to it) and `combine_weakest(figs: Sequence[Figure], *, note: str) -> Figure`
  (the sum of figures on different bases: INVOICE iff every input is INVOICE; else basis LIST, EXACT iff every
  input is EXACT, else ESTIMATED with the summed range; the note names the non-invoice inputs;
  LIST_EQUIVALENT inputs raise `ContractViolation` — pool credits are never added to dollars).
- **CA-18** `jsonl.py`: `parse_json_line(raw, *, exact_numbers: bool = False)` (Decimal floats) and
  `load_json_exact(path) -> object`.
- **CA-19** `ids.py`: `natural_id(prefix: str, source_kind: str, *parts: str | int | None) -> str`
  (`stable_id(prefix, source_kind, *("" if p is None else p))`). GitHub adapters use it for `line_id` /
  `agg_id` so overlapping exports produce the same ids (§7.1 latest-fetch-wins).
- **CA-20** `models.py` (content unchanged from revision 1):

```python
@dataclass(frozen=True)
class CopilotModel:
    model: str; routing: str; speed: str; pseudo: str | None; suffix_stripped: str | None
def normalize_copilot_model(model_raw: str) -> CopilotModel
```
  Rules in order: strip; leading `Auto:` (case-insensitive) → routing auto; exactly `auto` → model "",
  pseudo `auto_unattributed`; `unknown` / `others` → pseudo `unknown`; contains `code review` → pseudo
  `code_review`; contains `coding agent`, `padawan` or `cloud agent` → pseudo `cloud_agent` (**VERIFY**
  native labels); remove `[^…]` footnotes and quotes; ` (fast mode)` → speed fast; ` (preview)` removed;
  lowercase; spaces → `-`; `claude-…` ids: `.` → `-`; strip trailing `-1m-internal`, `-1m`, `-internal`;
  collapse `--`. Test table: `Claude Opus 5.5`→`claude-opus-5-5`; `Auto: Claude Haiku 4.5`→
  (`claude-haiku-4-5`, auto); `claude-opus-4.7-1m-internal`→`claude-opus-4-7`; `Claude Opus 4.8 (fast mode)
  (preview)`→(`claude-opus-4-8`, fast); `GPT-5.6 Sol`→`gpt-5.6-sol`; `'GPT-5.4[^2]'`→`gpt-5.4`;
  `GPT-5.3-Codex`→`gpt-5.3-codex`; `Gemini 3.8 Flash`→`gemini-3.8-flash`; `MAI-Code-1.1-Flash`→
  `mai-code-1.1-flash`; `Kimi K2.7 Code`→`kimi-k2.7-code`; `claude-sonnet-4`→`claude-sonnet-4`;
  `gpt-4o-mini-2024-07-18`→`gpt-4o-mini-2024-07-18` (utility; gh-aw fixture). **Revision 3:** the merged
  `normalize_model` returns `ModelId(model, channel_hint, endpoint_scope, reason)` and cannot carry routing,
  speed or pseudo; `normalize_model(raw, provider_hint="github")` therefore returns
  `ModelId(model=<copilot model or "">, channel_hint=None, endpoint_scope="unknown", reason=…)` with reason
  `"copilot pseudo: <pseudo>"` for pseudo labels (model "") and `None` otherwise — a compatibility view only.
  Every Copilot adapter calls `normalize_copilot_model` directly. Also `is_copilot_resource(service_name: str | None, scope_names:
  Iterable[str], attr_keys: Iterable[str], *, extra_service_names: Iterable[str] = ()) -> bool` — the §5.10
  predicate, in core so TELEM (skip) and CP-OTEL (claim) apply the same rule.

### 3.4 `core/registry.py`, `core/protocols.py` (F-CORE-C)

- **CA-21** Registry entries and resilience.
  `BUILTIN_ADAPTERS`: `"copilot-cli"` and `"copilot-otel"` inserted immediately before `"otlp"`; appended:
  `"copilot-vscode-traces"`, `"gh-aw-token-usage"`, `"github-ai-usage"`, `"github-metered-usage"`,
  `"github-billing-api"`, `"github-copilot-config"`, `"github-copilot-metrics"`, `"github-copilot-seats"`,
  `"github-agent-tasks"`, `"github-usage-records"` (dotted paths in §5), and revision 3 (CA-44)
  `"copilot-export"` (zip magic; inserted **first**, so no other adapter sniffs a bundle),
  `"github-copilot-activity-report"`, `"copilot-admin-answers"`. `BUILTIN_DETECTORS` +=
  `"copilot.seats-budgets": "tokenbill.detect.copilot_seats:CopilotSeatsBudgets"`, `"copilot.org-scan":
  "tokenbill.detect.copilot_org:CopilotOrgScan"`, `"copilot.lanes": "tokenbill.detect.copilot_lanes:CopilotLanes"`.
  `CONVENTION_MODULES` += `"tokenbill.core.spans"` (revision 3: `github_copilot.otel`, CA-42),
  `"tokenbill.adapters.copilot_conventions"`, `"tokenbill.adapters.github_billing"`,
  `"tokenbill.adapters.gh_aw"` (`tokenbill.adapters.copilot_otel` no longer registers a convention).
  **Resilience (replaces SPEC §3.7 "raises when requested" for iterating callers):** `sniff_adapter` skips an
  entry whose module cannot be imported and records `dq.adapter_unavailable` (name) in an optional `notes`
  list argument; `get_adapter(name)` still raises for an explicitly requested missing adapter;
  `all_detectors` already skips (E.1).
  **Detector attributes** (class attributes read with `getattr` defaults, so existing detectors need no
  change): `extension: str | None = None`, `aggregate: bool = False`, `families: frozenset[str] | None =
  None` (product families the detector runs on; None = every family not excluded).
  **`run_detectors(lanes, ctx, *, only=None, emit_missing=True, aggregates_only: bool | None = None)`:**
  `aggregates_only=None` runs every detector (tests, back-compat); `False` runs only `aggregate=False`
  detectors (the per-shard call); `True` runs only `aggregate=True` detectors plus the missing-capabilities
  pass (the once-per-run call). Extension detectors run only when `"ext:<extension>" in ctx.capabilities`.
  Lanes passed to a detector are filtered by `core.findings.product_family(lane)` against `families` and
  `core.catalog.FAMILY_EXCLUSIONS` (detector-level); findings whose (detector_id, kind, scope `product`) is
  excluded at kind level are dropped after the run. Output order unchanged.
- **CA-22** Channel extensions:

```python
@dataclass(frozen=True)
class ArgvAlias:
    verb: str; trigger: str
    position: str                 # "first" (argv[1] == trigger) | "any" (trigger anywhere after the verb)
    target: tuple[str, ...]       # replacement prefix; the trigger token is removed

@dataclass(frozen=True)
class ExtensionSpec:
    name: str                                    # "copilot" (RunResult slot name)
    channels: tuple[str, ...]                    # owned channels: RECON/OrgScan skip; FOCUS rows owned
    rate_files: tuple[str, ...]                  # "package:resource.json"
    rate_verifier: str | None
    reconciler: str | None                       # ChannelReconciler
    record_store: str | None                     # "module:Class" implementing ExtRecordStore
    context_enricher: str | None                 # (store, record_stores, ctx, *, today, reconciled_channels,
                                                 #  decisions) -> AnalysisContext (revision 3: matches CA-39)
    summary_builder: str | None                  # (store, record_stores, ctx, findings, plan, pricer, *,
                                                 #  today, k) -> summary
    section_renderer: str | None                 # "module:Class" implementing SectionRenderer
    focus_rows: str | None                       # (store, record_stores, *, since_ms, until_ms,
                                                 #  reconciled_channels, k, allow_unreconciled, role)
                                                 #  -> Iterable[FocusRow]
    showback: str | None                         # (result, out_dir, formats) -> list[Path]
    policy_targets: tuple[tuple[str, str], ...]  # target → builder
    panel_builder: str | None
    command_module: str | None                   # extra top-level verb module ("copilot")
    argv_aliases: tuple[ArgvAlias, ...] = ()

EXTENSIONS: dict[str, ExtensionSpec] = {"copilot": ExtensionSpec(
    name="copilot", channels=("github_copilot", "github_actions", "github_sandbox"),
    rate_files=("tokenbill.copilot.data:github_copilot.json",),
    rate_verifier="tokenbill.copilot.rates_verify:verify",
    reconciler="tokenbill.copilot.recon:reconcile_copilot",
    record_store="tokenbill.copilot.record_store:CopilotRecordStore",
    context_enricher="tokenbill.copilot.enrich:enrich_context",
    summary_builder="tokenbill.pipeline.copilot:summarize",
    section_renderer="tokenbill.copilot.render:CopilotSection",
    focus_rows="tokenbill.copilot.focus:focus_rows",
    showback="tokenbill.copilot.showback:render_copilot_showback",
    policy_targets=(("github-copilot", "tokenbill.pipeline.copilot:policy_packs"),),
    panel_builder="tokenbill.copilot.panel:build_copilot_panel",
    command_module="tokenbill.commands.copilot",
    argv_aliases=(ArgvAlias("scan", "--copilot", "any", ("copilot", "scan")),
                  ArgvAlias("me", "--copilot", "any", ("copilot", "me")),
                  ArgvAlias("collect", "copilot-cli", "first", ("copilot", "collect", "--source", "cli")),
                  ArgvAlias("collect", "copilot-vscode", "first",
                            ("copilot", "collect", "--source", "vscode"))))}
```
  The policy hook `tokenbill.pipeline.copilot:policy_packs` (CP-WIRE) has the `core.extensions.policy_packs`
  signature, assembles the Copilot inputs and calls CP-POLICY's pack builder
  `tokenbill.copilot.policy:build_copilot_packs` (§11.3); revision 2 named both `policy_packs`.
  Core keeps **no** reconciliation-merge logic and **no** RATES file names (revision-1 `merge_reconciliation`
  and `rate_files()` are withdrawn): RECON owns `merge_reports` (§21.4) and RATES keeps its own builtin file
  list, adding `core.extensions.extension_rate_files()`.
- **CA-23** `protocols.py`:
  `ChannelReconciler` — `(ledger: LedgerStore, record_stores: Sequence[ExtRecordStore], pricer, *, since_ms,
  until_ms, tolerance_pct, unexplained_pct, closed_only, today, rounding_remainders: Mapping[str, Decimal] |
  None = None) -> ReconciliationReport` (reads what it needs: cost lines, aggregates, usage records, lanes
  with COST_STATE events, config, licenses); revision 3: `ReconciliationReport` += `decisions:
  tuple[ChannelDecision, ...] = ()` (CA-14), filled by channel reconcilers and ignored by RECON's own
  reconciler; `merge_reports` concatenates them;
  `SectionRenderer` — `name`; `terminal(result, *, width) -> str`; `html(result) -> str` (escaped
  `<section>`, no scripts); `json(result) -> dict | None` (MONEY via `figure_json`);
  `ExtRecordStore` — `name: str`; `put(result: IngestResult, *, principal_key_id: str | None,
  accepted_key_ids: frozenset[str] = frozenset()) -> dict[str, int]` (revision 3: `accepted_key_ids` = the
  store's org key id plus its adopted key ids, R-E21); `licenses(**window) -> list[LicenseSnapshot]`; `activity(**window) -> list[ActivityDay]`;
  `config(**window) -> list[ConfigSnapshot]`; `count_users(*, since_ms, until_ms, where: Mapping[str, str],
  source: str) -> int` (source `"licenses" | "activity"`); `retain(*, identity_before_ms: int) -> int`;
  `purge(*, principal: str | None, before_ms: int | None, actor: str) -> int`; revision 3:
  `put_decisions(decisions: Sequence[ChannelDecision]) -> int` and `decisions() -> list[ChannelDecision]`
  (latest per source; a channel reconciler persists its decisions through the record stores it receives, so
  `findings` / `report` runs on a persistent store get them without re-reconciling; `enrich` falls back to
  them when its `decisions` argument is empty).
- **CA-24** `LedgerStore`: `count_users(*, since_ms, until_ms, where, source: str = "requests") -> int`
  (`"requests"` as SPEC; `"cost_lines"` counts DISTINCT `principal` over cost lines; `where` keys for cost
  lines: `team`, `cost_center`, `channel`, `model`, `sku`, `workspace_id`, `cost_type`); new
  `source_stats(*, adapter: str | None = None) -> dict[str, int]` (Σ of integer `sources.stats_json` values
  by key; used for `rounding_remainders`).

### 3.5 `core/facts.json`, `facts.py`, `builders.py`, SPEC lists, ownership (F-CORE-C)

- **CA-25** `facts.json` gains a `copilot` section. Every row: `source`, `finding`, `verified_on:
  "2026-09-23"`, `verification: "research"` (opened by the research and fact-check tracks; upgraded to
  `"primary"` at the Copilot release gate, ruling R-E19). Contents: `credit` (usd_per_credit "0.01",
  nano_aiu_per_credit); `plans` (business $19/1,900; enterprise $39/3,900; promo 3,000/7,000 for
  2026-06-01 → 2026-09-01); **`rates` = every row of §19.2 (current) and every history row listed there
  (GPT-5.6 Sol/Luna/Terra, Gemini 3.6, closed Opus 4.5/4.6/Sonnet 4.5 rows), each with `effective_from`,
  `effective_to` and `date_source` (B/C/D/K)**; `modifiers` (auto 0.9 with surfaces; compliance 1.1; fast
  replace_base for Opus 4.8); `write_1h_rule` (`claude-*`: 2 × input, `verified: false`); `band_rules`
  (thresholds 272,000 / 200,000 per model); `promotions` (§3.6 CA-36); `skus` (SKU → cost type);
  `workflow_paths`; `utility_models`; `model_categories`; `retirements` (model → date, successor);
  `remaps` (same-vendor candidates with `tokenizer_same`); `runner_rates`; `included_minutes`;
  `review_estimates` (display-only quote); `dates` (§19.1); `settings_keys` (§11.4); `cache_ttl_statement`
  (display); `report_lag_days` 3; `aic_default_cap_per_run` 1,000 (gh-aw); plus the CA-43 sections.
  **Separation (revision 3):** Copilot rate rows, modifiers, promotions and settings keys live only under
  `facts.copilot`; the merged top-level `rate_rows`, `modifiers`, `promotions` and `settings_keys` (pinned by
  `test_facts_evidence.py` and `test_catalog.py`, every top-level settings key has target `claude-code`) are
  unchanged. Every `copilot` entry carries the META keys with `verified_on: "2026-09-23"` so
  `test_facts_load_and_every_entry_has_metadata` holds. Activity / config vocabularies and editor families
  are code in `core.records` (CA-41), not facts.
- **CA-26** `facts.py` accessors: `copilot_rates()`, `copilot_modifiers()`, `copilot_plans()`,
  `copilot_dates()`, `copilot_skus()`, `copilot_workflow_paths()`, `copilot_remaps()`, `copilot_retirements()`,
  `copilot_runner_rates()`, `copilot_settings_keys()`, `copilot_report_lag_days()`.
- **CA-27** `builders.py`: `make_copilot_ctx(model="claude-opus-5-5", **kw)` (provider github, channel
  github_copilot, billing_path copilot_pool), `make_license`, `make_activity`, `make_config`,
  `make_ai_usage_row(...) -> tuple[CostLine, UsageAggregate]` (natural ids, CA-4 fields), `make_seat_line`,
  `make_actions_line`, `make_pool_month`; `CANARY_LOGIN = "tb-canary-login-7f3a91"`; `FlatRates` prices both
  Copilot paths on basis LIST_EQUIVALENT.
- **CA-28** SPEC list amendments: §5.1 capabilities += `credits`, `licenses`, `activity`, `config`,
  `copilot_billing`, `ext:<name>`; §5.1 dq codes += the Copilot codes of §5 and `dq.adapter_unavailable`,
  `dq.extension_unavailable`, `dq.convention_module_unavailable`; §2.4 no-float modules += `core/pool.py`,
  `core/extensions.py`, `adapters/github_billing.py`, `adapters/github_config.py`,
  `adapters/github_metrics.py`, `adapters/github_agent_tasks.py`, `adapters/copilot_cli.py`,
  `adapters/copilot_otel.py`, `adapters/copilot_vscode.py`, `adapters/gh_aw.py`, `copilot/rates_verify.py`,
  `copilot/recon.py`, `copilot/panel.py`, `copilot/enrich.py`, `copilot/plan.py`, `copilot/budgets.py`,
  `copilot/policy.py`, `copilot/summary.py`, `copilot/showback.py`, `copilot/focus.py`,
  `copilot/record_store.py`, `detect/copilot_seats.py`, `detect/copilot_org.py`, `detect/copilot_lanes.py`,
  `synth/copilot_truth.py`, and revision 3 `copilot/admin_actions.py` (projections), `copilot/render.py`,
  `synth/copilot_world.py`, `core/spans.py`, `copilot/handoff.py`, `copilot/admin_answers.py`,
  `adapters/copilot_export.py`, `adapters/github_activity_report.py`, `adapters/copilot_vscode_collect.py`;
  §7.3 `sources_mask` bits `copilot-cli` 256, `copilot-otel` 512, `copilot-vscode-traces` 1024,
  `gh-aw-token-usage` 2048, and revision 3 `copilot-export` 4096.
  Fixture provenance classes stay exactly three (`primary`, `third-party`, `real-redacted`, §5); synthetic
  files written by CP-SYNTH-W are labelled `synthetic` and are **not** a provenance class (they never count as
  acceptance evidence for a parser; revision 2's "primary-shaped synthetic" label is withdrawn).
- **CA-29** `pyproject.toml` package data += `tokenbill/copilot/data/*.json`,
  `tokenbill/copilot/data/snapshots/*.json`, and revision 3 `tokenbill/copilot/handoff_data/*.md`,
  `tokenbill/copilot/handoff_data/*.json` (admin guide and answers template, CP-HANDOFF); new docstring-only
  `tokenbill/copilot/__init__.py` (F-CORE).
- **CA-30** Ownership: `OWNERSHIP.toml` gains every §21.3 row (new packages F-EXT, F-POOL and the 19
  Copilot packages incl. CP-HANDOFF and CP-VSCODE; the new F-CORE file `tokenbill/core/spans.py`; F-KIT gains
  `tests/v2/gates/test_gateF_copilot*.py` under its existing `tests/v2/gates/**`), and the §21.5 move of
  `tokenbill/outputs/ccusage.py` + its tests from OUT to CLI-LEDGER (binding in revision 3); the orchestrator
  mirrors the same rows into SPEC Appendix O and PLAN §2.1 (documents, not code). `scripts/check_ownership.py`
  needs no change.

**Revision-3 additions (F-CORE-C, wave 1.5a).**

- **CA-41** `core/records.py`:
  - Vocabularies moved here from the revision-2 catalog plan (so CA-8 validators need nothing from wave 1.5b):
    `ACTIVITY_KEYS` (§5.5 list), `ACTIVITY_FLAGS`, `ACTIVITY_KEY_PATTERNS = ("ide:", "model:", "feature:")`,
    `CONFIG_KINDS`, `CONFIG_KEYS: Mapping[str, frozenset[str]]` (per kind; `run_flags` keys are prefix
    patterns `billing_mode.`, `renewal_date.`, `capped_policy.`, `pool_seats.`, `plan.`, `stop_usage.` plus
    `promo_eligible`, `compliance`), `EDITOR_FAMILIES = ("vscode", "jetbrains", "visualstudio", "xcode",
    "eclipse", "neovim", "vim", "emacs", "zed", "github_web", "github_mobile", "cli", "copilot_app",
    "other", "unspecified")`, `editor_family(raw: str | None) -> str` (seat `last_activity_editor` strings such
    as `vscode/1.77.3/copilot/1.86.82` → `vscode`; activity-report `last_surface_used` strings such as
    `VS Code 1.89.1` → `vscode`; `JetBrains*` / `IntelliJ*` / `intellij/…` / `jetbrains-*` → `jetbrains`
    (**VERIFY**: exact JetBrains strings, §19.5 #22); `Unspecified` / empty → `unspecified`; unmatched →
    `other`), `PLAN_SOURCES` (CA-14, precedence order).
  - `LicenseSnapshot.assigned_via_team: bool | None` (CA-8) and source kind `github.copilot_activity_report`;
    `ConfigSnapshot` source kind `tokenbill.admin_answers`.
  - `record_fields(cls) -> frozenset[str]` — the JSON field names of a core record dataclass, so TRACE derives
    its closed key sets instead of maintaining hand lists (§21.5; keeps TRACE's increment ≤ 0 LOC).
- **CA-42** New F-CORE file `core/spans.py` — the one Copilot chat-span field extractor, shared by CP-OTEL
  (OTLP and OTel-JS files) and CP-VSCODE (`agent-traces.db`), so two wave-2 packages never import each other:
  `COPILOT_SPAN_KEYS: frozenset[str]` (the §5.13 allowlist; also the privacy contract: no other attribute key
  is ever read); `CopilotSpanFields` (frozen: operation, provider, request/response model, response id,
  conversation id, input / output / cache-read / cache-creation / reasoning tokens, nano-AIU, max prompt
  tokens, turn index, api type, endpoint type, location, service name); `copilot_span_fields(name: str,
  attrs: Mapping[str, object], *, service_name: str | None) -> CopilotSpanFields | None` (None for non-`chat`
  operations; ints parsed exactly; unknown keys ignored, never copied). Pure, no I/O, no floats. The module also registers the convention
  `github_copilot.otel` (§5.11) and is listed in `CONVENTION_MODULES` (CA-21), so CP-OTEL and CP-VSCODE use
  one convention without importing each other.
- **CA-43** `facts.json` `copilot` additions (same row metadata as CA-25): `plan_evidence` (seat SKU → plan:
  `copilot_for_business` → business, `copilot_enterprise` → enterprise, `copilot_standalone` → business
  **VERIFY**; report quota → plan: 1,900 → business, 3,900 → enterprise, promo months 2026-06/07/08: 3,000 →
  business, 7,000 → enterprise, **VERIFY**: the quota column is known only from GitHub's parser fixtures);
  `editor_strings` (the `editor_family` patterns above, JetBrains ones `verified: false`); `vscode_paths`
  (per OS and channel, `verified: false`, §5.13); `handoff` (the §22 auth table rows with their sources and
  VERIFY flags; `copilot admin-guide` renders from them); accessors `copilot_plan_evidence()`,
  `copilot_vscode_paths()`, `copilot_handoff_auth()`.
- **CA-44** Registry and builders for revision 3: `BUILTIN_ADAPTERS` entries of CA-21
  (`"copilot-export": "tokenbill.adapters.copilot_export:CopilotExportAdapter"` first;
  `"github-copilot-activity-report": "tokenbill.adapters.github_activity_report:ActivityReportAdapter"`;
  `"copilot-admin-answers": "tokenbill.copilot.admin_answers:AdminAnswersAdapter"`);
  `core.builders` += `make_plan_evidence`, `make_activity_report_row`, `make_bundle_manifest` (a dict with
  every §22.5 manifest key), `CANARY_EMAIL` reuse in activity-report rows.

### 3.6 `core/findings.py`, `cache_rules.py`, `transitions.py`, `conventions.py` (F-SEM-C)

- **CA-31** `product_family(lane) -> str` (`"copilot"` when the lane's first request's channel is
  `github_copilot`, else `"default"`) and `is_copilot_scope(scope) -> bool` (dims contain `product=copilot`
  or `billing_class=pool`). **`cohort_key` is unchanged** (the merged 3-tuple `(team, lane_kind,
  billing_class)`, pinned by `test_cohort_key`; billing class `pool` already separates Copilot cohorts;
  revision 2's 4-tuple is withdrawn). Copilot detectors put `product=copilot` in their scopes; generic
  detectors' pool-cohort scopes are recognized by `billing_class=pool`. `build_finding` on a Copilot scope:
  validates figures by R-E20 (CA-45); for `billing_class=pool` scopes whose figures include LIST_EQUIVALENT
  it normalizes (never rejects) the title to the prefix `"Copilot credits: "` (truncated to 120 chars) and
  appends " (list-equivalent AI-credit value)" to the summary when absent (≤ 400 chars); it replaces `fix`
  with `core.catalog.fix_for(detector_id, kind, "copilot")` when one exists, else keeps the text but drops
  every `config_patch` whose target is not `github-copilot` and sets `target=None` with the suffix "(no Copilot
  setting known)". So DETECT-CACHE and DETECT-OTHER need no Copilot code.
- **CA-32** Cache rules for (`github`, `github_copilot`) **without widening the frozen `CacheRules` type**
  (its fields are non-optional and DETECT-CACHE, started under R-E15, consumes them): `RulesTable._build`
  returns a dedicated row with `ttl_options_s=()` (no known TTL option; τ comes only from `write_ttl_hint` /
  runtime `cacheTtlSeconds`; the docs statement "24 hours for OpenAI models and 1 hour for most others" is
  display text only), `ttl_measured_from="request_start"`, `refresh_on_read=True`,
  `visible_from="response_end"`, `lookback_positions=None`, `collapse_tool_runs=False`, `max_breakpoints=4`,
  `scope="organization"` (the same values as the merged conservative default for unknown providers),
  `tier_params` = Anthropic's with `context_tier` added to the `system` tier, `sources` naming the
  optimize-ai-usage tutorial and the cost-levers F8 fact-check (1h gated by model; up to 20 + 2 breakpoints
  on the VS Code Responses path). New constant `CONSERVATIVE_TTL_CHANNELS = frozenset({"github_copilot"})`:
  on these channels transitions apply the rule for unknown TTL semantics — a miss is `ttl-expiry` only when τ
  is known and `gap > τ + prev_duration + 10 s` (true under every TTL-start/refresh reading); `ambiguous` when
  `|gap − τ| ≤ prev_duration + 10 s`; else the usual causes. Every other channel is unchanged.
- **CA-33** transitions: `param-change` sub-cause `context-tier-change` (serving inference
  `pricing.context_tier` differs from the previous request's), after `effort-change`.
  `conventions.normalize` imports each `CONVENTION_MODULES` entry independently; an unimportable module is
  skipped with `dq.convention_module_unavailable` (it never blocks other conventions).
- **CA-34** `findings.py`: `headroom` validated by R-E20 (LIST_EQUIVALENT, only on Copilot scopes);
  `min_usd` helper `min_usd_gate(finding, ctx) -> bool` applies to max(recoverable p50, headroom p50) for
  Copilot scopes (credits and dollars compared only as numbers of nano for the gate, never added).
- **CA-45** (revision 3) **R-E20 figure bases in `_validate`** — an explicit amendment of the ratified R-E8
  rule ("one basis per finding; LIST_EQUIVALENT exactly when `billing_class=allowance`; PROVIDER_ESTIMATE
  only in data-quality findings"), which as merged rejects every pool finding of §10:
  1. **Non-Copilot scopes: unchanged** (every merged test in `tests/v2/sem/test_findings.py` stays green);
     additionally `headroom` must be None.
  2. **Copilot scopes** (`is_copilot_scope`): PROVIDER_ESTIMATE only in `data-quality` findings (R4
     unchanged; such findings follow rule 1 otherwise). `cost_observed` ∈ {LIST_EQUIVALENT, LIST, INVOICE}.
     `recoverable`, `recoverable_shapley`, `projected_monthly` share one basis B ∈ {LIST_EQUIVALENT, LIST,
     INVOICE}: B = LIST or INVOICE means pool-converted dollars (`core.pool` realization, seat fees);
     B = LIST_EQUIVALENT means unconverted credit value (generic detectors on pool lanes, the self-view) and
     then `headroom` must be None. `headroom` ∈ {None, LIST_EQUIVALENT}. A Copilot scope never also carries
     `billing_class=allowance`.
  3. Figures are never summed across fields; `core.labels.combine_weakest` still refuses LIST_EQUIVALENT
     inputs.
  New tests (`tests/v2/sem/test_copilot_bases.py`): a `pool-regime`-shaped finding (cost LIST_EQUIVALENT, no
  recoverable) passes; an `idle-seat`-shaped one (cost LIST ESTIMATED, recoverable LIST) passes; an
  `auto-adoption`-shaped one (cost LIST_EQUIVALENT, recoverable LIST, headroom LIST_EQUIVALENT) passes; a
  DETECT-CACHE-shaped pool-lane finding (all LIST_EQUIVALENT) passes and gets the title prefix; LIST_EQUIVALENT
  recoverable + headroom raises; headroom on a billed scope raises; PROVIDER_ESTIMATE on a non-dq Copilot
  finding raises.

### 3.7 `core/catalog.py`, `kanon.py`, `testing.py`, `keys.py` (F-KIT-C)

- **CA-35** Levers. **Revision 3: Copilot levers live in a separate table `COPILOT_LEVERS:
  tuple[LeverDef, ...]`** and Copilot keys in `COPILOT_ALLOWLIST: Mapping[str, AllowedKey]` (built from
  `facts.copilot.settings_keys`), because the merged `test_catalog.py` pins `LEVERS` to the 24 SPEC levers in
  order, `replay ∈ {usage, block, none}`, non-empty grids unless replay is `none`, `patch_keys ⊆ ALLOWLIST`
  and `ALLOWLIST == facts.settings_keys` (31 keys). `LEVERS` and `ALLOWLIST` are unchanged; `lever(id)` and
  `allowed(key)` search the Copilot tables after the SPEC ones; PLAN iterates `LEVERS` only (so it never sees
  an aggregate lever); CP-PLAN and CP-POLICY iterate `COPILOT_LEVERS`. `LeverDef.replay` domain += `"aggregate"`
  (valid only in `COPILOT_LEVERS`; the new catalog test asserts it). Aggregate levers have `grid=()` and their
  candidates in
  `AGGREGATE_GRIDS: Mapping[str, tuple[str, ...]]` using the aggregate grammar
  `copilot:<param>=<value>[@<scope>]` with `<param>` ∈ {`auto`, `auto_tier`, `remap`, `fast`, `seats_idle`,
  `seats_team`, `seat_policy`, `plan`, `runner`, `context_tier`, `mcp`, `aw_cap`} and `<scope>` ∈ {`all`,
  `team:<t>`, `entity:<e>`, `model:<m>`, `org:<o>`}; `parse_aggregate_spec(spec) -> AggregateSpec(param,
  value, scope)` and `to_aggregate_spec` (exact inverse; unknown → `UsageError`). `LeverResult.params` for an
  aggregate lever is its canonical aggregate spec. `ADMIN_ACTIONS: Mapping[str, AdminActionDef]` (id,
  where, doc_url, template text, rest method/path or None, auth note) lists every checklist item and REST
  request of §11.3; Copilot levers' `patch_keys` ⊆ `COPILOT_ALLOWLIST ∪ ADMIN_ACTIONS`. Gate-F' test
  (`tests/v2/kit/test_copilot_catalog.py`): every `AGGREGATE_GRIDS` entry parses and round-trips; every
  Copilot lever's `patch_keys` resolves; `COPILOT_ALLOWLIST == facts.copilot.settings_keys`, every entry
  target `"github-copilot"`; the SPEC tables are byte-identical to gate F. `COPILOT_LEVERS` rows: §11.1;
  `COPILOT_ALLOWLIST` keys: §11.4.
- **CA-36** Data and accessors: **`COPILOT_PROMOTIONS`** (revision 3: separate table; the merged test asserts
  `PROMOTIONS == (openai.gpt-5.6-sol.2026-08,)`; `promotion_for()` searches `PROMOTIONS` then
  `COPILOT_PROMOTIONS`) = `github.gpt-5.6-sol.2026-08` (model gpt-5.6-sol, channel
  github_copilot, start 2026-08-20 (K), not_before_end 2026-09-03 (D)) and **three** Gemini rows
  `github.gemini-3.6-flash.2026`, `github.gemini-3.7-flash.2026`, `github.gemini-3.8-flash.2026` (start
  2026-08-13 / 2026-08-13 / 2026-09-03 (K), not_before_end 2026-12-31 (D)); the hard end lives in the rate
  rows' `effective_to`. `COPILOT_RETIREMENTS: Mapping[str, tuple[str, str]]` (model → (date, successor));
  `RR_PRIORS`; `copilot_allowance(plan, month, *, promo_eligible=True) -> tuple[Decimal, str | None]`
  (revision 3: `plan` ∈ {business, enterprise}; `"unknown"` / `"mixed"` raise `ContractViolation` — callers
  expand scenarios first, CA-48);
  `copilot_cost_type(sku, *, username_present: bool) -> str` (∈ `GITHUB_COST_TYPES`);
  `copilot_workload(workflow_path) -> str | None` (three dynamic paths + `.github/workflows/*.lock.yml` →
  `agentic_workflow`, **VERIFY**); `copilot_category(model)`; `copilot_remap(model) -> tuple[str, bool] |
  None`; `runner_rate(sku)` (both spellings); re-exports of `ACTIVITY_KEYS`, `ACTIVITY_FLAGS`,
  `CONFIG_KEYS`, `editor_family` from `core.records` (CA-41); `agent_family(agent_product)` (incl.
  `copilot_vscode`, `copilot_jetbrains`, `copilot_cli`, `copilot_gh_aw`, `copilot_other`); `fix_for(detector_id, kind,
  family) -> Fix | None` (table §10.4); **`FAMILY_EXCLUSIONS: Mapping[tuple[str, str | None], frozenset[str]]`**
  ((detector_id, kind or None for the whole detector) → excluded families; table §10.4);
  **`COUNT_SOURCE: Mapping[tuple[str, str], str]`** ((detector_id, kind) → `"requests" | "cost_lines" |
  "licenses" | "activity" | "entity"`; default `"requests"`).
- **CA-37** `kanon.py`: `scope_counter(ledger: LedgerStore, record_stores: Sequence[ExtRecordStore], *,
  since_ms, until_ms, source_of: Callable[[Finding], str]) -> Callable[[Finding, Scope], int]` — maps
  Scope → `where` (`team`→team, `cost_center`→cost_center, `model`→model, `org`→workspace_id (cost lines) /
  org (records), `entity` `cc:<n>`→cost_center=n, `org:<o>`→org=o, `enterprise`→no filter, `product`
  `copilot`→channel family filter / product=`github_copilot`, `plan`/`bucket` → license filters) and routes
  by source; `rescope_findings(..., count_users=...)` accepts the two-argument form. Copilot parent chain
  (scopes with a `product` dim): `team` → `editor_family` → `bucket` → `plan` → `model` → `cost_center` →
  entity root (`product`, `entity`, `org` and `plan_scenario` never removed, so scenario findings are never
  merged across scenarios); scopes without `product` keep SPEC's chain. Findings with count source `"entity"`
  follow ruling R-E16; the k-exemption rule for `product`-scoped findings is part of R-E16 (CA-46).
- **CA-38** `testing.py`: `FakePricer` loads **every** `facts.copilot.rates` row and the Copilot modifiers,
  implements the band-hypothesis rule and the Claude 1h write range (§6.2), prices both Copilot paths on
  LIST_EQUIVALENT into `PricedTotal.pool` (and keeps `allowance` = subscription lines, CA-12), and reproduces
  Appendix C.G1–G17. `MemoryStore` implements `count_users(source=…)`, `source_stats`,
  `ClusterDay.pool_nano` and key-id adoption (CA-47). New `MemoryRecordStore` (`ExtRecordStore`).
  `assert_detector_conforms` skips shard invariance for `aggregate=True` detectors and instead asserts
  lane-independence (same findings for `lanes=[]` and any lanes);
  `assert_adapter_conforms` also asserts every `LicenseSnapshot` / `ActivityDay` / `CostLine.principal`
  matches `^p_[0-9a-f]{20}$` and `CANARY_LOGIN` / `CANARY_EMAIL` appear nowhere.
  `assert_record_store_conforms(factory)`. Gate-F' test `tests/v2/gates/test_gateF_copilot_smoke.py` (§21.6).
- **CA-46** (revision 3) `kanon.py` changes owned by F-KIT-C:
  1. **R-E16 `_exempt`.** The merged rule exempts every `category == "aggregate"` finding without person or
     API-key dims (R-E9), which would let team- and cost-center-scoped Copilot findings skip k. New rule: a
     finding whose scope has a `product` dim is exempt **iff** `COUNT_SOURCE[(detector_id, kind)] ==
     "entity"` and its scope has no `team`, `cost_center`, `bucket`, `editor_family` or person dim (R-E16);
     every other `product`-scoped finding is counted with its count source and re-scoped. Scopes without a
     `product` dim keep R-E1 / R-E9 exactly (merged tests unchanged).
  2. **Re-scoping.** `RESCOPE_LEVELS` stays for scopes without `product`; the Copilot chain of CA-37 is a
     second table `COPILOT_RESCOPE_LEVELS`. `_merge_findings` sums `headroom` like `recoverable`
     (`_sum_opt`), so merged findings keep their pool headroom.
  3. **R-E10 `publish()` fix** (scheduled by SPEC E.2 for wave 1.5 and owned by no revision-2 package): rows
     with `n_users == 0` whose grouping keys contain no person or person-proxy dimension (`principal`,
     `session`, `session_key`, `api_key_id`, `cwd_key`) are published with note `users_unknown` instead of
     being merged; audience `self` never suppresses. Tests: the merged `test_kanon.py` stays green; new
     tests for the three rules.
- **CA-47** (revision 3) **Key-id adoption in the fakes** (mirror of the STORE amendment, R-E21):
  `MemoryStore(org_key=None, *, adopt_key_ids: Collection[str] = ())` accepts `p_` / `h_` values of a
  source whose `SourceInfo.principal_key_id` / `name_key_id` equals its org / name key id **or** is in
  `adopt_key_ids` (persisted in its meta as `adopted_key_ids`, append-only); `r_` / `c_` principals without an
  org key still raise `PrivacyError`; values under a key id neither owned nor adopted are nulled with
  `dq.principal_key_mismatch` (SPEC §7.2 behaviour). `MemoryRecordStore.put(result, *, principal_key_id,
  accepted_key_ids)` applies the same rule. `assert_store_conforms` gains the adoption cases. The rule itself
  is one pure helper in F-KIT's `core/keys.py`: `accepted_key_id(source_key_id: str | None, own_key_id: str |
  None, adopted: Collection[str]) -> bool`, which STORE, `MemoryStore` and CP-STORE all call (keeps STORE's
  increment small, §21.4).

### 3.8 `core/extensions.py` (F-EXT, new)

- **CA-39** Host API. Every function that resolves an extension module or resource takes a
  `notes: list[DataQualityNote]` argument and never raises for a missing one; the pure table readers
  (`extensions`, `delegated_channels`, `rewrite_argv`, `command_modules`, `policy_targets`,
  `rate_verifiers`, `capabilities_present`, `count_users_fn`) read only `EXTENSIONS` strings, import nothing
  and take no `notes` (revision 2 said "all functions take notes", which the signatures below never did):

```python
def extensions() -> list[ExtensionSpec]                          # sorted by name
def delegated_channels() -> frozenset[str]
def extension_rate_files(notes) -> tuple[Traversable, ...]      # importlib.resources; missing → dq, skipped
def rewrite_argv(argv: Sequence[str]) -> list[str]               # ArgvAlias rules, first match wins
def command_modules() -> dict[str, str]                          # verb → module (for cli.COMMANDS merge)
def open_record_stores(db_path: Path, *, create: bool, notes) -> list[ExtRecordStore]
def persist(record_stores, result: IngestResult, notes, *,
            accepted_key_ids: frozenset[str] = frozenset()) -> dict[str, int]      # revision 3 (R-E21)
def retain(record_stores, *, identity_before_ms: int, notes) -> int
def purge(record_stores, *, principal: str | None, before_ms: int | None, actor: str, notes) -> int
def capabilities_present(store, record_stores, *, since_ms, until_ms) -> frozenset[str]   # "ext:<name>"
def run_reconcilers(store, record_stores, pricer, *, since_ms, until_ms, tolerance_pct, unexplained_pct,
                    closed_only, today, notes) -> list[ReconciliationReport]   # one per extension
def enrich(store, record_stores, ctx, *, today, reconciled_channels,
           decisions: Sequence[ChannelDecision] = (), notes) -> AnalysisContext
                                    # revision 3: decisions from ReconciliationReport.decisions (CA-23);
                                    # the enricher copies them into ctx.channel_decisions and passes the
                                    # decided convention / gross_is_list to core.pool (CA-48)
def summarize(store, record_stores, ctx, findings, plan, pricer, *, today, k, notes) -> dict[str, object]
def render_sections(result: RunResult, fmt: str, *, width: int = 100, notes) -> list[str | dict]
def focus_rows(store, record_stores, *, since_ms, until_ms, reconciled_channels, k, allow_unreconciled,
               role, notes) -> tuple[list[FocusRow], frozenset[str]]   # rows + channels they own
def showback(result, out_dir: Path, formats, notes) -> list[Path]
def policy_targets() -> dict[str, str]
def policy_packs(target, store, record_stores, ctx, findings, result, *, out_dir, current, cohort_by,
                 include_tradeoffs, notes) -> list[PolicyPack]
def panel(name, store, record_stores, **kw) -> list[PanelRow]
def rate_verifiers() -> list[str]
def count_users_fn(ledger, record_stores, *, since_ms, until_ms) -> Callable[[Finding, Scope], int]
                                                                  # wraps core.kanon.scope_counter
```
  Tests use a fake `ExtensionSpec` registered by `monkeypatch` (a test-only extension named `"fake"` whose
  modules live under `tests/v2/ext/fake_ext/`).

### 3.9 `core/pool.py` (F-POOL, new)

- **CA-40** Pure functions, no I/O, no floats, integer nano; the only implementation of the pool rule.

```python
@dataclass(frozen=True, slots=True)
class Cell:
    month: str; date_utc: str | None; entity_id: str
    team: str | None; cost_center: str | None; org: str | None
    sku: str; cost_type: str; model: str; routing: str; speed: str; pseudo: str | None; workload: str | None
    usage: UsageBuckets               # report tokens under the chosen convention
    credits: str                      # Σ quantity (decimal string)
    gross_nano: int; discount_nano: int; net_nano: int
    n_users: int                      # distinct principals of the contributing cost lines
    final: bool

def entity_of(cost_center: str | None, org: str | None, *, capped: Mapping[str, Decimal],
              entity_mode: str = "enterprise") -> str      # "cc:<n>" iff capped; "org:<o>" in org mode
def build_cells(aggregates: Iterable[UsageAggregate], cost_lines: Iterable[CostLine], *, grain: str = "day",
                convention: str = "excl", capped: Mapping[str, Decimal] = {},
                entity_mode: str = "enterprise") -> tuple[list[Cell], int]   # + count of clamped cells
    # join on (date, team, cost_center, org, sku, model, routing, speed, pseudo) using the CA-4 CostLine
    # fields and the CA-5 aggregate dims; never parses description
def capped_cost_centers(config) -> dict[str, Decimal]
def capped_policies(config) -> dict[str, str]            # run_flags capped_policy.<cc>; default "unknown"
def billing_modes(cost_lines, config, licenses) -> dict[str, str]
    # seat SKU cost lines for the entity → "metered"; run_flags billing_mode.<entity> wins; seats in licenses
    # but no seat lines → "unknown"; azure cost-center subscription with run flag → "azure"
def seat_months(cost_lines, licenses, config, month) -> tuple[dict[tuple[str, str], Decimal], str]
    # ((entity, plan) → seat-months, seats_source): seat SKU lines → run_flags pool_seats → licenses (max count)
def pool_credits(seats: Mapping[str, Decimal], month: str, *, promo_eligible: bool) -> tuple[Decimal, str | None]
def direct_draws_pool(cells: Sequence[Cell]) -> str       # "yes" if any direct cell has discount > 0;
    # "no" if direct cells have discount 0 on days where pooled cells still had discount > 0; else "unknown"
def classify_discounts(cells: Sequence[Cell], *, gross_is_list: bool | None, pool_nano: int
                       ) -> tuple[int | None, int, int]     # (pool_draw, other, unclassified)
    # gross_is_list True (L1: gross = tokens × list for the rows) and Σ discount ≤ pool → pool_draw = Σ
    # discount; Auto rows whose discount = 10% of gross while net > 0 → that part "other"; else unclassified
def forecast(daily_nano: Sequence[tuple[str, int]], *, month: str, today: str, lag_days: int = 3
             ) -> tuple[int, int, int, int]          # (observed, point, low, high); rule below (P9)
def pool_months(cells, cost_lines, licenses, config, *, today: str, promo_eligible: bool = True,
                recent_estimates: Sequence[tuple[str, str | None, int]] = (),
                gross_is_list: bool | None = None,
                aggregates: Iterable[UsageAggregate] = (),                      # revision 3 (quota evidence)
                plans: Mapping[tuple[str, str], PlanEvidence] | None = None    # revision 3; None → detect_plans
                ) -> list[PoolMonth]
    # one PoolMonth per entity × month when every seat's plan is known; else two (plan_scenario "business"
    # and "enterprise"), CA-48
def regime(consumed_low: int, consumed_high: int, pool_nano: int) -> str
def overage_total(entity_consumption: Mapping[str, int], pools: Mapping[str, int], capped: Mapping[str, int],
                  *, policies: Mapping[str, str]) -> tuple[int, int]
    # (low, high): capped cc c with policy continue: over_c = max(0, use_c − cap_c); block: 0 (excess demand
    # blocked, reported separately); unknown: low uses block, high uses continue. Enterprise remainder:
    # max(0, uncapped_use + Σ_c min(use_c, cap_c) − enterprise_pool)
def invoice_delta(consumed_before: int, consumed_after: int, pool_nano: int) -> int
def realize_credit_saving(saving: Figure, pm: PoolMonth) -> tuple[Figure, Figure]   # (invoice, headroom)
def realize_seat_change(pm: PoolMonth, delta: Mapping[str, int], *, month_fee: Mapping[str, Decimal]
                        ) -> Figure | None
    # None when pm.billing_mode is "volume" or "azure" (savings only at renewal; note carries the renewal
    # date when known); else saving = −Δfees − Δoverage, ESTIMATED basis LIST (fees are list prices)
```

**Forecast rule (binding; revision 2 only said "unchanged", revision 1 text restated here).** Observed days
are the days of the month ≤ `today − lag_days` (UTC); `observed` = Σ their consumption. For the remaining days
of the month (including the lag days), each business day (Mon–Fri) is forecast from the last 10 observed
business days and each weekend day from the last 4 observed weekend days (fewer when fewer exist; none →
the other class's statistic; no observed day at all → forecast None and regime "unknown"): `point` adds
the nearest-rank median, `low` the nearest-rank p10, `high` the nearest-rank p90 of that class (nearest rank:
the value at index `ceil(q · n) − 1` of the ascending list, q ∈ {0.1, 0.5, 0.9}). Days after `today` in a
past month do not exist (closed months have no forecast). Integer nano throughout. Appendix C.P9 is the
acceptance case (binding series in the F-POOL brief).

- **CA-48** (revision 3) **Plan detection and scenarios (R17).**

```python
def detect_plans(cost_lines, licenses, config, aggregates, *, month: str
                 ) -> dict[str, PlanEvidence]          # entity_id → evidence for that month
def plan_scenarios(ev: PlanEvidence) -> tuple[str | None, ...]   # (None,) when known; else ("business",
                                                                 #  "enterprise")
```
  Precedence per entity × month (first source with data decides each seat's plan; later sources only
  confirm or flag a conflict): (1) **seat SKU lines** (`cost_type="seat"`; SKU → plan via
  `facts.copilot.plan_evidence`); (2) **seats API** `LicenseSnapshot.plan` (source kind
  `github.copilot_seats`, value ≠ `unknown`); (3) **org billing** `ConfigSnapshot(org_settings).plan_type`
  (applies to that org's seats); (4) **report quota** (`github.ai_usage_report.quota` aggregates, only with
  the experimental flag `copilot-ai-usage-quota`; quota → plan per CA-43, promo quotas only in 2026-06 … 08;
  `Unknown` quota → no evidence; the quota aggregate gives **counts** per plan — n users with 3,900 — not a
  per-seat mapping, so at most that many seats become known and the rest stay unknown); (5) **admin statement** `run_flags plan.<entity>` (`business` /
  `enterprise`; `mixed` states only that both exist and leaves seats unknown). A source that disagrees with
  a higher one sets `conflict=True` and adds `dq.copilot_plan_conflict` naming both sources (data always
  beats the statement; the statement never overrides data). Seat counts come from `seat_months` (seat lines
  → `pool_seats` run flags → licenses incl. activity-report licenses, whose plan is always `unknown`).
  Entity `plan` = `business` / `enterprise` when every seat is known and equal, `mixed` when known and
  both, else `unknown` with `hint` = "check the seats API `plan_type`, the detailed usage report's seat SKU
  (`copilot_for_business` / `copilot_enterprise`) or Enterprise settings → Licensing".
  **Scenarios.** When any seat is unknown, `pool_months` emits two `PoolMonth`s for that entity × month:
  `plan_scenario="business"` (every unknown seat counted as Business) and `"enterprise"` (every unknown seat
  as Enterprise); known seats keep their plan in both; `plan_source` and `plan_conflict` are copied from the
  evidence. Consumption, discounts, direct rows and forecasts are identical in both; pool, overage, regime,
  realized savings and seat fees differ. `realize_credit_saving` / `realize_seat_change` run per scenario;
  `copilot_allowance` is never called with `unknown`. No function ever picks one scenario.
  Acceptance: Appendix C.P13–P15; property tests: a known-plan input yields exactly one PoolMonth per entity
  × month with `plan_scenario=None`; scenario pairs differ only in the pool-dependent fields; permutation
  invariance.

### 3.10 Rulings for SPEC Appendix E (orchestrator; added as a new section E.3)

Revision 2 numbered these R-E5 … R-E8, which SPEC Appendix E.2 already uses (effort channels, fast mode,
unattributed team, semantics ratified, … R-E15). The orchestrator adds E.3 "After wave 1.5 (Copilot)" with the
text of `CORE-AMENDMENTS.md` O-2, which is the single source; revision 3.1 mirrors it here verbatim (revision
3's split of the k-exemption into R-E16 + R-E21 and its R-E22 adoption text are withdrawn):

- **R-E16** (was addendum R-E5; kanon). A finding whose scope has a `product` dim is exempt from
  k-suppression iff its count source (`core.catalog.COUNT_SOURCE`) is `"entity"` and its scope dims are ⊆
  {`product`, `entity`, `org`, `model`, `sku`, `plan_scenario`}; every other `product`-scoped finding uses its
  count source and re-scopes on the Copilot parent chain, **whatever its category** (this overrides R-E9's
  category-`aggregate` exemption for `product`-scoped findings only). Per-user budget facts are published only
  as counts at team (k ≥ 5) or entity level.
- **R-E17** (was R-E6). Aggregate detectors run once per run (`run_detectors(..., aggregates_only=True)`);
  per-shard calls use `aggregates_only=False`; shard invariance applies to lane detectors only.
- **R-E18** (was R-E7). Iterating callers (sniffing, detector listing, convention loading, extension hooks,
  extension rate files) skip unimportable modules and missing resources with a dq note; explicit
  single-entry requests raise.
- **R-E19** (was R-E8). Copilot facts carry `verification: "research"` until the Copilot release gate upgrades
  them to `"primary"` or ships them disabled.
- **R-E20** (amends E.2 R-E8 for Copilot). For findings whose scope has `product=copilot` or
  `billing_class=pool`, the one-basis rule is replaced by per-field domains: `cost_observed` ∈
  {LIST_EQUIVALENT, LIST, INVOICE}; `recoverable`, `recoverable_shapley`, `projected_monthly` ∈ {LIST,
  LIST_EQUIVALENT}; `headroom` = LIST_EQUIVALENT; CONTRACT never; PROVIDER_ESTIMATE only in data-quality
  findings (R4 unchanged). `headroom` on any other finding is a `ContractViolation`. Non-Copilot findings keep
  R-E8 exactly.
- **R-E21** (stores; owner answer 1). A store opened with `adopt_key_ids=True` — with or without its own org
  key — adopts the `principal_key_id` and `name_key_id` of the first ingested **`copilot-export` bundle**
  (`SourceInfo.adapter == "copilot-export"`; never a key id of any other source, so a developer's install
  key is never adopted by accident). It records them in `meta` (`adopted_key_id`, `adopted_name_key_id`; a
  store without its own key also sets `org_key_id` / `name_key_id` to them and `org_key_mode = "adopted"`)
  with an `audit` row, and keeps `p_` / `h_` values whose key id is its own or the adopted one; values under
  any other key id are nulled with `dq.principal_key_mismatch` / `dq.name_key_mismatch`, and a second bundle
  under a different key id is refused (`UsageError`; CLI exit 2, hint "use the same export key every month").
  `r_` / `c_` principals (VS Code / CLI collector files) are pseudonymized with the store's own org key and
  raise `PrivacyError` in a store without one. Rows under the two key ids are never joined per person
  (record-store joins require equal key ids; bundle × collector analysis is by team only). Default
  `adopt_key_ids=False` keeps SPEC §7.2 / §7.6 unchanged. The rejected alternative (widening records to `c_`
  and re-keying with the owner's key) is not implemented.
- **R-E22** (plan; owner answer 2). The Copilot plan is never assumed. `core.pool.detect_plans` decides per
  entity × month with precedence seat SKU lines > seats API `plan_type` > org billing `plan_type` > report
  quota (experimental) > admin statement; data always beats a statement; while any seat's plan is unknown
  every pool-dependent figure is computed and shown for both an all-Business and an all-Enterprise scenario,
  labelled ESTIMATED, side by side, never merged or chosen.
- **R-E23** (late change requests). TELEM and REPLAY started before wave 1.5 build against the gate-F core;
  after gate F' they rebase onto the new core and apply A-4 / A-6 as recorded change requests
  (`tests/v2/kit/CONTRACT-CHANGE-COPILOT-TELEM.md`, `…-REPLAY.md`), gate-tested at gate 1. No other started
  package needs a Copilot change (DETECT-CACHE consumes `CacheRules`, which only gains a defaulted field).

---

## 4. Canonical mapping per source

| source (adapter, owner) | records | channel / billing path | money & label |
|---|---|---|---|
| AI usage report CSV (`github-ai-usage`, CP-BILL) | `CostLine` per row (natural `line_id`; `principal` p_, `team`, `cost_center`, `workspace_id` = org, `repo` h_, `quantity` credits, `cost_type` `ai_credit.user` \| `ai_credit.direct` \| `ai_credit.legacy_pru`, `model`/`routing`/`speed`/`pseudo`, `workload` from pseudo or SKU); `UsageAggregate` per (day, team, cost center, org, model, sku, routing, speed, pseudo) with tokens; one coverage aggregate per (file, day) | `github_copilot` | `amount_nano` = net, `list_amount_nano` = gross; discount = gross − net, class per DC22; labels per R16 |
| Detailed / summarized usage CSV (`github-metered-usage`, CP-BILL) | `CostLine` per row: `seat` (plan from SKU), `actions` (workload from `workflow_path`), `sandbox`, `code_quality.license`, `metered.ai_credit` (cross-check only) | `github_copilot`, `github_actions`, `github_sandbox` | net / gross as reported; seat lines are the only seat-fee source that can become INVOICE |
| Billing REST pages (`github-billing-api`, CP-BILL) | `CostLine` `rest.ai_credit` / `rest.summary` / `rest.usage` | as SKU | invoice proxy (L3) |
| Budgets, cost centers, org Copilot billing incl. `seat_breakdown` and `seat_management_setting` (`github-copilot-config`, CP-ORGDATA) | `ConfigSnapshot` | — | budget amounts in nano (whole dollars) |
| Usage metrics NDJSON (`github-copilot-metrics`, CP-ORGDATA) | `ActivityDay` per user-day; `OutcomeAggregate` per (day, team) (k-merged) and `(enterprise)` / `(org:<login>)` PR rows; user-teams → team map (not stored) | — | `reported_cost_nano` provider estimate (R12) |
| Seats pages (`github-copilot-seats`, CP-ORGDATA) | `LicenseSnapshot` per seat | — | no money (seat fees come from seat lines, else ESTIMATED list × count) |
| Cloud-agent tasks (`github-agent-tasks`, CP-ORGDATA) | `UsageAggregate(github.agent_tasks)` per session | `github_copilot` | provider estimate |
| Usage records (`github-usage-records`, CP-ORGDATA) | nothing; `dq.raw_bodies_ignored` | — | refused (DC8) |
| Copilot CLI events (+ store, experimental) (`copilot-cli`, CP-LOCAL) | sessions, lanes, requests, exact COMPACTION inferences, LaneEvents, COST_STATE; `UsageAggregate(copilot.cli_rollup)` fallback | `github_copilot`; `copilot_pool` unless `--billing-path copilot_direct` (both class `pool`) | priced by us, LIST_EQUIVALENT; nano-AIU → provider estimate |
| Copilot OTel (`copilot-otel`, CP-OTEL) | requests from `chat` spans; root `invoke_agent` totals → COST_STATE `copilot.otel.invoke_agent`; JetBrains resources only with `copilot-jetbrains-otel` | same | same |
| VS Code `agent-traces.db` + otel outfile (`copilot-vscode-traces` and `copilot collect --source vscode`, CP-VSCODE; opt-in per developer) | requests from chat spans (allowlisted attributes, `core.spans`), `agent_product="copilot_vscode"`; collector output is trace@2 `usage` | same | same |
| AI usage report `total_monthly_quota` (`github-ai-usage`, CP-BILL; experimental `copilot-ai-usage-quota`) | `UsageAggregate(github.ai_usage_report.quota)` per month × org × quota value (`n_users`) | — | plan evidence only (CA-48) |
| Copilot activity report CSV (`github-copilot-activity-report`, CP-HANDOFF) | `LicenseSnapshot` per user (`plan="unknown"`, `assigned_via_team=None`, `last_activity_surface` from `last_surface_used`, source kind `github.copilot_activity_report`) | — | no money; seat count evidence |
| Admin answers JSON (`copilot-admin-answers`, CP-HANDOFF) | one `ConfigSnapshot(kind="run_flags", source_kind="tokenbill.admin_answers")` + `ConfigSnapshot(kind="cost_center")` rows for stated caps | — | statements, labelled as such (R17) |
| Handoff bundle `*.tbx` (`copilot-export`, CP-HANDOFF) | the records written by the admin's export (cost lines, aggregates, outcomes, licenses, activity, config), unchanged; `SourceInfo.principal_key_id` / `name_key_id` = the export key id | as recorded | as recorded; requires key-id adoption (R-E21) |
| gh-aw `token-usage.jsonl` (`gh-aw-token-usage`, CP-OTEL) | one request per line (workload ci, `agent_product="copilot_gh_aw"`); run total → COST_STATE `gh_aw.run_total`; one `UsageAggregate(gh_aw.run)` per run (dims `repo`, `workflow`, `source`) | `github_copilot`, `copilot_direct` default | priced by us; gh-aw AIC → tool estimate (R12) |

Identity: every GitHub source is central (`identity_mode="central-ingest"`): logins and numeric user ids are
mapped to team / cost center through `opts.team_map` / `opts.cost_center_map`, pseudonymized with
`opts.principal_key` (the org key; in a handoff export, the admin's **export key**, which is both
`principal_key` and `name_key`, §22.5) and dropped at parse time. Org logins and cost-center names are
organizational (clear by default; `--hash-workspaces` HMACs them). Repo names and agentic-workflow paths →
`h_` (name key). The same login yields the same `p_` in the seats, activity report, metrics and AI usage
report because one key is used for all of them in one run (and across months when the key file is reused).

---

## 5. Adapters

All adapters: stdlib only; lenient quarantine; exact decimals (`core.money`, `core.jsonl.load_json_exact`);
natural ids (`core.ids.natural_id`); `assert_adapter_conforms` with `CANARY`, `CANARY_LOGIN`, canary emails
and repo names planted; hypothesis fuzz per parser. Every fixture is listed in the area README with a
**provenance class**: `primary` (copied or derived field-for-field from a GitHub / Microsoft document, schema
or repository file), `third-party` (derived from a third-party reader), or `real-redacted` (from the adopting
enterprise). Only `primary` and `real-redacted` fixtures count as acceptance evidence for a parser;
features whose only evidence is `third-party` ship behind an `IngestOptions.experimental` flag.

**Priority (revision 3, DC28).** Release-blocking: §5.1–§5.8 (org data) and §5.15–§5.17 (handoff inputs),
because they need no laptop collection and cover 100% of billed usage. Per-developer sources in priority
order: VS Code (§5.13, §5.18), JetBrains (org data only, plus experimental OTel in §5.10), Copilot CLI (§5.9,
non-blocking; kept for CI and `GITHUB_TOKEN` runs), gh-aw (§5.14).

### 5.1 `github-ai-usage` (CP-BILL, `adapters/github_billing.py: AiUsageReportAdapter`)

**Input.** The AI usage report CSV (UI download, or the report export with `report_type: "ai_credit"`), and
the legacy `premium_request` CSV. Documented fields (billing-reports reference, 2026-09-23): `date, product,
sku, quantity, unit_type, applied_cost_per_quantity, gross_amount, discount_amount, net_amount, username,
organization, repository, cost_center_name, model, input, output, cache_read, cache_write`. The reference
says the AI usage report sums amounts by (`date`, `model`, `username`); `sku`, `organization`, `repository`
and `cost_center_name` are columns whose grouping role is **not** documented (§19.5 #1). Observed in
GitHub's own parser and fixtures (`github/copilot-billing-preview`): `total_monthly_quota` (may be
`Unknown`), preview `aic_quantity`, `aic_gross_amount`; legacy `exceeds_quota`. The parser maps by header
name; required: `date, username, product, sku, model, quantity, unit_type, applied_cost_per_quantity,
gross_amount, discount_amount, net_amount, organization`; token columns, `cost_center_name`, `repository`,
`total_monthly_quota` and the aliases `total_input_tokens`, `total_output_tokens`, `total_cache_read_tokens`,
`total_cache_creation_tokens` are optional (0 ≠ absent).

**Sniff.** A CSV header containing `unit_type`, `applied_cost_per_quantity` and `model`.

**Rules.**
1. BOM stripped; quoted fields; dates `YYYY-MM-DD`, `M/D/YY(YY)` or ISO timestamps → UTC date.
2. Money: `usd_str_to_nano` on `gross_amount`, `discount_amount`, `net_amount` (float tails parsed exactly;
   remainders summed into `stats["rounding_remainder_e18"]` as an integer count of 1e-18 USD); `quantity` kept
   as a decimal string; row identity `gross − discount == net` (±1 nano) else `dq.copilot_row_identity`.
3. Kind: `exceeds_quota` present or `unit_type == "requests"` → `ai_credit.legacy_pru` (no tokens,
   `dq.copilot_legacy_pru`); else AI-credit row. Rows dated 2026-04-01 … 30 → `dq.copilot_directional_report`
   and `finality = "provisional"` forever; 2026-04-24 … 30 duplicate rows (`quantity == 0` and quota ≠ 0)
   dropped as GitHub's parser does; non-zero `aic_*` after 2026-06-01 ignored (`dq.copilot_preview_columns`).
4. Model: `normalize_copilot_model(model)` → `model`, `routing`, `speed`, `pseudo` (CostLine fields, CA-4);
   `description = sku + " " + model label` (≤ 128 chars, informational only; no code parses it).
5. Principal and cost type: non-empty `username` → team / cost center via maps (`cost_center_name` column
   wins when present) → `p_`, `cost_type = copilot_cost_type(sku, username_present=True)`; empty username →
   `principal=None`, `cost_type="ai_credit.direct"` (`dq.copilot_unattributed_rows`). `workload`:
   pseudo `code_review` → `copilot_code_review`; pseudo `cloud_agent` or SKU `coding_agent_ai_credit` →
   `copilot_cloud_agent`; SKU `code_quality_ai_credit` → `code_quality`; else None. Unknown SKU → `other`,
   `dq.unmapped_sku`.
6. Tokens → `UsageBuckets` under convention `github.ai_usage_report.excl` (default; `cache_write` →
   `cache_write_unknown`). The reconciler decides the convention per file (§12). Pseudo-model rows carry
   tokens but are never token-priced.
7. Ids: `line_id = natural_id("cl", "github.ai_usage_report", date, principal, model_raw_normalized, sku,
   organization, cost_center_name, repo_h)`; rows of one file with the same natural key are summed
   (`dq.copilot_duplicate_key_summed`); aggregates keyed by `natural_id("ag", …)` on their dims; one
   `UsageAggregate(github.ai_usage_report.coverage)` per (source, day) with dims `channel`, `source`,
   `reported_cost_nano` = Σ net and `list_cost_nano` = Σ gross of that file-day (the row count goes to
   `stats["rows:<date>"]`). `finality="provisional"` for dates within `report_lag_days` of `opts.now_ms` or
   in the open month, else `final`; `fetched_ms = opts.now_ms`.
8. **Plan evidence (revision 3; only with `"copilot-ai-usage-quota" in opts.experimental`).** When the
   `total_monthly_quota` column is present, one `UsageAggregate(source_kind="github.ai_usage_report.quota")`
   per (month, organization, quota value) with dims `organization`, `quota` (decimal string) and `n_users` =
   distinct usernames with that value in the month (usernames are counted, never stored); `Unknown` / empty
   → skipped with a count. The quota never changes a price or a pool; it is plan evidence for CA-48
   (`report_quota`, precedence 4). Without the flag the column is ignored (`dq.copilot_quota_ignored` once).

Capabilities `{aggregates, cost, copilot_billing}`. **Fixture** `ai_usage_2026-09.csv` (primary-derived: the
documented field order, 40 rows): Opus 5.5 rows dated ≥ 2026-09-22 and Sonnet 5 rows with tokens, an
`Auto: Claude Haiku 4.5` row, a fast-mode row, a `code review` pseudo row with an empty username, a GPT-5.5
row, float tails, `total_monthly_quota=Unknown`, a BOM, `M/D/YY` dates, `CANARY_LOGIN`; plus
`ai_usage_legacy_pru.csv`, `ai_usage_april_preview.csv` and `ai_usage_overlap_{a,b}.csv` (two exports
overlapping by 3 days with one revised amount). GitHub's test row
`2026-06-01,mona,copilot,copilot_ai_credit,Auto: Claude Haiku 4.5,42.726213,ai-credits,0.01,0.4272621300000001,0.4272621300000001,0,3900,example-org,`
(header `date,username,product,sku,model,quantity,unit_type,applied_cost_per_quantity,gross_amount,
discount_amount,net_amount,total_monthly_quota,organization,cost_center_name`) parses to gross = discount =
427,262,130 nano, net 0, routing auto, model `claude-haiku-4-5`.

### 5.2 `github-metered-usage` (CP-BILL, `github_billing.py: MeteredUsageAdapter`)

Detailed usage CSV (`date, product, sku, quantity, unit_type, applied_cost_per_quantity, gross_amount,
discount_amount, net_amount, username, organization, repository, workflow_path, cost_center_name`; sums by
`date, sku, organization, repository, cost_center_name, username, workflow_path`) and summarized CSV (sums by
`date, sku, repository, cost_center_name` (+ organization); no `username`, no `workflow_path`). Sniff: header
with `sku` and (`workflow_path` or no `model`). Every row → `CostLine(source_kind="github.metered_usage")`
with channel by product (`copilot` → `github_copilot`, `actions` → `github_actions`, `sandbox` →
`github_sandbox`; other products skipped with a count), `quantity` / `unit`, cost type (`seat` for
`copilot_for_business`, `copilot_enterprise`, `copilot_standalone`; `code_quality.license`; `actions`;
`sandbox`; `metered.ai_credit`). Workload via `copilot_workload(workflow_path)`: the three Copilot dynamic
paths (`dynamic/copilot-swe-agent/copilot` → `copilot_cloud_agent`;
`dynamic/agents/copilot-pull-request-reviewer` and
`dynamic/copilot-pull-request-reviewer/copilot-pull-request-reviewer` → `copilot_code_review`;
`dynamic/github-code-quality/codeql` → `code_quality`) and `.github/workflows/<name>.lock.yml` →
`agentic_workflow` with `workflow = h_(path)` (**VERIFY**: gh-aw compiles each agentic workflow to a
`.lock.yml` Actions workflow; that billing rows carry that path is unconfirmed). Other workflow paths are
never stored (count only). Larger-runner SKUs accepted with and without the `actions_` prefix. The
`discount_amount` of Actions rows (included minutes, public repos, self-hosted) is kept (gross − net).
Principal as §5.1 when `username` is present. Seat lines are the evidence for `billing_mode = "metered"`.
Fixture `detailed_2026-09.csv` (primary-derived) with seats, the three dynamic paths on standard and
`linux_16_core` runners, a `.lock.yml` row, a sandbox row, a user workflow (dropped), public-repo discount
rows.

### 5.3 `github-billing-api` (CP-BILL, `github_billing.py: BillingApiAdapter`)

Recorded JSON pages (one file per response, or JSONL `{"request": {path, query}, "response": {...}}`
envelopes from CP-PULL), parsed with `load_json_exact`:
- `GET /enterprises/{e}/settings/billing/ai_credit/usage` (and org/user variants): `{timePeriod{year, month?,
  day?}, enterprise|organization|user, user?, organization?, product?, model?, costCenter?{id, name},
  usageItems[{product, sku, model, unitType, pricePerUnit, grossQuantity, grossAmount, discountQuantity,
  discountAmount, netQuantity, netAmount}]}` → `CostLine(cost_type="rest.ai_credit")`, date = first day of the
  period, quantity = netQuantity; SKU/unit names through a small alias table (`"Copilot AI Credits"` /
  `"AI Credit"`, `credits` / `ai-credits`; **VERIFY** with a recording).
- `…/premium_request/usage` → legacy lines (`unitType: "requests"`).
- `GET …/settings/billing/usage/summary` → `rest.summary` per `{product, sku}`.
- `GET …/settings/billing/usage` (usage by cost center; `cost_center_id` query) → `rest.usage` per
  `{date, product, sku, quantity (integer), unitType, pricePerUnit, grossAmount, discountAmount, netAmount,
  organizationName, repositoryName?}`; integer `quantity` of AI-credit SKUs ignored
  (`dq.copilot_integer_quantity`).
- Report-export envelopes → no records; `download_urls` never stored.

### 5.4 `github-copilot-config` (CP-ORGDATA, `adapters/github_config.py`)

- Budgets `GET /enterprises/{e}/settings/billing/budgets` (and org): accept `budget_product_sku` (string) and
  `budget_product_skus` (array), `budget_type` ∈ {BundlePricing, ProductPricing, SkuPricing}, integer or
  `1000.0` amounts → `ConfigSnapshot(kind="budget", entity_id="budget:<id>")` attrs `scope, type, sku,
  amount_nano, prevent_further_usage, will_alert, n_recipients, expires_at, consumed_nano, target` where
  `target` is the scope entity (enterprise / `org:<login>` / `cc:<name>`) and, for user and multi-user
  scopes, **`team` / `cost_center` of the user from the maps — never the user or a `p_`** (R14).
- `…/budgets/{id}/user-states` → one `budget_users` snapshot: `n_users, n_at_or_over_target,
  consumed_p50_nano, consumed_p90_nano` (quantiles only when `n_users ≥ k`).
- Cost centers `GET …/cost-centers` → `ConfigSnapshot(kind="cost_center", entity_id="cc:<name>")` attrs
  `cost_center_id, state, pool_enabled, pool_target_credits, pool_current_credits, n_users, n_teams, n_orgs,
  n_repos, azure`; resource names are not stored (`copilot team-map` reads them to build maps).
- `GET /orgs/{org}/copilot/billing` → `ConfigSnapshot(kind="org_settings", entity_id="org:<login>")` attrs
  `plan_type, seat_management_setting` (`assign_all | assign_selected | disabled | unconfigured`),
  `ide_chat, platform_chat, cli, seats_total, seats_added_this_cycle, seats_pending_cancellation,
  seats_pending_invitation, seats_active_this_cycle, seats_inactive_this_cycle`. `plan_type` is plan
  evidence for that org's seats (CA-48, precedence 3).
Sniff by top-level keys (`budgets`, `user_states`, `costCenters`, `seat_breakdown`). Capabilities `{config}`.

### 5.5 `github-copilot-metrics` (CP-ORGDATA, `adapters/github_metrics.py`)

NDJSON from the usage-metrics reports (`users-1-day`, `enterprise-1-day` / `organization-1-day`,
`user-teams-1-day`, `repos-1-day`; 28-day files accepted with `dq.copilot_28day_window`, never joined with
daily team rows), parsed with `parse_json_line(exact_numbers=True)`.
- **users-1-day** → `ActivityDay`: `day` → date; `user_login` / `user_id` → team, cost center, `p_`
  (dropped); `ai_credits_used` → `reported_cost_nano` via `credits_str_to_nano(str(Decimal))`; flags from
  `used_chat, used_agent, used_cli, used_copilot_app, used_copilot_cloud_agent (== used_copilot_coding_agent),
  used_copilot_code_review_active, used_copilot_code_review_passive` (null → absent); counts
  (`ACTIVITY_KEYS`, source field → key): `user_initiated_interaction_count` → `interactions`;
  `code_generation_activity_count` → `code_generation`; `code_acceptance_activity_count` →
  `code_acceptance`; `loc_suggested_to_add_sum` → `loc_suggested_add`; `loc_suggested_to_delete_sum` →
  `loc_suggested_delete`; `loc_added_sum` → `loc_added`; `loc_deleted_sum` → `loc_deleted`;
  `totals_by_cli.{session_count, request_count, prompt_count, token_usage.prompt_tokens_sum,
  token_usage.output_tokens_sum}` → `cli_sessions, cli_requests, cli_prompts, cli_prompt_tokens,
  cli_output_tokens`; `totals_by_copilot_app` likewise → `app_*`; `distinct_mcp_use_count,
  distinct_skill_use_count, distinct_custom_agent_use_count, distinct_plugin_use_count,
  distinct_slash_cmd_use_count` → `mcp_distinct, skill_distinct, custom_agent_distinct, plugin_distinct,
  slash_cmd_distinct`; Σ `totals_by_3rd_party_agent[].user_initiated_interaction_count` →
  `third_party_agent_jobs`; `feature:<f>` for the documented feature values = interactions + generations;
  `model:<id>` from `totals_by_model_feature` (ids normalized; `auto`, `unknown`, `others` kept); `ide:<ide>`
  from `totals_by_ide` (interaction counts). Unknown keys → `dq.unknown_fields` (never stored); masked
  customization names never stored.
- The same records aggregated per (day, team) with `core.kanon.merge_small_groups` → `OutcomeAggregate`
  (`lines_added` ← Σ `loc_added_sum`, `lines_removed` ← Σ `loc_deleted_sum`, `edits_accepted` ← code
  acceptance, `n_users`).
- **enterprise/organization-1-day** → `OutcomeAggregate(team="(enterprise)" | "(org:<login>)")` with
  `pull_requests = pull_requests.total_merged` and `extra` (CA-6); org files never summed into enterprise.
- **repos-1-day** → `(enterprise)` PR totals only (repo ids never stored). **user-teams-1-day** → not stored;
  read by `copilot team-map`. Legacy metrics JSON → counted, skipped (`dq.copilot_legacy_metrics`).
Re-ingested days replace (latest `fetched_ms` wins, record store). Capabilities `{activity, outcomes}`.
Fixture (primary-derived from the usage-metrics field reference and example schema): the documented example
record (`ai_credits_used: 12.5`), 12 users × 3 days, a user-teams file (two teams ≥ 5, one of 3), an
enterprise-1-day record with `pull_requests`.
**Revision 3.** (a) IDE keys: `totals_by_ide[].ide` values (documented examples `vscode, visualstudio,
intellij, eclipse, xcode, neovim, vim, emacs, zed`) are kept as `ide:<value>` counts; `core.records.
editor_family` maps `intellij` (and every JetBrains product name, **VERIFY**) to `jetbrains`, which feeds
the VS Code vs JetBrains split (§14.2) and Auto reach (§9.3). The fixture gains a JetBrains-only user and a
mixed VS Code + IntelliJ user. (b) **Dashboard NDJSON export** (the admin's no-token path, §22.4: Insights →
Copilot usage → export; 28-day rolling, excludes Copilot CLI): accepted only with `"copilot-dashboard-ndjson"
in opts.experimental`, and only records whose keys match the API `users-*` / `*-28-day` shapes (**VERIFY**:
the export's shape is not documented as identical to the API's); treated as a 28-day file
(`dq.copilot_28day_window`, `dq.copilot_dashboard_export_excludes_cli`); non-matching records quarantined with
reason `experimental:copilot-dashboard-ndjson`.

### 5.6 `github-copilot-seats` (CP-ORGDATA, `adapters/github_seats.py`)

`GET /enterprises/{e}/copilot/billing/seats` and `/orgs/{org}/copilot/billing/seats`: `{total_seats,
seats[{created_at, updated_at, pending_cancellation_date, last_activity_at, last_activity_editor,
last_authenticated_at, plan_type ("business"|"enterprise"|"unknown"), assignee{login, id, …},
assigning_team?, organization?}]}` → `LicenseSnapshot` per (principal, org); `assigned_via_team =
assigning_team is not None`; buckets against the fetch date; `last_activity_editor` →
`core.records.editor_family`; assignee name, email, avatar and URLs dropped at parse time. A principal seated
via several orgs keeps one snapshot per org. `plan_type` is plan evidence (CA-48, precedence 2; `unknown`
is no evidence). Capabilities `{licenses}`.

### 5.7 `github-agent-tasks` (CP-ORGDATA, `adapters/github_agent_tasks.py`)

Pages of `GET /agents/repos/{owner}/{repo}/tasks[/{task_id}]` recorded by CP-PULL with a user token (§5.12)
and, for the self-view only, `GET /agents/tasks` (the authenticated user's own tasks; files with that
request path are refused in fleet scans with `dq.copilot_agent_tasks_user_scope`). Sessions `sessions[{id,
state, model, created_at, completed_at, user{id}, repository{id}, usage{type, amount}}]`, `artifacts[{type}]`
→ `UsageAggregate(github.agent_tasks)` per session, dims `model, state, artifact, repo (h_), team`,
`reported_cost_nano = amount / 100` (nano-credits → nano-USD), basis `provider_estimate`;
`premium_requests` sessions skipped (`dq.copilot_legacy_pru`); `prompt` and `name` never read. The manifest's
repository count is carried as the aggregate coverage note ("tasks from N listed repositories").

### 5.8 `github-usage-records` (CP-ORGDATA, `adapters/github_usage_records.py`)

Sniffs `/copilot/usage-records` pages or streams (`type`, `github_request_id`, `endpoint`, `body`,
`@timestamp`); `read()` returns an empty result with `dq.raw_bodies_ignored` (count only).

### 5.9 `copilot-cli` (CP-LOCAL, `adapters/copilot_cli.py`, `adapters/copilot_collect.py`)

**Status (revision 3, DC28).** Non-blocking: the developers of the adopting enterprise use VS Code and
IntelliJ, so CLI data matters mainly for CI (`GITHUB_TOKEN` runs, `--ci`) and for developers who also use the
CLI or VS Code's in-editor CLI agent. Nothing in the org-data or handoff paths depends on this package; its
gate tests run with `importorskip`. The store reader stays experimental.

**Inputs.** `$COPILOT_HOME` or `~/.copilot` (CI: `$HOME/.copilot`): `session-state/<id>/events.jsonl`
(always) and `session-store.db` (**experimental**, only with `IngestOptions.experimental ⊇ {"copilot-store"}`
/ CLI `--experimental copilot-store`, because its schema is known only from third-party readers). Never
opened: `config.json`, `mcp-config.json`, `mcp-oauth-config/`, `mcp-secrets/`, `providers.json`, `logs/`,
`session.db`, `plan.md`, `checkpoints/`, `files/`, `~/.config/github-copilot/apps.json`.

**events.jsonl** (schema: `github/copilot-sdk` `session-events.ts` @075f027; only persisted types appear):
envelope `{id, parentId, timestamp, type, data, agentId?}`; unknown types → `dq.unknown_entry_type`;
concatenated lines recovered from the **last** `{"type":` (`dq.copilot_concatenated_line`). Used for:
`session.start` / `session.resume` (session key `stable_id("ses", "github_copilot", sessionId)`,
`copilotVersion`, `selectedModel == "auto"` → SESSION_META `routing_mode`, `sessionLimits.maxAiCredits` →
`credit_limit_nano`, `contextTier` → SESSION_META `context_tier` and `PricingContext.context_tier` of the
session's requests, `reasoningEffort`; `context.*` → `repo = h_(repository)`, `cwd_key = h_(cwd)`, rest
dropped); `assistant.message` (per `apiCallId` output tokens, `requestId` → `provider_request_id`,
`serviceRequestId`, `model`, `agentId`; content dropped); `session.compaction_complete` (COMPACTION event with
`trigger` auto/manual and `copilot_trigger`, `preCompactionTokens`, `postCompactionTokens`, `tokensRemoved`,
`systemTokens`, `toolDefinitionsTokens`, **and** an exact `Inference(kind=COMPACTION)` from
`compactionTokensUsed` under `github_copilot.token_details` when `copilotUsage.tokenDetails` is present);
`session.model_change` (MODEL_SWITCH_USER or MODEL_FALLBACK); `session.auto_mode_resolved` (routing auto);
`session.truncation` (CONTEXT_EDIT `copilot_truncation`); `session.usage_checkpoint` (COST_STATE
`copilot.cli.checkpoint`; `modelCacheState[].cacheTtlSeconds` → `write_ttl_hint` 300 → `5m`, 3600 → `1h`);
`session.error` (API_ERROR); `tool.execution_complete` (`AppendedItem` sizes only; MCP names `h_`);
`session.shutdown` (rollup → `UsageAggregate(copilot.cli_rollup)` under `github_copilot.shutdown_rollup`
when no per-request input exists; cumulative across resume legs → differenced; `dq.copilot_resume_legs`,
`dq.copilot_compaction_reset`, `dq.copilot_unclean_shutdown`).

**Session store (experimental).** Probe `PRAGMA table_info(assistant_usage_events)`; required `id,
session_id, model, input_tokens, cache_read_tokens, cache_write_tokens, created_at`; optional `turn_index,
copilot_usage_model, output_tokens, reasoning_tokens, total_nano_aiu, duration_ms, initiator,
request_multiplier` (`dq.copilot_store_schema`); `sessions.summary` never read; `cwd` / `repository` → `h_`;
one row = one API request under `github_copilot.session_store`; joined to `assistant.message` by (session,
turn index, model, created_at ± 2 s); unjoined output → `OUTPUT_RESIDUAL`.

**Events-only sessions** (the default until the store is verified) yield exact compaction inferences,
output-only requests and rollup aggregates; lanes lack `usage_sequence` (cache detectors skip them; the
collector README says so).

**Attribution.** `agent_product="copilot_cli"` (host-type mapping **VERIFY**), `billing_path` =
`opts.attribution.billing_path` or `copilot_pool` (`dq.copilot_billing_path_assumed`; both class `pool`),
`routing` per request, `compliance` from `dict(opts.attribution.extra).get("copilot_compliance")`, lanes MAIN /
SUBAGENT, cache scope `"unknown"`, fidelity `NO_TTL_SPLIT`, source priority 39.
**Collector.** `collect_incremental_copilot(home, state, opts, *, now_ms) -> Iterator[IngestResult]` —
per-file byte cursors for `events.jsonl` (head-sha rotation check), per-DB high-water mark on
`assistant_usage_events.id` when the store flag is on, in-flight sessions re-read; never writes to Copilot
files; state file private. Capabilities `{timing, events, lanes_exact, params, credits, appended}` plus
`usage_sequence` only with the store.

### 5.10 `copilot-otel` (CP-OTEL, `adapters/copilot_otel.py`)

**Copilot resource predicate** (`core.models.is_copilot_resource`, CA-20; TELEM and this adapter call the
same function): a resource is Copilot iff `service.name ∈ {"github-copilot", "copilot-chat"}
∪ opts.otel_service_names` (the CLI's `service.name` defaults to `github-copilot` and is configurable via
`OTEL_SERVICE_NAME`; managed `telemetry.serviceName` may set it), **or** an instrumentation scope name starts
with `github.copilot`, **or** any span attribute key under it starts with `github.copilot.` or
`copilot_chat.`.
**Inputs.** (a) The VS Code / Copilot "JSON-lines" file exporter output (VS Code
`github.copilot.chat.otel.outfile`): one OTel-JS SDK object per line (`readableSpanToJson`: `traceId, spanId,
parentSpanContext, name, kind, startTime/endTime` HrTime, `attributes{}`, `events[]`,
`resource{attributes}`; log records; `ResourceMetrics`) — primary (VS Code `fileExporters.ts`); CLI envelope
variants (`type:"span"`, `spanContext`, `hrTime`/`_hrTime`, `timeUnixNano`, `COPILOT_OTEL_FILE_EXPORTER_PATH`)
are **experimental** (`copilot-cli-otel-file`) until a real CLI file is a fixture; (b) OTLP/JSON collector
files (`resourceSpans[].scopeSpans[].spans[]`, int64 as strings).
**Claim rule.** `sniff` is True only when every resource in the head is a Copilot resource; mixed files are
claimed by `otlp` (TELEM), which skips Copilot resources and sets `stats["defer:copilot-otel"] = n`; WIRING's
ingest loop then reads the same file with `copilot-otel`, which reads only Copilot resources (foreign
resources counted in `stats["foreign_resources"]`, no warning). The two passes are disjoint by construction.
**Mapping.** `chat` spans → one Request each (response model else request model, normalized; request model
`auto` → routing auto; `gen_ai.response.id` → `provider_message_id`; `gen_ai.conversation.id` → session key
(same formula as CP-LOCAL); tokens by `github_copilot.otel`; `github.copilot.nano_aiu` /
`copilot_chat.copilot_usage_nano_aiu` → provider estimate; `github.copilot.cost` ignored;
`gen_ai.request.reasoning.level` → effort; initiator / agent attrs → lane kind). `invoke_agent` root spans →
never requests; their nano-AIU total → one COST_STATE event (`reporter="copilot.otel.invoke_agent"`) on the
conversation's main lane (the reconciler's L0 parity input; summing every span double-counts per the CLI
docs). Span events `github.copilot.session.compaction_*`, `…truncation`, `…shutdown` → LaneEvents.
**Dedupe:** inside one trace, native `github-copilot` spans win over VS Code synthesized `copilot-chat` spans
(`dq.copilot_synthesized_span_skipped`); requests keyed by `gen_ai.response.id`, else (conversation id, turn
id, span id). **Content:** `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
`gen_ai.tool.definitions`, tool arguments/results, `github.copilot.tool.parameters.*`,
`copilot_chat.hook_input|hook_output` are counted, never parsed (`dq.raw_bodies_ignored`); identity
(`enduser.pseudo.id`, `user.name`, `process.user.name`, `host.name`) → team map → `p_` → dropped; repository
attributes → `h_`. `agent_product` from `service.name` (`github-copilot` → `copilot_cli`, `copilot-chat` →
`copilot_vscode`, configured names → `copilot_other`). Fidelity `NO_TTL_SPLIT`, priority 21. Pre-1.0.64 CLI
underscore cache attribute names → `dq.copilot_legacy_otel_names`, aggregated only. Span fields are extracted
only through `core.spans.copilot_span_fields` (CA-42), the same function CP-VSCODE uses.
**JetBrains (revision 3, DC30).** JetBrains has OTel export settings ("Settings > Tools > GitHub Copilot >
Chat", changelog 2026-07-27), but the emitted `service.name`, scope names and attribute set are undocumented,
and GitHub's managed-settings page is internally inconsistent about managed `telemetry` for JetBrains (the
support table says yes, the `telemetry` prose says "Copilot CLI and VS Code"). Therefore: JetBrains resources
are read only with `"copilot-jetbrains-otel" in opts.experimental`, and only when their `service.name` is
listed in `opts.otel_service_names` together with the mapping `--otel-service-name NAME=copilot_jetbrains`
(CP-WIRE parses `NAME[=agent_product]`; without `=` the product is `copilot_other`); spans are mapped with the
same allowlist; unknown attribute layouts are counted (`dq.copilot_jetbrains_otel_unmapped`), never guessed.
The flag stays until a real JetBrains file is a `real-redacted` fixture (§21.6). Without the flag, JetBrains
usage is covered by org data only (§5.1 per-user rows, §5.5 `ide:intellij`, §5.6 / §5.15 editor strings).

### 5.11 Conventions

| convention_id (owner) | semantics | mapping | sum-check |
|---|---|---|---|
| `github_copilot.shutdown_rollup` (CP-LOCAL) | `session.shutdown` model metrics; input **includes** cache read and write (CLI 1.0.51 changelog; rollup arithmetic 23,399 = 6 + 10,069 + 13,324) | uncached = input − read − write | read + write ≤ input |
| `github_copilot.session_store` (CP-LOCAL, experimental) | `assistant_usage_events.input_tokens` assumed cache-inclusive — **third-party evidence only** (codeburn, tokscale), **VERIFY** | as above; negative → quarantine | read + write ≤ input else `dq.convention_mismatch` |
| `github_copilot.token_details` (CP-LOCAL) | `copilotUsage.tokenDetails[{tokenType, tokenCount, batchSize, costPerBatch}]`, `input` = uncached (payload arithmetic reproduces published Opus 4.7 rates) | input → uncached; cache_read; cache_write → unknown; output | Σ tokenCount × costPerBatch / batchSize == totalNanoAiu, else `dq.copilot_nano_aiu_mismatch` |
| `github_copilot.otel` (`core.spans`, F-CORE-C; used by CP-OTEL and CP-VSCODE) | `gen_ai.usage.input_tokens` inclusive of `…cache_read.input_tokens` and `…cache_creation.input_tokens` (VS Code source; CLI **VERIFY**) | uncached = input − read − creation; creation → unknown TTL | read + creation ≤ input else exclusive + `dq.convention_mismatch` |
| `gh_aw.token_usage` (CP-OTEL) | gh-aw proxy log; inclusive iff `input_tokens_include_cache` is true; absent → decided by the sum-check | as above | `dq.convention_mismatch` |
| `github.ai_usage_report.excl` / `.incl` (CP-BILL) | report `input`, `cache_read`, `cache_write`, `output`; inclusivity **VERIFY** | excl: input → uncached; incl: uncached = input − read − write | decided per file by the reconciler (§12) |

Models without a published cache-write price: reported write tokens are folded into `uncached_input` with
`usage_source=ESTIMATED` on that inference (`dq.copilot_write_folded_to_input`, **VERIFY**). Utility-model
calls (model ∈ `facts.copilot.utility_models` **and** nano-AIU == 0, or `interactionType ==
conversation-background` with nano-AIU 0) → `billable=False`, `billing_rule_id="github.copilot.utility_unbilled"`
(never by name alone: GPT-5.4 nano is also billed). BYOK calls → `billable=False`,
`billing_rule_id="github.copilot.byok_not_billed_by_github"`.

### 5.12 Live pulls (CP-PULL, `copilot/pull_common.py`, `copilot/pull_billing.py`, `copilot/pull_metrics.py`)

`pull(kinds, *, enterprise, orgs, since, until, token: TokenSource, user_token: TokenSource | None,
agent_repos: Sequence[str], out_dir, resume: bool, opener=None, sleep=time.sleep, now_ms) -> Manifest`,
only via `--live`. **Token sources:** `--github-token-env VAR` or `--github-token-file PATH` (private file,
0600 on POSIX, re-read before every unit and once after a 401). GitHub App installation tokens expire after
one hour, so work is split into **units** (one export window, one metrics day, one REST page set); the
manifest records completed units; a 401 that survives a re-read stops the pull cleanly with exit 3 ("token
expired: refresh the token and re-run with --resume"); `--resume` skips completed units. No token command is
ever executed. stdlib `urllib`, TLS on, 30 s timeout, `Accept: application/vnd.github+json`,
`X-GitHub-Api-Version: 2026-03-10`, `Link` pagination (`per_page=100`), secondary-rate-limit backoff honoring
`retry-after` / `x-ratelimit-reset` (≤ 60 s per wait), 409 on report export → wait and retry (one export per
account at a time). Report export: `POST /enterprises/{e}/settings/billing/reports` (`report_type` `ai_credit`
| `detailed` | `summarized`, `start_date`, `end_date`, `send_email: false`) in ≤ 31-day windows (summarized
≤ 366), poll every 30 s (cap 30 min per window, then the unit is left incomplete for `--resume`), download
immediately. Metrics: `GET …/copilot/metrics/reports/{users-1-day|enterprise-1-day|user-teams-1-day|
repos-1-day}?day=` per day (D-3 and newer re-pulled; 204/404 → "not ready"). Seats, org billing, budgets
(+ user-states), cost centers, usage summary: paged GETs. **Agent tasks** need `--github-user-token-env` (a
GitHub App user token or fine-grained PAT with "Agent tasks" read; installation tokens are not supported) and
`--agent-repos FILE` (owner/repo lines); each repository is paged via `/agents/repos/{o}/{r}/tasks`; the
manifest records the repository count. Recorded files strip `Authorization`, signed URLs and headers other
than `Link`. `copilot pull --help` prints the §19.4 auth table.
**Handoff output (revision 3).** The recorded directory still contains logins (AI usage report `username`,
seats `assignee.login`, metrics `user_login`) and must never be the handoff artifact. With `--out FILE.tbx`
(CP-PULL + CP-HANDOFF, composed by CP-WIRE at runtime): the pull records into a private temporary directory
(`tempfile.mkdtemp`, mode 0700, under the user's temp dir), calls `tokenbill.copilot.handoff.export_bundle`
(§22.5) on it with the same `--user-teams` / `--team-map-csv` / `--answers` / `--k` / `--key-file` options as
`copilot export`, and removes the raw directory afterwards (also on failure, in a `finally`), unless
`--keep-raw DIR` names a directory to move it to (0700; the admin's responsibility; a warning is printed).
`--resume` with `--out` keeps the temp directory path in a private state file so an interrupted pull
continues; it is deleted when the bundle is written. `--out DIR` (a directory) keeps the revision-2
behaviour for admins who analyse themselves.

### 5.13 `copilot-vscode-traces` (CP-VSCODE, `adapters/copilot_vscode.py`; supported per-developer source)

**Revision 3 (DC29).** Revision 2 kept this file a self-view-only source. The fact-check (client-telemetry
correction 1) shows that `insertSpan()` writes **every** span attribute into `span_attributes`, including
`copilot_chat.copilot_usage_nano_aiu` and `gen_ai.usage.cache_creation.input_tokens` set by
`chatMLFetcher.ts` on chat spans. The file therefore carries per-request uncached / cached / cache-write /
output tokens and a runtime credit estimate for VS Code — the editor this enterprise uses. It is now a
supported source for the self-view, for volunteer panels and for opt-in fleet collection (§5.18), and it is
the §21.6 release-gate evidence for per-developer data. The adapter moves from CP-OTEL to the new package
CP-VSCODE.

VS Code writes `agent-traces.db` (`<globalStorage>/agent-traces.db`, fallback
`<tmpdir>/copilot-agent-traces.db`) only when the user enables `github.copilot.chat.otel.dbSpanExporter.enabled`
(boolean, default false, user settings only); retention 7 days / 100 sessions (`otelSqliteStore.ts`). Default
locations searched (all **VERIFY**, facts `vscode_paths`; `--vscode-traces PATH` overrides): the
`globalStorage` directory of the Copilot Chat extension under the VS Code user-data directory —
macOS `~/Library/Application Support/Code/User/globalStorage/github.copilot-chat/`, Linux
`~/.config/Code/User/globalStorage/github.copilot-chat/`, Windows `%APPDATA%\Code\User\globalStorage\github.copilot-chat\`,
each also with `Code - Insiders` for VS Code Insiders — plus the documented `<tmpdir>` fallback. Whether a
managed OTLP policy also disables the SQLite exporter is unknown (**VERIFY**; the file exporter is disabled
by policy per `managedOTelOutfileValue()`); an empty or absent file yields `dq.copilot_vscode_no_spans`.
Read read-only: table `spans` (typed columns `span_id, trace_id, parent_span_id, name, start_time_ms,
end_time_ms, status_code, operation_name, provider_name, agent_name, conversation_id, request_model,
response_model, input_tokens, output_tokens, cached_tokens, reasoning_tokens, chat_session_id, turn_index,
ttft_ms`; `tool_*` columns ignored) and `span_attributes(span_id, key, value)` selected **only** with `key IN
(allowlist)`: `gen_ai.operation.name, gen_ai.provider.name, gen_ai.request.model, gen_ai.response.model,
gen_ai.response.id, gen_ai.conversation.id, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
gen_ai.usage.cache_read.input_tokens, gen_ai.usage.cache_creation.input_tokens,
gen_ai.usage.reasoning.output_tokens, gen_ai.usage.reasoning_tokens, copilot_chat.copilot_usage_nano_aiu,
copilot_chat.request.max_prompt_tokens, copilot_chat.turn.index, copilot_chat.api_type,
copilot_chat.endpoint_type, copilot_chat.location` (= `core.spans.COPILOT_SPAN_KEYS`, CA-42). `span_events`
is not read. Chat spans map exactly as §5.10 through `core.spans.copilot_span_fields`; tokens under
convention `github_copilot.otel` (input inclusive of cache read and creation; creation → `cache_write_unknown`
with `write_ttl_hint` None); `copilot_chat.copilot_usage_nano_aiu` → `provider_reported_cost_nano` (basis
`provider_estimate`, R12; L0 parity input); `agent_product="copilot_vscode"`; lanes by `chat_session_id` /
`gen_ai.conversation.id`, requests ordered by `(start_time_ms, span_id)`. **Dedupe with the in-editor CLI
agent:** chat spans synthesized by VS Code for the Copilot CLI agent host (service `copilot-chat`, no
`gen_ai.response.id`, `copilotcliSession.ts`) are skipped with `dq.copilot_synthesized_span_skipped` when a
native span of the same trace exists, and — in the collector and self-view — when
`~/.copilot/session-state/<gen_ai.conversation.id>/` exists on the same machine (the CLI collector reads that
session; conversation id = CLI session id is **VERIFY**); otherwise they are kept with
`dq.copilot_synthesized_span_kept`. Identity: the file carries none; the collector attaches the developer's
principal by identity mode (§5.18); the self-view uses the install key. Sniff: SQLite magic and
`span_attributes` in the head. Capabilities `{usage_sequence, timing, params, credits}`. Fixture built by a
script from the DDL in `otelSqliteStore.ts` (primary), with canary text in content attribute rows that must
never be selected; a SQL trace hook proves the parameterized `IN` list is the only attribute query and
`span_events` is never touched.

### 5.14 `gh-aw-token-usage` (CP-OTEL, `adapters/gh_aw.py`)

`token-usage.jsonl` written by the gh-aw API proxy (`/tmp/gh-aw/sandbox/firewall/logs/api-proxy-logs/` and
audit variants, collected into gh-aw run artifacts; artifact name **VERIFY**). Fields (`pkg/cli/
token_usage_types.go` and fixture `awf-v0.28.7-aic-token-usage.jsonl`, `github/gh-aw` @358fbf2): `_schema,
timestamp, event, request_id, provider, model, path, status, streaming, input_tokens, output_tokens,
cache_read_tokens, cache_write_tokens, reasoning_tokens?, duration_ms, response_bytes, x_initiator,
ai_credits_this_response, ai_credits_total, ai_credits_pricing_source, ai_credits_pricing_tier,
ai_credits_accounting_policy, ai_credits_fallback_pricing_used, input_tokens_include_cache?`. Only
`provider == "copilot"` lines become requests (others counted, skipped); `request_id` →
`provider_request_id`; one session per file (session key from the source id), lane MAIN; `path` kept only
when in {`/chat/completions`, `/responses`, `/v1/messages`}; `status` → attempt outcome;
`ai_credits_this_response` → `provider_reported_cost_nano` (basis `provider_estimate`; gh-aw prices from
`ai_credits_pricing_source`, e.g. `models.dev`); the last `ai_credits_total` → COST_STATE `gh_aw.run_total`;
one `UsageAggregate(source_kind="gh_aw.run")` per file (dims `channel`, `repo`, `workflow`, `source`; usage =
Σ tokens under `gh_aw.token_usage`; `reported_cost_nano` = the run total, basis `provider_estimate`) so the
aggregate detector can compute per-run statistics without lanes; `workload_class=ci`,
`agent_product="copilot_gh_aw"`, billing path from attribution else `copilot_direct`
(`dq.copilot_billing_path_assumed`); repo / workflow from `--attr` or GITHUB_* env (h_). Sniff: first line has
`"_schema":"token-usage/` and `"event":"token_usage"`. Fixture: the five-line GitHub fixture verbatim
(content-free) plus synthetic variants. GitHub's fixture uses `gpt-4o-mini-2024-07-18`, a utility model with
no Copilot rate row: such requests stay unpriced (never priced from gh-aw's AIC).

### 5.15 `github-copilot-activity-report` (CP-HANDOFF, `adapters/github_activity_report.py`)

The UI-only Copilot activity report CSV (Enterprise or organization settings → Copilot / Licensing → "Get
activity report"; exact click path **VERIFY**, §22.4). Documented fields (metrics-data reference, re-opened by
the data-apis fact-check 2026-09-23): `report_time, login, last_authenticated_at, last_activity_at,
last_surface_used`; refreshes every 30 minutes; 90-day retention; "lacks consistent telemetry from some third
party IDEs outside of VS Code (such as JetBrains and Xcode)"; Spaces and Spark not fully recorded. Sniff: a
CSV header containing `login`, `last_surface_used` and `report_time`. Each row → `LicenseSnapshot(plan=
"unknown", assigned_via_team=None, org=<from --github-org when the file is an org report, else None>,
seat_created=None, pending_cancellation=None, last_activity_bucket` / `last_authenticated_bucket` against
`report_time`, `last_activity_surface = editor_family(last_surface_used)`, `source_kind=
"github.copilot_activity_report")`; `login` → team / cost center via maps → `p_` → dropped. Whether the
report lists every seat holder including never-active ones is **VERIFY** (§19.5 #31); seat counts derived from
it carry note "activity report: seat holders as listed". Capabilities `{licenses}`. Fixture: documented field
order, 12 rows incl. `VS Code 1.89.1`, a JetBrains surface string (third-party-derived, `verified: false`),
`Unspecified`, `CANARY_LOGIN`.

### 5.16 `copilot-export` (CP-HANDOFF, `adapters/copilot_export.py`)

Reads a `tokenbill/copilot-export@1` bundle (§22.5). Sniff: zip magic `PK\x03\x04` and a `manifest.json`
member whose `schema` is `tokenbill/copilot-export@1` (registry position first, CA-44). Safety before any
parse: at most 16 members, only the §22.5 member names (no directories, no absolute or `..` paths, no
symlinks), each member ≤ 512 MiB uncompressed and the sum ≤ 2 GiB, compression ratio ≤ 200:1, stored or
deflate only, no encryption flag; violations → `UsageError` naming the rule (exit 2), nothing ingested. The
manifest's `principal_key_id` / `name_key_id` become `SourceInfo.principal_key_id` / `name_key_id`; if they
are not in `opts.adopt_key_ids` (the CLI passes the ids the user confirmed, §15), `read()` raises
`PrivacyError("bundle key id not adopted")`. Records are decoded with `core.records.from_json`; every
`principal` must match `^p_[0-9a-f]{20}$` and every name field `^h_`, else the record is quarantined; the leak
scan of §22.5 is repeated on read (defence in depth; a hit quarantines the member). Manifest counts are
checked against the members (`dq.copilot_export_count_mismatch`). Capabilities: the union recorded in the
manifest (`aggregates, cost, copilot_billing, licenses, activity, outcomes, config` as present).

### 5.17 `copilot-admin-answers` (CP-HANDOFF, `copilot/admin_answers.py`)

The admin's questionnaire `admin_answers.json` (template packaged as
`tokenbill/copilot/handoff_data/admin_answers.template.json`, schema `tokenbill/copilot-admin-answers@1`):
`plan_as_shown` per org (`business` | `enterprise` | `mixed` | `unknown`), `billing_mode` per entity
(`metered` | `volume` | `azure` | `unknown`), `renewal_date`, `cost_centers[{name, pool_enabled, cap_policy:
block|continue|unknown}]`, `compliance` (`none` | `data_residency` | `fedramp` | `unknown`),
`budgets[{scope: enterprise|org|cost_center, amount_usd, stop_usage}]`, `paid_usage_policy_on`,
`seat_policy` per org (`assign_all` | `assign_selected` | `unknown`), `promo_eligible`. Free text is not
accepted (unknown keys → `UsageError`). → one `ConfigSnapshot(kind="run_flags", source_kind=
"tokenbill.admin_answers")` (`plan.<entity>`, `billing_mode.<entity>`, `renewal_date.<entity>`,
`capped_policy.<cc>`, `compliance`, `promo_eligible`, `stop_usage.<scope>`) plus `ConfigSnapshot(kind=
"org_settings")` rows with only `seat_management_setting` for stated seat policies (lower precedence than a
pulled org-settings snapshot). Every value is a statement: plan statements rank last (CA-48), and findings
name "admin statement" as their source. `unknown` answers produce no attribute. Sniff: JSON object with
`schema == "tokenbill/copilot-admin-answers@1"`. Capabilities `{config}`.

### 5.18 VS Code collector (CP-VSCODE, `adapters/copilot_vscode_collect.py`; `tokenbill copilot collect --source vscode`)

`collect_incremental_vscode(paths: VsCodePaths, state: CollectorState, opts: IngestOptions, *, now_ms: int)
-> Iterator[IngestResult]` — the per-developer collector for DC29 (opt-in; run at least daily by MDM, a
scheduled task or the developer):
- **agent-traces.db:** opened read-only (`mode=ro`, immutable off, WAL-safe); high-water mark `(start_time_ms,
  span_id)` per database file id (inode + creation time); selects spans strictly after the mark ordered by
  the same key, then their allowlisted attributes; in-flight conversations (a chat span newer than
  `now_ms − 10 min`) are re-read on the next run and deduplicated by request id.
- **Retention awareness:** VS Code keeps 7 days / 100 sessions. When the oldest span in the file is newer
  than the stored mark, spans may have been pruned unread → `dq.copilot_vscode_retention_gap` with the gap
  length; when the newest collected span is more than 5 days older than `now_ms` at the next run, the run
  emits `dq.copilot_vscode_collect_stale` ("run at least daily"). The collector README states the daily rule.
- **otel outfile** (`github.copilot.chat.otel.outfile`, when the developer set it): tailed with per-file byte
  cursors and a head-sha rotation check (as the CC collector); lines parsed with `core.jsonl.parse_json_line`;
  only Copilot resources (`core.models.is_copilot_resource`) and only chat spans through
  `core.spans.copilot_span_fields`; requests already collected from the database (same response id) are
  skipped. The outfile is empty when a managed OTLP endpoint is set (VS Code suppresses file export).
- **Identity modes** exactly as the CC collector (SPEC §5.12 table): `central` (`--principal-ref env:VAR |
  mdm-file:PATH | none` → `r_<ref>`), `two-stage` (`--collection-key-file` → `c_`), `install` (self). Team from
  `--team` or the central team map at ingest. Names (`repository`, `cwd`) are never present in the
  allowlisted attributes.
- **Output:** `IngestResult`s that CP-WIRE writes as trace@2 `usage` profile files with TRACE's writer
  (runtime composition, gate-tested), plus a private JSON state file (0600). Never writes to VS Code files;
  never reads `span_events`, `chatSessions/`, debug logs or settings files.
- **Opt-in snippet:** the setting is user-level only (not managed), so CP-POLICY ships
  `vscode/settings.snippet.json` (`{"github.copilot.chat.otel.dbSpanExporter.enabled": true}`) and a
  one-page developer note (what is recorded, the 7-day retention, how to opt out); no managed key is
  claimed.
Tests: fixture DB built from the DDL; incremental runs over a growing DB; pruning simulated (retention gap
dq); outfile rotation; synthesized-span dedupe against a fake `~/.copilot` tree; canary never read.

---

## 6. Pricing (CP-RATES data; RATES loads it; FakePricer mirrors facts)

### 6.1 Rate file `tokenbill/copilot/data/github_copilot.json` (`tokenbill/rates@1`, `provider: "github"`)

One row per (model, tier) and effective interval, channel `github_copilot`, built by replaying the 38 dated
revisions of `github/docs:data/tables/copilot/models-and-pricing.yml` (`copilot/raw/yml/<date>_<sha>.yml`,
2026-04-27 → 2026-09-22). `effective_from` rules and **date source** (recorded in `notes` as
`date_source=<X>` and in `sources[].finding`): **B** = row existed before usage-based billing, clamped to
2026-06-01; **C** = a changelog dates the availability or price (e.g. Opus 5.5 and GPT-6 Sol/Luna on
2026-09-22); **D** = the pricing text itself dates it (GPT-5.6 Sol promo "through September 3" → the $4/$20
row from 2026-09-04; Gemini Flash promo end 2026-12-31); **K** = only the docs commit date is known
(**VERIFY**: a merge date, not a billing effective date). `effective_to` exclusive. Row example:

```json
{"row_id": "github/github_copilot/claude-opus-5-5/2026-09-22", "channel": "github_copilot",
 "model": "claude-opus-5-5", "aliases": ["Claude Opus 5.5"], "generation": "5.5",
 "effective_from": "2026-09-22", "effective_to": null,
 "usd_per_mtok": {"input": "4.00", "output": "20.00"},
 "multipliers": {"cache_read": "0.05", "cache_write_5m": "1.25", "cache_write_1h": "2.0"},
 "published_absolute": {"cache_read": "0.20", "cache_write_5m": "5.00"},
 "min_cacheable_tokens": null, "tokenizer_family": "claude-4.7+", "per_request_usd": {},
 "long_context": null, "promotion": null, "supports": [], "enabled": true, "verified_on": "2026-09-23",
 "notes": "date_source=C; category Powerful; cache_write_1h unpublished: 2 x input assumed (SDK cacheWrite1hPrice exists; VERIFY)",
 "sources": [{"url": "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing",
              "retrieved": "2026-09-23", "finding": "copilot-billing F4; yml 2026-09-22_d1153b57c9; changelog 2026-09-22"}]}
```

Rules: Copilot rows never alias or fall back to provider rows; "Not applicable" writes → `cache_write_* =
null`; non-Claude rows with a published write price carry it in both TTL classes; long-context rows carry
`long_context{threshold: 272000|200000, usd_per_mtok{…}}` (the key name the merged `facts.json` /
`facts.py::_rate_row` read; revision 2's `threshold_input_tokens` is withdrawn; 272K = 272,000 **VERIFY**);
`claude-opus-4-8` has `supports: ["fast_mode"]`; promotional rows name their promotion ids (CA-36); retired
models keep closed rows. The in-force table and history are §19.2.

Modifiers (unchanged):

```json
[{"modifier_id": "github.auto", "kind": "multiply", "factor": "0.9", "applies_to": ["*"],
  "when": {"routing": "auto", "channel_in": "github_copilot"}, "stacking": "assumed"},
 {"modifier_id": "github.compliance", "kind": "multiply", "factor": "1.1", "applies_to": ["*"],
  "when": {"compliance_in": "data_residency,fedramp", "channel_in": "github_copilot"}, "stacking": "assumed"},
 {"modifier_id": "github.fast.opus-4-8", "kind": "replace_base",
  "base_usd_per_mtok": {"input": "10.00", "output": "50.00"},
  "when": {"speed": "fast", "model_in": "claude-opus-4-8", "channel_in": "github_copilot"},
  "stacking": "documented"}]
```

### 6.2 Pricing rules (applied by RATES and FakePricer)

1. Predicate keys `routing` and `compliance_in` join the SPEC §6.1 allowlist.
2. **Writes (DC6):** SPEC R5 applies unchanged. A Claude-model `cache_write_unknown` line is a range
   [published write, 2 × input], point at `write_ttl_hint` (else the published write); zero-width ranges
   (other vendors) are still ESTIMATED. The 1h rate is replaced by the verified value once L0 parity or L1
   shows it (facts change at a release, not at runtime).
3. Both Copilot billing paths → basis LIST_EQUIVALENT; contract overlays never apply; lines route to
   `PricedTotal.pool`.
4. **Long-context bands.** Hypothesis A (documented: the YAML's "Input token threshold for pricing tier"):
   the whole request is priced at band rates when `total_input` > threshold. Hypothesis B (the session's
   `context_tier`, SDK per-tier `maxPromptTokens` and prices): band rates iff `context_tier ==
   "long_context"`. When `ctx.context_tier` is known and B's choice differs from A's, every line of that
   request is a range [min, max] with point = A, ESTIMATED, `dq.copilot_band_hypothesis`; when it is unknown,
   A alone decides (EXACT). The uncached-vs-total reading is diagnosed only by the L1 residual
   `copilot_long_context_band`.
5. Legacy PRU lines are never token-priced.
6. RATES' facts parity test covers **every** `facts.copilot.rates` row; `pricing verify` calls
   `tokenbill.copilot.rates_verify:verify(layer, *, snapshot=None, live=False, opener=None)` (stdlib YAML
   mini-parser; packaged snapshot `tokenbill/copilot/data/snapshots/models-and-pricing-2026-09-22.json`).

Golden cases Appendix C.G1–G17 are acceptance tests for CP-RATES (with the real RateCard, gate) and F-KIT-C
(FakePricer).

---

## 7. Store

### 7.1 STORE amendments (SPEC STORE package, applied to SPEC §7)

```sql
-- in the §7.1 DDL (new installs); STORE's schema_version bump migrates existing stores with ALTER TABLE
inferences  += routing TEXT, compliance TEXT, context_tier TEXT
cost_lines  += quantity TEXT, unit TEXT, cost_center TEXT, team TEXT, repo TEXT, workload TEXT, workflow TEXT,
               routing TEXT, speed TEXT, pseudo TEXT
outcomes    += extra_json TEXT                       -- OutcomeAggregate.extra (sorted pairs)
daily_rollup += pool_nano INTEGER NOT NULL DEFAULT 0 -- LIST_EQUIVALENT lines on Copilot billing paths
cluster_day  += pool_nano INTEGER NOT NULL DEFAULT 0 -- (allowance_nano keeps only the subscription path)
CREATE INDEX cl_date_cc ON cost_lines(date_utc, channel, cost_center);
```

Merge: `cost_lines` and `aggregates` rows whose id already exists are replaced iff the incoming `fetched_ms`
is larger (ties keep the existing row) — with natural ids this makes overlapping and revised exports
idempotent. `count_users(source="cost_lines")` and `source_stats()` (CA-24); `sources_mask` bits 256 / 512 /
1024 / 2048 / 4096; retention nulls `cost_lines.principal` with the other identity columns. `cluster_days()`
fills `ClusterDay.pool_nano`. LicenseSnapshot / ActivityDay / ConfigSnapshot are **not** STORE tables.

**Key-id adoption (revision 3, R-E21 — superseded in detail by CORE-AMENDMENTS A-2 / R-E21, revision 3.1; STORE has not started, so this is brief text).**
`SqliteStore(path, *, create=True, org_key=None, name_key_id=None, pricer=None, read_only=False,
adopt_key_ids: Collection[str] = ())`: every id in `adopt_key_ids` is appended to `meta.adopted_key_ids`
(JSON list, append-only) with an `audit` row (`action="adopt_key_id"`, actor, key id, time). Ingest accepts
`p_` values when `SourceInfo.principal_key_id` equals `meta.org_key_id` **or** is in `meta.adopted_key_ids`,
and `h_` values when `SourceInfo.name_key_id` equals `meta.name_key_id` or is adopted; otherwise the SPEC
§7.2 behaviour holds (null + `dq.principal_key_mismatch` / `dq.name_key_mismatch`). A store without an org
key may exist for adoption only; `r_` / `c_` principals still raise `PrivacyError` there. `meta.name_key_id`
is not overwritten by adoption. Per-person joins across key ids are never made: `count_users` counts
`DISTINCT principal` within one source family, and principals under different key ids are distinct values
that never collide in practice; showback and findings join sources by team only. `purge(principal=p_…)` works
for adopted values (the admin supplies the `p_`, §22.7). Gate test: a bundle with key id K_a imported into a
store with org key K_o and `adopt_key_ids={K_a}` keeps both sources' principals; without adoption the bundle's
principals are nulled with the dq code.

### 7.2 Copilot record store (CP-STORE, `copilot/record_store.py: CopilotRecordStore`)

Implements `core.protocols.ExtRecordStore` on the same SQLite file (its own connection, WAL):

```sql
CREATE TABLE copilot_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);          -- schema_version
CREATE TABLE copilot_license_snapshots(rec_key TEXT PRIMARY KEY, snapshot_date TEXT NOT NULL,
  product TEXT NOT NULL, plan TEXT NOT NULL, principal TEXT, team TEXT, cost_center TEXT,
  org TEXT NOT NULL DEFAULT '', seat_created TEXT, pending_cancellation TEXT,
  last_activity_bucket TEXT NOT NULL, last_activity_surface TEXT, last_authenticated_bucket TEXT NOT NULL,
  assigned_via_team INTEGER, fetched_ms INTEGER NOT NULL, source_kind TEXT NOT NULL,
  principal_key_id TEXT NOT NULL DEFAULT '');       -- revision 3: NULL = unknown; key id of `principal`
CREATE TABLE copilot_activity_days(rec_key TEXT PRIMARY KEY, date_utc TEXT NOT NULL, product TEXT NOT NULL,
  principal TEXT, team TEXT, cost_center TEXT, reported_cost_nano INTEGER, counts_json TEXT NOT NULL,
  flags TEXT NOT NULL, fetched_ms INTEGER NOT NULL, source_kind TEXT NOT NULL,
  principal_key_id TEXT NOT NULL DEFAULT '');
CREATE TABLE copilot_config_snapshots(rec_key TEXT PRIMARY KEY, snapshot_ms INTEGER NOT NULL,
  source_kind TEXT NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL, attrs_json TEXT NOT NULL,
  fetched_ms INTEGER NOT NULL);
CREATE TABLE copilot_decisions(source_id TEXT NOT NULL, channel TEXT NOT NULL, decided_ms INTEGER NOT NULL,
  decision_json TEXT NOT NULL, PRIMARY KEY(source_id, channel));                 -- revision 3 (CA-23)
CREATE INDEX cls_date_team ON copilot_license_snapshots(snapshot_date, team);
CREATE INDEX cad_date_team ON copilot_activity_days(date_utc, team);
```

`rec_key = stable_id("rk", record_key(rec))` (never NULL, so re-ingest is idempotent — the revision-1 NULL
`org` key defect is gone); upsert keeps the larger `fetched_ms`. Principal key check: records are accepted
only when `SourceInfo.principal_key_id` equals STORE's `meta.org_key_id` **or is in `meta.adopted_key_ids`**
(read-only read of the SPEC §7.1 `meta` table; revision 3, R-E21; `accepted_key_ids` of CA-23 — revision 3.1: read from `meta` instead), else skipped
with `dq.principal_key_mismatch`; each row keeps its `principal_key_id`. **Joins stay inside one key id:**
the enricher and the detectors join licenses × activity × cost lines per principal only among rows with the
same key id (in the handoff deployment all three come from one bundle, so they share the export key); rows
under another key id join by team only (`dq.copilot_key_id_mixed`, once). `count_users(source="licenses"|
"activity")` with `where` keys `team, cost_center, org, plan, bucket, product, editor_family, date_from,
date_to`.
**Enricher (revision 3).** `enrich_context(store, record_stores, ctx, *, today, reconciled_channels,
decisions)` fills `licenses`, `activity`, `config`, `outcomes` (Copilot `OutcomeAggregate` rows), `plans`
(`core.pool.detect_plans` per entity × month), `pools` (`core.pool.pool_months` with `convention` and
`gross_is_list` taken from the `ChannelDecision` of each AI-usage-report source — `excl` and `None` when no
decision exists), `channel_decisions` and `reconciled_channels`. `retain` deletes
license and activity rows older than `retention.identity_days` (team-level history survives in findings,
showback and rollups); `purge(principal=…)` deletes that principal's rows and writes an audit row through
STORE's `audit` table contract. The ingest pipeline persists `IngestResult.licenses/activity/config` through
`core.extensions.persist` (WIRING, §21.4).

---

## 8. Privacy (Copilot)

### 8.1 Rules

- Content tier `none` only for every Copilot source; `--content fingerprint|full` → `UsageError`. Canary
  planted in fixture usernames (`CANARY_LOGIN`), `user_login`, `assignee.login`, emails, repository names,
  workflow paths, `cwd`, `branch`, `sessions.summary`, event content fields, OTel message attributes, VS Code
  `span_attributes` content keys and span names; absent from every record and output.
- GitHub logins, numeric user ids, EMU suffixes and emails never reach the store (maps at ingest, `p_` with
  the org key, raw value dropped). Budgets never store the user of a user-scope budget (team / cost center
  only). Budget user-states are summarized at ingest (quantiles only with ≥ k users).
- Seat findings: counts per team (k ≥ 5) and the API filter an admin runs themselves, e.g.
  `GET /orgs/{org}/copilot/billing/seats` filtered on `last_activity_at < <date>` and `assigning_team` —
  never usernames.
- VS Code traces: key allowlist (§5.13); gh-aw: only `token-usage.jsonl` is opened (the copied
  session-state directories in gh-aw artifacts are never read).
- The EMU usage-records API is refused. Tokens and signed URLs are never written.
- `docs/COPILOT.md` carries the works-council note: Copilot credits per person are shown only to that person
  (`copilot me`).
- **Handoff (revision 3, R18, §22).** The export key never leaves the admin machine; the bundle holds only
  `p_` / `h_` values under it, team labels of teams with ≥ k seated users, organizational names and numbers;
  user-scope budgets appear as team counts only; the leak gate (§22.5) runs before any byte is written; raw
  GitHub files stay on the admin machine (the pull's temp directory is deleted). The analyst holds no key
  that can map a login to a pseudonym (DC31). `--aggregate-only` bundles hold no per-person record at all.
  Monthly bundles reuse the export key so months join; rotating the key (`copilot export --rotate-key`)
  breaks joins on purpose and is recorded in the manifest.
- **VS Code collection** is opt-in per developer (user setting); the developer note states what is recorded
  (token counts, model, timing, a credit estimate; no prompts, no file names) and that team-level results
  only are shown to others.

### 8.2 Counting people for k-anonymity (CA-24, CA-36, CA-37, R-E16)

Each Copilot finding kind has a count source in `core.catalog.COUNT_SOURCE`:

| source | used by | counted over |
|---|---|---|
| `requests` | `copilot.lanes` kinds, generic detectors on Copilot lanes | STORE requests (SPEC) |
| `cost_lines` | `copilot.org-scan` kinds scoped to team / cost center / model | STORE cost lines (`ai_credit.user` rows) |
| `licenses` | seat kinds (`idle-seat`, `seat-auto-assign`, `completions-only-seat`, `plan-mix`, `duplicate-seat`) | record store licenses |
| `activity` | `mcp-sprawl`, `context-heavy-cli`, `editor-mix` (revision 3), Auto reach factors | record store activity days |
| `entity` | `pool-regime`, `plan-status` (revision 3), `overage-forecast`, `promo-cliff`, entity-level `budget-config` kinds, `direct-org-usage`, `unattributed-spend`, `agentic-workflow-cost` at entity scope | exempt (R-E16) when the scope has only `product`, `entity` (enterprise / org), `model`, `sku`, `plan_scenario` dims |

`core.kanon.scope_counter` maps Scope → `where` (CA-37). Re-scoping follows the Copilot parent chain
(`team` → `editor_family` → `bucket` → `plan` → `model` → `cost_center` → entity root; `plan_scenario` never
dropped). **R-E16 (amends R-E9):** `category="aggregate"` does not by itself exempt a `product`-scoped finding (the merged
R-E9 rule would have exempted team-scoped Copilot findings); only count source `entity` without team /
cost-center / bucket / editor dims does. `budget-config zero-user-budget` is published as a count of
user-scope budgets set to $0 per team (k ≥ 5) or at entity level; never with a `p_`.

---

## 9. Simulation (Copilot)

### 9.1 Lane-level replay on Copilot lanes (REPLAY engine, unchanged except billing class)

Copilot lanes are ordinary lanes on channel `github_copilot` with billing class `pool` (REPLAY accepts
`pool` as a third class, treated like `allowance`: LIST_EQUIVALENT; mixed classes still raise). `model_remap`,
`effort`, `fast_off` and repairs apply with Copilot rates. TTL and keepalive levers never match Copilot lanes
(their selectors name `agent_product:claude_code` / `lane_kind:api_run`); `compaction_window` finds no
Copilot row with `1m_context`. Results are list-equivalent pool headroom.

### 9.2 The cell model (Copilot aggregate replay, CP-PLAN)

Cells (`core.pool.build_cells`, monthly grain) cover 100% of billed Copilot usage. A lever set S transforms
cells in fixed order, then the pool rule prices the invoice:

1. **Model policy remap** (`copilot:remap=<t>@model:<m>`): cells on m with a `copilot_remap` target t →
   tokens × band point (1.00; `TOKENIZER_BAND` only when `tokenizer_same` is False) priced at t's rates on
   the cell's date; pseudo cells unchanged; trade-off, `needs_eval`.
2. **Fast off** (`copilot:fast=off`): `speed=fast` cells → standard rates.
3. **Auto default** (`copilot:auto=on@…`): cells with `routing="direct"`, cost type `ai_credit.user` or
   `ai_credit.direct`, not pseudo `code_review` → credits × (1 − 0.1 × reach_t) (§9.3); which model Auto picks
   is not modeled (`needs_eval`, trajectory prior). Without `Auto:` labels (**VERIFY**), routing comes from
   metrics `model:auto` interaction shares per team (ESTIMATED, noted).
4. **Seats** (`copilot:seats_idle=…`, `copilot:seat_policy=assign_selected@org:<o>`,
   `copilot:plan=business`): Δseats per plan per entity from the seat findings — only seats a delivery can
   remove (direct assignments in `assign_selected` orgs; for `seat_policy`, idle seats in `assign_all` orgs),
   effective the next month; entities with `billing_mode` `volume` / `azure` get no seat value (renewal).
5. **Runner standardization** (`copilot:runner=actions_linux`): Copilot-workload Actions lines on larger
   runners re-priced as the range of §10.2 `larger-runner`.

`invoice_e(S) = fees_e(S) + overage_e(S) + direct_e(S) + actions_e(S)` with `overage` from
`core.pool.overage_total` (a (low, high) pair when a capped cost center's policy is unknown). `v(S) = Σ_e
invoice_e(∅) − invoice_e(S)` at the point, low and high consumption of each entity's `PoolMonth` (latest
closed month, or the forecast month with `--forecast`); exact Shapley over ≤ 6 selected levers;
`headroom(S) = Σ_e (C_e(∅) − C_e(S)) − (overage_e(∅) − overage_e(S))` in LIST_EQUIVALENT. Result: an
`ActionPlan` with `headline_monthly` (invoice, ESTIMATED, LIST), `pool_headroom_monthly` (LIST_EQUIVALENT),
`levers` (`LeverResult.params` = aggregate spec), `method="shapley-exact"`, `sample="copilot cells, <n>
cells, month <m>"`.
**Unknown plan (revision 3, R17).** When any entity has scenario `PoolMonth`s, v(S), Shapley values and
headroom are computed once per scenario (`business`, `enterprise`) on the same cells, giving two
`ActionPlan`s (`CopilotSummary.plans_by_scenario`); `sample` names the scenario; the lever
`copilot.seat_downgrade` is not a candidate; the two plans are never averaged or summed.

### 9.3 Delivery reach (binding for projections)

A projection is multiplied by the share of the affected credits its delivery reaches; every `AdminAction`
carries `reach`. Reach below 1 is ESTIMATED.

| lever / delivery | reaches | reach estimate |
|---|---|---|
| `copilot.default_model_auto` via managed `model` | CLI, VS Code ≥ 1.126, Copilot app, cloud agent; **not** JetBrains (cost-levers F2 fact-check); Visual Studio / Xcode / Eclipse unverified → excluded | per team: 1 − share of interactions on excluded IDEs (ActivityDay `ide:*` counts, CLI and app counts) — for this enterprise effectively **1 − JetBrains interaction share**; cloud-agent cells reach 1; no metrics → low 0, point 0 ("reach unknown"), high 1. Teams whose JetBrains share ≥ 50% (revision 3) get the server-side model policy (`copilot.model_policy`, reach 1) as the primary delivery and Auto-tier communication for JetBrains users; the admin checklist states that managed `model` does not apply in JetBrains |
| `copilot.model_policy`, `copilot.fast_mode_off` (server-side model policies) | every surface | 1 |
| `copilot.mcp_trim` via managed `deniedMcpServers` / `allowedMcpServers` | CLI, VS Code, Copilot app, JetBrains; not cloud agent | lanes' agent product share |
| `copilot.telemetry_on` via managed `telemetry` | CLI, VS Code; JetBrains contradictory in GitHub's docs (table yes, prose no) → not counted | enabler, not projected |
| `copilot.context_default` via repo `.github/copilot/settings.json` `contextTier` | Copilot CLI only, and only in trusted working directories (CLI config-dir reference) | CLI lanes' share × trusted share (unknown → low 0) |
| `copilot.auto_tier` (Efficiency / Balance / Intelligence) | VS Code, CLI, Copilot app (user choice; no managed key documented) | not projected ("Usage is charged based on the model auto selects, regardless of tier") |
| `copilot.review_effort_lite` (enterprise / org default) | reviews not governed by a personal default effort (personal defaults apply to reviews a user requests, changelog 2026-09-23) | not projected (R13) |

---

## 10. Detectors (CP-DET-SEATS, CP-DET-USAGE, CP-DET-LANES)

### 10.0 Common rules

- Classes: `copilot.seats-budgets` and `copilot.org-scan` are `aggregate=True`, `extension="copilot"`,
  `families={"copilot"}`; `copilot.lanes` is a lane detector with `families={"copilot"}`. All pure,
  deterministic, declared kinds only, `assert_detector_conforms`, publication through
  `core.kanon.rescope_findings` with `core.extensions.count_users_fn` and `COUNT_SOURCE` (§8.2).
- **Gating (revision 3).** The merged `run_detectors` skips a detector whose `requires` is not a subset of
  `ctx.capabilities`, so a whole-detector `requires` would switch off kinds that do not need the missing
  data (revision 2: the no-token handoff without a seats file got no pool findings; events-only CLI sessions
  got no `compaction-cost`). Therefore each Copilot detector declares `requires=frozenset()` (it runs only
  when `ext:copilot` is present, CA-21) and a module constant `KIND_REQUIRES: Mapping[str, frozenset[str]]`
  gates each kind **any-of**: a kind runs when at least one listed capability set is present; each skipped kind
  is named once in a single `data-quality` finding `copilot.<detector>/skipped-kinds` (no dollars, R-E1).
  `copilot.seats-budgets`: seat kinds need `licenses`; pool and budget kinds need `copilot_billing` or
  `config` (pool kinds need `copilot_billing` for consumption; seat counts may come from licenses, run flags
  or seat lines); `plan-status` always runs. `copilot.org-scan`: report kinds need `aggregates` +
  `copilot_billing`; `mcp-sprawl` / `context-heavy-cli` / `editor-mix` need `activity`; `agent-failed-sessions`
  needs `aggregates`. `copilot.lanes`: `long-context-band` needs `usage_sequence`; `compaction-cost` needs
  `events` (exact COMPACTION inferences exist in events-only CLI sessions and VS Code); `static-overhead`
  needs `events` or `usage_sequence`; `subagent-share` and `ci-uncapped` need none beyond lanes.
- **Categories (R-E16).** Entity-scope info kinds whose count source is `entity` use category `aggregate`;
  seat and budget kinds use `lever` (with a lever) or `attribution` (without); `premium-model-share`,
  `fast-mode`, `forced-migration`, `compliance-uplift` use `premium`; `cache-health` and lane kinds use
  `lever`; `agent-failed-sessions` uses `failure`; `unattributed-spend` uses `attribution`; skipped-kind notes
  use `data-quality`. Team- or cost-center-scoped findings are never exempt from k whatever their category.
- **Scenarios (R17).** While `ctx.plans` has an unknown-plan entity, every pool-dependent kind (`pool-regime`,
  `overage-forecast`, `promo-cliff`, `idle-seat`, `seat-auto-assign`, the budget kinds that use the pool or
  overage) is emitted once per scenario with scope dim `plan_scenario` and every pool-derived figure
  ESTIMATED (summary: "if all unknown seats are Business" / "… Enterprise"); `plan-mix` and any
  "Business instead of Enterprise" fix text are suppressed; kinds that do not depend on the pool are emitted
  once.
- **Labels (R16).** Credit valuations (report gross, cell repricing, lane pricing) are LIST_EQUIVALENT
  (EXACT when pure report arithmetic). Net overage, direct-org, seat-line and Actions/sandbox amounts are
  INVOICE only for final rows of a closed month on a reconciled channel (`ctx.reconciled_channels` and
  `PoolMonth.finality == "closed"`), else EXACT LIST with note "unreconciled" or "provisional". Forecasts,
  counterfactuals, count × list-price seat fees and pool conversions are ESTIMATED. Credits and dollars are
  never added in one figure; where a kind shows both, they are two figures (`cost_observed` and `headroom`,
  or evidence attrs).
- `recoverable` = the invoice part from `core.pool` realization (ESTIMATED, LIST); `headroom` = the rest
  (LIST_EQUIVALENT). `min_usd` (default $1.00) gates on max(recoverable p50, headroom p50); seat kinds gate
  on the seat-fee `cost_observed`, so an idle-seat finding in an overage entity is still shown with invoice
  saving $0 and the regime explained.
- Deadlines: metered seats → before the next 1st 00:00 UTC (removals are billed to cycle end); volume /
  azure → the renewal date when `--renewal-date` gives it, else none. Expiry (R15) per kind.

### 10.1 `copilot.seats-budgets` (`detect/copilot_seats.py: CopilotSeatsBudgets`, CP-DET-SEATS)

| kind (count source) | trigger | cost_observed | recoverable / lever | fix |
|---|---|---|---|---|
| `plan-status` (entity; info; first; revision 3) | every pool entity (latest month) | seat fees by plan: known plans from seat lines (R16) or count × list (ESTIMATED LIST); unknown seats as the range [n × $19; n × $39] ESTIMATED LIST; evidence attrs `plan`, `source`, `conflict`, seat counts per plan | none | when unknown or conflicting: the "how to find out" hint (seats API `plan_type`, detailed-report seat SKU, Licensing page) and `--plan` / answers file; never a plan recommendation |
| `pool-regime` (entity; info; per scenario while the plan is unknown) | every pool entity × month | pool, consumed (LIST_EQUIVALENT), overage and direct net (R16), regime, billing mode, direct draws | none | budgets, model policy, seats per regime |
| `overage-forecast` (entity) | open month, forecast overage p90 > 0 | forecast overage ESTIMATED p10–p90 (widened by an unknown cap policy) | none (drives the plan) | budgets (§11.3), cost-center pools (metered only), model policy |
| `promo-cliff` (entity; info; expires 2026-11-30) | promo months observed and a later standard month | promo-month consumption re-priced against the standard pool — ESTIMATED, note "at unchanged use" | none | re-size user-level and enterprise budgets |
| `idle-seat` (licenses) | seats with `last_activity_bucket ∈ {31-90, none_90d}`, zero reported cost in 30 days, not pending cancellation, created > 30 days ago; per team split into **removable** (direct assignment in an `assign_selected` org), **team-assigned** and **auto-assigned** (`assign_all` org) counts | seat fees of all idle seats: count × list price, ESTIMATED LIST (note: proration, upfront charges from 2026-10-01, volume/EA pricing not modeled) | removable only: `realize_seat_change(−n)` (None for volume/azure); lever `copilot.seat_reclaim`; team-assigned: lever `copilot.seat_reclaim_team`, not projected; auto-assigned: see `seat-auto-assign` | `DELETE /orgs/{org}/copilot/billing/selected_users` request file (placeholder list + the seats query filtered on `last_activity_at` and `assigning_team == null`); team seats: remove from the Copilot-granting team or `DELETE …/selected_teams` for a wholly idle team |
| `seat-auto-assign` (licenses; entity org scope) | org with `seat_management_setting == "assign_all"` and idle seats | idle seat fees ESTIMATED LIST | `realize_seat_change` for those idle seats, ESTIMATED, `needs_eval`, trade-off; lever `copilot.seat_policy_selected` | switch the org to selected members, then unassign idle seats (checklist; outcome of the switch for existing seats **VERIFY**) |
| `completions-only-seat` (licenses; info) | users with `code_generation` > 0, no chat/agent/cli/app/cloud-agent flags, zero reported cost in 30 days | — | none (seat feeds the pool at zero credit use; completions are free) | Business instead of Enterprise where applicable |
| `plan-mix` (licenses) | Enterprise seats whose users drew ≤ 1,900 credits (report rows; `ActivityDay` estimates only when no report) in each of the last 3 closed months | $20 × n seat-months ESTIMATED LIST | `realize_seat_change` (enterprise→business), trade-off, `needs_eval`; lever `copilot.seat_downgrade` | downgrade selected users (checklist) |
| `duplicate-seat` (licenses; info) | a principal seated via ≥ 2 orgs | — | none (billed once via a random org) | assign through one org / cost center |
| `budget-paid-usage-uncapped` (entity) | forecast overage p90 > 0 and no metered budget with "Stop usage" on the entity | exposure = forecast overage p90, ESTIMATED | none (risk) | budget request files |
| `budget-stop-usage-off` (entity; info) | metered budgets without stop | count | none | alerts first, then stop where acceptable |
| `budget-zero-user-budget` (licenses: team count ≥ k, else entity count) | user-scope budgets with amount $0 | count only | none | review: user-level budgets always hard-stop and cover pool + metered usage |
| `budget-ulb-gap` (entity; info) | Σ user-level caps − pool > Σ metered budgets | the gap ESTIMATED | none | GitHub's sizing check |
| `budget-org-multi-org-seats` (entity; info) | org budgets while users hold seats via several orgs | — | none | budget at enterprise / cost center |
| `budget-no-cost-center-pool` (cost_lines; metered entities only) | a cost center draws > 150% of its licences' allowance while the enterprise is in overage and its pool is off | its excess draw, LIST_EQUIVALENT | none (reallocation) | enable its AI credit pool and choose block or continue at the cap |
| `budget-enterprise-misread` (entity; info) | enterprise budget below seat fees with stop off | — | none | the enterprise budget governs metered usage, not the total bill |

**Seat kinds from the activity report (revision 3).** Licenses from `github.copilot_activity_report` have
`assigned_via_team=None` and no org seat policy unless the answers file or an org-settings pull states it.
`idle-seat` then reports one extra count bucket **assignment unknown** (projection None, note "seat
assignment not in the activity report; the seats API or the org's seat policy settles it"); only seats with
`assigned_via_team is False` in an `assign_selected` org are **removable**. Idle buckets come from
`last_activity_at` exactly as for seats; `seat_created` is unknown, so the "created > 30 days ago" condition
is replaced by "listed in two reports ≥ 30 days apart" when two exist, else the finding carries
`needs_eval=True` and note "seat age unknown". Per scenario (R17): the idle-seat projection uses
`realize_seat_change` on that scenario's `PoolMonth` (Appendix C.P13b: $0 if Business, $390 if Enterprise).

Volume / azure / unknown billing mode: the cost-center kinds are suppressed with a note (cost centers apply
only to metered usage); `idle-seat` shows counts with projection None.

### 10.2 `copilot.org-scan` (`detect/copilot_org.py: CopilotOrgScan`, CP-DET-USAGE)

Reads `ctx.aggregates`, `ctx.cost_lines`, `ctx.activity`, `ctx.config`, `ctx.pools`, `ctx.licenses`,
`ctx.outcomes` (revision 3, CA-11: enterprise / org `OutcomeAggregate.extra` for `cloud-agent-cost`) and
`ctx.channel_decisions` (the convention each report file was decided under); cells via
`core.pool.build_cells(convention=…)` with that convention (`excl` when undecided). Scope dims `product:
copilot`, `entity`, `team` / `cost_center`, `model`, `editor_family` (editor-mix only), `plan_scenario`
(pool-dependent kinds while the plan is unknown).

| kind (count source) | trigger | cost_observed | recoverable / lever | fix |
|---|---|---|---|---|
| `premium-model-share` (cost_lines) | per team, credits on `Powerful` models with a remap target | those credits LIST_EQUIVALENT (EXACT) | cell replay of `copilot.model_policy` (price-only, trade-off, `needs_eval`), pool-converted | model policy per org (UI), managed `model` default |
| `fast-mode` (cost_lines) | `speed=fast` cells | premium vs standard on identical tokens, LIST_EQUIVALENT EXACT | same, lever `copilot.fast_mode_off`, pool-converted | disable the fast-mode model (policy) |
| `auto-adoption` (cost_lines) | direct-routed eligible credits > 0 | eligible credits LIST_EQUIVALENT | 10% × credits × reach (§9.3), `needs_eval`, pool-converted; lever `copilot.default_model_auto` | managed `"model": "auto"` (team files for waves); Auto tier guidance |
| `forced-migration` (entity; info; expires with the retirement) | usage on models retiring 2026-10-02 / 2026-10-19 | Δ at the suggested successor vs a cheaper same-vendor option on identical tokens (rate arithmetic EXACT, projection ESTIMATED) | none | choose the successor deliberately before the date |
| `compliance-uplift` (entity; info) | run flag `compliance` ≠ none | uplift = observed credits × (1 − 1/1.1) = observed / 11, ESTIMATED (scope from the flag; stacking VERIFY) | none (never recommends disabling) | prefer cheaper compliant models |
| `cache-health` (cost_lines; info) | per team × model: read share `read/(uncached+read+write)` below the org's own median for that model | model spend LIST_EQUIVALENT | `(median_share·T − R)·(w − r)` ESTIMATED `upper_bound`, pool-converted | install collectors / OTel for causes |
| `review-cost` (entity; info) | `pseudo=code_review` cells and `copilot_code_review` Actions lines | two figures: review credits (LIST_EQUIVALENT) and review Actions $ (R16) | none | checklist (effort, triggers, MCP, instructions) |
| `review-default-balanced` (entity; expires 2026-10-28) | review credits observed and today < 2026-09-28 + 30 d | review credits last 30 days, LIST_EQUIVALENT | not projected (R13); quotes GitHub's published ranges with source and date | set Lite explicitly at enterprise/org level before 2026-09-28; personal default efforts still apply to reviews users request (2026-09-23) — communicate; measure with ITS |
| `review-drivers` (entity; info) | review cells exist | — | none | repository setting "Allow Copilot to use MCP tools when reviewing pull requests" is on by default (GitHub and Playwright MCP servers on by default); consumption grows with PR size and repository custom instructions; personal automatic review of new pushes and drafts |
| `direct-org-usage` (entity) | `ai_credit.direct` cells | net $ (R16) and their pool draw (from their discounts; `direct_draws_pool`) | none | unlicensed-member review policy; `--max-ai-credits` for `GITHUB_TOKEN` CLI; agentic-workflow caps; cost-center budgets |
| `agentic-workflow-cost` (entity or cost_lines by repo team) | Actions lines with workload `agentic_workflow`, plus `ai_credit.direct` cells in the same repositories (heuristic join, ESTIMATED), plus `gh_aw.run` aggregates when present | Actions $ (R16); AI credits LIST_EQUIVALENT; per-run p50/p90 of our price on the `gh_aw.run` token totals; exposure = runs × 1,000 AIC default cap for workflows without a known cap (ESTIMATED upper bound) | none (lever `copilot.agentic_workflow_caps`, behavioral) | `max-ai-credits` in frontmatter near p99 × 1.5; review `on:` schedules; review the org policy "Allow use of Copilot CLI billed to the organization" (on by default when Copilot CLI is enabled) |
| `larger-runner` (entity) | Copilot-workload Actions lines on larger-runner SKUs | net $ (R16) | ESTIMATED LIST range: low = net − minutes × `actions_linux` rate (no included minutes left), high = net (standard minutes covered by included minutes; larger runners never use them); lever `copilot.agent_runner_standard` | org runner type for code review / cloud agent |
| `cloud-agent-cost` (entity; info) | `coding_agent_ai_credit` SKU or pseudo `cloud_agent` cells, `copilot_cloud_agent` Actions lines | two figures (credits LIST_EQUIVALENT; Actions $ R16); per merged Copilot-authored PR from the enterprise outcome `extra` (ESTIMATED: windows differ) | none | setup steps, runner, session limits |
| `agent-failed-sessions` (entity; info; category `failure`) | agent-task sessions `failed` / `timed_out` / `cancelled` with usage | revision 3: `cost_observed = unpriced("provider estimate only", LIST_EQUIVALENT)`; the provider-estimate credits and the session counts are evidence attrs (R12) — a PROVIDER_ESTIMATE figure would be rejected outside data-quality findings (R4 / R-E20) | none | task scoping |
| `unattributed-spend` (entity; info) | share of net $ with no username or no cost center | net $ (R16) | none | cost-center assignment |
| `mcp-sprawl` (activity; info) | per team median `mcp_distinct` and share of users with ≥ 5 | — | none | `deniedMcpServers` / `allowedMcpServers`, tool search |
| `context-heavy-cli` (activity; info) | per team p50/p90 of `cli_prompt_tokens / cli_requests` | — | none | `/compact`, default context tier |
| `editor-mix` (activity; info; revision 3) | per team (k ≥ 5): share of interactions by `editor_family` (`ide:*` counts; seat / activity-report surfaces when no metrics) and the resulting Auto reach (§9.3) | — | none | JetBrains-heavy teams (share ≥ 50%): server-side model policy first; managed `model` does not reach JetBrains; VS Code teams: managed `"model": "auto"` |

### 10.3 `copilot.lanes` (`detect/copilot_lanes.py: CopilotLanes`, CP-DET-LANES)

Runs on Copilot-family lanes only; pool cohorts are LIST_EQUIVALENT with the "Copilot credits:" prefix.
Revision 3: no whole-detector `requires` (revision 2's `{"usage_sequence"}` switched off `compaction-cost`
and `ci-uncapped` for events-only CLI sessions, the default); per-kind gates per §10.0. Lanes from VS Code
(`agent_product="copilot_vscode"`) are the primary input; CLI and gh-aw lanes are secondary.

| kind | trigger | cost_observed | recoverable / lever | fix |
|---|---|---|---|---|
| `long-context-band` | requests priced at band rates (hypothesis A) or as A/B ranges (§6.2 #4) | band premium vs default rates on identical tokens: EXACT when A alone decided, else ESTIMATED range | premium ESTIMATED `upper_bound`; lever `copilot.context_default` (reach §9.3) | default context tier; `/compact` at task boundaries |
| `compaction-cost` | COMPACTION inferences (exact) by `copilot_trigger`; forced share (`context_limit_retry`, `memory_pressure`) | exact compaction credits | none | smaller tool output, earlier `/compact`, default context |
| `static-overhead` | `system_tokens + tool_definitions_tokens` from COMPACTION events (else the static-prefix floor) | carry `static × r` per warm request + `× w` per miss, ESTIMATED | `tool_definitions_tokens × TOOL_SEARCH_REDUCTION_BAND` share, ESTIMATED `upper_bound`; lever `copilot.mcp_trim` | `deniedMcpServers`, tool search, trim instructions |
| `subagent-share` (info) | SUBAGENT lane spend by `agent_type` | spend | none | cheaper subagent models (`/subagents`) |
| `ci-uncapped` | Copilot CI lanes (`workload_class=ci`: CLI `--ci` collector lanes, gh-aw lanes) | spend per session p50/p90 and the share of CLI sessions without `credit_limit_nano`; gh-aw runs: cap (default 1,000 AIC) vs p99 | none | `copilot -p … --max-ai-credits N`; `max-ai-credits: N` frontmatter (N ≈ p99 × 1.5) |

Revision-1 kinds `auto-not-used` and `effort-high` are removed (they duplicated `auto-adoption` and the
generic `model.routing effort-mix`).

### 10.4 Generic detectors on Copilot lanes, exclusions and fixes (`core.catalog`, F-KIT-C data)

`FAMILY_EXCLUSIONS` for family `copilot`:

| excluded (detector[, kind]) | why | replaced by |
|---|---|---|
| `cache.ttl-advisor`, `cache.gateway-disabled`, `cache.cold-fanout` | TTL not configurable; no gateway; fan-out repair is SDK-only | — |
| `premium.sticky-escalation` | Claude Code settings | — |
| `aggregate.org-scan`, `block.breakers` | channels delegated; no fingerprints | `copilot.org-scan` |
| `premium.modifiers`, kind `fast-premium` | the report shows 100% of fast use | `copilot.org-scan fast-mode` |
| `automation`, kind `ci-run-cost` | same sessions | `copilot.lanes ci-uncapped` |
| `context.static-prefix`, kind `static-prefix` | measured static tokens exist | `copilot.lanes static-overhead` |
| `model.routing`, kinds `default-model`, `default-effort` | Claude Code managed keys | `copilot.org-scan auto-adoption`, model policy |

Kept on Copilot lanes (fix text from `fix_for(…, "copilot")`, CA-31): `cache.miss-by-cause` (causes
`model-switch`, `param-change` incl. `context-tier-change`, `compaction`, `context-shrank`; `ttl-expiry` only
with a known τ per CA-32; fix: "Auto switches models only at cache boundaries; switch models at `/new` or via
a subagent; keep effort and context tier fixed mid-session"), `cache.switch-churn`, `cache.rebuild`
(`compaction-cold`: "`/compact` while the session is warm, `/new` for a new task"), `cache.cold-resume`
("`/compact` before stepping away; resume with `/new` for a new task"), `cache.unread-write`,
`context.size-tax`, `attrib.carry` ("large tool output is saved to a file above
`COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES`; trim MCP tools"), `tail.runaway` ("`--max-ai-credits`, `/limits set
max-ai-credits`"), `failure.path`, `model.routing` `delegation-routing` ("assign cheaper subagent models with
`/subagents`"; summary notes the overlap with `premium-model-share`, never added), `same-tier-upgrade`
("choose the successor deliberately; see retirement dates"), `effort-mix` ("keep default effort medium; repo
`effortLevel` in the CLI"), `rebaseline`. Each Copilot `Fix` has `target="github-copilot"`, a docs URL and,
where a key exists, a `config_patch` with allowlisted `copilot.*` keys.

---

## 11. Plan and Copilot policy pack (CP-PLAN, CP-POLICY; catalogs in `core.catalog`, F-KIT-C)

### 11.1 Lever catalog additions (`COPILOT_LEVERS` — a separate table, CA-35; aggregate grids in `AGGREGATE_GRIDS`; replay `"aggregate"` unless noted)

| lever_id | class | aggregate grid | delivery (`patch_keys`: ALLOWLIST keys / `ADMIN_ACTIONS` ids) | needs_eval / tradeoff |
|---|---|---|---|---|
| `copilot.default_model_auto` | trajectory | `copilot:auto=on@all`, `copilot:auto=on@team:<t>` | `copilot.managed.model` | yes / no |
| `copilot.model_policy` | trajectory | `copilot:remap=<t>@model:<m>` per remap pair | `admin:model_policy`, `copilot.repo.allowed_models` | yes / yes |
| `copilot.fast_mode_off` | rate | `copilot:fast=off@all` | `admin:model_policy_fast` | no / no |
| `copilot.seat_reclaim` | rate | `copilot:seats_idle=30d@all`, `copilot:seats_idle=60d@all` | `rest:org_selected_users_delete` | no / no |
| `copilot.seat_policy_selected` | rate | `copilot:seat_policy=assign_selected@org:<o>` | `admin:org_seat_policy` | yes / yes |
| `copilot.seat_downgrade` | rate | `copilot:plan=business@all` | `admin:seat_plan_change` | yes / yes |
| `copilot.agent_runner_standard` | rate | `copilot:runner=actions_linux@all` | `admin:runner_type` | no / yes |
| `copilot.seat_reclaim_team` | behavioral (replay none) | — | `admin:team_membership_review`, `rest:org_selected_teams_delete` | — |
| `copilot.auto_tier` | behavioral (replay none) | — | `admin:communicate_auto_tier` | — |
| `copilot.review_effort_lite` | trajectory (replay none; not projected, R13) | — | `admin:review_effort_default`, `admin:communicate_personal_review_settings` | yes / yes |
| `copilot.review_triggers` | behavioral | — | `admin:review_triggers`, `admin:communicate_personal_review_settings` | — |
| `copilot.review_mcp_off` | behavioral | — | `admin:repo_review_mcp_off` | — |
| `copilot.review_instructions_trim` | behavioral | — | `admin:review_instructions` | — |
| `copilot.review_unlicensed_off` | behavioral | — | `admin:review_unlicensed_policy` | — |
| `copilot.session_limits` | behavioral | — | `copilot.ci.max_ai_credits`, `admin:ci_limits_snippet` | — |
| `copilot.agentic_workflow_caps` | behavioral | — | `copilot.aw.max_ai_credits`, `admin:aw_triggers`, `admin:org_cli_billing_policy` | — |
| `copilot.budget_plan` | behavioral | — | `rest:budget_create`, `rest:cost_center_create`, `rest:cost_center_patch` | — |
| `copilot.mcp_trim` | cache_transform (replay none; projection from `static-overhead`) | — | `copilot.managed.deniedMcpServers`, `copilot.managed.allowedMcpServers` | no / no |
| `copilot.context_default` | trajectory (replay none) | — | `copilot.repo.contextTier` | yes / yes |
| `copilot.telemetry_on` | behavioral (enabler) | — | `copilot.managed.telemetry.*` | — |
| `copilot.vscode_traces_optin` (revision 3) | behavioral (enabler; replay none) | — | `admin:communicate_vscode_traces_optin` (developer snippet, §5.18) | — |

`copilot.seat_downgrade` is excluded from candidate sets while the entity's plan is unknown (R17).

### 11.2 Copilot aggregate plan (`copilot/plan.py`, CP-PLAN)

```python
def plan_copilot(cells: Sequence[Cell], pools: Sequence[PoolMonth], findings: Sequence[Finding], pricer: Pricer,
                 *, lines: Sequence[CostLine], activity: Sequence[ActivityDay], month: str,
                 include_tradeoffs: bool = False, forecast: bool = False,
                 scenario: str | None = None) -> ActionPlan          # revision 3: one call per scenario
def plan_copilot_scenarios(cells, pools, findings, pricer, **kw) -> tuple[tuple[str | None, ActionPlan], ...]
    # ((None, plan),) when every plan is known; else (("business", plan_b), ("enterprise", plan_e))
```
Candidates are the aggregate levers linked by Copilot findings; trade-off levers enter the joint set only
with `include_tradeoffs`; behavioral levers never; `copilot.seat_downgrade` never while the plan is unknown.
§9.2 defines the value function, §9.3 the reach. Each scenario plan uses only that scenario's `PoolMonth`s
and findings (`plan_scenario` dim). The headline is two columns ("if Business" / "if Enterprise"), never one
number (R17).
Acceptance: Appendix C.P1–P5, P7, P10–P13 reproduce; a seat lever in an overage entity has Shapley 0 and
headroom 0; Auto + model policy in an overage entity: Σφ = v(both) exactly; in a slack entity every credit
lever has invoice 0 and headroom = its list-equivalent saving; volume entities give seat levers no value;
P13: the idle-seat lever is worth $0 in the Business scenario and $390 in the Enterprise scenario.

### 11.3 Policy pack for target `github-copilot` (`copilot/policy.py`, `copilot/admin_actions.py`, `copilot/budgets.py`, CP-POLICY)

`build_copilot_packs(...) -> list[PolicyPack]` in `copilot/policy.py` (revision 3: the name the CP-POLICY
brief uses; the extension hook `tokenbill.pipeline.copilot:policy_packs` of CP-WIRE calls it, CA-22), one
pack per cohort (`--cohort-by team` uses managed-settings team files). Files (in `PolicyPack.hooks` as (path,
text)):

- `copilot/managed-settings.patch.json` — RFC 7386 merge patch against `--current` for the client-enforced
  keys only: `model` (`"auto"`; with teams `{"overridable": "auto"}` at enterprise level), `telemetry`
  (`enabled, endpoint, protocol, captureContent: false, lockCaptureContent: true, serviceName` (kept at
  `github-copilot` unless `--otel-service-name` is given, which is then also passed to ingestion),
  `resourceAttributes{team.id, cost_center}`), `deniedMcpServers` / `allowedMcpServers`; plus
  `rollback.patch.json`. Destination: `.github-private` repo `copilot/managed-settings.json` (applied within
  about an hour), MDM or system files. Unverified keys appear only as comments. The README states the reach
  of each key (§9.3).
- `copilot/team-mappings.patch.json`, `copilot/teams/<team>.json` — per-team `model` for stepped-wedge waves.
- `github/admin-checklist.md` — every `AdminAction` (§3.2 CA-14): what to change, where, the docs URL, the
  projection with label and reach, needs-eval / trade-off, deadline, rollback, auth note and the
  verification pointer. Server-side and communication items (`ADMIN_ACTIONS`): model policies (disable fast
  mode / premium models per cohort; "Default availability for released models"); code-review default effort
  (Lite before 2026-09-28) **and** a note that personal default efforts and personal automatic-review
  settings (new pushes, drafts) apply to reviews users request; repository setting "Allow Copilot to use MCP
  tools when reviewing pull requests"; repository custom instructions size; unlicensed-member review policy;
  runner type; "AI credits paid usage" policy; org seat policy (selected members vs all members); team
  membership review for team-assigned idle seats; cost-center AI credit pools **with the block-or-continue
  choice at the cap**; budgets with "Stop usage when budget limit is reached"; the org policy "Allow use of
  Copilot CLI billed to the organization" and agentic-workflow `max-ai-credits` / triggers; Auto tier
  guidance (Efficiency for routine work; charged at the model Auto selects).
- `github/requests.jsonl` — REST request specs, one per line, **never executed**: `{"method", "path",
  "api_version": "2026-03-10", "body", "note", "lever_id", "auth"}` for `POST
  /enterprises/{enterprise}/settings/billing/budgets` (`budget_amount` whole dollars,
  `prevent_further_usage`, `budget_alerting{will_alert, alert_recipients}`, `budget_scope`, `budget_type:
  "BundlePricing"`, `budget_product_sku: "ai_credits"`, `budget_entity_name`), `POST …/cost-centers`
  (`name`, `ai_credit_pool_enabled: true`), `PATCH …/cost-centers/{cost_center_id}`, `DELETE
  /orgs/{org}/copilot/billing/selected_users` (`{"selected_usernames": ["<from GET
  /orgs/{org}/copilot/billing/seats where last_activity_at < DATE and assigning_team is null>"]}`),
  `DELETE /orgs/{org}/copilot/billing/selected_teams` (only for teams whose every seat is idle, as counts),
  `PUT /enterprises/{enterprise}/copilot/policies/coding_agent`. `auth` carries the §19.4 requirement (e.g.
  seat removal: org owner; classic PAT `manage_billing:copilot` or `admin:org`). The README shows the
  equivalent `gh api` invocation as text.
- `repo/.github/copilot/settings.patch.json` (`model`, `effortLevel`, `contextTier`, `disabledMcpServers`;
  README: CLI only, trusted directories only) and `repo/.github/allowed_models.txt` (globs + one
  `fallback:` line).
- `ci/copilot-limits.md` — workflow snippet adding `--max-ai-credits` and the post-step `tokenbill collect
  copilot-cli --ci --billing-path copilot_direct`; `ci/agentic-workflows.md` — frontmatter `max-ai-credits:
  N` per workflow (N from `agentic-workflow-cost`), trigger review, and how to download gh-aw
  `token-usage.jsonl` artifacts for `tokenbill ingest`.
- `vscode/settings.snippet.json` + `vscode/README.md` (revision 3, §5.18): the developer opt-in for the SQLite
  span exporter (user setting, not managed), the daily `tokenbill copilot collect --source vscode` schedule
  (launchd / systemd timer / Task Scheduler examples as text), what is recorded and how to opt out.
- `github/admin-checklist.md` additions (revision 3): **JetBrains limits** — managed `model` does not apply
  in JetBrains and managed `telemetry` support for JetBrains is contradictory in GitHub's docs, so for
  JetBrains users cost control goes through server-side model policies, budgets and communication (Auto
  tier guidance); **plan unknown** — the "find out your plan" item (seats API `plan_type`, detailed report
  seat SKU, Licensing page) is listed first and the seat-plan item (`admin:seat_plan_change`) is omitted;
  every projection that depends on the plan shows both scenario values.

**Budget design** (`budgets.budget_design(pools, cells, config, *, k)`): metered entities with cost centers
(≥ k users): enable a cost center's AI credit pool when the entity is in overage and the cost center draws
> 100% of its licences' allowance, and state the block-or-continue choice; a `multi_user_cost_center`
user-level budget at the cohort p99 of monthly per-user credits (never below $1; computed from ≥ k users,
only the amount published); a `cost_center` metered budget = forecast overage p90 × 1.1 rounded up to whole
dollars, `prevent_further_usage` false with a trade-off note; an enterprise budget ≥ Σ cost-center budgets +
direct-org forecast; GitHub's sizing check "max metered = Σ user-level caps − pool" printed per entity.
Volume / azure / unknown billing mode: no cost-center pool or cost-center budget advice (VERIFY note);
enterprise / org budgets only. `prevent_further_usage` is `true` for `user` and `multi_user_customer`
scopes as the API requires. Unknown plan (R17): budget sizes that depend on the pool (cost-center budget =
forecast overage p90 × 1.1; the sizing check) are given per scenario ("$X if Business, $Y if Enterprise").

### 11.4 Settings allowlist additions (`COPILOT_ALLOWLIST` — a separate table, CA-35; target `github-copilot`)

| key | value domain | verified | source |
|---|---|---|---|
| `copilot.managed.model` | `"auto"`, a model id, `{"overridable": …}` | yes (not JetBrains) | managed-settings reference; changelog 2026-07-01 (VS Code 1.126+) |
| `copilot.managed.telemetry.{enabled,endpoint,protocol,captureContent,lockCaptureContent,serviceName,resourceAttributes,headers}` | per reference | yes | managed-settings reference |
| `copilot.managed.allowedMcpServers`, `copilot.managed.deniedMcpServers` | lists | yes | managed-settings reference |
| `copilot.repo.model`, `copilot.repo.effortLevel`, `copilot.repo.contextTier`, `copilot.repo.disabledMcpServers`, `copilot.repo.disabledSkills` | per CLI config reference (trusted directories only for model/effort/contextTier) | yes | CLI configuration-directory reference |
| `copilot.repo.allowed_models` | file with globs and `fallback: MODEL-ID` | yes | CLI configuration-directory reference |
| `copilot.ci.max_ai_credits` | int ≥ 30 | yes | set-session-limit doc; CLI 1.0.67 |
| `copilot.aw.max_ai_credits` | int > 0 (workflow frontmatter `max-ai-credits`; default 1,000 per run) | yes | about-github-agentic-workflows |

---

## 12. Reconciliation for Copilot (CP-RECON, `copilot/recon.py`)

`reconcile_copilot(...)` implements `ChannelReconciler` and returns one `ReconciliationReport` covering
`github_copilot`, `github_actions` and `github_sandbox`; RECON's `reconcile()` and `OrgScan` skip
`core.extensions.delegated_channels()`; the CLI merges reports with RECON's `merge_reports` (§21.4).
Rows are keyed by (month, entity, cost_center, sku, model, layer).

1. **L0 parity (per request, when client data exists):** our point price vs `provider_reported_cost_nano`
   (match = within 1 nano per line); per conversation, Σ our chat-span prices vs the COST_STATE
   `copilot.otel.invoke_agent` total. Diagnostics settle open facts: the ratio on `routing=auto` requests
   (whether nano-AIU includes Auto, §19.5 #3), requests with writes (published vs 2 × input write price,
   residual `copilot_write_1h_price`), requests with a known context tier near a band threshold (hypothesis
   A vs B, `copilot_band_hypothesis`). Residual `copilot_rate_mismatch`.
2. **L1 rate-card check on report tokens:** per (day, model, sku, routing) cells excluding pseudo cells,
   `Σ tokens × Copilot rates (modifiers applied)` vs `Σ gross` under both conventions. **Decision rule:** a
   file's convention is decided only when Σ(read + write) ≥ 5% of the cells' input tokens, the better
   convention's model-day error is within `tolerance_pct` (0.5%) and the other's exceeds 2 × tolerance; else
   `undecidable` (`dq.copilot_convention_undecidable`, `mapping_verified=False`, verdict at best
   `reconciled` "totals only"). L1 also yields `gross_is_list` for DC22 (gross within tolerance of tokens ×
   list on the decided convention). Days within ±2 of a K-dated rate change of a model in the cell →
   residual `copilot_rate_boundary`, excluded from the tolerance test. Auto rows ≈ −10%
   (`copilot_auto_discount`), compliance ≈ +10% (`copilot_compliance_uplift`), long-context models with
   positive error (`copilot_long_context_band`), rounding (`copilot_rounding` from
   `rounding_remainders["github.ai_usage_report"]`, fed by `LedgerStore.source_stats`).
3. **L2 token coverage:** ledger Copilot inferences vs report tokens per (day, team, model); ledger > report
   + max(1%, 1,000 tokens) = over-count row (fails the channel); report > ledger = `unobserved_traffic`.
4. **L3 invoice identities per entity × month:** row identity (±1 nano per row); discount classification
   (DC22; residual `copilot_discount_unclassified` until classified, `copilot_pool_included` after);
   direct rows' pool draw (`copilot_direct_pool_draw`); Σ net vs REST `usage/summary` `netAmount` **per
   product** (all AI-credit SKUs together) and per SKU only as a diagnostic while the report's SKU grouping is
   unverified (§19.5 #1); capped cost centers vs `ai_credit_pool_state.target_amount`; seats: seat SKU net vs
   count × list (`copilot_seat_proration`; `copilot_seat_contract` for volume / azure); Actions workload lines
   vs the summary's Actions SKUs → `github_actions` verdict; sandbox lines vs the summary's sandbox SKUs →
   `github_sandbox` verdict; revision check: the latest coverage aggregate per (day) vs Σ current cost lines
   of that day → `copilot_revision_stale_rows`.
5. **Plan diagnostic (revision 3).** Per entity × closed month with an unknown plan, which scenario's pool
   is consistent with the observed pooled discounts: when Σ discount classified pool-included exceeds the
   Business-scenario pool, only the Enterprise scenario fits (residual `copilot_plan_inferred`, value
   `enterprise`); when overage (net > 0 on pooled rows) starts while consumption is below the Enterprise
   pool and the entity has no capped cost center (a cap can create overage below the pool), only Business
   fits; else `undetermined`. The result is written to `ChannelDecision.plan_inferred`
   and printed as a diagnostic ("the bill looks like …; confirm with the seats API or the Licensing page");
   it **never** changes `PlanEvidence`, a label, a pool or a scenario (R17).

**Decisions handed on (revision 3).** For every AI-usage-report source the reconciler appends one
`ChannelDecision(channel="github_copilot", source_id, convention, gross_is_list, plan_inferred)` to
`ReconciliationReport.decisions` (CA-23); the pipeline passes them to `core.extensions.enrich(...,
decisions=…)` so CP-STORE's enricher builds cells under the decided convention and classifies discounts with
the decided `gross_is_list` (revision 2 had no carrier for either).

Other residual codes: `copilot_directional_report`, `copilot_preview_columns`, `copilot_utility_unbilled`,
`copilot_unattributed_org`, `copilot_plan_inferred`, `revision_window`, `unobserved_traffic`, `unexplained`. Verdict per channel
(SPEC §12.4 semantics): `github_copilot` is `reconciled` iff L1 is within tolerance on every closed model-day
with tokens (excluding pseudo and boundary cells), every row identity holds, L3 per-product net matches the
usage summary (else `mapping_verified=False` "report only"), and there are no over-count rows.
`ChannelVerdict.invoice_sources = ("github.ai_usage_report", "github.billing_api")`.

### 12.1 Test design against circularity (binding for CP-RECON and CP-SYNTH; revision 2 numbered it 12.6)

Synthetic reports are generated under **both** conventions and under one undecidable variant; the
reconciler must name each file's convention blind (and refuse the undecidable one). Synthetic verdicts are
printed as `reconciled (synthetic; schema unverified)` and never count as release evidence. **Release
gate:** a real, redacted AI usage report (token columns), the matching usage summary and a detailed usage
report with seat lines from the adopting enterprise are added as `real-redacted` fixtures before 0.2.0 GA;
until then `dq.recon_schema_unverified` is printed for `github_copilot`.

---

## 13. Verification (CP-RECON `copilot/panel.py`; VERIFY estimators unchanged)

`build_copilot_panel(store, record_stores, *, cluster_kind="team", since, until, baseline_pricer,
actual_pricer, arms)` → `PanelRow`s per team × day from report cells: `cost_baseline_nano` = report tokens
repriced at the pre-registered baseline card (pseudo cells at reported gross in both), `cost_actual_nano`
at the actual card, `active_dev_days` = distinct principals with an `ActivityDay` that day (k ≥ 5 at
publication), outcome PRs None. Unit: list-equivalent Copilot credits per active developer-day. Designs:
stepped wedge / cluster RCT by enterprise team via managed-settings team files (`copilot.default_model_auto`);
ITS with placebo dates for org-wide changes (review effort, model policies, seat policy). DC15's exogenous
events are covariates. Invoice realization of a measured saving uses `core.pool.invoice_delta` on the
observed pool month (label capped by the estimator's label); receipts keep list-equivalent and invoice
figures apart. Revision 3: while the plan is unknown, the realization is given per scenario (R17); panels
from a handoff bundle count active developers among adopted-key principals only (R-E21); JetBrains-heavy
teams are stratified in stepped-wedge designs for `copilot.default_model_auto` because managed `model` does
not reach them (§9.3). Registered through `ExtensionSpec.panel_builder`; `measure --panel copilot` reaches it via
`core.extensions.panel`.

---

## 14. Outputs (CP-OUT)

### 14.1 Bill lines and labels (R16)

| line | amount and label |
|---|---|
| `seats.business`, `seats.enterprise` | seat-SKU lines of the month: INVOICE if closed, final and `github_copilot` reconciled, else EXACT LIST ("unreconciled"/"provisional"); no seat lines: count × list price, ESTIMATED LIST ("list price × seats; proration, upfront charges, volume/EA pricing not modeled") |
| `ai_credits.gross` | Σ report gross, LIST_EQUIVALENT (EXACT) — the credit valuation of usage |
| `ai_credits.discount_pool` | classified pool-included discount (DC22), LIST_EQUIVALENT |
| `ai_credits.discount_other` | classified non-pool discounts (e.g. Auto when it appears as a discount), EXACT LIST |
| `ai_credits.discount_unclassified` | discount before classification, EXACT LIST, note "includes included usage and other discounts" |
| `ai_credits.overage`, `ai_credits.direct_org` | Σ net of pooled / direct rows: INVOICE per R16, else EXACT LIST; open months show observed-so-far (LIST, provisional) and the forecast separately (ESTIMATED, in the pool bar) |
| `code_quality.*`, `actions.code_review`, `actions.cloud_agent`, `actions.agentic_workflow`, `sandbox` | Σ net of their lines, INVOICE per R16 with their own channel's verdict (`github_actions`, `github_sandbox`), else EXACT LIST |
| `seats.unknown_plan` (revision 3) | seats whose plan is unknown: one line per scenario (`scenario="business"`: n × $19; `"enterprise"`: n × $39), ESTIMATED LIST, note "plan unknown: both scenarios shown" |
| `total.invoice` | `core.labels.combine_weakest` over the dollar lines (never LIST_EQUIVALENT lines): INVOICE only if every component is; `components` lists them; the note names each non-invoice component. Revision 3: while the plan is unknown, one `total.invoice` per scenario (its `seats.unknown_plan` line and that scenario's overage), ESTIMATED; the two are never combined |

### 14.2 Sections

- **Bill section** (`CopilotSection.terminal/html/json`): per entity × month the bill lines with label chips;
  the pool bar (pool vs consumed vs forecast range, cap-policy range) with regime, billing mode and
  direct-draw status; per-channel verdict badges; the Copilot plan (headline invoice ESTIMATED, pool headroom
  on its own line, levers with Shapley credit, reach, trade-off / needs-eval tags, never a standalone sum);
  "WHAT TO CHANGE IN GITHUB" (admin actions with deadlines and auth notes). The generic BILL shows
  `PricedTotal.pool` on its own line ("Copilot credits seen by collectors, list-equivalent").
- **Plan block (revision 3, R17)** — first in the section: per entity the detected plan with its evidence
  source ("Enterprise — from seat SKU lines"), or "Plan unknown — both scenarios shown" with the how-to-find-out
  hint (seats API `plan_type` via `copilot pull --sources seats`; the detailed usage report's seat SKU
  `copilot_for_business` / `copilot_enterprise`; Enterprise settings → Licensing; or `--plan ENTITY=…`), and
  conflicts shown with both sources. While unknown, the bill, the pool bar and the plan headline render as two
  side-by-side columns **"if Business" | "if Enterprise"** (terminal: two columns; HTML: a two-column table
  with a shared row header; JSON: `"scenarios": {"business": {…}, "enterprise": {…}}`), each ESTIMATED;
  scenario-independent lines (gross credits, discounts, direct-org net, Actions, sandbox) render once.
- **Showback** (`render_copilot_showback`): per team (k ≥ 5): seats by plan and activity bucket, pooled
  credits per active developer-month (p50/p90), share of the entity pool, overage allocated pro rata by
  credits (`AllocatedMethodId = copilot_pro_rata_credits`, ESTIMATED allocation) beside GitHub's own per-row
  net (R16 label), review / cloud agent / agentic workflow figures (credits and dollars apart), top Copilot
  findings; revision 3: a **VS Code vs JetBrains** split per team (report credits apportioned by the team's
  `ide:*` interaction shares, ESTIMATED allocation, `CopilotSummary.editor_split`, only cells with ≥ k users;
  "other editors" merged) and, while the plan is unknown, the pool share and allocated overage per scenario;
  HTML (CSP, table twins), CSV, JSON.
- **result@2 JSON**: `"copilot": {…}` via `CopilotSection.json` (MONEY via `core.labels.figure_json`; no
  floats; `evidence` keys; list-equivalent never under billed keys).

### 14.3 FOCUS rows (`copilot/focus.py: focus_rows`)

Extension channels own their FOCUS rows: `core.extensions.focus_rows` returns the rows **and** the channels
they cover, and OUT drops ledger `cost_rows` for those channels (collector-lane detail for Copilot never
enters FOCUS, so `copilot_direct` lanes cannot add `BilledCost` beside the report rows). AI-credit rows:
`ChargeCategory=Usage`, `ChargeFrequency=Usage-Based`, `ListCost` = gross, `BilledCost` / `EffectiveCost` =
net (only for reconciled channels unless `allow_unreconciled`, then `x_Reconciled=false`), `x_DiscountPool`
/ `x_DiscountOther` / `x_DiscountUnclassified`, `x_CreditsQuantity`, `x_PriceBasis`,
`x_Channel=github_copilot`, `SkuId` = SKU, `PricingUnit="AI Credits"` (**VERIFY** allowed values), Tags
team / cost_center; seat rows `ChargeCategory=Purchase`, `ChargeFrequency=Recurring` (**VERIFY**, fallback
`Usage` with a note); Actions rows on `x_Channel=github_actions`; sandbox rows on `github_sandbox`. FOCUS
rows carry only amounts from GitHub's own lines (report rows, seat lines, Actions / sandbox lines), so they
never depend on a plan scenario; estimated seat fees for unknown plans (`seats.unknown_plan`) are not FOCUS
rows (revision 3). OUT
accepts any `x_` column a `FocusRow` carries (regex-checked) into its header union. `--role enrichment`
zeroes Billed / Effective; teams < k merged (`x_SuppressedUsers`).

---

## 15. CLI (CP-WIRE: `commands/copilot.py`, `pipeline/copilot.py`)

**Aliases (DC23).** `core.extensions.rewrite_argv` runs in `cli.main` before argparse: `tokenbill scan
--copilot …` → `tokenbill copilot scan …`; `tokenbill me --copilot …` → `tokenbill copilot me …`;
`tokenbill collect copilot-cli …` → `tokenbill copilot collect --source cli …`; revision 3: `tokenbill
collect copilot-vscode …` → `tokenbill copilot collect --source vscode …`. No Copilot flag is added to the
`scan`, `me` or `collect` parsers, and `scan --org` (boolean, D32) is untouched; the Copilot org selector is
`--github-org`. The parser lives in `tokenbill/commands/copilot.py` (CP-WIRE); the handoff verbs call
CP-HANDOFF functions resolved lazily (`tokenbill.copilot.handoff`), so CP-WIRE's unit tests use fakes and the
end-to-end path is a gate test.

**Who runs what (revision 3).** *Admin* (GitHub rights, runs offline except `pull`): `admin-guide`, `pull …
--out export.tbx`, `export`, `inspect`, `pseudonym`. *Product owner / analyst* (no GitHub rights, no network):
`scan --from export.tbx`, `inspect`, `rates`, `policy --target github-copilot`. *Developer* (opt-in): `me`,
`collect --source vscode`.

| command | purpose | flags |
|---|---|---|
| `tokenbill copilot scan` | org scan from files, a handoff bundle or `--live` | revision 3: `--from FILE.tbx…` (one or more bundles; the key ids shown by `inspect` are adopted after confirmation or with `--adopt-key-id KID…`, R-E21 — revision 3.1: adopted automatically, see the precedence table; bundles with different key ids are refused together unless `--allow-mixed-keys`, which then joins them by team only), `--plan ENTITY=business\|enterprise\|mixed` (an admin statement, lowest precedence, **no default**, R17), `--activity-report FILE…`, `--answers FILE`; and `--ai-usage FILE…`, `--metered FILE…`, `--billing-json FILE…`, `--config-json FILE…` (budgets, cost centers, org settings), `--metrics FILE…`, `--seats FILE…`, `--agent-tasks FILE…`, `--trace FILE…`, `--otel FILE…`, `--vscode-traces FILE…`, `--gh-aw FILE…`, `--from DIR` (a `copilot pull` directory), `--team-map FILE`, `--cost-center-map FILE`, `--pool-seats ENTITY:business=N,enterprise=M`, `--billing-mode ENTITY=metered\|volume\|azure`, `--renewal-date ENTITY=DATE`, `--capped-policy CC=block\|continue`, `--compliance {none,data_residency,fedramp}`, `--no-promo`, `--forecast`, `--since/--until`, `--as-of DATE`, `--db` (default temp), `--include-tradeoffs`, `--policy-out DIR`, `--otel-service-name NAME[=agent_product]…` (revision 3: `=copilot_jetbrains` with `--experimental copilot-jetbrains-otel`), `--experimental FEATURE…`, `-o report.html`, `--format`, `--require-reconciled`, `--live` with `--github-token-env VAR \| --github-token-file PATH`, `--github-enterprise SLUG`, `--github-org SLUG…`, `--record DIR`, `--resume`; `--from DIR` still accepts a `copilot pull` directory on the admin's machine |
| `tokenbill copilot pull` | `--live` only: record sources into `--out DIR`, or (revision 3) produce a handoff bundle with `--out FILE.tbx` (raw files in a private temp dir, deleted after export unless `--keep-raw DIR`, §5.12) | `--github-enterprise`, `--github-org…`, `--since/--until`, `--sources ai_usage,metered,summary,metrics,seats,config,agent_tasks`, token flags, `--github-user-token-env VAR` + `--agent-repos FILE` (agent tasks), `--resume`; with `.tbx`: the `copilot export` options below |
| `tokenbill copilot admin-guide` (revision 3) | print the admin guide (§22.2) with the facts-backed permission table; `--format md\|text`, `-o FILE`, `--answers-template FILE` writes the answers template | — |
| `tokenbill copilot export` (revision 3) | offline, no token: raw GitHub files → `copilot-export@1` bundle (§22.5) | `--in DIR\|FILE…` (report CSVs, detailed CSV, activity report, seats / org / budgets / cost-center JSON, metrics NDJSON incl. dashboard export with `--experimental copilot-dashboard-ndjson`), `--user-teams FILE…` \| `--team-map-csv FILE` (`login,team` or `login,team,cost_center`), `--answers FILE`, `--k 5`, `--key-file PATH` (default `~/.config/tokenbill/copilot-export.key`, created 0600), `--rotate-key`, `--aggregate-only`, `--hash-workspaces`, `--exclude-logins FILE` (§22.7), `--since/--until`, `--experimental FEATURE…`, `--out FILE.tbx` (required; refuses to overwrite without `--force`) |
| `tokenbill copilot inspect FILE.tbx` (revision 3) | counts only: what the bundle holds (manifest, per-member record counts, teams kept / merged, months, plan evidence, key ids, leak-scan result) — never a row, never a pseudonym list | `--json` |
| `tokenbill copilot pseudonym` (revision 3, admin only) | print the `p_` of one login under the export key, for an erasure request the analyst then runs as `tokenbill purge --principal p_…` | `--key-file PATH`, `--login LOGIN` (read from stdin when `-`; never logged) |
| `tokenbill copilot team-map` | build `team_map.json` / `cost_center_map.json` (k ≥ 5) | `--user-teams FILE…`, `--cost-centers FILE`, `--k 5`, `-o DIR` |
| `tokenbill copilot collect` (aliases `collect copilot-cli`, `collect copilot-vscode`) | on-device / CI collector → trace@2 `usage` | revision 3: `--source vscode\|cli` (required; `vscode` = §5.18 with `--vscode-traces PATH`, `--vscode-outfile PATH`, `--insiders`), `--copilot-home`, `--out DIR`, `--state FILE`, `--since`, `--principal-ref`, `--identity-mode`, `--collection-key-file`, `--team`, `--attr K=V`, `--ci` (requires `--billing-path copilot_pool\|copilot_direct`), `--experimental copilot-store`; reads `GITHUB_*` env like the CC CI collector |
| `tokenbill copilot me` (alias `me --copilot`) | self-view: VS Code `agent-traces.db` first (default global-storage paths of §5.13), then `~/.copilot`; JetBrains users get the "no local per-request data; your team's numbers come from org data" note | `--vscode-traces PATH`, `--copilot-home`, `--agent-tasks FILE`, `--since/--until`, `-o` |
| `tokenbill copilot rates` | show / verify the Copilot rows | `show [MODEL] [--at DATE]`, `verify [--snapshot FILE] [--live]` |
| `tokenbill policy --target github-copilot` | policy pack from a store (CLI-SAVINGS dispatches the target via `core.extensions.policy_targets`) | as SPEC §15 + `--cohort-by team` |

`run_scan_copilot` = ingest (sniffed adapters; deferral re-reads; bundles through `copilot-export` into a
store opened with `adopt_key_ids`) into STORE + record store → Copilot reconciliation (+ RECON for other
channels) → `enrich` (plans, pools under the decided conventions, reconciled channels, `decisions` from the
reports) → `run_detectors`
(`aggregates_only=False` per shard, then `True` once) → `rescope_findings` with `count_users_fn` → PLAN for
lanes (when Copilot lanes exist) → `summarize` (aggregate plan, admin actions, `CopilotSummary`) → policy
packs when `--policy-out` → `RunResult`. Exit codes as SPEC §15 (`--live` without a token flag → 2; token
expiry during a pull → 3 with the resume hint; reconciliation failure is reported, exit 3 only with
`--require-reconciled`). Revision 3: `export` / `pull --out *.tbx` exit 3 when the leak gate finds anything
(nothing written; the report names the member and the kind of value, never the value); `scan --from` exits 2
for a malformed or unsafe bundle and 3 for an unadopted key id; `--plan` without `ENTITY=` is a usage error
(there is no default plan).

---

## 16. Backward compatibility

All core additions have defaults; no field renamed; demo/analyze goldens unchanged. `billing_class()`
returns `"pool"` only for the two new paths. trace@2 files written before this addendum stay valid; TRACE
derives closed key sets from the core dataclass fields, so the new optional keys are accepted. **Mixed OTLP
files** that `otlp` ingests today keep every non-Copilot span (TELEM skips only Copilot resources and defers
them; §5.10). Sniffing and convention loading skip unimportable modules (R-E18), so wave-2 branches merged
out of order never break sibling tests or the gate-1 smoke. Claude-only fleets see no Copilot output
(extension detectors gated on `ext:copilot`; no Copilot channels → no extension FOCUS rows). Revision 3:
the pinned wave-1 tests of §2.3 are untouched (separate Copilot tables; `cohort_key` unchanged); `CacheRules`
keeps its field types; `LicenseSnapshot.assigned_via_team` becomes optional (additive: `None` is a new
value, `bool` inputs behave as before). The bundle format is versioned (`tokenbill/copilot-export@1`); a
reader refuses a higher major version with a clear message and accepts unknown manifest keys (ignored,
counted).

## 17. Performance budgets (Copilot)

| operation | budget |
|---|---|
| AI usage report parse | ≥ 50,000 rows/s; 600,000 rows ≤ 20 s, RSS ≤ 300 MB |
| metrics NDJSON parse | ≥ 5,000 records/s; 150,000 user-days ≤ 45 s, streaming |
| cells + pool months + forecast | 10⁶ cost lines ≤ 20 s |
| `copilot scan`, 5,000 seats, one month, no lanes | ≤ 3 min single process, RSS ≤ 1 GB |
| aggregate plan (≤ 6 levers, 64 joint evaluations, ≤ 50k monthly cells) | ≤ 10 s |
| CLI events collector | 10⁵ events ≤ 5 s |
| VS Code `agent-traces.db` read | 10⁵ spans ≤ 10 s |
| VS Code collector, incremental run (revision 3) | 10⁴ new spans ≤ 2 s; no-op run ≤ 0.2 s |
| `copilot export` (revision 3), 5,000 seats, one 31-day AI usage report (600,000 rows), metrics for 31 days | ≤ 90 s, RSS ≤ 1 GB, leak scan included |
| `copilot inspect` of that bundle | ≤ 5 s |
| `copilot scan --from export.tbx`, same data | ≤ 3 min (as `copilot scan`) |

## 18. Synthetic Copilot enterprise (CP-SYNTH, CP-SYNTH-W) and flagship acceptance

`synth/copilot_world.py: generate(seed=7, *, users=127, start="2026-07-03", end="2026-09-22") ->
CopilotWorld` (canonical records and closed-form truth, int nano; `synth/copilot_truth.py`) and
`synth/copilot_writers.py: write_world(world, out_dir) -> dict[str, Path]` (schema-true files). The window
starts on 2026-07-03 so that every model used is priced on every day and no usage falls within ±2 days of
a K-dated rate change (Sonnet 5 row from 2026-06-30; Opus 4.8 fast from 2026-06-29); Opus 5.5 (row from
2026-09-22, changelog-dated) is used only on 2026-09-22; GPT-5.6 Sol is not used. Prices come from
`core.testing.FakePricer` (every Copilot row). Canary in every content field.

| team (users) | plant | expected recovery |
|---|---|---|
| platform (20, Enterprise, direct seats, `assign_selected` org A) | 6 idle seats (none_90d), 4 plan-mix seats, 5 $0 user-level budgets | `idle-seat` removable 6; `plan-mix` 4; `budget-zero-user-budget` 5; seat savings per regime = truth (±0) |
| infra (10, org A, team-assigned) | 5 idle team-assigned seats | `idle-seat` team-assigned 5, projection None |
| ops (8, org B, `assign_all`) | 5 idle seats | `seat-auto-assign` 5 with projection = truth |
| payments (20) | Opus 4.8 on 2026-07-03 … 09-21 and Opus 5.5 on 09-22 (60% of credits), no Auto | `premium-model-share` (price-only ±0 on identical tokens); `auto-adoption` 10% × reach ±0 |
| mobile (15) | Opus 4.8 fast mode | `fast-mode` premium exact ±0 |
| data (15) | GPT-5.4 and GPT-5.5 | `forced-migration` Δ exact ±0 |
| ci (automation) | empty-username code review + direct CLI; review on `linux_16_core`; one agentic workflow (`.lock.yml` Actions lines, gh-aw `token-usage.jsonl`, direct credits in its repo) | `direct-org-usage` net exact; `larger-runner` range contains truth; `agentic-workflow-cost` per-run p50/p90 exact on the gh-aw lanes |
| agents (5, CLI collectors; revision 3: shrunk from 10) | CLI events (+ store in the gate test that enables `copilot-store`) + CLI OTel: forced compactions, 30k tool definitions, one events-only session | `compaction-cost` ±0 (also on the events-only session, §10.3 gating), `static-overhead` range contains truth, `ci-uncapped` on the `--ci` lane |
| vscode (10; revision 3: enlarged from 5) | per developer a VS Code `agent-traces.db` (built from the DDL) + an otel outfile for 3 of them: band requests (some with a known context tier → ranges), cache writes, model switches mid-session, synthesized CLI-agent spans (dedupe), one database pruned between two collector runs | requests ingested through the collector (trace@2) and the self-view; `long-context-band` ±0 (A) / range (A vs B), `cache.miss-by-cause model-switch`, L0 parity match on nano-AIU, `dq.copilot_vscode_retention_gap` on the pruned one; content rows never selected |
| jetbrains (10; revision 3, new) | org data only (report rows, metrics with `ide:intellij` ≈ 90% of interactions, seats with JetBrains editor strings); no local data | `editor-mix` JetBrains share = truth; `auto-adoption` projection uses reach = 1 − JetBrains share (±0 vs truth); fix text puts the server-side model policy first; `copilot me` prints the JetBrains note |
| core (10, control; revision 3: 20 → 10) | Auto default, Sonnet 5 | no finding with invoice or headroom ≥ `min_usd` |
| tiny (4) | normal use | merged into `(other)` everywhere |

Months: July and August on promo allowances (slack), September standard (overage) → `promo-cliff` and
`pool-regime` truths; one capped cost center with an unknown cap policy (overage range); budgets with
stop-usage off. **Report files** are written three times: under the `excl` convention, under the `incl`
convention and as an undecidable variant (no cache tokens) — the reconciler must identify the first two
blind and refuse the third. A `--billing-mode enterprise=volume` variant gives seat levers no value.
**Revision-3 variants.** (a) `plan_unknown`: only the AI usage report, the activity report and an answers file
with `plan_as_shown: unknown` (no seats API, no seat lines, no org settings) → `plan-status` unknown, two
scenario `PoolMonth`s per month, bill / plan / seat projections per scenario equal the world's truth for
**both** scenarios (closed-form), `plan-mix` suppressed, `copilot.seat_downgrade` absent. (b)
`plan_conflict`: seats API `plan_type=business` for org B while its seat lines say `copilot_enterprise` →
seat lines win, `plan_conflict=True`, `dq.copilot_plan_conflict`. (c) `plan_quota`: (a) plus the
`total_monthly_quota` column with the experimental flag → plan detected from `report_quota` (Appendix C.P15).
(d) **Handoff paths.** The world is written twice for one admin: the **token path** (recorded REST pages and
export CSVs, as `copilot pull` records them) and the **UI path** (AI usage CSV, detailed CSV, activity-report
CSV, dashboard NDJSON, answers JSON); each is turned into a bundle by CP-HANDOFF's `export_bundle` with one
export key. Gate: the `cost_lines` and `aggregates` members of both bundles are byte-identical; `licenses`
agree on every field both paths carry (principal, team, activity buckets, surface) and differ only where the
UI path has no data (`plan`, `assigned_via_team`, `org`); both bundles pass the leak scan; `scan --from` on
either reproduces every org-data plant (seat-assignment kinds only where the path carries assignment data).
CP-SYNTH-W writes the raw inputs of both paths and the expected manifest counts; it does not write `.tbx`
files itself (one bundle writer, CP-HANDOFF).
Flagship (INTEGRATION `tests/v2/e2e/test_copilot_flagship.py`): every plant recovered within tolerance;
control clean; `github_copilot` reported `reconciled (synthetic; schema unverified)` on the `excl` and
`incl` files; the aggregate plan's seat lever has invoice 0 in September and > 0 in a slack variant; no
login or canary in any output; JSON byte-identical across two processes; `copilot scan` keyless and
networkless < 60 s; revision 3: the same assertions for `copilot scan --from export.tbx` on both handoff
bundles, and no login, email, repository name or workflow path inside either bundle (unzipped byte scan).

---

## 19. Facts the Copilot build depends on (opened 2026-09-23 unless stated)

Status column: **FC** = confirmed by the fact-check (primary source re-opened); **FC-corr** = corrected by the
fact-check (corrected value used); **LA** = re-opened by the lead architect for revision 2 (local copies of
the primary source, 2026-09-23); **VERIFY** = not settled (ships disabled, commented, or as an assumption
with a dq/residual code).

### 19.1 Billing model

| # | fact | value | source | status |
|---|---|---|---|---|
| 1 | Credit unit | 1 AI credit = $0.01 | `/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/billing` | FC |
| 2 | Seats, included credits | Business $19 / 1,900; Enterprise $39 / 3,900 | same; plans page | FC |
| 3 | Pool | pooled per billing entity; resets 00:00 UTC on the 1st; forfeited; adds raise the pool at once; removals next cycle | org billing doc | FC |
| 4 | Promo | existing Business/Enterprise: $30 / $70 of credits per user-month for June–August 2026 | blog 2026-04-27; docs `copilot.yml`; removal commit `19a11020` (2026-09-11) | FC |
| 5 | Overage | "AI credits paid usage" on by default; $0.01/credit | org billing doc | FC |
| 6 | Free usage | completions, next edit suggestions, utility models (GPT-4o mini, GPT-4o, GPT-4.1, GPT-5.4 nano), local BYOK | models-and-pricing; utility-models | FC (BYOK = inference) |
| 7 | Auto discount | 10% on paid plans; Chat / CLI / Copilot app / cloud agent; charged at the model Auto selects, **regardless of Auto tier** (Efficiency / Balance / Intelligence, rolling out in VS Code, CLI, Copilot app) | auto-model-selection; changelog 2026-09-14 | FC, LA; report representation **VERIFY** |
| 8 | Data residency / FedRAMP | +10% when a restrict-to-compliant-models policy is on | data-residency and FedRAMP docs | FC-corr; stacking **VERIFY** |
| 9 | Budgets | user-level budgets always hard-stop and cover pool + metered; cost-center/org/enterprise budgets act after the pool, hard-stop only with "Stop usage…" | budgets concept | FC |
| 10 | Cost-center pool cap | caps a cost center's included usage at its licences' allowance; "When a cost center reaches its cap, you choose whether its members are blocked or their additional usage continues as paid overage"; REST exposes `ai_credit_pool_enabled` and `ai_credit_pool_state` only | billing cost-centers concept; budgets concept; ghec.json | FC, LA (which choice an entity made: input `--capped-policy`) |
| 11 | Seat billing | metered: adds prorated; removals billed to cycle end; a multi-org seat is billed once via a random org | license-changes, seat-assignment | FC |
| 12 | Upfront seats | card/PayPal customers charged upfront from 2026-10-01; included usage may be prorated | changelog 2026-08-28 | FC; proration **VERIFY** |
| 13 | Code review | credits + Actions minutes; published estimates $0.05–$1 Lite, $0.25–$5 Balanced; default flips to Balanced on 2026-09-28 unless Lite chosen; unlicensed/bot reviews billed directly to the org | code-review concept; changelog 2026-08-28 | FC-corr (pool draw **VERIFY**) |
| 14 | Code-review cost drivers | "Consumption generally increases with pull request size and repository custom instructions"; repository setting "Allow Copilot to use MCP tools when reviewing pull requests" enabled by default; GitHub and Playwright MCP servers enabled by default | code-review concept | LA |
| 15 | Personal review settings | every plan: automatic review, review of new pushes and drafts, default effort that "applies to reviews you request"; enterprise-wide default effort (Lite, Balanced, GitHub default) | changelog 2026-09-23 | LA |
| 16 | Review efficiency | "reduced Copilot code review costs by about 20%" | changelog 2026-06-25 | LA |
| 17 | Cloud agent | credits + Actions minutes; 59-minute hard limit; SKU `coding_agent_ai_credit` | about-cloud-agent; SKU reference | FC |
| 18 | CLI in Actions | `GITHUB_TOKEN` in an org repo: metered to the org, user budgets not considered; PAT: owner's seat | CLI-in-Actions doc | FC; pool draw **VERIFY** |
| 19 | Agentic workflows | Actions minutes + inference; "1 AIC = $0.01 USD"; Copilot engine AIC maps to AI credits; `max-ai-credits` frontmatter, "default cap is 1,000 AIC per run"; org billing needs `copilot-requests: write` and the policy "Allow use of Copilot CLI billed to the organization" (enabled by default when "Copilot CLI" is enabled); compiled to a `.lock.yml` Actions workflow; `gh aw logs` / `gh aw audit` AIC values are "best-effort estimates" | about-github-agentic-workflows (public preview) | FC (F16), LA |
| 20 | Session limits | `--max-ai-credits N` / `/limits set max-ai-credits N`, minimum 30, soft | set-session-limit; CLI 1.0.67 | FC |
| 21 | Actions rates | `actions_linux` $0.006, `_slim` 0.002, `_arm` 0.005, `actions_windows` 0.010, `actions_windows_arm` 0.010, `actions_macos` 0.062; larger runners from $0.005 up to $0.552; `linux_16_core` 0.042; GHEC 50,000 included minutes; "Included minutes cannot be used for larger runners"; public-repo standard runners free | actions-runner-pricing; Actions billing | FC-corr |
| 22 | Code Quality | $10 / active committer / month; AI credits from the shared pool | Code Quality billing | FC |
| 23 | Retirements | 2026-10-02: Opus 4.7 → Opus 5, Gemini 3.5/3.6 Flash → 3.8 Flash, Kimi K2.7 Code → K3; 2026-10-19: GPT-5.5 & GPT-5.4 → GPT-5.6 Sol, GPT-5.4 mini & GPT-5 mini → GPT-5.6 Luna, Gemini 3.7 Flash → 3.8 Flash, Grok 4.5 → 4.6 | changelogs 2026-09-03, 2026-09-18 | FC |
| 24 | Cost centers vs billing mode | "Cost centers only apply to metered usage, and do not work with volume or subscription billing" | billing cost-centers concept | FC (F22), LA |
| 25 | Volume billing cycle | volume licences "are often billed based on the anniversary date of your subscription rather than by calendar month" | billing-cycles concept | FC (F22), LA |
| 26 | Seat removal semantics | `DELETE /orgs/{org}/copilot/billing/selected_users` sets seats to "pending cancellation … at the end of the current billing cycle unless they retain access through team membership"; `…/selected_teams` likewise for all members of listed teams | ghec.json | LA |
| 27 | Seat assignment mode | `seat_management_setting` ∈ `assign_all`, `assign_selected`, `disabled`, `unconfigured` | ghec.json (`copilot-organization-details`) | LA |
| 28 | Legacy PRU; individual plans | annual Pro/Pro+ only, $0.04/request; Pro/Pro+/Max allowances with variable flex | legacy docs; plans; discussion #197089 | FC |

### 19.2 Copilot rate table (USD per 1M tokens; YAML `2026-09-22_d1153b57c9`)

Date source: **B** billing start (row existed before 2026-06-01), **C** changelog, **D** pricing text,
**K** docs commit only (**VERIFY**). Write column: published write price / assumed 1h price (Claude models
only, 2 × input, **VERIFY**).

| model (canonical id) | category | tier | input | cached | write | output | effective_from (source) |
|---|---|---|--:|--:|--:|--:|---|
| gpt-5-mini | Lightweight | – | 0.25 | 0.025 | – | 2.00 | 2026-06-01 (B) |
| gpt-5.3-codex | Powerful | – | 1.75 | 0.175 | – | 14.00 | 2026-06-01 (B) |
| gpt-5.4 | Versatile | ≤ / > 272K | 2.50 / 5.00 | 0.25 / 0.50 | – | 15.00 / 22.50 | 06-01 (B); band 06-04 (K) |
| gpt-5.4-mini | Lightweight | – | 0.75 | 0.075 | – | 4.50 | 06-01 (B) |
| gpt-5.4-nano | Lightweight | – | 0.20 | 0.02 | – | 1.25 | 06-01 (B) |
| gpt-5.5 | Powerful | ≤ / > 272K | 5.00 / 10.00 | 0.50 / 1.00 | – | 30.00 / 45.00 | 06-01 (B); band 06-04 (K) |
| gpt-5.6-luna | Lightweight | ≤ / > 200K | 0.20 / 0.40 | 0.02 / 0.04 | 0.25 / 0.50 | 1.20 / 1.80 | 07-30 (K); write 08-03 (K) |
| gpt-5.6-sol | Powerful | ≤ / > 272K | 4.00 / 8.00 | 0.40 / 0.80 | 5.00 / 10.00 | 20.00 / 30.00 | 09-04 (D) |
| gpt-5.6-terra | Versatile | ≤ / > 272K | 2.00 / 4.00 | 0.20 / 0.40 | 2.50 / 5.00 | 12.00 / 18.00 | 07-30 (K); write 08-03 (K) |
| gpt-6-astra | Powerful | ≤ / > 272K | 10.00 / 20.00 | 1.00 / 2.00 | 12.50 / 25.00 | 50.00 / 75.00 | 09-04 (K) |
| gpt-6-luna | Lightweight | ≤ / > 272K | 0.10 / 0.20 | 0.01 / 0.02 | 0.125 / 0.25 | 0.50 / 0.75 | 09-22 (C) |
| gpt-6-sol | Powerful | ≤ / > 272K | 2.00 / 4.00 | 0.20 / 0.40 | 2.50 / 5.00 | 10.00 / 15.00 | 09-22 (C) |
| claude-haiku-4-5 | Versatile | – | 1.00 | 0.10 | 1.25 / 2.00 | 5.00 | 06-01 (B) |
| claude-sonnet-4, claude-sonnet-4-6 | Versatile | – | 3.00 | 0.30 | 3.75 / 6.00 | 15.00 | 06-01 (B) |
| claude-opus-4-7, claude-opus-4-8 | Powerful | – | 5.00 | 0.50 | 6.25 / 10.00 | 25.00 | 06-01 (B) |
| claude-opus-4-8 speed fast | Powerful | – | 10.00 | 1.00 | 12.50 / 20.00 | 50.00 | 06-29 (K) |
| claude-opus-5 | Powerful | – | 5.00 | 0.50 | 6.25 / 10.00 | 25.00 | 07-24 (K) |
| claude-opus-5-5 | Powerful | – | 4.00 | 0.20 | 5.00 / 8.00 | 20.00 | 09-22 (C) |
| claude-sonnet-5 | Versatile | – | 2.00 | 0.20 | 2.50 / 4.00 | 10.00 | 06-30 (K) |
| claude-fable-5 | Powerful | – | 10.00 | 1.00 | 12.50 / 20.00 | 50.00 | 06-09 (K) |
| claude-fable-5-1 | Powerful | – | 10.00 | 0.25 | 12.50 / 20.00 | 50.00 | 09-01 (K) |
| gemini-3.5-flash | Lightweight | – | 1.50 | 0.15 | – | 9.00 | 06-01 (B) |
| gemini-3.6-flash / 3.7 / 3.8 (promo; `effective_to` 2027-01-01 (D)) | Versatile | – | 0.75 | 0.075 | – | 3.75 | 08-13 / 08-13 / 09-03 (K) |
| grok-4.5 / 4.6 / 4.7 | Versatile | ≤ / > 200K | 2.00 / 4.00 | 0.50 / 1.00 | – | 6.00 / 12.00 | 07-28 / 08-14 / 09-21 (K) |
| mai-code-1.1-flash | Lightweight | – | 0.20 | 0.02 | – | 1.20 | 08-11 (K) |
| kimi-k2.7-code | Versatile | – | 0.95 | 0.19 | – | 4.00 | 07-01 (K) |
| kimi-k3 | Powerful | – | 3.00 | 0.30 | – | 15.00 | 08-07 (K) |

History rows (replayed): GPT-5.6 Sol $5/$0.50/$30 from 07-09 (K; write $6.25 from 08-03 K), promo
$2.50/$0.25/$3.125/$15 from 08-20 (K), promo $2/$0.20/$2.50/$10 (long $4/$0.40/$5/$15) from 08-21 (K)
"through September 3" (D), $4/$20 from 09-04 (D); GPT-5.6 Luna $1/$6 07-09 → 07-30 (K); GPT-5.6 Terra
$2.50/$15 07-09 → 07-30 (K); Gemini 3.6 Flash $1.50/$7.50 07-21 → 08-13 (K); Opus 4.5/4.6 and Sonnet 4.5
closed 09-04 (K; retired 09-01); Sonnet 5 carried a promo footnote 06-30 → 08-11 with unchanged prices.
No GPT-5.6 write price was listed 07-30 → 08-03 (**VERIFY**: those rows ship the write bucket disabled).

### 19.3 Data sources and field names

| # | fact | source | status |
|---|---|---|---|
| 1 | AI usage report sums `quantity, gross_amount, discount_amount, net_amount` by `date, model, username`; per model `input, output, cache_read, cache_write`; ≤ 31 days; UTC; `gross − discount = net`; `discount_amount` covers included usage and other discounts (incl. Actions) | billing-reports reference | FC, LA |
| 2 | Token columns added 2026-08-11; `aic_*` zeroed from 2026-06-01 | changelogs 2026-08-11, 2026-06-11 | FC |
| 3 | GitHub parser: native report = `unit_type == "ai-credits"` and SKU ending `_ai_credit`; required base columns; BOM, `M/D/YY`; empty-username code-review rows; `Auto: <model>` labels; product classification from the model text | `github/copilot-billing-preview` `parser.ts`, `reportAdapters.test.ts`, `productClassification.ts` | FC-corr |
| 4 | Detailed report sums by `date, sku, organization, repository, cost_center_name, username, workflow_path`; summarized by `date, sku, repository, cost_center_name` (+ organization); detailed only via the web UI or the report export | billing-reports reference | LA |
| 5 | Report export API `POST/GET /enterprises/{e}/settings/billing/reports[/{id}]`, `report_type ∈ {detailed, summarized, premium_request, ai_credit}`, one export at a time | ghec.json; changelog 2026-06-04 | FC, LA |
| 6 | `ai_credit/usage` schema; `usage/summary`; `usage` (integer `quantity`; usage by cost center) | ghec.json | FC, LA |
| 7 | Metrics report endpoints, signed NDJSON links, ~2–3 UTC days lag | ghec.json; metrics docs | FC, LA |
| 8 | `ai_credits_used` "not a billed total", no model/feature breakdown (from 2026-06-19) | changelog 2026-06-19 | FC |
| 9 | users-1-day fields `loc_suggested_to_add_sum`, `loc_suggested_to_delete_sum`, `loc_added_sum`, `loc_deleted_sum`, `user_initiated_interaction_count`, `code_generation_activity_count`, `code_acceptance_activity_count`, `totals_by_cli`, `totals_by_copilot_app`, `totals_by_ide`, `totals_by_model_feature`, `distinct_*_use_count`, `used_*` flags | usage-metrics field reference and example schema | LA |
| 10 | user-teams report excludes teams with < 5 seated users | team-level-metrics | FC |
| 11 | Seats endpoints; `plan_type`; `assigning_team`; `last_activity_at` lag ≤ 24 h, nil after 90 days | ghec.json; metrics-data reference | FC, LA |
| 12 | Budgets API (`budget_product_sku(s)`, `BundlePricing`, user-states); cost centers API (`ai_credit_pool_enabled`) | ghec.json | FC-corr, LA |
| 13 | Coding-agent policy `PUT /enterprises/{e}/copilot/policies/coding_agent` | ghec.json | FC, LA |
| 14 | Agent tasks: `/agents/repos/{o}/{r}/tasks` and `/agents/tasks` ("for the authenticated user"); "GitHub App installation access tokens are not supported"; fine-grained PAT or App user token with "Agent tasks" read; `usage{type, amount}` nano-credits | ghec.json; cloud-agent API doc | FC, LA |
| 15 | Usage-records API (EMU owners, raw bodies) | ghec.json; changelog 2026-07-02 | FC |
| 16 | Workflow paths of Copilot dynamic workflows | live REST 2026-09-23; Code Quality doc | FC (billing-report paths **VERIFY**) |
| 17 | SKUs incl. `spark_ai_credits` / `spark_ai_credit` | product-and-sku-names; parser | FC-corr (**VERIFY** spelling) |
| 18 | Runtime units: 1 AIU = 1e9 nano-AIU = 1 credit; `github.copilot.cost` is a multiplier; read nano_aiu from the root `invoke_agent` span | CLI command reference; SDK `workflow.ts` | FC, LA |
| 19 | Persisted CLI events (`assistant.usage` is ephemeral) | `github/copilot-sdk` `session-events.ts` @075f027 | FC |
| 20 | `session-store.db` `assistant_usage_events` columns and input inclusivity | tokscale, codeburn (third-party) | FC-corr (experimental) |
| 21 | CLI OTel env and attributes; `service.name` "`github-copilot` (configurable via `OTEL_SERVICE_NAME`)" | CLI command reference | FC, LA |
| 22 | VS Code OTel `copilot_chat.copilot_usage_nano_aiu`; input cache-inclusive; file exporter = OTel-JS dumps; managed OTLP disables file export | `microsoft/vscode` `extensions/copilot` | FC |
| 23 | VS Code `agent-traces.db`: `spans` + `span_attributes(span_id, key, value)` (every span attribute, incl. `gen_ai.usage.cache_creation.input_tokens` and `copilot_chat.copilot_usage_nano_aiu` set by `chatMLFetcher.ts`; content when capture is on); written only with `github.copilot.chat.otel.dbSpanExporter.enabled` (default false, user settings only); path `globalStorageUri/agent-traces.db`, fallback `os.tmpdir()/copilot-agent-traces.db`; 7 days / 100 sessions | `otelSqliteStore.ts`, `otelConfig.ts`, `services.ts`, `chatMLFetcher.ts`, `package.json` | FC-corr (the correction that makes DC29 possible), LA; per-OS / Insiders directories **VERIFY** |
| 24 | SDK `ModelBillingTokenPrices`: `cacheWritePrice`, `cacheWrite1hPrice`, per-tier `maxPromptTokens`, `longContext`; session `contextTier` | `github/copilot-sdk` `rpc.ts`, `session-events.ts` @075f027 | LA |
| 25 | Managed settings keys and delivery; `model` not supported in JetBrains; repo `settings.json` `model` / `effortLevel` / `contextTier` apply only in trusted directories | managed-settings reference; CLI config-dir reference | FC-corr, LA |
| 26 | Cache invalidation: model switch, resume after expiry ("24 hours for OpenAI models and 1 hour for most others"), effort, context size, tools / MCP changes; VS Code 1h TTL gated by model, up to 20 + 2 breakpoints on the Responses path, "2x write premium" comment | optimize-ai-usage tutorial; cost-levers F8 | FC-corr; per-model TTL **VERIFY** |
| 27 | gh-aw `token-usage.jsonl` fields (§5.14), proxy log paths, AIC priced from `ai_credits_pricing_source` | `github/gh-aw` @358fbf2 (`token_usage_types.go`, `ai_credits_context.cjs`, fixture) | LA |
| 28 | Copilot activity report CSV (UI only): fields `report_time, login, last_authenticated_at, last_activity_at, last_surface_used` (IDE: editor name and version, e.g. "VS Code 1.89.1"; GitHub.com feature; "Unspecified"); refreshes every 30 min; 90-day retention; may lack consistent telemetry from JetBrains and Xcode; Spaces / Spark not fully recorded | metrics-data reference (`copilot/reference/metrics-data`) | FC (data-apis F19/I) |
| 29 | Usage dashboard (Insights → Copilot usage): 28-day rolling, lag up to 3 UTC days, **excludes Copilot CLI**, exports NDJSON; the Impact ROI section is dashboard-only | view-usage-and-adoption; copilot-metrics concept | FC (data-apis F27/F28) |
| 30 | Usage reports requested in the UI are e-mailed to the requester's default e-mail address; one report per account at a time; AI usage and detailed reports ≤ 31 days, summarized ≤ 1 year; export records kept 31 days | billing-reports reference; OAS | FC (e-mail link validity 24 h: data-apis source table, **VERIFY** in the admin guide) |
| 31 | `total_monthly_quota` column (may be `Unknown`); 3,900 on an Enterprise seat in GitHub's fixture | `github/copilot-billing-preview` `reportAdapters.test.ts`, `report-format.md` | FC-corr (column required by GitHub's parser, not documented in the billing-reports reference; current exports **VERIFY**) |
| 32 | Seats API `plan_type` ∈ {`business`, `enterprise`, `unknown`}; org billing `plan_type`; example `last_activity_editor` `vscode/1.77.3/copilot/1.86.82`; if both a Business and an Enterprise seat are assigned only the Enterprise seat is billed | ghec.json; licenses doc | FC, LA |
| 33 | Metrics `totals_by_ide` values include `vscode`, `intellij`, `visualstudio`, `eclipse`, `xcode`; minimum clients for inclusion VS Code 1.107.1 / Copilot Chat 0.35.3, JetBrains 2024.2.6 / plugin 1.5.52-241 | usage-metrics field reference; copilot-metrics concept | FC |
| 34 | JetBrains: OTel export settings under "Settings > Tools > GitHub Copilot > Chat" (changelog 2026-07-27); managed `telemetry` marked supported for JetBrains in the support table but the `telemetry` prose says "Copilot CLI and VS Code"; managed `model` not supported in JetBrains; local chat stores hold content and no token counts | changelog 2026-07-27; managed-settings reference; client-telemetry F19 + fact-check | FC-corr (attribute set **VERIFY**) |
| 35 | VS Code synthesizes one `chat` span per SDK `assistant.usage` for the in-editor CLI agent when the bridge is absent (input, output, cache read, nano-AIU; no cache creation, no response id) | `copilotcliSession.ts` | FC (client-telemetry cct-double-count) |

### 19.4 Authentication for live pulls and emitted requests

| call | who / token | classic PAT scopes (OAS) | notes |
|---|---|---|---|
| `GET /enterprises/{e}/copilot/billing/seats` | enterprise owners / billing managers | `manage_billing:copilot` or `read:enterprise` | App access undocumented (**VERIFY**) |
| `GET /orgs/{org}/copilot/billing`, `/orgs/{org}/copilot/billing/seats` | org owners; App org "GitHub Copilot Business: read" | `manage_billing:copilot` or `read:org` | |
| enterprise metrics reports | enterprise owners / billing managers; App "Enterprise Copilot metrics: read" | `manage_billing:copilot` or `read:enterprise` | |
| org metrics reports | org owners, fine-grained "View Organization Copilot Metrics" | `read:org` | |
| billing usage, usage summary, ai_credit, premium_request, reports, budgets, cost centers (GET) | enterprise admins, billing managers (usage: also org owners), custom role with enterprise-billing read; App installation token with "Enterprise billing: read" | none documented (roles only); fine-grained PATs not supported by the billing usage endpoints | changelog 2026-08-26 |
| `GET /agents/repos/{o}/{r}/tasks` | GitHub App user token or fine-grained PAT with "Agent tasks" read | — | installation tokens not supported |
| emitted: `DELETE /orgs/{org}/copilot/billing/selected_users` / `selected_teams` | org owners | `manage_billing:copilot` or `admin:org` | enterprise variants: `admin:enterprise` or `manage_billing:copilot` |
| emitted: `PUT /enterprises/{e}/copilot/policies/coding_agent` | enterprise | `admin:enterprise` or `manage_billing:copilot` | |
| emitted: budgets / cost centers POST / PATCH | enterprise admins, billing managers; App "Enterprise billing: read and write" | roles only | |

Installation tokens last one hour; GHEC installation 15,000 requests/h, PAT 5,000/h; 100 concurrent; 900
points/min per endpoint (data-apis F25, corrected). The minimal read-only credential per data source for the
admin handoff, and the no-token alternative for each, are in §22.3 (revision 3).

### 19.5 Open facts (VERIFY; owner and fallback)

Release gate §21.6 (real redacted AI usage report with token columns + usage summary + detailed report with
seat lines) settles items 1–6, 10; the handoff release gate (§21.6 (5)) settles items 30–35 for the adopting
enterprise; the VS Code release gate (§21.6 (2)) settles 36–37.

| # | open fact | fallback until verified | owner |
|---|---|---|---|
| 1 | AI usage report columns in current exports; whether `sku`, `organization`, `repository`, `cost_center_name` are grouping keys (documented grouping is `date, model, username`); whether cloud agent / code review appear as SKUs or only as model labels | map by header; natural keys include every present column; SKU and pseudo both drive workloads; L3 per product | CP-BILL, CP-RECON |
| 2 | Whether report `input` includes `cache_read` / `cache_write` | decided per file by L1 with the power rule; else undecidable | CP-RECON |
| 3 | How the Auto 10% appears (gross, discount, label) and whether nano-AIU includes it | L0/L1 residual `copilot_auto_discount`; DC22 classification | CP-RECON |
| 4 | Stacking/rounding of Auto ×0.9 with compliance ×1.1 | multiplicative, `stacking: assumed` | CP-RATES |
| 5 | Long-context basis: request input (A) vs session context tier (B); total vs uncached input; 272K = 272,000 | A priced; A/B ranges when the tier is known; L1 residual | CP-RATES, CP-RECON |
| 6 | Rounding of fractional credits | residual `copilot_rounding` | CP-RECON |
| 7 | Whether direct-org usage draws on the pool first | read from each row's discount (`direct_draws_pool`), else unknown | F-POOL |
| 8 | Model label formats; code-review / cloud-agent labels | normalized; substring pseudo rules | CP-BILL |
| 9 | `spark_ai_credits` vs `spark_ai_credit` | both accepted | CP-BILL |
| 10 | Included-credit proration; seat SKU unit and volume/EA seat pricing | month seat-months; residuals `copilot_seat_proration`, `copilot_seat_contract` | F-POOL, CP-RECON |
| 11 | Which block/continue choice each capped cost center uses | `--capped-policy`; unknown → range | F-POOL |
| 12 | `session-store.db` schema at a released CLI | experimental flag `copilot-store` | CP-LOCAL |
| 13 | CLI OTel file envelope; CLI input inclusivity; pre-1.0.64 names; JetBrains / Copilot app service names; the JetBrains OTel attribute set; whether managed `telemetry` applies to JetBrains (docs contradict themselves) | experimental flags `copilot-cli-otel-file`, `copilot-jetbrains-otel`; configurable service names (`NAME=copilot_jetbrains`); JetBrains via org data | CP-OTEL |
| 14 | Per-model cache TTL on GitHub's proxy; 1h write price (SDK `cacheWrite1hPrice`); refresh-on-read and TTL start | τ from runtime hints; writes ranges [published, 2 × input]; conservative ttl-expiry rule (CA-32) | CP-RATES, F-SEM-C |
| 15 | Writes on models without a published write price; GPT-5.6 writes 07-30 → 08-03 | folded to input (ESTIMATED); window's write bucket disabled | CP-RATES |
| 16 | Billing `workflow_path` for cloud agent, third-party agents and agentic workflows (`.lock.yml`); cloud agent on sandboxes after 2026-09-28 | dynamic paths + `.lock.yml` rule; sandbox lines shown apart | CP-BILL |
| 17 | Contract / volume / EA discounts (in `discount_amount` or invoice only); Azure meter names | `discount_other` / unclassified; `unexplained` residual | CP-RECON |
| 18 | Enterprise BYOK credit consumption; MCP sampling billing | BYOK excluded; sampling billed as chat | CP-LOCAL |
| 19 | Utility calls carry nano-AIU 0 in client data | unbilled only when nano-AIU is 0 | CP-LOCAL |
| 20 | FOCUS allowed values for seat purchase rows and `PricingUnit` | fallback `Usage` with a note | CP-OUT |
| 21 | App permission for the enterprise seats list; signed-URL expiry; 204/404 for unprocessed days | classic PAT documented; download immediately; "not ready" marker | CP-PULL |
| 22 | `last_activity_editor` and activity-report `last_surface_used` strings for JetBrains IDEs → editor families | patterns in `core.records.editor_family` (JetBrains `verified: false`); unknown → `other` | F-CORE-C, CP-ORGDATA, CP-HANDOFF |
| 23 | `sessions.host_type` / `producer` values | `copilot_cli` | CP-LOCAL |
| 24 | Rate `effective_from` for K-dated rows (commit ≠ billing date) | commit date; `copilot_rate_boundary` residual ±2 days; synthetic data avoids boundaries | CP-RATES, CP-RECON |
| 25 | What happens to existing seats when an org switches from `assign_all` to `assign_selected`; whether removing team membership frees a team-assigned seat at cycle end | not projected for team seats; `seat_policy` projection `needs_eval` | CP-DET-SEATS |
| 26 | gh-aw artifact name and whether agentic-workflow Actions rows carry the `.lock.yml` path | documented download recipe; `.lock.yml` rule | CP-OTEL, CP-BILL |
| 27 | Whether volume-licensed seats' included credits can be assigned to cost centers | no cost-center advice for volume / azure / unknown | CP-POLICY |
| 28 | Enterprise-only features that make an Enterprise seat necessary | `plan-mix` is a trade-off with `needs_eval` | CP-DET-SEATS |
| 29 | Whether the included-credit pool of volume-licensed or Azure-billed entities also resets on the calendar month (the pool doc says the 1st) and how their overage is invoiced | calendar-month pool months; seat savings only at renewal; overage labelled LIST until a real invoice reconciles | F-POOL, CP-RECON |
| 30 | Whether a GitHub App with "Enterprise billing: read" (and a billing-manager PAT) may call the report-export `POST …/settings/billing/reports` (data-apis F25 says read suffices; the App permissions page marks the reports endpoints as needing additional permissions) | the admin guide lists both; a 403 on a unit stops that unit with the endpoint and the suggestion to use the UI download (§22.4) | CP-PULL, CP-HANDOFF |
| 31 | Whether the activity report lists every seat holder (incl. never-active seats) or only users with activity; exact `last_surface_used` strings for JetBrains | seat counts from it carry "seat holders as listed"; JetBrains patterns unverified | CP-HANDOFF |
| 32 | Whether an enterprise billing manager (not an org owner) can read `GET /orgs/{org}/copilot/billing` and the org seats list | the guide asks for an org owner for these two calls, or the enterprise seats list instead | CP-HANDOFF |
| 33 | Exact UI click paths and labels for the AI usage report, detailed usage report, activity report and dashboard export; validity of the e-mailed download link (24 h per the data-apis table) | the admin guide gives the documented page names and marks the paths VERIFY; the admin confirms on first use | CP-HANDOFF, INTEGRATION |
| 34 | Whether the dashboard NDJSON export has the same record shape as the API `users-*` / `*-28-day` NDJSON | experimental flag `copilot-dashboard-ndjson`; non-matching records quarantined | CP-ORGDATA |
| 35 | SKU `copilot_standalone` → plan (Business assumed); whether current AI usage reports still carry `total_monthly_quota` and whether it is the user's seat allowance | standalone → business with a VERIFY note; quota evidence only behind `copilot-ai-usage-quota` | F-CORE-C, CP-BILL, F-POOL |
| 36 | VS Code `agent-traces.db` directory per OS and for VS Code Insiders; whether a managed OTLP policy also disables the SQLite exporter | facts `vscode_paths` (`verified: false`) + `--vscode-traces PATH`; absent file → dq | CP-VSCODE |
| 37 | Whether the in-editor CLI agent's `gen_ai.conversation.id` equals its `~/.copilot` session id, and whether the JetBrains CLI agent writes `~/.copilot` | dedupe by session directory when present, else keep with `dq.copilot_synthesized_span_kept` | CP-VSCODE, CP-LOCAL |

---

## 20. Deferred (Copilot)

| item | decision | reason |
|---|---|---|
| ~~VS Code `agent-traces.db` beyond the self-view~~ | **no longer deferred (revision 3)** | the fact-check showed `span_attributes` carries cache-creation tokens and nano-AIU; supported fleet / volunteer source with a collector (DC29, §5.13, §5.18) |
| VS Code `chatSessions/*.jsonl`, Agent Debug Logs (incl. an experimental self-view reader limited to `copilotCredits`, `sessionCopilotCredits`, `modelTotals`) | v0.3 | content-heavy internal formats whose lines interleave content with the numbers (a key allowlist cannot avoid reading the content bytes); `agent-traces.db` carries the same per-request numbers through a key allowlist |
| JetBrains / Visual Studio / Xcode / Eclipse local data | never (org data only) | no per-request usage on disk; JetBrains stores hold content only |
| JetBrains OTel as a supported (non-experimental) source | after a real file is a fixture | attribute set and `service.name` undocumented; managed-telemetry support contradictory in GitHub's docs (DC30) |
| Per-person joins between a handoff bundle (export key) and collector data (analyst's org key) | v0.3 (two-stage re-keying of bundle `c_` values) | would require widening `CostLine` / `LicenseSnapshot` / `ActivityDay` to `c_` and passing the org key to the record store; team-level joins suffice for team outputs (R-E21) |
| Encrypted bundles | v0.3 | stdlib `zipfile` cannot write encrypted archives; the bundle holds pseudonyms and team aggregates only, and the admin guide prescribes the org's approved transfer channel |
| Minting GitHub App installation tokens | never | stdlib has no RS256; the admin's own tooling writes the token file (§22.3) |
| Scheduled / unattended handoff (the admin's machine pushing bundles) | v0.3 | v0.2 is a manual, monthly, inspected handoff; `copilot pull --out *.tbx` can be scheduled by the admin's own scheduler |
| Critic 0.16 residue | resolved, not deferred | `ccusage.py` moves to CLI-LEDGER (§21.5 binding); TRACE derives key sets via `core.records.record_fields` (CA-41) |
| GitHub Copilot app `data.db` | v0.3 | undocumented |
| Copilot SDK embedded recorder | v0.3 | usage events are live-only |
| EMU usage-records ingestion | never in `none` tier | raw bodies |
| Enterprise BYOK arbitrage | v0.3 | billing undocumented |
| Cross-vendor model remaps in projections | v0.3 | no verified token-count band across vendors |
| Review-trigger multiplier (reviews per push) | v0.3 | needs PR event history |
| Audit-log adapter (seat / budget / policy change dates) | v0.3 | dates can be supplied manually |
| Azure-billed Copilot invoice | v0.3 | Azure meter names unknown |
| Volume / EA contract seat pricing model | v0.3 | terms are per contract; seats labelled ESTIMATED meanwhile |
| Individual-plan optimizer | v0.3 | enterprise focus |
| Code Quality and sandbox optimization | info lines only | small, separate meters |
| Copilot app automations detector | v0.3 | no content-free per-automation source |
| gh-aw `gh aw logs` / `gh aw audit` JSON ingestion and third-party agentic engines | v0.3 | `token-usage.jsonl` suffices; other engines bill through their providers |
| Live per-model prices from the SDK `models.list` | v0.3 | experimental API |

---

## 21. Packages, waves, ownership, amendments, gates

### 21.1 Wave 1.5 packages (core; frozen at gate F')

Revision 3.1: rows, sizes and ownership are the briefs' (`copilot/briefs/`); the item lists are
`CORE-AMENDMENTS.md` §2–§4.

| id | wave | title | depends_on | LOC | ownership check |
|---|---|---|---|---|---|
| F-CORE-C | 1.5a | Copilot core contracts: records (+ the C-9 vocabularies), types (+ `PlanEvidence`, scenario fields, `ReconciliationReport.decisions`, `AnalysisContext.recon_decisions`), money / labels / jsonl / ids / models, registry, protocols (+ `LedgerStats`), facts, builders, ownership (C-1 … C-33, T-1 … T-3) | F-CORE, gate F | 2,300 | `--package F-CORE` |
| F-SEM-C | 1.5b | product families, R-E20 figure bases, fix substitution, cache-rules row (`ttl_semantics_known`), transitions, convention resilience (S-1 … S-5) | F-CORE-C | 600 | `--package F-SEM` |
| F-KIT-C | 1.5b | separate Copilot catalog tables (levers, grids, admin actions, exclusions, count sources, fixes, promotions), kanon (scope counter, R-E16, headroom merge, R-E10 `publish()` fix), FakePricer / MemoryStore (R-E21 adoption) / MemoryRecordStore, gate F' (K-1 … K-6) | F-CORE-C | 1,900 | `--package F-KIT` |
| F-EXT | 1.5b | `core/extensions.py` host (E-1) | F-CORE-C | 850 | `--package F-EXT` |
| F-POOL | 1.5b | `core/pool.py` incl. plan detection and scenarios (P-1) | F-CORE-C | 2,100 | `--package F-POOL` |

### 21.2 Wave 2 Copilot packages (depend only on core; cross-package checks are gate tests)

Release-blocking (org-data path, DC28): CP-RATES, CP-BILL, CP-ORGDATA, CP-HANDOFF, CP-STORE, CP-RECON,
CP-DET-SEATS, CP-DET-USAGE, CP-PLAN, CP-POLICY, CP-OUT, CP-WIRE, CP-SYNTH, CP-SYNTH-W, and CP-PULL (token
path of the handoff). Per-developer packages, release-relevant but not blocking the org-data release:
CP-VSCODE and CP-OTEL (VS Code, priority 1; CP-OTEL also reads the VS Code database), CP-DET-LANES, CP-LOCAL
(CLI; lowest).

| id | title | LOC |
|---|---|---|
| CP-RATES | Copilot rate data, YAML verifier, golden cases | 1,500 |
| CP-BILL | AI usage report (+ experimental quota evidence as `plan_quota` rows), metered usage CSV, billing REST pages | 2,500 |
| CP-ORGDATA | config, metrics (+ IDE keys, dashboard NDJSON export), seats, agent tasks, usage-records refusal, team maps (`teammap.py`) | 2,850 |
| CP-PULL | live pulls (auth, units, resume, export polling, agent-task enumeration) + private work directory for `--out *.tbx` | 2,000 |
| CP-HANDOFF | admin handoff kit: bundle writer / reader / inspector, leak gate, team k-merge, aggregate-only mode, `pseudonym_of`, `exclude_logins`, `copilot-export` and activity-report adapters, admin answers, packaged admin guide + answers template (§22) | 2,500 |
| CP-VSCODE | VS Code collector: incremental, retention-aware `agent-traces.db` extract and OTel outfile tail, CLI-agent dedupe, developer opt-in snippet | 1,700 |
| CP-LOCAL | CLI events adapter, experimental store reader, conventions, collector (non-blocking) | 2,850 |
| CP-OTEL | the Copilot span mapping: VS Code `agent-traces.db` (raw and CP-VSCODE extracts), OTel files (VS Code, CLI; JetBrains experimental), gh-aw token usage | 2,800 |
| CP-STORE | Copilot record store (key ids per R-E21), context enricher (plans, pools per scenario, outcomes, decided conventions) | 1,900 |
| CP-RECON | Copilot reconciler (+ decisions, plan-fit diagnostic), verification panels | 2,850 |
| CP-DET-SEATS | `copilot.seats-budgets` (+ `plan-status`, scenarios, activity-report and aggregate-only seats, per-kind gates) | 2,400 |
| CP-DET-USAGE | `copilot.org-scan` (+ editor reach, outcomes, per-kind gates, scenarios) | 2,850 |
| CP-DET-LANES | `copilot.lanes` + generic-detector-on-Copilot gates | 1,450 |
| CP-PLAN | aggregate plan (cells, reach, Shapley, pool conversion, per scenario) | 2,000 |
| CP-POLICY | policy pack, admin actions, REST files, budget design, VS Code opt-in note, JetBrains checklist | 2,300 |
| CP-OUT | summary assembly, section renderer (plan block, scenario columns), showback (+ editor split), FOCUS rows | 2,900 |
| CP-WIRE | `tokenbill copilot …` command group (+ admin-guide, export, inspect, pseudonym, plan, `scan --from`, `collect --source`) and pipeline | 2,850 |
| CP-SYNTH | synthetic Copilot world and closed-form truth (+ vscode / jetbrains teams, plan and handoff variants) | 2,600 |
| CP-SYNTH-W | schema-true writers for every Copilot source (+ activity report, dashboard NDJSON, answers, VS Code DB and outfile, UI path) | 2,200 |

Totals: wave 1.5 ≈ 7.8k LOC, Copilot wave 2 ≈ 45.0k LOC (code + tests, 19 packages). Every package ≤ 3k.
Least headroom: CP-OUT (2,900); CP-ORGDATA, CP-LOCAL, CP-RECON, CP-DET-USAGE, CP-WIRE (2,850); CP-OTEL (2,800).
A brief that would exceed 3,000 splits by orchestrator decision, never by growing past 3k — candidates: CP-OUT
→ FOCUS rows (`copilot/focus.py`); CP-RECON → panels (`copilot/panel.py`); CP-DET-USAGE → review / agent kinds;
CP-ORGDATA → `teammap.py` + agent tasks; CP-LOCAL → the experimental session-store reader; CP-WIRE → the
developer commands (`collect`, `me`); CP-OTEL → the gh-aw adapter.

### 21.3 File ownership (no overlap with SPEC Appendix O; mirrored into `OWNERSHIP.toml` by F-CORE-C)

Revision 3.1: equal to the **Owns** sections of the briefs; checked with `scripts/check_ownership.py`'s own
matcher against `OWNERSHIP.toml` at 99ba098 (every new pattern has exactly one owner; every tracked file still
has exactly one).

| package | owns |
|---|---|
| F-CORE-C | edits within F-CORE's files (`tokenbill/core/{records,types,money,labels,jsonl,ids,models,registry,protocols,facts,builders}.py`, `tokenbill/core/facts.json`, `OWNERSHIP.toml`, `pyproject.toml`) + new `tokenbill/copilot/__init__.py` (owned by F-CORE afterwards) + `tests/v2/core/test_copilot_*.py` + the T-1 … T-3 edits of `tests/v2/core/test_{registry,types,records}.py` |
| F-SEM-C | edits within `tokenbill/core/{findings,cache_rules,transitions,conventions}.py`, `tests/v2/sem/test_copilot_*.py` |
| F-KIT-C | edits within `tokenbill/core/{catalog,testing,kanon}.py`, `tests/v2/kit/test_copilot_*.py`, `tests/v2/gates/test_gateF_copilot_smoke.py` |
| F-EXT | `tokenbill/core/extensions.py`, `tests/v2/ext/**` |
| F-POOL | `tokenbill/core/pool.py`, `tests/v2/pool/**` |
| CP-RATES | `tokenbill/copilot/data/__init__.py`, `tokenbill/copilot/data/github_copilot.json`, `tokenbill/copilot/data/snapshots/**`, `tokenbill/copilot/rates_verify.py`, `tests/v2/copilot_rates/**`, `tests/v2/fixtures/copilot_rates/**` |
| CP-BILL | `tokenbill/adapters/github_billing.py`, `tests/v2/copilot_bill/**`, `tests/v2/fixtures/copilot_bill/**` |
| CP-ORGDATA | `tokenbill/adapters/{github_config,github_metrics,github_seats,github_agent_tasks,github_usage_records}.py`, `tokenbill/copilot/teammap.py`, `tests/v2/copilot_orgdata/**`, `tests/v2/fixtures/copilot_orgdata/**` |
| CP-HANDOFF | `tokenbill/copilot/{handoff,admin_answers}.py`, `tokenbill/copilot/handoff_data/**` (`__init__.py`, `admin_guide.md`, `admin_answers.template.json`), `tokenbill/adapters/{copilot_export,github_activity_report}.py`, `tests/v2/copilot_handoff/**`, `tests/v2/fixtures/copilot_handoff/**` |
| CP-PULL | `tokenbill/copilot/{pull_common,pull_billing,pull_metrics}.py`, `tests/v2/copilot_pull/**` |
| CP-LOCAL | `tokenbill/adapters/{copilot_cli,copilot_collect,copilot_conventions}.py`, `tests/v2/copilot_local/**`, `tests/v2/fixtures/copilot_local/**` |
| CP-OTEL | `tokenbill/adapters/{copilot_otel,copilot_vscode,gh_aw}.py`, `tests/v2/copilot_otel/**`, `tests/v2/fixtures/copilot_otel/**` |
| CP-VSCODE | `tokenbill/adapters/copilot_vscode_collect.py`, `tests/v2/copilot_vscode/**`, `tests/v2/fixtures/copilot_vscode/**` |
| CP-STORE | `tokenbill/copilot/{record_store,enrich}.py`, `tests/v2/copilot_store/**` |
| CP-RECON | `tokenbill/copilot/{recon,panel}.py`, `tests/v2/copilot_recon/**`, `tests/v2/fixtures/copilot_recon/**` |
| CP-DET-SEATS | `tokenbill/detect/copilot_seats.py`, `tests/v2/copilot_det_seats/**` |
| CP-DET-USAGE | `tokenbill/detect/copilot_org.py`, `tests/v2/copilot_det_usage/**` |
| CP-DET-LANES | `tokenbill/detect/copilot_lanes.py`, `tests/v2/copilot_det_lanes/**` |
| CP-PLAN | `tokenbill/copilot/plan.py`, `tests/v2/copilot_plan/**` |
| CP-POLICY | `tokenbill/copilot/{policy,admin_actions,budgets}.py`, `tests/v2/copilot_policy/**` |
| CP-OUT | `tokenbill/copilot/{summary,render,showback,focus}.py`, `tests/v2/copilot_out/**` |
| CP-WIRE | `tokenbill/commands/copilot.py`, `tokenbill/pipeline/copilot.py`, `tests/v2/copilot_wire/**` |
| CP-SYNTH | `tokenbill/synth/{copilot_world,copilot_truth}.py`, `tests/v2/copilot_synth/**` |
| CP-SYNTH-W | `tokenbill/synth/copilot_writers.py`, `tests/v2/copilot_writers/**` |
| INTEGRATION (amendment) | `docs/COPILOT.md`, `docs/COPILOT-ADMIN.md` (generated from CP-HANDOFF's packaged `admin_guide.md`; `tests/v2/e2e/test_copilot_admin_doc.py` asserts equality), README Copilot section, `tests/v2/e2e/test_copilot_flagship.py`, the weekly Copilot YAML check workflow (within its existing `docs/**`, `README.md`, `tests/v2/e2e/**`, `.github/**`) |
| CLI-LEDGER (moved file, §21.5) | `tokenbill/outputs/ccusage.py` moves from OUT to CLI-LEDGER; its tests move into CLI-LEDGER's existing `tests/v2/cli_ledger/**` (`test_ccusage*.py`) |

### 21.4 Behavioral amendments to SPEC packages (brief text for packages not yet started; §21.4a for started ones)

Started on the gate-F core under R-E15 (worktrees at 99ba098): ADMIN, BLOCK, CC, DETECT-CACHE, REPLAY,
SYNTH-FLEET, SYNTH-ORACLE, TELEM, VERIFY. Not started: RATES, TRACE, STORE, RECON, DETECT-OTHER, PLAN, OUT,
WIRING (wave 2) and CLI-LEDGER, CLI-SAVINGS (wave 3) — their rows below are pasted into their briefs by the
orchestrator before they start. Rows for started packages are filed as late change requests (§21.4a).

| package | amendment | LOC | brief total |
|---|---|---|---|
| RATES | load `core.extensions.extension_rate_files()` beside its own builtin files (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses (§6.2 #4) using `ctx.context_tier`; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool`, no contract overlay; facts parity over every Copilot row; `pricing verify` runs `core.extensions.rate_verifiers()` | +120 | 2,720 |
| STORE | §7.1 DDL, latest-fetch-wins for cost lines and aggregates, `pool_nano` rollups, `count_users(source="cost_lines")`, `source_stats`, `sources_mask` bits (incl. 4096), retention of `cost_lines.principal`; revision 3.1: key-id adoption per R-E21 (`SqliteStore(…, adopt_key_ids: bool = False)`, adopts only the first `copilot-export` bundle's key ids, with or without an own org key; `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode`, audit row; a second bundle key id → `UsageError`; CORE-AMENDMENTS A-2) | +150 +40 | 2,990 |
| TRACE | closed key sets derived with `core.records.record_fields` (CA-41) and `RAW_USAGE_ENUMS` instead of hand lists: cost_line CA-4 fields, outcome `extra`, pricing `routing` / `compliance` / `context_tier`, CA-7 event attrs | ≤ 0 (hand lists deleted) | ≤ 3,000 (§21.5) |
| TELEM (started; late change request, §21.4a) | `otlp` skips resources for which `core.models.is_copilot_resource` is true, sets `stats["defer:copilot-otel"]` | +40 | 2,640 |
| RECON | `reconcile()` skips `core.extensions.delegated_channels()`; new `recon/reconcile.py: merge_reports(reports) -> ReconciliationReport` (disjoint channels; SPEC §12.4 verdict, coverage and finality over the union; the only merge implementation; also unions `ReconciliationReport.decisions`, a key with two values → `ContractViolation`) | +90 | 2,790 |
| REPLAY (started; late change request, §21.4a) | accept billing class `pool` (treated like `allowance`) | +10 | 2,810 |
| DETECT-CACHE (started), DETECT-OTHER | none (cohorts, exclusions, Copilot fixes, the title prefix and the R-E20 bases are handled in core, CA-21/CA-31/CA-36/CA-45; the `github_copilot` cache row keeps `CacheRules` types, CA-32) | 0 | unchanged |
| PLAN | billing class `pool` → `pool_headroom_monthly` / LIST_EQUIVALENT lever results; skip replay `aggregate`; priors from `core.catalog.RR_PRIORS` | +40 | 2,540 |
| OUT | render `core.extensions.render_sections`; `write_focus(..., extra_rows=(), owned_channels=frozenset())` drops ledger rows of owned channels and accepts row `x_` columns; `money_json` delegates to `core.labels.figure_json` (−20); BILL shows `PricedTotal.pool`; `ccusage.py` moves out (§21.5, −150) | +60 − 20 − 150 | 2,890 |
| WIRING | `bill_summary` fills `PricedTotal.pool` and restricts `allowance` to subscription lines; `ingest_paths` persists `licenses/activity/config` via `core.extensions.persist(...)` **after** the ledger ingest (so an adopted key id exists before the record stores check it) and re-reads deferred files (`stats["defer:<adapter>"]`); `open_store(..., adopt_key_ids=False)` passes adoption through | +90 | 1,390 |
| CLI-LEDGER (wave 3) | `cli.main` calls `core.extensions.rewrite_argv`; `COMMANDS` merges `command_modules()`; `reconcile` runs `run_reconcilers` + RECON `merge_reports` with `rounding_remainders` from `source_stats` and prints the merged decisions (`recon_decisions_of`; not persisted, revision 3.1); `export focus` passes extension rows and owned channels; `showback`, `pricing verify`, `purge` call their host functions (`purge --principal p_…` also removes adopted-key rows); receives `ccusage.py` (§21.5, +150) | +45 +150 | 2,895 |
| CLI-SAVINGS (wave 3) | `run_findings` uses `aggregates_only=False` per shard and `True` once; `rescope_findings(count_users=core.extensions.count_users_fn(...))`; `findings` / `report` first run the reconcilers over the window (decisions are not persisted, revision 3.1) and call `enrich(…, recon_decisions=recon_decisions_of(reports))` and `summarize`; `policy --target` dispatches `policy_targets()`; `measure --panel copilot` via `panel` | +65 | 2,965 |
| VERIFY, CC, ADMIN, BLOCK, SYNTH-ORACLE, SYNTH-FLEET (started) | none; rebase onto the gate-F' core before merging (R-E23) | 0 | unchanged |
| INTEGRATION | `docs/COPILOT.md` (fleet recipe: managed OTel with a collector routing Copilot resources, VS Code opt-in collection, MDM `copilot collect --source vscode\|cli`, CI post-step, gh-aw artifact download, auth table, privacy), revision 3: `docs/COPILOT-ADMIN.md` generated from the packaged admin guide with an equality test, release gates §21.6, flagship §18, weekly YAML check | docs/tests | — |

### 21.4a Late change requests for packages started on the gate-F core (R-E15, R-E23)

Filed by the orchestrator as `tests/v2/kit/CONTRACT-CHANGE-COPILOT-TELEM.md` and `…-REPLAY.md` (the same
mechanism as the merged `tests/v2/kit/CONTRACT-CHANGE-KIT-*.md`; ruling R-E23) and copied into the package's
worktree, after gate F' merges into `v0.2`:

| id | package | change | depends on | acceptance |
|---|---|---|---|---|
| CC-COPILOT-1 | TELEM | rebase on gate F'; `otlp` skips resources with `core.models.is_copilot_resource(...)` true and sets `stats["defer:copilot-otel"] = n` | CA-20 (`is_copilot_resource` exists only in the gate-F' core) | TELEM tests green; the §21.6 gate-1 mixed-file test (with CP-OTEL) green |
| CC-COPILOT-2 | REPLAY | rebase on gate F'; billing class `pool` treated like `allowance` (LIST_EQUIVALENT results; mixed classes still raise) | CA-1 (`BILLING_CLASSES`) | REPLAY tests green; a `copilot_pool` lane replays to LIST_EQUIVALENT figures |
| CC-COPILOT-3 | ADMIN, BLOCK, CC, DETECT-CACHE, SYNTH-FLEET, SYNTH-ORACLE, VERIFY | rebase on gate F' only; no behaviour change | — | their own tests green on the gate-F' core |

If TELEM or REPLAY has already merged when gate F' lands, the change request is executed by the contract owner
as a follow-up commit on `v0.2` within the package's owned files under a one-time grant recorded in
`tests/v2/kit/RULINGS.md`.

### 21.5 Size resolution for OUT and TRACE (binding in revision 3; resolves critic finding 0.16)

OUT (3,000) and TRACE (3,000) are at the limit. **Decided:** `tokenbill/outputs/ccusage.py` and its tests
(≈ 150 LOC, SPEC §14.5) move from OUT to CLI-LEDGER (the only caller, `export --format ccusage`), giving OUT
2,890 and CLI-LEDGER 2,890; the OWNERSHIP.toml / Appendix O rows move with it (CA-30). TRACE derives its closed
key sets with `core.records.record_fields` (CA-41) and deletes its hand-written lists, so its increment is ≤ 0
and its brief stays ≤ 3,000; the revision-2 allowance of 3,015 is withdrawn.

### 21.6 Gates

**Gate F' (end of wave 1.5; owner F-KIT-C, `tests/v2/gates/test_gateF_copilot_smoke.py`):** exactly
`CORE-AMENDMENTS.md` §4 (revision 3.1: the binding list). In short: builders → Copilot cost lines, aggregates,
licenses (one activity-report license with plan `unknown`) and a Copilot lane → `MemoryStore` +
`MemoryRecordStore` → `core.pool.detect_plans` / `build_cells` / `pool_months` (C.P9 month; C.P13 scenario
pair) → `FakePricer` prices a `copilot_pool` inference into `PricedTotal.pool` (C.G1) →
`run_detectors(aggregates_only=False)` then `(True)` (family filter, `FAMILY_EXCLUSIONS` drop, aggregate
detector once, silent without `ext:copilot`) → `rescope_findings` with `count_users_fn` (R-E16; a
`plan_scenario` dim survives) → `build_finding` with R-E20 bases and `headroom` → `core.extensions` with the
fake extension (`rewrite_argv`, `render_sections`, `focus_rows` owned channels, `run_reconcilers`,
`recon_decisions_of`, missing-module dq) → an org-keyed `MemoryStore(adopt_key_ids=True)` adopting a
`copilot-export` batch's key id while nulling another adapter's foreign key id (R-E21); `publish()` keeps a
`users_unknown` row (R-E10) → a `RunResult` with `copilot` set. Also: every `AGGREGATE_GRIDS` entry parses
and round-trips; every Copilot lever's `patch_keys` resolves; every `fix_for` entry targets
`github-copilot`; `sniff_adapter` with every Copilot adapter module absent raises nothing and records
`dq.adapter_unavailable`; every wave-0/1 test green on Python 3.10 and 3.13 **with only the T-1 … T-3 edits**
of CORE-AMENDMENTS §7; `core/*` frozen again.

**Gate 1 additions** (`@pytest.mark.gate`, `importorskip`): CP-RATES real `RateCard` goldens; CP-STORE
Copilot fixtures through the real adapters, `SqliteStore` and `CopilotRecordStore` (idempotent re-ingest,
overlap exports); CP-OTEL mixed Claude + Copilot OTLP file through TELEM `otlp` + WIRING deferral (both
span sets present, none duplicated); CP-RECON fixtures + real `RateCard` (synthetic verdicts, both
conventions blind); CP-DET-* plants on the CP-SYNTH world with real adapters; CP-PLAN / CP-POLICY on the
same world; CP-OUT through OUT's renderers and `write_focus` on a real `SqliteStore` (no double counting);
CP-SYNTH-W files through every real adapter; CP-WIRE `copilot scan` end to end. Revision 3: CP-HANDOFF
`export_bundle` on the CP-SYNTH-W token-path and UI-path files → two bundles with byte-identical
`cost_lines` / `aggregates` members, leak scan clean, `copilot scan --from` through a real `SqliteStore` with
adoption reproduces the org-data plants (§18 (d)); CP-PULL `--out *.tbx` with an injected opener leaves no
raw directory behind (also on an injected failure); CP-VSCODE extracts read by CP-OTEL's adapters equal direct
ingest of the source database (revision 3.1); an org-keyed store holding a bundle and CP-VSCODE extracts keeps
both key spaces and joins them by team only (R-E21); the plan-unknown variant yields both scenarios end to end.

**Gate 2 additions:** `scan --copilot`, `me --copilot`, `collect copilot-cli`, `collect copilot-vscode`
aliases; `copilot` verbs in `COMMANDS` (incl. `admin-guide`, `export`, `inspect`, `pseudonym`, `plan`); `purge
--principal` removes record-store rows (also for adopted key ids, using the `p_` printed by `copilot pseudonym`); `export --format focus` with Copilot data
has no ledger rows on Copilot channels; `export --format ccusage` works from CLI-LEDGER (§21.5).

**Copilot release gates (INTEGRATION, before 0.2.0 GA):** (1) a real, redacted AI usage report with token
columns, the matching usage summary and a detailed usage report with seat lines are `real-redacted`
fixtures (clears `dq.recon_schema_unverified`; settles §19.5 #1–#6, #10); (2) **revision 3:** a real,
redacted VS Code `agent-traces.db` read through the allowlist is a `real-redacted` fixture (settles §19.5
#36–#37 for the adopting enterprise's VS Code version), else the VS Code source ships labelled
"schema from source code" in the release notes; the CLI evidence (a real `session-store.db` from a released
CLI and a real CLI OTel file) is **optional** — without it `copilot-store` and `copilot-cli-otel-file` stay
experimental and the release notes say so; JetBrains OTel stays behind `copilot-jetbrains-otel` unless a
real file is a fixture; (3) every `facts.copilot` row is re-verified `primary` or shipped disabled (R-E19);
(4) the weekly Copilot YAML check job is live; (5) **revision 3 (handoff):** one real bundle produced by the
adopting enterprise's admin with `copilot export` (or real redacted UI files — AI usage report, activity report,
answers file — exported in the test) passes the leak scan and `copilot inspect`, and `copilot scan --from` on
it runs keyless and networkless; the admin confirms or corrects the VERIFY items of the admin guide (§19.5
#30–#35) and `docs/COPILOT-ADMIN.md` is regenerated from the corrected guide.

---

## 22. Admin handoff kit (revision 3; CP-HANDOFF, with CP-PULL, CP-WIRE, INTEGRATION)

Owner answer 1: the analyst (product owner) has no GitHub admin access; a colleague with enterprise / org
owner or billing-manager rights does the setup. This section is the contract for everything that colleague
needs and everything that crosses from their machine to the analyst's. Principles: the admin runs **one
offline command** that turns GitHub files into a **content-free, pseudonymized, k-safe bundle**; the analyst
needs **no GitHub rights, no token and no network**; every permission asked for is **read-only and minimal**,
and each one is stated with its evidence (unverified ones are marked **VERIFY** and have a fallback); the
admin can always choose the **no-token path**.

### 22.1 Roles and monthly flow

```
 ADMIN (enterprise owner / org owner / billing manager)            ANALYST (product owner, no GitHub rights)
 1. tokenbill copilot admin-guide            (reads §22.2)
 2a. token path:  tokenbill copilot pull --live … --out export-2026-09.tbx
 2b. no-token path: download UI reports (§22.4), fill admin_answers.json,
     tokenbill copilot export --in downloads/ --answers admin_answers.json
                              [--team-map-csv teams.csv] --out export-2026-09.tbx
 3. tokenbill copilot inspect export-2026-09.tbx   (counts only; leak scan result)
 4. hand over the .tbx through the org's approved channel ───────────►  5. tokenbill copilot inspect export-2026-09.tbx
    delete raw downloads; keep the export key file                        6. tokenbill copilot scan --from export-2026-08.tbx
                                                                              --from export-2026-09.tbx [--db copilot.db]
                                                                              -o report.html --policy-out pack/
 8. apply the checklist / request files by hand  ◄──────────────────────  7. send pack/ (admin checklist, REST request files,
    (nothing is executed by Token Bill)                                       managed-settings patch) to the admin
```

Runs once per month (after the 3rd, so the previous month's report rows are final; `report_lag_days` 3) and
on demand. The same export key file is reused every month so pseudonyms join across months (DC31).

### 22.2 Admin guide (packaged `tokenbill/copilot/handoff_data/admin_guide.md`; `tokenbill copilot admin-guide`; INTEGRATION generates `docs/COPILOT-ADMIN.md` from it)

Plain language, ≤ 6 printed pages, rendered with the permission table of §22.3 from `facts.copilot.handoff`
(so VERIFY marks and sources stay in one place). Required sections, in this order:

1. **What you are asked for and why** — one paragraph: monthly Copilot cost analysis at team level; the
   analyst never sees names; nothing is changed in GitHub by the tool.
2. **What leaves your machine** — the bundle contents of §22.5 in words (pseudonymous IDs made with a key that
   stays with you, team names of teams with at least 5 Copilot users, org and cost-center names, numbers);
   **what never leaves**: logins, e-mail addresses, numeric user IDs, repository names, workflow paths,
   prompts or code, tokens, download links, the key file. How to check: `tokenbill copilot inspect`.
3. **Choose a path** — A: read-only token (fewer steps each month, more data: seat plan and assignment, usage
   summary for reconciliation, budgets); B: no token (UI downloads + a short questionnaire). Both produce the
   same bundle format; B loses seat assignment, the usage-summary cross-check and daily activity (§22.4).
4. **Path A: token** — the exact credential of §22.3 (classic PAT with the listed scopes and a short expiry,
   or a GitHub App with the listed read permissions), how to store it in a private file (`--github-token-file`,
   0600) or an environment variable, the one command (`tokenbill copilot pull --live --github-enterprise SLUG
   [--github-org ORG …] --since 2026-09-01 --until 2026-09-30 --sources ai_usage,metered,summary,metrics,
   seats,config --github-token-file ~/.config/tokenbill/gh-token --out export-2026-09.tbx`), what `--resume`
   does after a token expiry, and "revoke the PAT when done". Agent tasks are **not** requested (they need a
   per-user token and repository enumeration; not needed for billing).
5. **Path B: no token** — the download checklist of §22.4 with the documented page names (click paths marked
   VERIFY until the release-gate admin confirms them), the questionnaire (`tokenbill copilot admin-guide
   --answers-template admin_answers.json`, fields of §5.17 — including **"Which plan does the Licensing page
   show: Business, Enterprise, both, or I don't know"**), and the one command `tokenbill copilot export --in
   DIR --answers admin_answers.json --out export-2026-09.tbx`.
6. **Team names** — either the metrics `user-teams` report (token path; GitHub already omits teams with fewer
   than 5 seated users) or a CSV `login,team[,cost_center]` the admin prepares from the IdP / HR export
   (`--team-map-csv`); teams with fewer than 5 Copilot users are merged into "(other)" before anything is
   written; people without a team are "unattributed".
7. **The key file** — created on first run at `~/.config/tokenbill/copilot-export.key` (0600); keep it
   (back it up per your org's secret policy); never send it; losing it only breaks month-to-month joins
   (use `--rotate-key` deliberately; the manifest records it).
8. **Check, then hand over** — run `inspect`; send the `.tbx` through the org's approved file-transfer channel;
   delete the raw downloads (path B) — path A never keeps them unless `--keep-raw` was given.
9. **Requests you may get back** — the analyst sends a policy pack (checklist, managed-settings patch, REST
   request files). Nothing in it runs by itself; each item names the setting page or the API call and the
   role it needs (§11.3).
10. **Erasure requests** — `tokenbill copilot pseudonym --key-file … --login -` (reads the login from stdin)
    prints the pseudonymous ID to pass to the analyst for `tokenbill purge --principal`; add the login to
    `--exclude-logins FILE` for future exports (§22.7).
11. **Troubleshooting** — 401 (token expired: refresh the file, `--resume`), 403 (missing permission: the
    message names the endpoint and the permission row of §22.3; switch that source to path B), 409 (another
    usage report is being generated; the pull waits), e-mailed report link expired (request it again), leak
    gate stop (exit 3: the message names the file member and field, e.g. a team label equal to a login —
    rename the team label or use `--hash-workspaces`).
12. **Last resort** — handing over raw GitHub files instead of a bundle is allowed only with the data
    protection officer's / works council's written approval; the analyst then runs `copilot export` on
    receipt and deletes the originals the same day. The guide says so explicitly and gives no other shortcut.

The template `admin_answers.template.json` (packaged) holds every §5.17 field with the value `"unknown"` and
a one-line comment key per field (`"_help_<field>"`, ignored by the reader).

### 22.3 Minimal permissions per data source (read-only; the guide's table)

Never requested: `manage_billing:copilot` (grants write), `admin:org`, `admin:enterprise`, any `write`
permission, the usage-records API (refused, DC8), audit-log access, agent-task user tokens. Evidence:
§19.4 and the fact-checked research (data-apis F2, F19, F25 and its corrections).

| data source (why) | token path: endpoint and minimal credential | no-token path | status |
|---|---|---|---|
| **AI usage report** (per user × day × model credits, gross / discount / net, tokens: the billed truth) | `POST/GET /enterprises/{e}/settings/billing/reports` (`report_type=ai_credit`, ≤ 31-day windows, one export at a time) + download. Enterprise owner or billing manager (or custom role with enterprise-billing read). Classic PAT of that person: the billing endpoints document **roles, not scopes**, and fine-grained PATs are not supported. GitHub App: "Enterprise billing: read" | UI: enterprise Billing and licensing → Usage → AI usage → "Get usage report" (≤ 31 days; e-mailed link) | roles, endpoints: FC; App read sufficient for the export **POST**: **VERIFY** (§19.5 #30); click path and 24 h link validity: **VERIFY** (#33) |
| **Detailed usage report** (seat SKU lines → plan and billing mode; Actions minutes of code review / cloud agent / agentic workflows) | same export API, `report_type=detailed`, same credential | UI: Usage → "Get usage report" → detailed | FC; click path **VERIFY** |
| **Usage summary** (invoice cross-check, L3) | `GET /enterprises/{e}/settings/billing/usage/summary`; same credential | none (verdict "report only", `mapping_verified=False`) | FC |
| **Seats** (plan per seat, assignment via team, last activity and editor) | enterprise: `GET /enterprises/{e}/copilot/billing/seats` — enterprise owner / billing manager, classic PAT `read:enterprise` (App access undocumented, **VERIFY** #21); or per org: `GET /orgs/{org}/copilot/billing/seats` — org owner, classic PAT `read:org`, App org permission "GitHub Copilot Business: read" | UI: the Copilot **activity report** CSV (Licensing → "Get activity report"; no plan, no assignment, §5.15) | FC-corr; click path **VERIFY** |
| **Org Copilot settings** (`plan_type`, `seat_management_setting`) | `GET /orgs/{org}/copilot/billing` — org owner, classic PAT `read:org`, App "GitHub Copilot Business: read"; billing-manager access **VERIFY** (#32) | answers file (`plan_as_shown`, `seat_policy`) | FC |
| **Usage metrics** (per user-day activity, IDE split VS Code / JetBrains, CLI tokens) | `GET /enterprises/{e}/copilot/metrics/reports/{users-1-day,enterprise-1-day,user-teams-1-day}?day=` — enterprise owner / billing manager, classic PAT `read:enterprise`, App "Enterprise Copilot metrics: read" (org variant: `read:org` or fine-grained "View Organization Copilot Metrics") | UI: Insights → Copilot usage → NDJSON export (28-day; excludes CLI; experimental flag, shape **VERIFY** #34) | FC |
| **Team labels** | metrics `user-teams-1-day` (teams with < 5 seated users already excluded by GitHub) | `--team-map-csv login,team[,cost_center]` from the IdP / HR | FC |
| **Budgets** (+ user-states, summarized) | `GET /enterprises/{e}/settings/billing/budgets[/{id}/user-states]`; billing roles; App "Enterprise billing: read" | answers file `budgets` | FC-corr |
| **Cost centers** (pools, caps) | `GET /enterprises/{e}/settings/billing/cost-centers`; billing roles; App "Enterprise billing: read" | answers file `cost_centers` (incl. block / continue at the cap, which REST does not expose) | FC |

**Recommended credential.** One-off: a classic PAT of an enterprise owner or billing manager with only
`read:enterprise` (plus `read:org` when org seats / org settings are pulled), expiry ≤ 7 days, revoked after
the pull (whether the billing endpoints accept it without any further scope is **VERIFY**; the guide tells the
admin to switch the affected source to path B on a 403). Recurring: a GitHub App installed on the enterprise
with "Enterprise billing: read", "Enterprise Copilot metrics: read" and, per org, "GitHub Copilot Business:
read"; installation tokens last one hour and Token Bill never mints them (no RS256 in stdlib) — the admin's
own tooling (e.g. `gh`-based or a CI job) refreshes the token file, which Token Bill re-reads before each unit
and after a 401 (§5.12).

### 22.4 No-token path (UI downloads)

Files the admin downloads into one folder (all optional except the first; each ≤ 31 days where the UI limits
it; one file per window):

| file | UI location (documented page; click path **VERIFY**) | adapter | enables |
|---|---|---|---|
| AI usage report CSV (with token columns) | enterprise Billing and licensing → Usage → AI usage → "Get usage report" (e-mailed to the requester; one report at a time) | `github-ai-usage` (§5.1) | the bill, credits per team / model, overage, findings on report cells |
| Detailed usage report CSV | Billing and licensing → Usage → "Get usage report", type detailed | `github-metered-usage` (§5.2) | seat SKU lines → **plan detection** (precedence 1) and billing mode; Actions minutes of Copilot workloads |
| Copilot activity report CSV | enterprise or org settings → Copilot → Licensing / access → "Get activity report" | `github-copilot-activity-report` (§5.15) | seat holders and idle buckets (plan and assignment unknown) |
| Copilot usage dashboard NDJSON export (optional) | Insights → Copilot usage → export | `github-copilot-metrics` with `--experimental copilot-dashboard-ndjson` (§5.5) | 28-day IDE split (VS Code vs JetBrains) and activity; excludes CLI |
| `admin_answers.json` | filled from the template | `copilot-admin-answers` (§5.17) | plan statement, billing mode, cost-center caps and block / continue, compliance, budgets, stop flags, seat policy |
| `teams.csv` (optional) | the admin's IdP / HR export | `--team-map-csv` | team labels |

Command: `tokenbill copilot export --in downloads/ --answers admin_answers.json [--team-map-csv teams.csv]
--out export-2026-09.tbx` (offline; the export key is created on first use). Compared with the token path
the analyst gets: no seats API `plan_type` (the detailed report's seat SKU lines still decide the plan; if
the detailed report is missing too, the plan is unknown and both scenarios are shown), no seat assignment
(idle seats get the "assignment unknown" bucket, §10.1), no usage-summary cross-check (verdict at best
"report only"), budgets / cost centers only as answered, and 28-day instead of daily activity. The guide lists
these losses next to the path choice.

### 22.5 Bundle format `tokenbill/copilot-export@1` (`copilot/handoff.py`)

```python
def export_bundle(inputs: Sequence[Path], *, out: Path, key_file: Path, rotate_key: bool = False,
                  user_teams: Sequence[Path] = (), team_map_csv: Path | None = None,
                  answers: Path | None = None, k: int = 5, aggregate_only: bool = False,
                  hash_workspaces: bool = False, since: str | None = None, until: str | None = None,
                  exclude_logins: Path | None = None, experimental: frozenset[str] = frozenset(),
                  now_ms: int) -> ExportReport          # counts only; raises LeakError / UsageError
def inspect_bundle(path: Path) -> InspectReport         # counts, key ids, plan evidence, leak-scan status
def leak_scan(members: Mapping[str, bytes], sensitive: SensitiveSet) -> list[LeakHit]   # (member, field) only
def pseudonym(login: str, *, key_file: Path) -> str     # the p_ of one login under the export key
```

**Reading the inputs.** Every input file is read through `core.registry` (`sniff_adapter` / `get_adapter`,
runtime composition; unit tests register fake adapters) with `IngestOptions(identity_mode="central-ingest",
principal_key=K, principal_key_id=key_id(K), name_key=K, name_key_id=key_id(K), team_map=…,
cost_center_map=…, content_tier=NONE, experimental=…)`, where K is the export key
(`core.keys.load_or_create(key_file)`, 32 random bytes, file 0600, directory 0700). While parsing, the
raw identity values of every input are collected **in memory only** into a `SensitiveSet` by CP-HANDOFF's
own identity harvester, which reads each input file a second time independently of the adapters (CSV columns
`username`, `login`, `repository`, `workflow_path`, and — with `--hash-workspaces` — `organization`,
`cost_center_name`; JSON keys `login`, `user_login`, `user_id`, `id` under `assignee` / `user`, `email`,
`name` under `assignee`; the team map's logins and the labels of teams merged for k). Values: logins, e-mail
addresses, display names, repository full and short names (≥ 4 characters), workflow paths, merged team
labels, and (with `--hash-workspaces`) organizational names. Numeric user ids are **not** scanned (they
collide with numbers); their absence is guaranteed because every adapter drops them at parse time, which
`assert_adapter_conforms` checks with a canary id. The set is discarded when the command exits.

**Team labels and k.** Team map from `--user-teams` (metrics user-teams NDJSON, via `copilot/teammap.py`) or
`--team-map-csv`. Seated users per team are counted from licenses (seats or activity report), else from
distinct principals in the report. Teams with fewer than k users are relabelled `(other)` **before any record
is built**; if `(other)` then has fewer than k users, the smallest kept team is merged into it too
(complementary suppression, as `core.kanon.publish`); users without a team stay unattributed (team None).
User-scope budgets carry only team counts (§5.4). The manifest records `teams_kept` and
`teams_merged_into_other` as counts (merged labels never appear).

**Members** (stdlib `zipfile`, `ZIP_DEFLATED`, fixed member order, timestamps 1980-01-01 00:00, no extra
fields, no directories, no comments — so identical inputs and key give a byte-identical bundle):

```
manifest.json
records/cost_lines.jsonl      core.records.to_json(CostLine), one per line, sorted by line_id
records/aggregates.jsonl      UsageAggregate (incl. coverage and quota aggregates), sorted by agg_id
records/outcomes.jsonl        OutcomeAggregate (team rows already k-merged; enterprise / org rows)
records/licenses.jsonl        LicenseSnapshot (p_ principals)
records/activity.jsonl        ActivityDay (p_ principals)
records/config.jsonl          ConfigSnapshot (budgets, budget_users, cost_center, org_settings, run_flags)
```

Only these member names are allowed; absent sources simply omit their member. JSON is canonical
(`sort_keys`, `separators=(",", ":")`, UTF-8, exact decimals as strings, no floats).

**Manifest** (`manifest.json`, canonical JSON): `schema: "tokenbill/copilot-export@1"`, `tool_version`,
`created_date` (UTC date only), `window: [since, until]`, `entities` (enterprise slug and org logins, clear or
`h_` with `--hash-workspaces`), `sources: [{adapter, source_kind, files: <count>, records: {kind: n},
rows_skipped: n, dq: {code: count}}]` (file **counts** only, never file names), `principal_key_id`,
`name_key_id`, `key_rotated: bool`, `k`, `teams_kept: n`, `teams_merged_into_other: n`, `privacy_mode:
"pseudonymized" | "aggregate-only"`, `plan_evidence: [PlanEvidence JSON per entity × month]` (from
`core.pool.detect_plans` on the admin side, so the analyst sees what the admin's data said), `answers:
{field: "stated" | "unknown"}` (which questionnaire fields were answered, not their values — the values are
in `config.jsonl`), `experimental: [flags]`, `leak_scan: {status: "clean", values_checked: n, members: n}`,
`record_counts: {member: n}`. Never: logins, e-mails, numeric user ids, file names, paths, URLs, tokens.

**`--aggregate-only`** (for works councils that do not allow per-person pseudonyms to leave the admin
machine): no `licenses` / `activity` members; `cost_lines` re-keyed without `principal` (summed per natural key
minus the user; cells with fewer than k distinct users merged to team `(other)` by the same rule); seat counts
per entity × plan as `run_flags` `pool_seats.<entity>.<plan>`; aggregates with `n_users ≥ k` only. Loss (stated
in the guide and in the analyst's report): seat kinds (`idle-seat`, `seat-auto-assign`, `completions-only-seat`,
`duplicate-seat`) and per-person joins are unavailable, and team-scoped findings are re-scoped to entity level
because the analyst cannot count users (R14 is kept by construction).

**Leak gate (binding).** Before the zip is written, every member's bytes are scanned: (1) each `SensitiveSet`
value, case-insensitively, wherever it occurs delimited on both sides by a boundary character (anything outside
`[A-Za-z0-9_.@-]`) or the member start / end — so multi-part values such as `org/repo` are matched whole;
values shorter than 3 characters are ignored; logins, e-mails and display names are matched in every field,
repository names and workflow paths in every field except the organizational label fields (`team`,
`cost_center`, `org`, `organization`, `workspace_id`, `entity_id`), where a legitimate team called like a
repository would otherwise stop the export, (2) `core.secrets.find_secrets` over each member's text, (3) the test constants
`CANARY`, `CANARY_LOGIN` and `CANARY_EMAIL` (`core.builders`), (4) any `@` inside a JSON string value (except the manifest's `schema` value), (5) any
`http://` / `https://` substring. Any hit aborts with `LeakError` → exit 3; nothing is written (the zip is
built in a temporary file in the target directory and renamed only after a clean scan); the message names the
member, the record kind and the field — never the value — and the common fix (§22.2 item 11). The reader
(§5.16) repeats checks (2)–(5).

### 22.6 How the product owner analyses a bundle

- `tokenbill copilot inspect export.tbx` — offline: schema, window, entities, per-member record counts, teams
  kept / merged (counts), plan evidence, key ids, privacy mode, leak-scan status; never a record or a
  pseudonym. It also runs the reader's safety checks (§5.16).
- `tokenbill copilot scan --from export-2026-08.tbx --from export-2026-09.tbx [--db copilot.db] -o report.html
  [--policy-out pack/]` — no token, no network. The bundles' key ids are shown and adopted after confirmation
  (`--adopt-key-id KID` for non-interactive runs); bundles with different key ids (a rotated key) are refused
  together unless `--allow-mixed-keys` (then joined by team only). The default store is temporary; `--db`
  keeps a persistent store whose `meta.adopted_key_ids` records the adoption (R-E21), so later months and
  trend views accumulate.
- **What the analyst gets:** the bill per entity × month (per plan scenario while the plan is unknown, R17),
  the plan block with evidence and the how-to-find-out hint, team showback (k ≥ 5) incl. the VS Code vs
  JetBrains split, Copilot findings, the aggregate plan (two columns while the plan is unknown) and a policy
  pack for the admin. Reconciliation verdicts appear when the bundle contains the usage summary (token path).
- **What the analyst cannot do:** see or derive any login (no key; DC31), run live pulls, execute any change.
- **Adding per-developer data:** VS Code collector files (§5.18) are ingested into the same persistent store,
  which then also holds its own org key for them (`--key-file` on first use); bundle principals and collector
  principals are never joined per person (R-E21).
- **Answering questions the data cannot:** `--plan ENTITY=business|enterprise|mixed`, `--billing-mode`,
  `--capped-policy` etc. on `scan` are statements (R17) and override nothing the data shows.

### 22.7 Keys, retention and erasure

- **Custody.** The export key stays on the admin machine (0600); it is both the principal key and the name
  key of the bundle. The analyst's store holds only its key id. Rotating (`--rotate-key`) creates a new key
  and sets `key_rotated` in the manifest; months before and after do not join per person.
- **Retention.** The analyst's store applies `retention.identity_days` to adopted principals exactly as to its
  own (license and activity rows deleted, cost-line principals nulled; team history kept).
- **Erasure.** The admin runs `tokenbill copilot pseudonym --key-file … --login -` (login read from stdin,
  never logged) and passes the printed `p_…` to the analyst, who runs `tokenbill purge --principal p_… --db
  copilot.db` (removes STORE and record-store rows for adopted keys too, with an audit row). To keep the
  person out of later bundles the admin adds the login to `--exclude-logins FILE` (one login per line; rows of
  excluded logins are dropped before pseudonymization and counted in the manifest as `rows_excluded`).
- **Raw files.** Path A deletes them (private temp dir); path B's guide tells the admin to delete the
  downloads after `inspect`.

### 22.8 Acceptance (CP-HANDOFF; offline, fixtures from verified schemas)

- Fixtures (`tests/v2/fixtures/copilot_handoff/`, provenance per §5): AI usage report and detailed report CSVs
  in the documented field order (primary-derived), activity report CSV in the documented field order
  (primary-derived; the JetBrains surface string third-party-derived and flagged), seats / org-settings JSON
  from the OAS examples, metrics NDJSON from the field reference, an answers file; `CANARY_LOGIN`,
  `CANARY_EMAIL`, canary repository names and workflow paths planted everywhere identities occur.
- `export_bundle` → unzip → byte scan: no canary, no login, no e-mail, no repository name, no workflow path;
  every `principal` matches `^p_[0-9a-f]{20}$`; the manifest has every key of §22.5 and no file name.
- Leak gate: a team label equal to a planted login → `LeakError`, exit 3, no output file, message without the
  value; a planted `ghp_` token string in a team label → caught by `find_secrets`.
- Determinism: two runs → byte-identical bundles; same key over two months → same `p_` for the same login;
  `--rotate-key` → different `p_`, `key_rotated=true`.
- k: teams of 3 and 4 users → `(other)`; if `(other)` < k, the smallest kept team joins it; merged labels
  absent from every member.
- Reader safety: path traversal, absolute path, symlink, directory member, unknown member, encrypted flag,
  oversize member, 1000:1 ratio → `UsageError`, nothing ingested; unadopted key id → `PrivacyError`.
- Equivalence: for teams with ≥ k users, `copilot scan --from bundle` gives the same bill lines, pool months,
  findings and showback rows as `copilot scan` on the raw files with the same key (gate test with the real
  adapters and `SqliteStore`).
- `pseudonym(login)` equals the `p_` of that login in the bundle; `--exclude-logins` removes its rows.
- `aggregate-only`: no `licenses` / `activity` members, no `principal` field anywhere, seat kinds reported as
  unavailable by the analyst's scan.
- `admin-guide` output contains every §22.2 section heading and the §22.3 table rows with their VERIFY marks
  (rendered from `facts.copilot.handoff`); INTEGRATION's `test_copilot_admin_doc.py` asserts
  `docs/COPILOT-ADMIN.md` equals the rendered guide.

---

## Appendix C. Hand-computed fixtures (binding for CP-RATES, F-KIT-C FakePricer, F-POOL, CP-DET-*, CP-PLAN)

Prices are the §19.2 rows. 1 µ$ = 1,000 nano. Figures on either Copilot billing path carry basis
LIST_EQUIVALENT.

**Golden pricing (G).**
- **G1** Opus 5.5, 2026-09-23: uncached 12,000; read 180,000; write (unknown TTL) 6,000; output 3,000 →
  48,000,000 + 36,000,000 + write [30,000,000; 48,000,000] + 60,000,000 → point **174,000,000**, range
  [174,000,000; 192,000,000], ESTIMATED, `exact_nano` 144,000,000. With `write_ttl_hint="1h"` the point is
  192,000,000.
- **G2** G1 with `routing="auto"` (×0.9): point **156,600,000**, range [156,600,000; 172,800,000].
- **G3** G1 with `compliance="data_residency"` (×1.1): point **191,400,000**, high 211,200,000.
  **G4** auto + residency (×0.99, assumed): 47,520,000 + 35,640,000 + [29,700,000; 47,520,000] + 59,400,000 →
  point **172,260,000**, high 190,080,000.
- **G5** GPT-5.5, 2026-09-10, context tier unknown: uncached 20,000, read 280,000 (total 300,000 > 272,000),
  output 4,000 → band $10/$1/$45: **660,000,000** EXACT; read 250,000 (total 270,000): **345,000,000** EXACT.
  **G5b** total 270,000 with `context_tier="long_context"` → hypothesis B band 200,000,000 + 250,000,000 +
  180,000,000 = 630,000,000 → range [345,000,000; 630,000,000], point 345,000,000, ESTIMATED.
- **G6** GPT-5.6 Sol, uncached 2,000, read 6,000, write 2,000, output 1,000: 2026-09-10 → 8,000,000 +
  2,400,000 + 10,000,000 + 20,000,000 = **40,400,000** (zero-width write range, ESTIMATED); 2026-08-25
  (promo $2/$0.20/$2.50/$10) → **20,200,000**.
- **G7** Same tokens dated 2026-08-20 (row $2.50/$0.25/$3.125/$15, K-dated: a mechanics test of our file,
  not a billing fact) → 5,000,000 + 1,500,000 + 6,250,000 + 15,000,000 = **27,750,000**.
- **G8** Opus 4.8 `speed="fast"`, no writes: uncached 10,000, read 90,000, output 2,000 → 100,000,000 +
  90,000,000 + 100,000,000 = **290,000,000**; standard **145,000,000**; fast premium **145,000,000** EXACT.
  **G8b** plus 5,000 unknown-TTL writes: fast write [62,500,000; 100,000,000], standard write
  [31,250,000; 50,000,000] → premium point 176,250,000, range [176,250,000; 195,000,000], ESTIMATED.
- **G9** Gemini 3.8 Flash 2026-09-23: uncached 100,000, read 400,000, output 10,000 → **142,500,000**; dated
  2027-01-01 → unpriced, `dq.promotion_expired`.
- **G10** Grok 4.7: uncached 50,000 + read 160,000 (210,000 > 200,000), output 5,000 → $4/$1/$12:
  **420,000,000** EXACT (tier unknown).
- **G11** nano-AIU parity (payload from ccusage issue #1174): tokenDetails input 6 @ 500,000,000,000 per
  1,000,000; cache_read 127,386 @ 50,000,000,000; cache_write 2,220 @ 625,000,000,000; output 6,210 @
  2,500,000,000,000 → totalNanoAiu **23,284,800,000** = **232,848,000 nano**; our point at Opus 4.7 rates:
  30,000 + 63,693,000 + 13,875,000 + 155,250,000 = **232,848,000** (match; the write was billed at the
  published price).
- **G12** Utility call (gpt-4o-mini, nano-AIU 0, `conversation-background`) → billable False, EXACT $0.
- **G13** G1 on `copilot_pool` and on `copilot_direct` → both in `PricedTotal.pool`; `exact` unchanged.
- **G14** Kimi K2.7 Code read 1,000,000 → **190,000,000** EXACT.
- **G15** GPT-5.3-Codex with 1,000 reported write tokens → folded to uncached: **1,750,000**, ESTIMATED
  (zero-width), `dq.copilot_write_folded_to_input`.
- **G16** Opus 5.5 dated 2026-09-21 → unpriced, `dq.model_before_effective_date` (C-dated boundary).
- **G17** `credits_str_to_nano("42.726213")` → 427,262,130 (remainder 0); `usd_str_to_nano("0.4272621300000001")`
  → 427,262,130 (remainder 1E-16).

**Pool (P)** — enterprise E, metered, 1,000 Business + 200 Enterprise seats, direct assignments in
`assign_selected` orgs, standard month, seat lines present.
- **P1** pool = 1,000 × 1,900 + 200 × 3,900 = 2,680,000 credits ($26,800); seat fees $19,000 + $7,800 =
  $26,800; pooled use 3,100,000 → overage 420,000 credits = **$4,200**; direct-org review 15,000 credits,
  discount 0 on days with pool left (`direct_draws_pool="no"`) → **$150**; invoice **$31,150** + Actions,
  labelled INVOICE only for a closed month on a reconciled channel.
- **P2** remove 50 idle Business seats next month, use unchanged: fees −$950, pool 2,585,000, overage 515,000
  (+$950) → **saving $0**.
- **P3** same with use 2,000,000 (slack 680,000) → **saving $950/month**.
- **P4** same with use 2,650,000 (slack 30,000 = $300): new overage 65,000 ($650) → **saving $300**.
- **P5** credit saving S = 100,000 credits ($1,000 list-equivalent): overage 420,000 → invoice **$1,000**,
  headroom $0; overage 60,000 → invoice **$600**, headroom **$400**; slack → invoice $0, headroom **$1,000**.
- **P6** promo cliff: 100 Business seats, July–August use 250,000/month vs promo pool 300,000 (slack);
  September pool 190,000 → **$600/month** at unchanged use, ESTIMATED.
- **P7** capped cost center A (50 Business seats, cap 95,000) uses 130,000; others 2,000,000; enterprise pool
  2,680,000. Policy continue → A's overage 35,000 (**$350**); block → **$0** overage and 35,000 credits of
  blocked demand reported apart; unknown → overage range [$0; $350], ESTIMATED. Shared slack 2,680,000 −
  (2,000,000 + 95,000) = 585,000 in every case.
- **P8** ex post (`invoice_delta`): C1 = 2,700,000, pool 2,680,000, S = 100,000 → **$1,000**; C1 = 2,500,000 →
  **$0**; C1 = 2,600,000 → **$200**.
- **P9** forecast, 2026-09 (Sept 1 is a Tuesday), today 2026-09-23, lag 3 → observed days 1–20 (14 business,
  6 weekend) = 2,000,000; remaining 8 business + 2 weekend days; medians 130,000 / 30,000, p10 110,000 / 20,000,
  p90 150,000 / 40,000 → point **3,100,000**, low **2,920,000**, high **3,280,000**; regime **overage**,
  forecast overage $4,200 ($2,400–$6,000), ESTIMATED.
- **P10** billing mode volume, 10 idle Business seats → `idle-seat` count 10, fees ESTIMATED $190/month,
  `realize_seat_change` → **None** (savings only at renewal).
- **P11** org B `assign_all`, 10 idle Business seats, entity in P3's slack regime → `seat-auto-assign`
  projection **$190/month** ESTIMATED, `needs_eval`; `idle-seat` removable count 0 for org B.
- **P12** 20 idle Business seats in P3's regime, 8 of them team-assigned → removable 12 → **$228/month**;
  team-assigned 8 → projection None.

**Plan (P13–P15, revision 3; binding for F-POOL CA-48, CP-DET-SEATS, CP-PLAN, CP-OUT).** Entity E, metered,
standard month 2026-09, no capped cost center, no direct usage, seat count known (e.g. from the activity
report), plan unknown unless stated.
- **P13** 100 seats, plan unknown, pooled use 250,000 credits. `detect_plans` → plan `unknown`, source `none`,
  seats `{unknown: 100}`; `pool_months` → two PoolMonths. **Business scenario:** pool 100 × 1,900 = 190,000
  credits; overage 60,000 credits = **$600**; seat fees **$1,900** (`seats.unknown_plan`, scenario business);
  `total.invoice` **$2,500**; regime overage. **Enterprise scenario:** pool 100 × 3,900 = 390,000 (slack
  140,000); overage **$0**; seat fees **$3,900**; `total.invoice` **$3,900**; regime slack. Both are shown side
  by side, ESTIMATED; no scenario is chosen; `plan-status` shows seat fees [$1,900; $3,900].
  **P13b** (idle seats per scenario): 10 of the 100 seats idle and removable. Business: fees −$190, pool
  171,000, overage 79,000 (+$190) → saving **$0**. Enterprise: fees −$390, pool 351,000 ≥ 250,000 (still
  slack) → saving **$390/month**. The idle-seat finding carries both (`plan_scenario` dims), never an average.
  **P13c** (credit saving per scenario, P5 rule): S = 50,000 credits → Business: invoice **$500**, headroom $0;
  Enterprise: invoice $0, headroom **$500**.
- **P14** Conflicting evidence: 50 seats; seats API `plan_type="business"` for all 50; detailed-report seat
  lines `copilot_enterprise` quantity 50 seat-months; admin statement `plan.E=business`. → plan
  **enterprise**, source `seat_lines`, `conflict=True`, `dq.copilot_plan_conflict` naming `seat_lines` vs
  `seats_api` and `admin_statement`; one PoolMonth (`plan_scenario=None`), pool 50 × 3,900 = **195,000**
  credits (not 95,000); seat fees from the seat lines.
- **P15** Plan from report quota (flag `copilot-ai-usage-quota` on; no seat lines, no seats API, no org
  settings): 40 seat holders in the activity report; September report rows carry `total_monthly_quota` 3900
  for all 40 usernames → plan **enterprise**, source `report_quota`; pool 40 × 3,900 = **156,000** credits.
  Variant: 30 users with 1900 and 10 with 3900 → plan **mixed**, seats `{business: 30, enterprise: 10}`, pool
  30 × 1,900 + 10 × 3,900 = 57,000 + 39,000 = **96,000**. Variant: July 2026 rows with 7000 → enterprise (promo
  quota). Variant: only 36 of the 40 appear in the report (all with 3900) → seats `{enterprise: 36, unknown:
  4}` → a scenario pair in which only the 4 unknown seats vary (pools 156,000 and 36 × 3,900 + 4 × 1,900 =
  140,400 + 7,600 = 148,000). With the flag off → P13 behaviour (two scenarios).
