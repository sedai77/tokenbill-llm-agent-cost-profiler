"""The checked-in experiment fixtures are current, deterministic and content-free."""

from __future__ import annotations

import pytest

from tests.v2.blocksim.exps import EXPERIMENTS, FIXTURE_DIR, dump, load
from tokenbill.core.builders import CANARY


@pytest.mark.parametrize("name", sorted(EXPERIMENTS))
def test_fixture_file_is_what_the_build_script_writes(name: str) -> None:
    built = EXPERIMENTS[name]()
    text = (FIXTURE_DIR / f"{name}.jsonl").read_text(encoding="utf-8")
    assert text == dump(name, built), (
        f"{name}.jsonl is stale: run python tests/v2/fixtures/blocksim/build_fixtures.py")
    assert load(name) == built
    assert dump(name, EXPERIMENTS[name]()) == text              # deterministic rebuild


def test_fixtures_carry_no_content() -> None:
    for path in sorted(FIXTURE_DIR.glob("*.jsonl")):
        text = path.read_text(encoding="utf-8")
        assert CANARY not in text
        # the generated filler words never appear: only HMAC hashes and sizes are stored
        assert "archive" not in text and "planner" not in text and "journal" not in text
