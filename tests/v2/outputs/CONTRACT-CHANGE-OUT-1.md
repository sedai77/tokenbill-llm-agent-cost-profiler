# CONTRACT-CHANGE-OUT-1 — additive OUT signatures and two SPEC clarifications

No `tokenbill/core/*` change is requested. The items below are OUT-owned API additions (all
keyword-only with defaults, so the SPEC §14.7 calls keep working) and two SPEC-text clarifications
that the orchestrator may fold into Appendix E.

1. **`write_focus` keywords** (SPEC §14.7): besides A-8's `extra_rows=()` and
   `owned_channels=frozenset()`, `findings: Sequence[Finding] = ()` fills `x_FindingIds` /
   `x_TopWasteCause` and `reconciliation: ReconciliationReport | None = None` fills
   `x_ReconciliationDeltaPct`. Without them those columns are empty (the SPEC lists the columns but
   the §14.7 signature carries no source for them). CLI-LEDGER's `export focus` should pass the
   latest findings and the reconciliation report of the window.
2. **`contracted` / `recoverable_by_scope` keys**: `outputs.focus.row_key(row)` (all dimensions of a
   `LedgerCostRow`); `recoverable_by_scope` also accepts `(team, lane_kind)` and `(team,)`.
3. **Allocation splits in FOCUS**: `LedgerCostRow` has no allocation-method fields, so
   `finops.allocation.split_rows` returns `AllocatedCostRow` (a `LedgerCostRow` subclass owned by
   OUT, with `method_id` / `method_details`) and `write_focus` fills `AllocatedMethodId` /
   `AllocatedMethodDetails` from them. `apply_rules` marks split-owned requests with team
   `"(split:<rule id>)"`; CLI-LEDGER calls `split_rows(rows, rules, weights=<developer-days per
   team from cluster_days>)` before `write_focus` / `coverage`.
4. **`classify(lane, *, rules=None)`**: the SPEC's "rule-set class (0.9)" needs the rule set; it is
   an optional keyword.
5. **Clarification (SPEC §14.1)**: the root `generated_ms` is wall-clock metadata and exempt from
   the "integer ⇒ `evidence`" rule, as is the `range` object inside a MONEY object (its parent
   carries the label). OUT's validator implements exactly this.
6. **Clarification (SPEC §14.4 k-merge)**: when every allocation group of a charge identity stays
   below k after `core.kanon.publish`, the identity's cost is exported as one org-level
   `(other: <k users)` row (no team, `x_SuppressedUsers` = the withheld users) instead of being
   withheld, so FOCUS totals always equal the ledger. If the contract owner prefers withholding,
   the change is local to `outputs/focus.py::_publish`.
