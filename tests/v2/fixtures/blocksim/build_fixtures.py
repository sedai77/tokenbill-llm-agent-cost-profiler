"""Rebuild the BLOCK codebase-experiment fixtures (stand-alone script, not a test module).

    python tests/v2/fixtures/blocksim/build_fixtures.py

Writes ``<experiment>.jsonl`` next to this file from ``tests/v2/blocksim/exps.py`` (deterministic:
fixed word lists, fixed timestamps, the §5.8 test fingerprinter with a fixed key).
``tests/v2/blocksim/test_fixtures.py`` fails when a file is not what this script would write.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))

from tests.v2.blocksim.exps import EXPERIMENTS, dump  # noqa: E402


def main() -> None:
    for name, build in EXPERIMENTS.items():
        (HERE / f"{name}.jsonl").write_text(dump(name, build()), encoding="utf-8")
        print(f"wrote {name}.jsonl")


if __name__ == "__main__":
    main()
