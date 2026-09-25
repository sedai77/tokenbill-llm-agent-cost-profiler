### F-KIT-C — Copilot catalogs, k-anonymity for Copilot scopes, fakes and gate F' (wave 1.5b; contract owner)

**Goal.** Give every Copilot package the shared data and test doubles it builds against — **in separate
Copilot tables**, because the merged wave-1 tests pin the SPEC tables exactly: Copilot levers with an aggregate
grid grammar, admin actions, Copilot allowlist keys, promotions, family exclusions, count sources and Copilot
fix texts; a k-anonymity scope counter and the R-E16 exemption rule for `product`-scoped findings; the E.2 R-E10
`publish()` fix scheduled for wave 1.5; FakePricer / MemoryStore (incl. key-id adoption for handoff bundles) /
FakeReplayer / MemoryRecordStore with Copilot behavior; conformance additions; and the gate F' smoke test.
Read SPEC §3.18, §3.20, §3.23, §8.4, §9.5, §11.1, §11.4, Appendix E (E.2 R-E9, R-E10); addendum §3.7, §6.2,
§8.2, §9.3, §10.4, §11.1, §11.4, §19.2, §21.6, Appendix C; `CORE-AMENDMENTS.md` items K-1 … K-6, §4 (gate F')
and rulings R-E16, R-E17, R-E20, R-E21.

**Owns.** Edits within `tokenbill/core/{catalog,testing,kanon}.py`; `tests/v2/kit/test_copilot_*.py`;
`tests/v2/gates/test_gateF_copilot_smoke.py`. Ownership check `--package F-KIT`.

**Consumes.** F-CORE-C (records, vocabularies of C-9, types, `facts.copilot`, builders, protocols incl.
`LedgerStats`, registry detector attributes). F-SEM-C, F-EXT and F-POOL modules only inside the gate F' test
(`importorskip`), never in `core/testing.py`, `catalog.py` or `kanon.py` at import time.

**Provides.**
- `catalog.py` (K-1 … K-3): `COPILOT_LEVERS` (addendum §11.1 rows; aggregate levers `replay="aggregate"`,
  `grid=()`); `_REPLAY_KINDS += ("aggregate",)`; `AGGREGATE_GRIDS`, `AggregateSpec`, `parse_aggregate_spec`,
  `to_aggregate_spec` (grammar `copilot:<param>=<value>[@<scope>]`, params `auto`, `auto_tier`, `remap`,
  `fast`, `seats_idle`, `seats_team`, `seat_policy`, `plan`, `runner`, `context_tier`, `mcp`, `aw_cap`; scopes
  `all`, `team:<t>`, `entity:<e>`, `model:<m>`, `org:<o>`); `lever(id)` searching `LEVERS` then
  `COPILOT_LEVERS`; `levers_for_kind(kind, *, family="default")`; `COPILOT_ALLOWLIST` from
  `facts.copilot.settings_keys` and `copilot_allowed(key)`; `ADMIN_ACTIONS` (`admin:model_policy`,
  `admin:model_policy_fast`, `admin:org_seat_policy`, `admin:seat_plan_change`, `admin:runner_type`,
  `admin:team_membership_review`, `admin:communicate_auto_tier`, `admin:review_effort_default`,
  `admin:communicate_personal_review_settings`, `admin:review_triggers`, `admin:repo_review_mcp_off`,
  `admin:review_instructions`, `admin:review_unlicensed_policy`, `admin:ci_limits_snippet`,
  `admin:aw_triggers`, `admin:org_cli_billing_policy`, `admin:paid_usage_policy`, `admin:cost_center_pool`,
  `admin:budget_stop`, `admin:plan_confirm` (ask the admin to confirm the plan on the licensing page),
  `admin:vscode_db_exporter_optin` (developer opt-in text), `rest:org_selected_users_delete`,
  `rest:org_selected_teams_delete`, `rest:budget_create`, `rest:cost_center_create`, `rest:cost_center_patch`,
  `rest:coding_agent_policy` — each with where, docs URL, template text, REST method/path or None, auth note);
  `COPILOT_PROMOTIONS`, `copilot_promotion_for`; `COPILOT_RETIREMENTS`; `RR_PRIORS`; `copilot_allowance`
  (`"unknown"` → `UsageError`); `copilot_cost_type`; `copilot_seat_plan`; `copilot_workload`;
  `copilot_category`; `copilot_remap`; `runner_rate`; `editor_family`; `agent_family`; `fix_for` with the
  addendum §10.4 Copilot fix table; `FAMILY_EXCLUSIONS` (§10.4); `COUNT_SOURCE` (addendum §8.2 plus
  `plan-status` → `entity`, `dq.skipped-kinds` → `entity`); re-exports of `ACTIVITY_KEYS`, `ACTIVITY_FLAGS`,
  `CONFIG_KEYS`, `EDITOR_FAMILIES` from `core.records`.
- `kanon.py` (K-4): `scope_counter(…)` (CA-37 mapping; `plan_scenario` not a filter); two-argument
  `count_users` accepted by `rescope_findings`; the Copilot parent chain (`team` → `bucket` → `plan` → `model` →
  `cost_center` → entity root; `product`, `entity`, `org`, `plan_scenario` never removed); `_exempt` per R-E16
  (for scopes with a `product` dim the count source decides, not the category — so Copilot team- and
  cost-center-scoped findings are k-suppressed even with category `aggregate`; R-E9 unchanged for other
  scopes); `_merge_findings` sums `headroom`; E.2 R-E10 in `publish()`.
- `testing.py` (K-5): `FakePricer` Copilot rows / modifiers / band hypotheses / 1h write range / promotions,
  both Copilot paths on LIST_EQUIVALENT; `fake_price_total` → `PricedTotal.pool` for Copilot paths,
  `allowance` for `subscription` only; `MemoryStore(…, adopt_key_ids=False)` (R-E21), `count_users(…,
  source=…)`, `source_stats`, `ClusterDay.pool_nano`, latest-fetch-wins for cost lines and aggregates;
  `FakeReplayer` accepts billing class `pool`; `MemoryRecordStore`; `assert_record_store_conforms(factory)`;
  `assert_detector_conforms` lane-independence for `aggregate=True` detectors; `assert_adapter_conforms` `p_`
  checks for `LicenseSnapshot` / `ActivityDay` / `CostLine.principal` and `CANARY_LOGIN` absence.

**Build.** `CORE-AMENDMENTS.md` K-1 … K-6 verbatim. **Do not change** `LEVERS`, `ALLOWLIST`, `PROMOTIONS`,
`levers_for_kind(kind)`'s default result or any top-level facts reader: `tests/v2/kit/test_catalog.py` pins the
24 SPEC levers in order, `replay ∈ {usage, block, none}` for them, `patch_keys ⊆ ALLOWLIST`,
`ALLOWLIST == facts.settings_keys` (31 keys) and `PROMOTIONS == (openai promo,)`, and
`tests/v2/core/test_facts_evidence.py` pins the settings keys, modifiers and the single promotion. FakePricer's
Anthropic / OpenAI behavior stays byte-identical (the wave-1 parity tests must still pass). The aggregate
grammar is a tiny closed parser with an exact inverse.

**Acceptance tests.**
- FakePricer reproduces Appendix C.G1–G17 to the nano with the stated labels (G1 range [174,000,000;
  192,000,000]; G5 EXACT 660,000,000 / 345,000,000; G5b range [345,000,000; 630,000,000]; G8 premium
  145,000,000 EXACT; G9 expired on 2027-01-01; G13 both paths in `PricedTotal.pool`; G16 unpriced).
- Every `AGGREGATE_GRIDS` entry parses and round-trips; every Copilot lever's `patch_keys` ⊆
  `COPILOT_ALLOWLIST ∪ ADMIN_ACTIONS`; every `fix_for(…, "copilot")` Fix has `target="github-copilot"` and only
  `COPILOT_ALLOWLIST` patch keys; every `FAMILY_EXCLUSIONS` key names a registered detector (and a declared kind
  where given); `lever("copilot.seat_reclaim")` resolves; `levers_for_kind("idle-seat")` is `()` while
  `levers_for_kind("idle-seat", family="copilot")` is the seat-reclaim lever; every existing
  `test_catalog.py` / `test_facts_evidence.py` case unchanged and green.
- `scope_counter`: a person in two teams counted once at the cost-center parent; a team scope counts
  `cost_lines` principals, a seat scope counts licenses (and, without licenses, the `seat_counts` `n_people`
  rows through `MemoryRecordStore`); an entity-scope finding with count source `entity` is published with 3
  users (R-E16) while a 3-user team scope with category `aggregate` is re-scoped; a `plan_scenario` dim survives
  every re-scoping level; merged findings sum `headroom`; a `budget-*` finding carrying a `p_` dim raises
  `PrivacyError`.
- R-E10: `publish(…, audience=…)` (keyword appended, default `"org"`) keeps a row with `n_users == 0` grouped by
  `model` (note `users_unknown`), suppresses one grouped by `principal`-proxy keys, and never suppresses for
  `audience="self"`; every merged `test_kanon.py` case unchanged and green.
- `MemoryStore(adopt_key_ids=True)` (R-E21): a keyless store adopts the first **`copilot-export`** batch's key
  id A (kept, meta `org_key_mode == "adopted"`, `adopted_key_id == "A"`); a `p_` batch of any other adapter is
  never adopted (nulled with `dq.principal_key_mismatch`); a second bundle under key id B → `UsageError`; `r_`
  principals raise `PrivacyError` in the keyless store; an org-keyed store (K) with `adopt_key_ids=True` keeps
  its own `r_` → `p_` pseudonymization under K and the adopted bundle's A values side by side; default
  `adopt_key_ids=False` behavior identical to wave 1 (existing `assert_store_conforms` passes).
- `MemoryRecordStore` passes `assert_record_store_conforms` (idempotent re-ingest with `org=None`,
  latest-fetch-wins, retention deletes old license / activity rows, purge by principal, key-id check).
- `FakeReplayer` replays a `pool` lane on LIST_EQUIVALENT; mixed `pool` + `billed` lanes still raise.
- **Gate F'** (`@pytest.mark.gate`): `CORE-AMENDMENTS.md` §4 green.

**Size.** ~1.9k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** also apply SPEC Appendix E.4 R-E28 (MemoryStore/`LedgerStore.cluster_days` accept cluster kind `"gateway"`) and R-E31 (no mid-summary truncation in `core.kanon._merge_findings`).


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.5 R-E37 (carry `PricedTotal.pool` in `core.kanon._add_priced`; Copilot `sources_mask` bits in `core/testing.py`).
