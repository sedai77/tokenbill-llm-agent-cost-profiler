### F-KIT — Foundation kit: fakes, conformance, catalogs, keys, k-anonymity (wave 1)

**Goal.** The reference fakes and conformance suites every wave-2 package tests against, the data-only
catalogs (levers, settings allowlist, lifecycle), key-file handling, the single k-anonymity implementation,
and the gate-1 smoke test (SPEC §3.18, §3.20, §3.23, §8.2, §8.4, §11.1, §11.4, D29, D42). You are also the
**contract owner** for waves 2–3 (SPEC §21 #3): keep your session available to hot-fix core bugs and arbitrate
gate-test disputes (record rulings in `tests/v2/kit/RULINGS.md`).

**Owns.** `tokenbill/core/{testing,catalog,keys,kanon}.py`, `tests/v2/kit/**`, `tests/v2/gates/**`.

**Consumes (F-CORE).** `core.records`, `core.types`, `core.labels`, `core.money`, `core.ids`, `core.lanes`,
`core.facts`, `core.protocols`, `core.builders`, `core.registry`. Do not import F-SEM modules except in
gate-F tests (`importorskip`).

**Provides.** `core.testing`: `FakePricer` (rows/modifiers from `core.facts`; every `Pricer` method per SPEC
§6.2–§6.4 incl. per-line exactness, placeholder-output range, unknown-scope range, LIST_EQUIVALENT for the
subscription path, `with_contract`), `MemoryStore` (every `LedgerStore` method incl. §7.3 merge rules,
`lane_index`, `lane_first_reads`, `count_users`, cursors, findings, receipts, `aggregate`, `cost_rows`,
`cluster_days`, purge/audit), `FakeReplayer` (table keyed `(lane_key, spec)`, `from_function`),
`published_for_tests`, conformance helpers `assert_pricer_conforms`, `assert_adapter_conforms`,
`assert_store_conforms`, `assert_replayer_conforms`, `assert_detector_conforms` (incl. the shard-invariance
check by `(team, lane_kind)` groups), `smoke_pipeline_on_fakes`. `core.catalog`: `LeverDef`, `LEVERS` (SPEC §11.1
verbatim), `lever`, `levers_for_kind`, `AllowedKey`, `ALLOWLIST` (§11.4; `verified` from facts.json),
`allowed`, `Promotion`, `SUCCESSORS`, `RETIREMENTS`, `PROMOTIONS`, `ANNOUNCED_UNPRICED`, `successor`,
`retiring_within`, `promotion_for`, `SkuRule`, `SKU_RULES` (from facts.json `sku_rules`), `map_sku` (verified
rules only). `core.keys`: `load_or_create`, `load`. `core.kanon`: `publish`,
`rescope_findings(…, count_users=None)`, `require_self_or_aggregate`, `merge_small_groups`.

**Build.** FakePricer must reproduce SPEC §6.9 cases 1–10, 18, 21, 23, 24 exactly (the parity test in RATES
ties its rows to the real registry). MemoryStore is the executable specification of the store: same merge,
idempotence and privacy behavior as SPEC §7 (no SQL). k-anonymity per SPEC §8.4 including complementary
suppression and exact re-scoping via `count_users`. Catalog data exactly per SPEC §11.1/§11.4/§6.7 (successors
only for same-tier, same-tokenizer pairs). Gate-1 smoke test (`tests/v2/gates/test_gate1_smoke.py`): written
now against SPEC signatures, every real module behind `importorskip`: CC fixture tree (from
`tests/v2/fixtures/claude_code/`, read-only) → `SqliteStore` priced with `RateCard` → `reconcile` against
`tests/v2/fixtures/admin/` pages via ADMIN adapters → `calibrate_lanes` → `run_detectors` →
`build_action_plan` with the real `UsageReplayer`; asserts only structural invariants (labels, no floats,
exact bill equals Σ priced lines, findings sorted, no canary).

**Acceptance tests** (`tests/v2/kit/`):
- `assert_pricer_conforms(FakePricer())` and on `FlatRates`; FakePricer reproduces the §6.9 cases listed
  above; `unit_rates` agrees with `price_usage` on 200 random usages;
- `assert_store_conforms(MemoryStore)`; `MemoryStore.aggregate(group_by=["principal"])` raises; merge of a
  FULL transcript request and a NO_TTL_SPLIT OTel request keeps transcript usage and fills OTel attribution;
- `FakeReplayer` sums only the lanes it is given (cohort subsets work);
- `assert_detector_conforms` catches an undeclared kind, a missing reference, and a shard-dependent test
  detector;
- k-anonymity property test over 1,000 seeded random tables (no published row with `n_users < k`, totals
  preserved, no suppressed child recoverable by subtraction — brute force over subsets); `rescope_findings`
  with `count_users` refuses to publish a parent whose distinct users are 4 although children sum to 5;
- keys: file 0600/dir 0700 (POSIX), group-readable key refused, Windows path warns (fake runner);
- catalog: every `LEVERS` grid string parses with `core.policy.parse_policy` (gate F, `importorskip`); every
  `patch_keys` entry is in `ALLOWLIST`; `successor("claude-opus-4-8") is None`;
  `promotion_for("gpt-5.6-sol", "openai_api", "2026-10-01")` found; `map_sku` ignores unverified rules;
- `test_smoke_fakes.py` composes builders → MemoryStore → FakePricer → transitions (gate F) → a registered
  test detector → FakeReplayer → shapley → `RunResult`.

**Size.** ~2.9k LOC including tests.
