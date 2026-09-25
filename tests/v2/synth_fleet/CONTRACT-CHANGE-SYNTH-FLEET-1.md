# CONTRACT-CHANGE-SYNTH-FLEET-1 — trace@2 `blocks` records and position-dependent `lookback_pos`

**Status:** advisory (affects SPEC §4.2 / TRACE's codec, not `tokenbill/core`); SYNTH-FLEET is
implemented against the current text and does not depend on a change.

## What

SPEC §4.2 delta-encodes fingerprints: a `blocks` record defines each `BlockRef` once (keyed by its
hash `h`), and a request lists `append` hashes after keeping `keep` blocks of its parent. But
`BlockRef.lookback_pos` (§3.2) is a function of the block's **position** in the request (tool_use /
tool_result runs collapsed), not of its content. When a block keeps its hash but moves — e.g. context
editing that removes earlier blocks, or a tool-result run that merges with a neighbour — the same `h`
needs two different `lookback_pos` values, which one `blocks` definition per hash cannot express.

SYNTH-FLEET hit this in its own round-trip test (`test_writers.py::
test_trace_v2_fingerprint_file_round_trips`) and avoided it by modelling context edits the way the API
does them: cleared tool results are replaced **in place** by a short placeholder block, so no kept
block moves.

## Why it matters

A recorder that fingerprints a request after a history rewrite (or any client that drops blocks)
would either write a `blocks` record whose `lookback_pos` is wrong for later requests, or have to emit
duplicate definitions of one hash. BLOCK's lookback-overflow breaker reads `lookback_pos`.

## Proposed resolution (either)

1. Readers recompute `lookback_pos` from the reconstructed block list
   (`adapters.fingerprint.lookback_positions(blocks)`, already in TRACE's API) and writers may omit it
   from `blocks` records (or it is ignored on read); or
2. the `fp` object carries `"lookback": [...]` positions for the appended blocks.

Option 1 needs no schema change beyond documenting that `lookback_pos` in a `blocks` record is
informational.
