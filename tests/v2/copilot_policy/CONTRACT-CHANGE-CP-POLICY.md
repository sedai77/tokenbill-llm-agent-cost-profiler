# CONTRACT-CHANGE-CP-POLICY — seams CP-POLICY met in the frozen core (implemented against the current contract)

CP-POLICY builds on the core at base `16539c9` without editing `tokenbill/core/*`. Where the
contract is silent or too narrow, the workaround below is implemented and the proposed change is
recorded for the contract owner (SPEC §21 #3).

## 1. `AdminAction` has one projection, but an unknown plan needs two (R17, ruling R-E22)

- **What.** The brief requires the seat-reclaim action to "show two projections" while the plan is
  unknown; `AdminAction.projection` is a single `Figure`, and any single figure would merge or pick
  a scenario.
- **Now.** While scenario plans are given, a projected action carries `projection=None` and its
  `what` text holds both values with their labels: `Projection (plan unknown): if Business:
  $0.00/month (estimated, list); if Enterprise: $390.00/month (estimated, list, upper bound).`
  The checklist renders the same text on the item's Label line.
- **Proposed (additive, appended with default).**
  `AdminAction.scenario_projections: tuple[tuple[str, Figure], ...] = ()` (keys `business` /
  `enterprise`, sorted), so CP-OUT can render the two figures with `figure_json` instead of text.

## 2. No `ADMIN_ACTIONS` id for the JetBrains-limits item

- **Now.** The item has `action_id = "copilot:jetbrains-limits"`, `admin_action =
  "admin:model_policy"` (its delivery: the server-side model policy, reach 1) and its own `what`
  text; its docs URL is the managed-settings reference (`COPILOT_ALLOWLIST["copilot.managed.model"]
  .source`).
- **Proposed.** An `ADMIN_ACTIONS["admin:jetbrains_limits"]` row (where `enterprise settings`,
  the managed-settings reference URL, the template text above, auth as `admin:model_policy`).

## 3. Additive keywords beyond the brief's signatures

| function | keyword | why |
|---|---|---|
| `admin_actions` | `teams=None` | the JetBrains-limits trigger ("any team's JetBrains share > 0") needs team editor counts; `editor-mix` findings are read too |
| `admin_actions` | `config=()` | `renewal_date.<entity>` run flags give volume / azure seat deadlines |
| `build_copilot_packs` | `k=5` | team files, per-team reach rows and team seat removals only for teams of at least k people |
| `build_copilot_packs` | `today=None` | the seats-query cut-off in `requests.jsonl` (`<today minus 30 days>` placeholder without it) |
| `budget_design` | `cost_lines=()` | the cohort p99 of **per-user** monthly credits: `Cell`s carry no per-user amounts, only `n_users`; seat lines give each cost center's licences exactly |

`teams` (brief: an input of `build_copilot_packs`) is a mapping team → counts; CP-POLICY also
exports `team_counts(activity, config, licenses)` that builds it from records (`ide:*`, `cli`,
`app` interactions, `n_people`, `seats`, `idle_seats`), so CP-WIRE needs no own reader.

## 4. `budget_design` returns flat specs, labelled per scenario

`list[dict]` with `kind` (`cost_center_pool` | `budget` | `sizing_check` | `note`), `scenario`
(None or `business` / `enterprise`), `entity_id`, `month`, `billing_mode`, `text`, `request`.
"Two labelled sets" = the pool-dependent specs carry `scenario`; the pool-independent user-level
budgets (cohort p99) appear once with `scenario=None`. With every plan known no spec has a
scenario. Proposed: pin this dict shape in addendum §11.3 (or a `BudgetSpec` dataclass in
`core.types` later).

## 5. CP-VSCODE's `optin_snippet()` is not on disk at this wave

The pack ships its own `vscode/settings.snippet.json` and `vscode/agent-traces-optin.md` built
from `core.facts.copilot_vscode_traces()` (setting name, retention). CP-WIRE may pass CP-VSCODE's
text later; the brief's file name `vscode/agent-traces-optin.md` is used (the addendum's
`vscode/README.md` is superseded by the brief).

## 6. Observed at this base: CP-PLAN × CP-DET-SEATS seat-count seam (not CP-POLICY's to fix)

`tests/v2/copilot_plan/test_gate.py::test_gate_p12_…` and `…p13b…` fail at `16539c9`:
`copilot.plan.seat_counts` raises "seat count out of range" on CP-DET-SEATS's `idle-seat`
evidence (its `fees` item carries `nano` / `low_nano` / `high_nano`). CP-POLICY's gate tests
therefore compose CP-PLAN with an `idle-seat` finding in CP-PLAN's documented evidence names, and
CP-DET-SEATS's real findings with hand-built scenario plans (`test_gate.py`); once the seam is
fixed, the three can be chained in one test.

## 7. Code vs prose

- `PlanEvidence` in `core/types.py` has no `hint` field (addendum §3.2 prose has one): the plan
  confirmation text comes from `ADMIN_ACTIONS["admin:plan_confirm"]`.
- `AdminActionDef.template` texts are ≤ 400 characters; CP-POLICY appends counts / dates / the
  projection and shortens the template at a word boundary when the projection would not fit, so
  both scenario values are always complete.
