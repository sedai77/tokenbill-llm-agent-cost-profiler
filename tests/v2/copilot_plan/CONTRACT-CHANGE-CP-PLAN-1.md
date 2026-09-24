# CONTRACT-CHANGE-CP-PLAN-1 — seams the SPEC text leaves open (CP-PLAN, wave 2b)

Status: **proposed** (SPEC §21 #3). CP-PLAN implements against the current contract; nothing in
`tokenbill/core/*` was edited. Items 1 and 2 are requests to sibling packages built in parallel;
items 3–6 record contract gaps worked around in `tokenbill/copilot/plan.py` and proposed for the
contract owner's next amendment round. Gate tests: `tests/v2/copilot_plan/test_gate.py`.

## 1. Seat counts in seat findings (to CP-DET-SEATS)

`plan_copilot`'s signature carries no licenses, so the seat levers take Δseats from the seat
findings (addendum §9.2 step 4). The addendum names the counts (§10.1: removable, team-assigned,
auto-assigned, assignment unknown) but not their evidence attribute names. CP-PLAN reads
(`tokenbill.copilot.plan.seat_counts`):

| finding kind | read from | meaning for the plan |
|---|---|---|
| `idle-seat` | int evidence attrs `removable`, `team_assigned`, `auto_assigned`, `assignment_unknown`, summed over evidence items; scope dims `entity`, `team`, `org`, `plan` (`business` \| `enterprise` \| `unknown`), `bucket` (`31-90` \| `none_90d`); `plan_scenario` on per-scenario findings | `copilot.seat_reclaim` removes `removable` + `assignment_unknown` (the latter as an upper bound with `needs_eval`); `team_assigned` and `auto_assigned` are never counted here |
| `seat-auto-assign` | int attr `auto_assigned` (else a total `n` / `count` / `seats` / `idle`); dims `entity`, `org`, `plan` | `copilot.seat_policy_selected` removes those idle seats (`@org:<o>`) |
| `plan-mix` | int attr `enterprise` (else a total); dims `entity` | `copilot.seat_downgrade` moves them Enterprise → Business |

Tolerated spellings (normalized): `n_` / `num_` / `count_` / `seats_` prefixes and `_seats` /
`_count` / `_n` suffixes, hyphens; `plan` / `bucket` as string attrs; an item with a string attr
`assignment` (or `class`) naming the class and a total `n`. A finding with only a total is read as
assignment unknown (upper bound). **Request:** emit at least the four `idle-seat` counts under
these names (one evidence item per team is fine), the `plan` and `bucket` dims, and one finding
per scenario with `plan_scenario` while the plan is unknown (or one scenario-free finding with plan
`unknown`, which CP-PLAN reads in both scenarios).

## 2. `activity_counts` rows (to CP-HANDOFF)

Reach from an aggregate-only bundle must equal reach from the `ActivityDay`s the bundle was built
from (brief acceptance). CP-PLAN counts `ide:*` (any key; family by `core.catalog.editor_family`,
so `ide:jetbrains` and `ide:intellij` agree), `cli_requests`, and the Copilot-app interactions —
`app_requests` on `ActivityDay`, `app_interactions` on `activity_counts`. **Request:** build
`app_interactions` = Σ `app_requests` and `ide:<family>` = Σ of the family's `ide:*` counts
(the test `test_reach_from_activity_counts_equals_reach_from_the_activity_days` builds the rows
exactly so).

## 3. `plan_copilot_scenarios` key `"known"` vs `CopilotSummary.plans_by_scenario`

The brief returns `(("known", plan),)` when every plan is known; `CopilotSummary.__post_init__`
accepts only `business` / `enterprise` keys in `plans_by_scenario`. As the brief says, CP-WIRE
stores the known plan in `CopilotSummary.plan` and only scenario pairs in `plans_by_scenario`.
No change proposed; recorded so no consumer passes the tuple through unchanged.

## 4. No per-lever headroom field (`LeverResult`)

R11 wants each lever's pool headroom beside its invoice value; `LeverResult` has one `basis`. CP-PLAN
follows SPEC §11.2's allowance-cohort convention: each player has a LIST `LeverResult` (group
`copilot`, invoice Shapley) and, whenever a credit lever plays or any coalition frees headroom, a
LIST_EQUIVALENT one (group `copilot:pool_headroom`, headroom Shapley); only
`ActionPlan.pool_headroom_monthly` sums the latter. Consumers select by `basis`. Proposed for a
later amendment: `LeverResult.headroom: Figure | None = None` (appended, defaulted).

## 5. No notes on `ActionPlan`

Exclusions the brief wants listed with a reason (`copilot.seat_downgrade` — "plan unknown";
trade-off levers without `include_tradeoffs`; linked levers with nothing to act on; entities with
an unknown pool; unpriced cells) have no field. CP-PLAN writes them into the notes of
`headline_monthly` and `joint_saving` (`"not in the joint set: …"`, `"excluded (…): …"`).
Proposed: `ActionPlan.notes: tuple[str, ...] = ()` (appended, defaulted).

## 6. Additive keywords and interpretations

- `plan_copilot(…, config=(), scenario=None)` — beyond the brief's signature, defaulted: `config`
  carries `activity_counts`, `compliance`, `renewal_date.<entity>`; `scenario` plays one plan
  scenario (addendum §11.2 revision 3). `scenario=None` with scenario pool months raises
  `UsageError` (use `plan_copilot_scenarios`).
- **Headroom.** Addendum §9.2 writes `headroom(S) = Σ_e (C_e(∅) − C_e(S)) − (overage_e(∅) −
  overage_e(S))`, which would give a seat removal in an overage entity positive "headroom" (its
  overage rises). The brief's acceptance (seat lever headroom 0 in the overage entity) wins:
  `headroom(S) = ΔC − (overage(C₀, P_S) − overage(C_S, P_S))`, identical to the addendum for credit
  levers and 0 for seat-only coalitions.
- `ActionPlan.shapley_se` (Monte Carlo only) is keyed by the players' canonical specs
  (`LeverResult.params`), since several `copilot.model_policy` players (one per remap pair) share a
  lever id.
