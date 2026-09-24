# OUT tests — FinOps helpers (`tests/v2/finops/`)

Package OUT (SPEC §14.6): the allocation-rules engine and the workload classifier.

| module | provides |
|---|---|
| `tokenbill/finops/allocation.py` | `load_rules(path) -> RuleSet`, `parse_rules(doc)`, `apply_rules(req, rules) -> Attribution`, `coverage(rows) -> str`, `split_rows(rows, rules, *, weights)`, `split_marker`, `is_allocated`, `AllocatedCostRow`, `RuleSet` / `Rule` / `Condition` / `Split` |
| `tokenbill/finops/workload.py` | `classify(lane, *, rules=None) -> (WorkloadClass, confidence)`, `signals`, `AUTOMATION_RANK` |

## Fixtures and provenance

All synthetic, built with `core.builders` (no files, no real data):

- `test_allocation.py` — an ordered four-rule document (prefix, `in`, `extra.mdm_group`, regex +
  proportional split) and ~35 malformed documents.
- `test_workload.py` — a seeded (`common.rng(7, …)`) labeled set of 96 lanes, 12 per shape: CI
  action runs, SDK services, API-run services, batch jobs, cron-like schedules (±5% jitter on 15 min /
  1 h / 1 day periods), tagged eval runs, interactive CLI sessions with and without HUMAN_PROMPT
  events. Accuracy must be ≥ 95%.
- `test_fuzz.py` — hypothesis: arbitrary JSON, rule-shaped documents, regex text and raw file bytes
  through `parse_rules` / `load_rules`; only `TokenbillError` may escape.

## Acceptance map

| acceptance item | test |
|---|---|
| first-match semantics per target field | `test_allocation.py::test_first_match_wins_per_target_field` |
| proportional splits sum to the original | `test_allocation.py::test_split_rows_sum_to_the_original_and_carry_the_method`, `::test_apportion_is_exact` (property) |
| `(unallocated)` line | `test_allocation.py::test_unmatched_is_unallocated_and_split_marks_the_team` |
| coverage KPI | `test_allocation.py::test_coverage_kpi`, `tests/v2/outputs/test_store_backed.py::test_allocation_coverage_of_store_rows` |
| workload classifier ≥ 95% on a labeled set | `test_workload.py::test_accuracy_on_the_labeled_set_is_at_least_95_percent` |

## Interpretations

- `apply_rules`: a rule value beats the request's own value; a field no rule sets keeps the
  request's value; a request left without a team gets `"(unallocated)"`. Rules match the ledger's
  values, so hashed ids are written as `h_…` in rules. `principal` is not a match field (rules
  never target a person). Regex (id fields only) follows `re.search`; the safe subset refuses
  patterns over 200 characters, backreferences (`\1`, `\g<…>`, `(?P=…)`, `(?(…)`) and nested
  quantified groups (`(a+)+`).
- Splits: `apply_rules` sets team `"(split:<rule id>)"`; `split_rows(rows, rules,
  weights=<active developer-days per target team>)` apportions every amount and the quantity by
  largest remainder (exact sums; all-zero weights → equal parts, recorded in the method details).
- `coverage` counts billed-basis rows only (list-equivalent allowance is not billed); a split marker
  counts as allocated; `"1"` when there is nothing to allocate.
- `classify` adds two documented rules to SPEC §14.6: INTERACTIVE (0.5) for lanes with a
  HUMAN_PROMPT event or entrypoint `cli`, and cadence guards (no human prompt, mean gap above the
  5-minute cache TTL, as DETECT-OTHER's `scheduled-cadence`). The automated-session rule (0.7)
  applies to main / API-run / unknown lanes only and not to entrypoint `cli`. `rules=` (optional
  keyword) adds the rule-set signal (0.9).

## Unverified facts

- Claude Code's interactive entrypoint value `cli` (the SDK / headless values `sdk-py`, `sdk-ts`,
  `sdk-cli` and `claude-code-github-action` come from SPEC §14.6 / §19.4).
