# tests/v2/golden — v0.1.2 golden outputs (SPEC §16.2)

Captured by F-CORE with `scripts/capture_goldens.py` from the untouched v0.1.2 tree, before any v0.2 code
existed. Every `*.stdout.txt` / `*.report.html` is the exact output of the in-process CLI with two
substitutions (`capture_goldens.normalize`): the version string → `{{TOKENBILL_VERSION}}` and the report
date → `{{REPORT_DATE}}` (the capture pins the date to a sentinel so nothing else can be replaced).

`manifest.json` lists, per case, the argv (run from a scratch directory that holds the inputs; `-o` writes
`report.html` there), the exit code, the golden file names and their sha256, plus the sha256 of each
analyze input (the four demo scenarios, seed 7, written with the frozen `trace.write_trace`).

The byte-identical comparison against the v0.2 CLI is owned by CLI-LEDGER: run each argv, apply
`normalize(text, version=__version__, report_date=date.today().isoformat())`, compare with the file.
Do not edit these files; re-capture only from a v0.1.2 checkout (`--check` verifies determinism).

`.gitattributes` (`* -text`) keeps these files byte-identical on checkouts with `core.autocrlf=true`
(the default on Windows runners); without it Git rewrites every LF as CRLF and the pinned sha256 and
the byte comparison fail. Read goldens as bytes (or with `newline=""`), never with newline translation.
