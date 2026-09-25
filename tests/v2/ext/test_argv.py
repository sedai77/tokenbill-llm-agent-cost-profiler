"""``rewrite_argv`` (addendum DC23, §15; E-1 multi-token targets): the brief's table on the shipped
Copilot aliases, alias precedence, and hypothesis properties of the argv rewriter."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tokenbill.core import extensions as ext
from tokenbill.core.registry import ArgvAlias

from .support import COPILOT, bare_spec, fake_spec

Install = Callable[..., None]

#: (argv, expected) on the shipped registry (the Copilot extension only).
TABLE = [
    (["scan", "--copilot", "--since", "X"], ["copilot", "scan", "--since", "X"]),
    (["scan", "--since", "X", "--copilot"], ["copilot", "scan", "--since", "X"]),
    (["collect", "copilot-cli", "--out", "D"],
     ["copilot", "collect", "--source", "cli", "--out", "D"]),
    (["collect", "copilot-vscode", "--out", "D"],
     ["copilot", "collect", "--source", "vscode", "--out", "D"]),
    (["me", "--copilot"], ["copilot", "me"]),
    (["me", "--copilot", "--since", "2026-09-01"], ["copilot", "me", "--since", "2026-09-01"]),
    (["collect", "claude-code"], ["collect", "claude-code"]),
    (["collect", "claude-code", "--out", "copilot-cli"],
     ["collect", "claude-code", "--out", "copilot-cli"]),   # "first" needs argv[1]
    (["scan", "--org"], ["scan", "--org"]),
    (["scan", "--copilot=1"], ["scan", "--copilot=1"]),       # not inside --flag=value
    (["scan", "--", "--copilot"], ["scan", "--", "--copilot"]),   # after end-of-options
    (["scan", "--copilot", "--", "--copilot"], ["copilot", "scan", "--", "--copilot"]),
    (["scan", "--copilot", "--copilot"], ["copilot", "scan"]),    # every occurrence removed
    (["findings", "--copilot"], ["findings", "--copilot"]),      # no alias for the verb
    (["--copilot", "scan"], ["--copilot", "scan"]),              # the verb comes first
    (["copilot", "scan"], ["copilot", "scan"]),
    ([], []),
]


@pytest.mark.parametrize(("argv", "expected"), TABLE)
def test_brief_table_on_the_shipped_aliases(argv: list[str], expected: list[str]) -> None:
    before = list(argv)
    assert ext.rewrite_argv(argv) == expected
    assert argv == before  # the input is never mutated
    assert ext.rewrite_argv(tuple(argv)) == expected  # any sequence


def test_result_is_a_new_list() -> None:
    argv = ["collect", "claude-code"]
    out = ext.rewrite_argv(argv)
    assert out == argv and out is not argv


def test_first_match_wins_in_extension_name_order(install: Install) -> None:
    install(fake_spec(argv_aliases=(ArgvAlias("scan", "--copilot", "any", ("fake", "scan")),)),
            COPILOT)
    # "copilot" < "fake": the shipped alias is tried first
    assert ext.rewrite_argv(["scan", "--copilot"]) == ["copilot", "scan"]
    install(fake_spec(argv_aliases=(ArgvAlias("scan", "--copilot", "any", ("fake", "scan")),)),
            bare_spec("aaa", argv_aliases=(ArgvAlias("scan", "--copilot", "any",
                                                     ("aaa", "scan", "--x")),)))
    assert ext.rewrite_argv(["scan", "--copilot", "-v"]) == ["aaa", "scan", "--x", "-v"]


def test_declared_order_within_an_extension(install: Install) -> None:
    install(fake_spec(argv_aliases=(
        ArgvAlias("collect", "--fake", "any", ("fake", "collect", "--any")),
        ArgvAlias("collect", "--fake", "first", ("fake", "collect", "--first")))))
    assert ext.rewrite_argv(["collect", "--fake"]) == ["fake", "collect", "--any"]


def test_no_extensions_no_rewrite(install: Install) -> None:
    install()
    assert ext.rewrite_argv(["scan", "--copilot"]) == ["scan", "--copilot"]


# ---------- hypothesis ----------

_ALIAS_VERBS = {a.verb for a in COPILOT.argv_aliases}
_TRIGGERS = [a.trigger for a in COPILOT.argv_aliases]
_TOKENS = st.one_of(
    st.sampled_from(["--copilot", "copilot-cli", "copilot-vscode", "--", "--org", "-v", "scan",
                     "me", "collect", "--copilot=1", "claude-code", "--since", "2026-09-01"]),
    st.text(max_size=12))


@given(st.lists(_TOKENS, max_size=10))
def test_never_raises_and_is_idempotent(argv: list[str]) -> None:
    once = ext.rewrite_argv(argv)
    assert isinstance(once, list) and all(isinstance(t, str) for t in once)
    assert ext.rewrite_argv(once) == once  # "copilot" is not an alias verb


@given(st.lists(_TOKENS, max_size=10))
def test_unmatched_argv_is_unchanged(argv: list[str]) -> None:
    out = ext.rewrite_argv(argv)
    if not argv or argv[0] not in _ALIAS_VERBS:
        assert out == argv


@given(st.sampled_from(sorted(_ALIAS_VERBS)), st.lists(_TOKENS, max_size=8))
def test_rewrite_keeps_every_other_token_in_order(verb: str, rest: list[str]) -> None:
    argv = [verb, *rest]
    out = ext.rewrite_argv(argv)
    if out == argv:
        return
    alias = next(a for a in COPILOT.argv_aliases
                 if a.verb == verb and out[:len(a.target)] == list(a.target)
                 and (a.position == "any" or rest[:1] == [a.trigger]))
    tail = out[len(alias.target):]
    end = rest.index("--") if "--" in rest else len(rest)
    if alias.position == "first":
        assert tail == rest[1:]
    else:
        assert tail == [t for t in rest[:end] if t != alias.trigger] + rest[end:]
        assert alias.trigger not in tail[:tail.index("--") if "--" in tail else len(tail)]
    assert alias.trigger in _TRIGGERS
