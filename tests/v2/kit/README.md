# tests/v2/kit — F-KIT acceptance tests

Foundation kit (wave 1): the reference fakes and conformance suites (`tokenbill/core/testing.py`, SPEC
§3.18), the data-only catalogs (`core/catalog.py`, §3.20, §11.1, §11.4, §6.7), key files
(`core/keys.py`, §3.23, §8.2) and the only k-anonymity implementation (`core/kanon.py`, §8.4, ruling
R-E1). The gate-1 smoke test lives in `tests/v2/gates/test_gate1_smoke.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/kit tests/v2/gates` (also on 3.10).
Coverage: `uv run --extra dev coverage run -m pytest tests/v2/kit && uv run --extra dev coverage report
--include='tokenbill/core/testing.py,tokenbill/core/catalog.py,tokenbill/core/keys.py,tokenbill/core/kanon.py'`
(99% at hand-off; the lines left are defensive branches).

| file | covers |
|---|---|
| `test_fake_pricer.py` | `assert_pricer_conforms(FakePricer())`, on `FlatRates` and on a contract card; SPEC §6.9 cases 1–11, 15, 17, 18, 19, 21–24 one by one (per-line exact/estimated splits of 7, 9, 23, 24); channel fallback, US geo on Foundry, modifiers by generation, billing-rule lines, contract overrides/channels/dates, bucket fallbacks, `fake_price_total`; hypothesis: unit rates == `price_usage` |
| `test_memory_store.py` | `assert_store_conforms(MemoryStore)` over all 120 orders of the five sources; `aggregate(group_by=["principal"])` raises; FULL transcript + NO_TTL_SPLIT OTel merge keeps transcript usage and fills OTel attribution (both orders, two `sources_mask` bits); request-id collisions never join, in any order; split-entry and priority rules; diagnostics/params fill; write-time privacy (`r_`/`c_`/`p_`, name keys, atomic ingest); windows, filters, indexes, `count_users`, aggregates, cost rows, clusters, purge + audit |
| `test_fake_replayer.py` | sums only the lanes it is given (cohort subsets and single lanes add up), baseline priced with the given pricer, `from_function`, outcomes, modes, billing classes; `assert_replayer_conforms` and three replayers it must reject |
| `test_conformance.py` | `assert_detector_conforms` accepts a cohort-confined detector and catches an undeclared kind, a missing reference, a shard-dependent detector, a shard-reading detector, no fix / no "no mechanical fix", wrong ids, person scopes, self findings without a self principal, nondeterminism, non-allowlisted config keys, duplicates, long titles, allowance labeling, break-glass sessions; `assert_adapter_conforms` on a toy JSONL adapter (plain, `.gz`, directory) and what it catches (non-determinism, canary, source text > 64 bytes, capabilities, `h_`/`p_` without key ids); store and pricer suites catching broken implementations |
| `test_kanon.py` | worked `publish` examples (complementary suppression, two-level collapse, custom `parent_of`, nothing below k); **property test over 1,000 seeded random tables** (no published row below k, totals preserved, brute force over all subsets of published rows cannot recover a suppressed child); hypothesis variant; `rescope_findings` refusing a 4-person parent whose children sum to 5 (with `count_users`), escalation team → cost center → org, merge into an existing parent, person dims, R-E1 and aggregate exemptions; `require_self_or_aggregate`; `merge_small_groups` |
| `test_keys.py` | 0600 file / 0700 directories (POSIX), group- or world-accessible keys refused, the Windows path warning through a fake `icacls` runner (create and load), formats, short/malformed keys, creation race; hypothesis fuzz of the key-file parser (only `TokenbillError` escapes) |
| `test_catalog.py` | `LEVERS` is the §11.1 table in order (classes, needs-eval/trade-off, upper bounds, grid clause shapes); every `patch_keys` entry is in `ALLOWLIST`; `ALLOWLIST` mirrors facts.json; `successor("claude-opus-4-8") is None` (same tier, same tokenizer only); retirements, announcements; `promotion_for("gpt-5.6-sol", "openai_api", "2026-10-01")`; `map_sku` ignores unverified rules (and maps with verified ones); hypothesis fuzz of `map_sku` |
| `test_kit_internals.py` | the smoke pipeline's plumbing and the optional F-SEM hooks (sum check, rules table) on stand-ins; FakePricer fallbacks; MemoryStore corners; store privacy negatives |
| `test_gate_f.py` | **gate F** (`importorskip` F-SEM): every `LEVERS` grid parses with `core.policy.parse_policy` and round-trips; lever selectors are valid; `assert_detector_conforms` on a detector built with `core.findings`/`core.transitions` (and it catches a shard-dependent variant); fake keys equal `Policy.spec()` |
| `test_smoke_fakes.py` | **gate F**: `smoke_pipeline_on_fakes()` — builders → MemoryStore → FakePricer → transitions → a registered test detector → FakeReplayer → k-anonymity → Shapley → `RunResult`; structural invariants |
| `fsem_stubs.py` | area-local stand-ins for the F-SEM functions the kit calls (used only by `test_kit_internals.py`) |

`RULINGS.md` records the rulings and the interpretations F-KIT made where the SPEC was silent;
`CONTRACT-CHANGE-KIT-*.md` are the contract gaps raised for the orchestrator (SPEC §21 #3).

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
