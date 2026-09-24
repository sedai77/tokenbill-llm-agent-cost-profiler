"""``load_team_map`` (``--team-map FILE`` → ``IngestOptions.team_map``), with a hypothesis fuzz:
only ``UsageError`` escapes and no reference or team is echoed in an error."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import UsageError
from tokenbill.pipeline.common import load_team_map


def test_valid_map(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    path.write_text(json.dumps({"zoe@example.com": " search ", "adam": "payments"}))
    assert load_team_map(path) == (("adam", "payments"), ("zoe@example.com", "search"))
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert load_team_map(empty) == ()


@pytest.mark.parametrize("text", ['["a"]', '{"a": 1}', '{"a": ""}', '{"": "t"}',
                                  '{"a": "' + "x" * 129 + '"}', '{"a": "t\\u0000"}', "{", ""])
def test_malformed_maps(tmp_path: Path, text: str) -> None:
    path = tmp_path / "teams.json"
    path.write_text(text)
    with pytest.raises(UsageError, match="team map"):
        load_team_map(path)


def test_missing_file_and_no_echo(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="team map"):
        load_team_map(tmp_path / "missing.json")
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"secret-ref-7": 5}))
    with pytest.raises(UsageError) as info:
        load_team_map(path)
    assert "secret-ref-7" not in str(info.value)


_DIR = Path(tempfile.mkdtemp(prefix="tb-wiring-teammap-"))


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.binary(max_size=120),
                 st.dictionaries(st.text(max_size=10),
                                 st.one_of(st.text(max_size=140), st.integers(), st.none()),
                                 max_size=5).map(lambda d: json.dumps(d).encode())))
def test_fuzz_only_usage_errors(raw: bytes) -> None:
    path = _DIR / "fuzz.json"
    path.write_bytes(raw)
    try:
        pairs = load_team_map(path)
    except UsageError:
        return
    assert all(isinstance(k, str) and isinstance(v, str) and v and len(v) <= 128
               for k, v in pairs)
    assert list(pairs) == sorted(pairs)
