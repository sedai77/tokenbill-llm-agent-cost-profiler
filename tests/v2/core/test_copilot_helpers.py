"""GitHub Copilot helpers (CORE-AMENDMENTS C-19 … C-23): credit / nano-AIU money, ``figure_json``
and ``combine_weakest``, exact JSON, natural and Copilot ids, Copilot model normalization and the
OTel resource predicate."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import ids, jsonl, labels, models, money
from tokenbill.core.errors import ContractViolation, SourceError
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality

# ---------- C-19 money ----------


def test_credit_money_acceptance_values() -> None:
    assert money.NANO_USD_PER_CREDIT == 10_000_000 and money.NANO_AIU_PER_CREDIT == 10**9
    assert money.credits_str_to_nano("42.726213") == (427262130, 0)
    assert money.nano_aiu_to_nano(23_284_800_000) == (232_848_000, 0)
    # Appendix C.G17: the float tail of the report's gross amount is a remainder, not money
    assert money.usd_str_to_nano("0.4272621300000001") == (427_262_130, Decimal("1E-16"))
    assert money.credits_str_to_nano("12.5") == (125_000_000, 0)
    assert money.credits_str_to_nano("0.00000001") == (0, Decimal("1E-10"))
    nano, rem = money.nano_aiu_to_nano(150)   # 1.5 nano → 2 (half-even), remainder −0.5 nano
    assert nano == 2 and rem == Decimal("-5E-10")
    assert money.nano_aiu_to_nano(250) == (2, Decimal("5E-10"))
    assert money.nano_aiu_to_nano(-150)[0] == -2
    assert money.nano_to_credits_str(427262130) == "42.726213"
    assert money.nano_to_credits_str(10_000_000) == "1"
    assert money.nano_to_credits_str(0) == "0"
    assert money.nano_to_credits_str(-5) == "-0.0000005"
    for bad in (1.5, True, None):
        with pytest.raises(TypeError):
            money.nano_aiu_to_nano(bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            money.nano_to_credits_str(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        money.credits_str_to_nano(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        money.credits_str_to_nano("NaN")
    with pytest.raises(ValueError):
        money.credits_str_to_nano("twelve")


@given(st.integers(-(10**15), 10**15))
@settings(max_examples=300, deadline=None)
def test_credits_round_trip(nano: int) -> None:
    text = money.nano_to_credits_str(nano)
    assert money.credits_str_to_nano(text) == (nano, 0)


@given(st.integers(-(10**18), 10**18))
@settings(max_examples=300, deadline=None)
def test_nano_aiu_remainder_is_exact(n: int) -> None:
    nano, rem = money.nano_aiu_to_nano(n)
    # n nano-AIU = n / 100 nano-USD exactly = nano + rem (rem in USD)
    assert Decimal(nano) + rem * Decimal(10**9) == Decimal(n) / Decimal(100)
    assert abs(rem) <= Decimal("5E-10")


# ---------- C-20 labels ----------


def test_figure_json() -> None:
    fig = labels.estimated(1_500_000, Basis.LIST, low=1_000_000, high=2_000_000,
                           note="range", provenance=("r1",))
    assert labels.figure_json(fig) == {
        "usd": "0.0015", "nano": 1_500_000, "evidence": "estimated", "basis": "list",
        "finality": "n/a",
        "range": {"low_usd": "0.001", "low_nano": 1_000_000, "high_usd": "0.002",
                  "high_nano": 2_000_000},
        "ci_level_pct": None, "calibration": "uncalibrated", "upper_bound": False,
        "provenance": ["r1"], "note": "range",
    }
    un = labels.figure_json(labels.unpriced("no rate", Basis.LIST_EQUIVALENT))
    assert un["usd"] is None and un["nano"] is None and un["range"] is None
    assert un["basis"] == "list_equivalent"
    assert labels.figure_json(labels.exact(2 * 10**9, Basis.INVOICE))["usd"] == "2"
    json.dumps(labels.figure_json(fig))  # no float anywhere
    with pytest.raises(ContractViolation):
        labels.figure_json(5)  # type: ignore[arg-type]


def _inv(n: int) -> Figure:
    return labels.exact(n, Basis.INVOICE, finality=Finality.FINAL, provenance=(f"inv{n}",))


def test_combine_weakest() -> None:
    total = labels.combine_weakest([_inv(100), _inv(200)], note="total.invoice")
    assert (total.nano, total.evidence, total.basis, total.finality) == (
        300, Evidence.EXACT, Basis.INVOICE, Finality.FINAL)
    assert total.provenance == ("inv100", "inv200") and total.note == "total.invoice"
    mixed = labels.combine_weakest([_inv(100), labels.exact(50, Basis.LIST)], note="total")
    assert (mixed.nano, mixed.evidence, mixed.basis) == (150, Evidence.EXACT, Basis.LIST)
    assert "non-invoice: input 1 (list, exact)" in mixed.note
    est = labels.combine_weakest(
        [_inv(100), labels.estimated(40, Basis.LIST, low=30, high=60, note="seat fees")],
        note="total")
    assert (est.nano, est.low_nano, est.high_nano, est.evidence, est.basis) == (
        140, 130, 160, Evidence.ESTIMATED, Basis.LIST)
    assert est.calibration is Calibration.UNCALIBRATED
    point_est = labels.combine_weakest(
        [labels.estimated(7, Basis.LIST, note="x", upper_bound=True)], note="")
    assert (point_est.low_nano, point_est.high_nano, point_est.upper_bound) == (7, 7, True)
    assert point_est.note == "non-invoice: input 0 (list, estimated)"
    measured = Figure(nano=10, evidence=Evidence.MEASURED, basis=Basis.LIST, low_nano=5,
                      high_nano=15, ci_level_pct=95)
    m = labels.combine_weakest([measured, _inv(1)], note="n")
    assert m.evidence is Evidence.ESTIMATED and m.ci_level_pct is None and (m.low_nano,
                                                                            m.high_nano) == (6, 16)
    provisional = labels.combine_weakest(
        [_inv(1), labels.exact(1, Basis.INVOICE, finality=Finality.PROVISIONAL)], note="n")
    assert provisional.finality is Finality.PROVISIONAL
    unp = labels.combine_weakest([_inv(1), labels.unpriced("seat price unknown")], note="total")
    assert unp.nano is None and unp.note.startswith("unpriced:") and unp.basis is Basis.LIST
    unp2 = labels.combine_weakest(
        [labels.estimated(None, Basis.LIST, note="unpriced: x"), _inv(1)], note="")
    assert unp2.nano is None and unp2.evidence is Evidence.ESTIMATED
    for figs in ([_inv(1), labels.exact(1, Basis.LIST_EQUIVALENT)],
                 [labels.exact(1, Basis.PROVIDER_ESTIMATE)], [], [5]):
        with pytest.raises(ContractViolation):
            labels.combine_weakest(figs, note="x")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        labels.combine_weakest([_inv(1)], note=5)  # type: ignore[arg-type]


# ---------- C-21 jsonl ----------


def test_parse_json_line_exact_numbers() -> None:
    raw = b'{"ai_credits_used": 12.5, "n": 3, "e": 1e400, "tiny": 0.1}'
    exact_doc = jsonl.parse_json_line(raw, exact_numbers=True)
    assert exact_doc == {"ai_credits_used": Decimal("12.5"), "n": 3, "e": Decimal("1e400"),
                         "tiny": Decimal("0.1")}
    assert type(exact_doc["n"]) is int
    assert jsonl.parse_json_line(raw) is None  # the default path refuses the overflowing float
    assert jsonl.parse_json_line(b'{"x": 0.1}') == {"x": 0.1}  # default path unchanged
    assert jsonl.parse_json_line(b'{"x": NaN}', exact_numbers=True) is None
    assert jsonl.parse_json_line(b"[1.5]", exact_numbers=True) is None
    assert jsonl.parse_json_line(b"", exact_numbers=True) is None


def test_load_json_exact(tmp_path: Path) -> None:
    plain = tmp_path / "page.json"
    plain.write_bytes(b'\xef\xbb\xbf{"usageItems": [{"netAmount": 1.4272621300000001, "q": 7}]}')
    doc = jsonl.load_json_exact(plain)
    assert doc == {"usageItems": [{"netAmount": Decimal("1.4272621300000001"), "q": 7}]}
    gz = tmp_path / "page.json.gz"
    gz.write_bytes(gzip.compress(b"[1.25, 2]"))
    assert jsonl.load_json_exact(gz) == [Decimal("1.25"), 2]
    for name, data in (("nan.json", b'{"x": NaN}'), ("bad.json", b"{"), ("utf.json", b"\xff"),
                       ("sur.json", b'"\\ud800"'), ("deep.json", b"[" * 100_000)):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(SourceError):
            jsonl.load_json_exact(path)
    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b"1," * 50 + b"1]")
    with pytest.raises(SourceError):
        jsonl.load_json_exact(big, max_bytes=10)
    assert jsonl.load_json_exact(big, max_bytes=200)[0] == 1
    with pytest.raises(SourceError):
        jsonl.load_json_exact(tmp_path / "missing.json")
    corrupt = tmp_path / "corrupt.json.gz"
    corrupt.write_bytes(b"\x1f\x8b\x08\x00garbage")
    with pytest.raises(SourceError):
        jsonl.load_json_exact(corrupt)
    with pytest.raises(ValueError):
        jsonl.load_json_exact(plain, max_bytes=-1)


@pytest.mark.parametrize("depth", [300, 500, 900, 990, 2_000, 20_000])
def test_deep_nesting_with_surrogate_escapes_never_raises(tmp_path: Path, depth: int) -> None:
    """The unpaired-surrogate scan is iterative: a document nested as deeply as the JSON decoder
    accepts gives None / SourceError (lone surrogate) or parses (paired), never RecursionError."""
    lone = '{"a":' + "[" * depth + '"\\ud800"' + "]" * depth + "}"
    paired = '{"a":' + "[" * depth + '"\\ud83d\\ude00"' + "]" * depth + "}"
    for exact in (False, True):
        assert jsonl.parse_json_line(lone.encode(), exact_numbers=exact) is None
        got = jsonl.parse_json_line(paired.encode(), exact_numbers=exact)
        assert got is None or isinstance(got, dict)  # None only when the decoder refuses depth
    path = tmp_path / "deep.json"
    path.write_text(lone, encoding="utf-8")
    with pytest.raises(SourceError):
        jsonl.load_json_exact(path)
    path.write_text(paired, encoding="utf-8")
    try:
        doc = jsonl.load_json_exact(path)
    except SourceError:  # the decoder itself refused the depth
        return
    assert isinstance(doc, dict) and "a" in doc


@given(st.binary(max_size=200))
@settings(max_examples=300, deadline=None)
def test_exact_parsers_fuzz(blob: bytes) -> None:
    got = jsonl.parse_json_line(blob, exact_numbers=True)
    assert got is None or isinstance(got, dict)


@given(st.binary(max_size=120) | st.text(max_size=60).map(lambda t: t.encode("utf-8")))
@settings(max_examples=200, deadline=None)
def test_load_json_exact_fuzz(tmp_path_factory: pytest.TempPathFactory, blob: bytes) -> None:
    """Hostile files load or raise SourceError — nothing else escapes."""
    path = tmp_path_factory.mktemp("f") / "x.json"
    path.write_bytes(blob)
    try:
        jsonl.load_json_exact(path)
    except SourceError:
        pass


_exact_values = st.recursive(
    st.none() | st.booleans() | st.integers()
    | st.decimals(min_value=-(10**9), max_value=10**9, places=8)
    | st.text(max_size=5),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=4), c, max_size=3),
    max_leaves=10,
)


@given(_exact_values)
@settings(max_examples=200, deadline=None)
def test_load_json_exact_round_trip(tmp_path_factory: pytest.TempPathFactory,
                                    value: object) -> None:
    path = tmp_path_factory.mktemp("j") / "v.json"
    path.write_text(_dump_exact(value), encoding="utf-8")
    assert _normalize(jsonl.load_json_exact(path)) == _normalize(value)


def _has_decimal(v: object) -> bool:
    if isinstance(v, Decimal):
        return True
    if isinstance(v, list):
        return any(_has_decimal(x) for x in v)
    if isinstance(v, dict):
        return any(_has_decimal(x) for x in v.values())
    return False


def _dump_exact(v: object) -> str:
    if isinstance(v, Decimal):
        text = format(v, "f") if v == v.to_integral_value() else str(v)
        return text if any(c in text for c in ".eE") else text + ".0"
    if isinstance(v, list):
        return "[" + ",".join(_dump_exact(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(k) + ":" + _dump_exact(x) for k, x in v.items()) + "}"
    return json.dumps(v)


def _normalize(v: object) -> object:
    if isinstance(v, Decimal):
        return ("d", v.normalize() if v else Decimal(0))
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    if isinstance(v, dict):
        return {k: _normalize(x) for k, x in v.items()}
    return v


# ---------- C-22 ids ----------


def test_natural_and_copilot_ids() -> None:
    a = ids.natural_id("cl", "github.ai_usage_report", "2026-09-10", None, "claude-opus-5-5", 3)
    assert a == ids.stable_id("cl", "github.ai_usage_report", "2026-09-10", "", "claude-opus-5-5",
                              3)
    assert a.startswith("cl_") and len(a) == 27
    assert a != ids.natural_id("cl", "github.metered_usage", "2026-09-10", None,
                               "claude-opus-5-5", 3)
    ses = ids.copilot_session_key("0f4a…session")
    assert ses == ids.stable_id("ses", "github_copilot", "0f4a…session")
    assert ses == ids.copilot_session_key("0f4a…session")  # stable across calls / sources
    lane = ids.copilot_lane_key(ses, "main", None)
    assert lane == ids.stable_id("ln", ses, "main", "")
    assert lane == ids.copilot_lane_key(ses, "main", "")
    assert lane != ids.copilot_lane_key(ses, "subagent", "agent-1")
    # pinned values: other packages (CP-LOCAL, CP-OTEL, CP-VSCODE) must reproduce them
    assert ids.copilot_session_key("abc") == ids.stable_id("ses", "github_copilot", "abc")


# ---------- C-23 models ----------

CA20_TABLE = [
    ("Claude Opus 5.5", "claude-opus-5-5", "direct", "standard", None),
    ("Auto: Claude Haiku 4.5", "claude-haiku-4-5", "auto", "standard", None),
    ("claude-opus-4.7-1m-internal", "claude-opus-4-7", "direct", "standard", None),
    ("Claude Opus 4.8 (fast mode) (preview)", "claude-opus-4-8", "direct", "fast", None),
    ("GPT-5.6 Sol", "gpt-5.6-sol", "direct", "standard", None),
    ("'GPT-5.4[^2]'", "gpt-5.4", "direct", "standard", None),
    ("GPT-5.3-Codex", "gpt-5.3-codex", "direct", "standard", None),
    ("Gemini 3.8 Flash", "gemini-3.8-flash", "direct", "standard", None),
    ("MAI-Code-1.1-Flash", "mai-code-1.1-flash", "direct", "standard", None),
    ("Kimi K2.7 Code", "kimi-k2.7-code", "direct", "standard", None),
    ("claude-sonnet-4", "claude-sonnet-4", "direct", "standard", None),
    ("gpt-4o-mini-2024-07-18", "gpt-4o-mini-2024-07-18", "direct", "standard", None),
    ("auto", "", "auto", "standard", "auto_unattributed"),
    ("AUTO:", "", "auto", "standard", "auto_unattributed"),
    ("unknown", "", "unknown", "standard", "unknown"),
    ("Others", "", "unknown", "standard", "unknown"),
    ("Copilot code review", "", "direct", "standard", "code_review"),
    ("Auto: Copilot Coding Agent", "", "auto", "standard", "cloud_agent"),
    ("padawan", "", "direct", "standard", "cloud_agent"),
    ("Cloud agent", "", "direct", "standard", "cloud_agent"),
    ("  Claude  Sonnet 4.6  ", "claude-sonnet-4-6", "direct", "standard", None),
    ("claude-sonnet-4.6", "claude-sonnet-4-6", "direct", "standard", None),
    ('"GPT-5 mini"', "gpt-5-mini", "direct", "standard", None),
    ("Claude Opus 4.8 (Fast Mode)", "claude-opus-4-8", "direct", "fast", None),
    ("claude-opus-4-7-internal", "claude-opus-4-7", "direct", "standard", None),
    ("claude--opus-5", "claude-opus-5", "direct", "standard", None),
]


@pytest.mark.parametrize("raw,model,routing,speed,pseudo", CA20_TABLE)
def test_normalize_copilot_model_table(raw: str, model: str, routing: str, speed: str,
                                       pseudo: str | None) -> None:
    cm = models.normalize_copilot_model(raw)
    assert (cm.model, cm.routing, cm.speed, cm.pseudo) == (model, routing, speed, pseudo)


def test_normalize_copilot_model_suffix_and_delegation() -> None:
    assert models.normalize_copilot_model("claude-opus-4.7-1m-internal").suffix_stripped == (
        "-1m-internal")
    assert models.normalize_copilot_model("claude-opus-4-7-1m").suffix_stripped == "-1m"
    assert models.normalize_copilot_model("Claude Opus 5.5").suffix_stripped is None
    assert models.normalize_copilot_model("-1m").model == "-1m"  # nothing left to strip to
    assert models.normalize_copilot_model(None).model == ""  # type: ignore[arg-type]
    mid = models.normalize_model("Auto: Claude Haiku 4.5", "github")
    assert mid == models.ModelId("claude-haiku-4-5", None, "unknown", None)
    assert models.normalize_model("Copilot code review", "github") == models.ModelId(
        "", None, "unknown", "copilot pseudo model")
    assert models.normalize_model("auto", "github").reason == "copilot pseudo model"
    assert models.normalize_model("'[^1]'", "github").reason == "empty"
    assert models.normalize_model("", "github").reason == "empty"
    # other hints unchanged: "auto" is not a Claude Code alias, bedrock ids still parse
    assert models.normalize_model("anthropic.claude-opus-5-5").channel_hint == "bedrock"
    # the Claude Code config aliases do not apply to Copilot labels
    assert models.normalize_model("opus", "github").model == "opus"
    assert models.normalize_model("opus").reason == "config alias"


@given(st.text(max_size=40))
@settings(max_examples=400, deadline=None)
def test_normalize_copilot_model_fuzz(raw: str) -> None:
    cm = models.normalize_copilot_model(raw)
    assert cm.routing in ("direct", "auto", "unknown") and cm.speed in ("standard", "fast")
    assert cm.pseudo in (None, "code_review", "cloud_agent", "auto_unattributed", "unknown")
    assert cm.model == cm.model.strip() and " " not in cm.model and "--" not in cm.model
    assert (cm.pseudo is None) or cm.model == ""
    if cm.model.startswith("claude-"):
        assert "." not in cm.model
    assert models.normalize_copilot_model(raw) == cm  # deterministic


def test_is_copilot_resource_truth_table() -> None:
    f = models.is_copilot_resource
    assert f("github-copilot", [], []) and f("copilot-chat", (), ())
    assert not f("claude-code", [], []) and not f(None, [], [])
    assert f("acme-copilot", [], [], extra_service_names=["acme-copilot"])
    assert f("jb", [], [], extra_service_names=["jb=copilot_jetbrains"])  # NAME part only
    assert not f("jb=copilot_jetbrains", [], [], extra_service_names=["jb=copilot_jetbrains"])
    assert not f("x", [], [], extra_service_names=["", "=copilot_other"])
    assert f("svc", ["github.copilot.chat"], [])
    assert f("svc", ["github.copilot"], [])
    assert not f("svc", ["io.opentelemetry.github"], [])
    assert f("svc", [], ["gen_ai.usage.input_tokens", "github.copilot.nano_aiu"])
    assert f("svc", [], ["copilot_chat.copilot_usage_nano_aiu"])
    assert not f("svc", [], ["gen_ai.usage.input_tokens", "claude_code.cost", "copilot_chatx"])
    assert not f("svc", [None], [None])  # type: ignore[list-item]
    assert models.COPILOT_SERVICE_NAMES == {"github-copilot", "copilot-chat"}
