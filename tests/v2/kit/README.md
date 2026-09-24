# tests/v2/kit — F-KIT acceptance tests

Foundation kit (wave 1): the reference fakes and conformance suites (`tokenbill/core/testing.py`, SPEC
§3.18), the data-only catalogs (`core/catalog.py`, §3.20, §11.1, §11.4, §6.7), key files
(`core/keys.py`, §3.23, §8.2) and the only k-anonymity implementation (`core/kanon.py`, §8.4, ruling
R-E1). The gate-1 smoke test lives in `tests/v2/gates/test_gate1_smoke.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/kit tests/v2/gates` (also on 3.10).
Coverage: `uv run --extra dev coverage run -m pytest tests/v2/kit && uv run --extra dev coverage report
--include='tokenbill/core/testing.py,tokenbill/core/catalog.py,tokenbill/core/keys.py,tokenbill/core/kanon.py'`
(99% at hand-off; the lines left are defensive branches). The gate-F tests were also run on a scratch
merge of this branch with `pkg/F-SEM` (kit, sem and gate tests green; never committed).

| file | covers |
|---|---|
| `test_fake_pricer.py` | `assert_pricer_conforms(FakePricer())`, on `FlatRates` and on a contract card; SPEC §6.9 cases 1–11, 15, 17, 18, 19, 21–24 one by one (per-line exact/estimated splits of 7, 9, 23, 24); channel fallback, US geo on Foundry, modifiers by generation, billing-rule lines, contract overrides/channels/dates, bucket fallbacks, `fake_price_total`; hypothesis: unit rates == `price_usage` |
| `test_memory_store.py` | `assert_store_conforms(MemoryStore)` over all 120 orders of the five sources; `aggregate(group_by=["principal"])` raises; FULL transcript + NO_TTL_SPLIT OTel merge keeps transcript usage and fills OTel attribution (both orders, two `sources_mask` bits); request-id collisions never join, in any order; split-entry and priority rules; diagnostics/params fill; write-time privacy (`r_`/`c_`/`p_`, name keys incl. `extra`, atomic ingest); pricing per ingest and windowed `reprice`; one version per provider id (final, then latest); windows, filters, indexes, `count_users`, aggregates, cost rows, clusters, purge (incl. sources joined only by request id) + audit |
| `test_fake_replayer.py` | sums only the lanes it is given (cohort subsets and single lanes add up), baseline priced with the given pricer, `from_function`, outcomes, modes, billing classes; `assert_replayer_conforms` and three replayers it must reject |
| `test_conformance.py` | `assert_detector_conforms` accepts a cohort-confined detector and catches an undeclared kind, a missing reference, a shard-dependent detector, a shard-reading detector, no fix / no "no mechanical fix", wrong ids, person scopes, self findings without a self principal, nondeterminism, non-allowlisted config keys, duplicates, long titles, allowance labeling, break-glass sessions; `assert_adapter_conforms` on a toy JSONL adapter (plain, `.gz`, directory) and what it catches (non-determinism, canary, source text > 64 bytes, capabilities, `h_`/`p_` without key ids); store and pricer suites catching broken implementations |
| `test_kanon.py` | worked `publish` examples (complementary suppression, two-level collapse, custom `parent_of`, nothing below k); **property test over 1,000 seeded random tables** (no published row below k, totals preserved, brute force over all subsets of published rows cannot recover a suppressed child); hypothesis variant; `rescope_findings` refusing a 4-person parent whose children sum to 5 (with `count_users`), escalation team → cost center → org, merge into an existing parent, complementary absorption of the smallest published sibling, dropped scope values scrubbed from titles/summaries/fixes/evidence, person dims, R-E1 and aggregate exemptions; a property test over 500 seeded fleets (exact recount ≥ k, unique ids, every finding lands once unless the org is below k); `require_self_or_aggregate`; `merge_small_groups` |
| `test_keys.py` | 0600 file / 0700 directories (POSIX), group- or world-accessible keys refused, the Windows path warning through a fake `icacls` runner (create and load), formats, short/malformed keys, creation race (hard-link publication, no partial key visible, in-place fallback with retry), checks on the descriptor actually read; hypothesis fuzz of the key-file parser (only `TokenbillError` escapes) |
| `test_catalog.py` | `LEVERS` is the §11.1 table in order (classes, needs-eval/trade-off, upper bounds, grid clause shapes); every `patch_keys` entry is in `ALLOWLIST`; `ALLOWLIST` mirrors facts.json; `successor("claude-opus-4-8") is None` (same tier, same tokenizer only); retirements, announcements; `promotion_for("gpt-5.6-sol", "openai_api", "2026-10-01")`; `map_sku` ignores unverified rules (and maps with verified ones); hypothesis fuzz of `map_sku` |
| `test_kit_internals.py` | the smoke pipeline's plumbing and the optional F-SEM hooks (sum check, rules table) on stand-ins; FakePricer fallbacks; MemoryStore corners; store privacy negatives |
| `test_gate_f.py` | **gate F** (`importorskip` F-SEM): every `LEVERS` grid parses with `core.policy.parse_policy` and round-trips; lever selectors are valid; `assert_detector_conforms` on a detector built with `core.findings`/`core.transitions` (and it catches a shard-dependent variant); fake keys equal `Policy.spec()` |
| `test_smoke_fakes.py` | **gate F**: `smoke_pipeline_on_fakes()` — builders → MemoryStore → FakePricer → transitions → a registered test detector → FakeReplayer → k-anonymity → Shapley → `RunResult`; structural invariants |
| `fsem_stubs.py` | area-local stand-ins for the F-SEM functions the kit calls (used only by `test_kit_internals.py`); installed in `sys.modules` and on the `tokenbill.core` package so they also apply once the real F-SEM modules are importable |

`RULINGS.md` records the rulings and the interpretations F-KIT made where the SPEC was silent;
`CONTRACT-CHANGE-KIT-*.md` are the contract gaps raised for the orchestrator (SPEC §21 #3);
`CONTRACT-CHANGE-KIT-C-1.md` (wave 1.5b) records the VERIFY assertion that ruling R-E28 flips and the
R-E10 note representation.

## Fixtures and provenance

No fixture files. Every record is synthetic and built in code with `core.builders` and the kit's own
builders (`_conformance_sources`, `_smoke_result`, `_replayer_lanes`): transcript-, OTel-, trace@2-,
trace@1- and responses-shaped `IngestResult`s following the SPEC §3.2 records and §19.4 field
semantics, dated 2026-09-23 (Opus 5.5 is priced from 2026-09-22). The adapter-conformance tests write
a toy JSONL fixture (with the content canary planted in its `text` field) into `tmp_path`. No real
transcripts, pages or keys are used.

## Facts

F-KIT transcribes no fact beyond `tokenbill/core/facts.json`: FakePricer's rows and modifiers, the
settings allowlist (keys, domains, minimum versions, `verified` flags), lifecycle (successors,
retirement floors, retired models, promotions, announced models) and the CUR SKU rules are all read
from it. Consequently the unverified items are those of facts.json (see `tests/v2/core/README.md`),
in particular:

- **Every CUR 2.0 / GCP SKU rule ships `verified: false`**, so `map_sku` maps nothing yet (rows fall to
  `unmapped_cost_type` / `dq.unmapped_sku`); no GCP SKU ids are known.
- Lifecycle successors and the announced-but-unpriced Sonnet 5.5 / Haiku 5.5 are `research`-verified.
- The gpt-5.6-sol launch row is disabled (unpriced as "unverified rate row").

Catalog decisions that are interpretations rather than facts (grid encodings, finding-kind links) are
listed in `RULINGS.md`.

## GitHub Copilot additions (F-KIT-C, wave 1.5b)

CORE-AMENDMENTS K-1 … K-6 and rulings R-E10, R-E16, R-E21, R-E28, R-E31, R-E37 on F-KIT's files.
The SPEC tables (`LEVERS`, `ALLOWLIST`, `PROMOTIONS`, `levers_for_kind(kind)`) and every wave-1 test
above are unchanged; the Copilot data lives in separate tables. Gate F' (CORE-AMENDMENTS §4) is
`tests/v2/gates/test_gateF_copilot_smoke.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/kit tests/v2/gates` (also on 3.10).
Coverage of `catalog.py` / `kanon.py` / `testing.py` over `tests/v2/kit` + `tests/v2/gates`: 100% /
99% / 99% at hand-off (the lines left are defensive branches).

| file | covers |
|---|---|
| `test_copilot_catalog.py` | `COPILOT_LEVERS` = addendum §11.1 in order (aggregate levers `replay="aggregate"`, `grid=()`; patch keys ⊆ `COPILOT_ALLOWLIST ∪ ADMIN_ACTIONS`); every `AGGREGATE_GRIDS` entry parses and round-trips; the aggregate grammar table, rejections, a hypothesis round-trip over every parameter × scope and a fuzz test (only `UsageError` escapes); `lever` / `levers_for_kind(…, family=)` (`idle-seat` → `()` by default, the seat-reclaim lever for `copilot`); `COPILOT_ALLOWLIST == facts.copilot.settings_keys`; `allowed` searches both tables; the 27 `ADMIN_ACTIONS` ids of the brief (where, docs URL, REST method/path, auth note); promotions, retirements, RR priors, `copilot_allowance` (promo months, `unknown` → `UsageError`), SKU / workload / category / remap / runner accessors, the `editor_family` table, `agent_family`; every `fix_for` entry targets `github-copilot` with allowlisted keys; `FAMILY_EXCLUSIONS` keys name registered detectors and declared kinds; `COUNT_SOURCE` |
| `test_copilot_kanon.py` | `scope_counter`: a person in two teams counted once at the cost-center parent (5, never 6), team scopes over cost-line principals (not requests), seat scopes over licenses and, without licenses, the `seat_counts` rows; the scope → `where` mapping; R-E16 (entity scope with count source `entity` published with 3 users, a 3-user team scope re-scoped whatever its category); `plan_scenario` survives every level of `COPILOT_RESCOPE_LEVELS` and scenarios never merge; merged `headroom` sums; a `budget-*` finding with a `p_` dim → `PrivacyError`; Copilot and default chains never mix; one- and two-argument counters (arity); a hypothesis property over Copilot findings; R-E10 (`users_unknown` rows kept by model, suppressed with person-proxy keys, `audience="self"`); R-E37 pool through merged rows; R-E31 (short summaries byte-identical, labelling sentences always kept, word-boundary cut only as a last resort; property test) |
| `test_copilot_fake_pricer.py` | Appendix C.G1–G17 to the nano with their labels (G1 range, G2–G4 modifiers, G5 EXACT, G5b A/B range, G6/G7 dated rows, G8/G8b fast premium, G9 promotion expiry, G10/G14, G11 nano-AIU parity, G12 utility call EXACT $0, G13 both paths in `PricedTotal.pool`, G15 folded writes, G16 unpriced, G17 string conversions); contracts never apply to Copilot; Anthropic / OpenAI prices unchanged; every Copilot row resolves and agrees with its unit rates; a band-hypothesis property test; `assert_pricer_conforms` runs the Copilot goldens |
| `test_copilot_memory_store.py` | R-E21 adoption: keyless store adopts the first `copilot-export` key id A (`org_key_mode="adopted"`, audit row without identities), another adapter's `p_` values nulled (`dq.principal_key_mismatch`, never adopted), a second bundle key id → `UsageError` (atomic), `r_` principals need the own org key, an org-keyed store keeps both key spaces; default behaviour = wave 1 (`assert_store_conforms` also with `adopt_key_ids=True`); `assert_store_copilot_conforms(MemoryStore)` and three broken stores it catches; `count_users(source="cost_lines")`; `source_stats`; pool / allowance split in `aggregate` and `cluster_days`; cluster kind `gateway` (R-E28); latest-fetch-wins for Copilot records only; `SOURCES_MASK_BITS` |
| `test_copilot_record_store.py` | `MemoryRecordStore` passes `assert_record_store_conforms` (one- and two-argument factories); the key-id check follows the ledger's `meta` incl. an adopted key id; configuration rows always stored; count filters, the `seat_counts` / `activity_counts` fallbacks; windows, `retain`, `purge`, audit; five broken stores the suite catches |
| `test_copilot_conformance.py` | `assert_detector_conforms`: lane independence for `aggregate=True` detectors, Copilot fixes checked against `COPILOT_ALLOWLIST`, `headroom` only LIST_EQUIVALENT on Copilot scopes; `assert_adapter_conforms`: `CANARY_LOGIN` leaks and non-`p_` principals caught; `FakeReplayer` on `pool` lanes (mixed classes raise); `assert_replayer_conforms(pool=True)` |
| `../gates/test_gateF_copilot_smoke.py` | **gate F'**: catalog contracts; `sniff_adapter` with every Copilot adapter module absent; pool months C.P9 / C.P13 (`importorskip` F-POOL); FakePricer into `PricedTotal.pool`; `run_detectors` phases, family filter and exclusions (needs F-SEM-C); `rescope_findings` with `count_users_fn` (F-EXT when merged, else `scope_counter`); R-E20 `build_finding` (F-SEM-C); the extension host with a fake extension defined in the test module (`importorskip` F-EXT); adoption, `users_unknown`, a `RunResult` with `copilot` set |

### Fixtures and provenance (Copilot)

No fixture files: every record is built in code with `core.builders` (`make_ai_usage_row`,
`make_seat_line`, `make_license`, `make_config`, `make_pool_month`, …) and the kit's own builders
(`_record_batches`, `_copilot_ledger_batch`, `lane_from_table_pool`). The C.P9 daily series of the
gate test is the binding series of the F-POOL brief; the golden prices are addendum Appendix C.

### Facts (Copilot)

Every price, multiplier, plan, SKU, workflow path, editor pattern and settings key comes from
`facts.copilot` (F-CORE-C; `verification: "research"`, R-E19). F-KIT-C added no fact to `facts.json`.
Two data items are F-KIT-C's own and are listed here:

- **`ADMIN_ACTIONS` docs URLs** were checked against the GitHub Docs sitemap and page copies the
  research pass retrieved on 2026-09-23 (`copilot/raw/pagelist.txt` and the `copilot/raw/*.md` page
  copies in the orchestrator's scratchpad); the REST methods and paths against GitHub's GHEC OpenAPI
  description (`copilot/raw/ghec.json`); auth notes are addendum §19.4. UI click paths are not
  given (addendum §19.5 #33, VERIFY).
- **`fix_for` texts** paraphrase the addendum §10.1–§10.4 fix columns; they carry a docs URL and set a
  `config_patch` only where a Copilot allowlisted key has an obvious value (`copilot.managed.model`
  `"auto"`, `copilot.repo.effortLevel` `"medium"`, `copilot.repo.contextTier` `"default"`).

Unverified items inherited from facts that F-KIT-C's behaviour depends on: the 1h write price of
Claude models on Copilot (2 × input), the long-context thresholds (272,000 / 200,000) and the A/B band
reading, K-dated effective dates, the `copilot_standalone` → Business mapping, the plan-quota map, the
workflow paths and every non-VS Code editor pattern.
