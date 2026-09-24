# CONTRACT-CHANGE-KIT-3 — k-anonymity details the SPEC leaves open

**What.** SPEC §8.4 does not say (a) how many users a merged `"(other)"` row has, (b) what the parent
of an arbitrary finding scope is beyond "team → cost_center → org", or (c) how provider-side org-scan
findings (category `aggregate`, scoped by channel / workspace / model, no people, `n_users` usually 0)
pass `rescope_findings`, which the pipeline applies to them (§15 `run_findings`). Read literally, (c)
suppresses every org-scan finding and defeats D32 (`scan --org` from Admin data alone).

**Current implementation** (`tests/v2/kit/RULINGS.md` K-1, K-2).
(a) The merged row's `n_users` is the largest constituent count (an exact lower bound on distinct users
when rows share people), so small-only merges always take a complementary row.
(b) `RESCOPE_LEVELS`: keep {team, cost_center, lane_kind, billing_class} → {team, cost_center,
billing_class} → {cost_center, billing_class} → {billing_class}; person/session dimensions always
force re-scoping for the org audience; `self` findings pass through.
(c) `aggregate` findings with no person or API-key dimension pass through like R-E1; API-key-scoped
ones below k are re-scoped to their workspace (§8.5). This goes beyond R-E1's "every other finding
follows §8.4" and needs an explicit ruling.
(d) Review additions: complementary suppression for findings (a re-scoped merge absorbs the published
finding at the parent scope, else the smallest published sibling of the same detector and kind), and
scrubbing of dropped scope values from the generated text of re-scoped findings.

**Proposal.** Record (a)–(d) in SPEC Appendix E as rulings (R-E5…), or give `publish` an optional
`count_users`-style exact recount for merged rows if sums over disjoint dimensions (e.g. `team`) should
be allowed. Affected: F-KIT only (callers: STORE, OUT, pipeline).
