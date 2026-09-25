"""HTML report (SPEC §14.3, §8.7): CSP, no scripts or external URLs, table twins, contrast, size."""

from __future__ import annotations

import dataclasses
import re
from html.parser import HTMLParser

import pytest

from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import published_for_tests
from tokenbill.core.types import AggRow, BillSummary, RawAggregate
from tokenbill.outputs import html as H

from . import extension
from .sample import T0, T1, empty_result, full_result, priced

URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
PSEUDONYM_RE = re.compile(r"\b[pc]_[0-9a-f]{20}\b")


class _Tags(HTMLParser):
    """Records the sequence of start tags and checks nesting balance of the elements we emit."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[tuple[str, dict]] = []
        self.stack: list[str] = []
        self.void = {"meta", "br", "rect", "link", "img"}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.starts.append((tag, dict(attrs)))
        if tag not in self.void:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.starts.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack[-1] == tag, (tag, self.stack[-5:])
        self.stack.pop()


def parse(doc: str) -> _Tags:
    p = _Tags()
    p.feed(doc)
    p.close()
    assert p.stack == []
    return p


def test_page_is_self_contained_and_safe() -> None:
    doc = H.render_html(full_result())
    tags = parse(doc)
    metas = [a for t, a in tags.starts if t == "meta" and a.get("http-equiv")]
    assert metas == [{"http-equiv": "Content-Security-Policy", "content": H.CSP}]
    assert H.CSP == "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    assert "<script" not in doc.lower() and "javascript:" not in doc.lower()
    assert not URL_RE.search(doc)
    assert not [a for t, a in tags.starts if "src" in a or t in ("link", "iframe", "img")]
    assert not PSEUDONYM_RE.search(doc)
    assert "prefers-color-scheme: dark" in doc


def test_every_chart_is_followed_by_a_captioned_table() -> None:
    doc = H.render_html(full_result())
    tags = [t for t, _a in parse(doc).starts]
    svgs = [i for i, t in enumerate(tags) if t == "svg"]
    assert svgs
    for i in svgs:
        after = [t for t in tags[i + 1:] if t not in ("title", "text", "rect")]
        assert after[:2] == ["table", "caption"], after[:4]
    assert doc.count("<table>") == doc.count("<caption>")


def test_sections_and_labels() -> None:
    doc = H.render_html(full_result())
    for section in ("legend", "bill", "where", "findings", "plan", "calibration",
                    "reconciliation", "policy", "whatif", "measure-plan", "measurements", "check",
                    "pricing", "data-quality", "notes", "methodology"):
        assert f'<section id="{section}">' in doc, section
    assert "exact·list" in doc and "allowance·list-equivalent (not billed)" in doc
    assert "Copilot credits seen by collectors" in doc
    assert "users unknown" in doc
    assert "Context tax:" in doc
    assert "Synthetic demo data" in doc
    assert "code.claude.com/docs/en/costs" in doc        # sources shown, without a scheme
    assert "Threats to validity" in doc
    assert "not reconciled" in doc                       # per-channel badge in words


def test_text_is_escaped() -> None:
    r = full_result(notes=("<b>bold</b> & \x1b[2Jansi",))
    doc = H.render_html(r)
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; ansi" in doc
    assert "\x1b" not in doc


def test_pseudonyms_never_reach_the_page() -> None:
    f = dataclasses.replace(full_result().findings[0], title="p_0123456789abcdef0123 lane",
                            summary="c_fedcba9876543210fedc too")
    doc = H.render_html(full_result(findings=(f,)))
    assert not PSEUDONYM_RE.search(doc)


def _luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_wcag_aa_contrast(theme: str) -> None:
    pal = H.PALETTES[theme]
    for surface in pal["surface"].values():
        for name, color in pal["text"].items():
            assert contrast(color, surface) >= 4.5, (theme, name, surface)
    for name, color in pal["marks"].items():
        assert contrast(color, pal["surface"]["bg"]) >= 3.0, (theme, name)
    doc = H.render_html(empty_result())
    for color in [*pal["text"].values(), *pal["marks"].values()]:
        assert color in doc


def test_size_budget_for_ten_thousand_rows() -> None:
    rows = tuple(AggRow(dims=(("team", f"team-{i:05d}"),), n_users=5, n_requests=100,
                        usage=UsageBuckets(uncached_input=1000, output=100),
                        priced=priced(f"{i}.25")) for i in range(10_000))
    agg = published_for_tests(RawAggregate(group_by=("team",), rows=rows, window=(T0, T1)))
    r = full_result()
    many = tuple(dataclasses.replace(r.findings[0], finding_id=f"f_{i:05d}")
                 for i in range(5000))
    big = dataclasses.replace(r, findings=many,
                              bill=BillSummary(total=r.bill.total, esr=None,
                                               breakdowns=(("team", agg),)))
    doc = H.render_html(big)
    assert len(doc.encode("utf-8")) <= H.MAX_BYTES
    assert "9,950 more rows in the JSON output" in doc
    assert "4,950 more findings in the JSON output" in doc


def test_billed_places_refuse_non_billed_figures() -> None:
    r = full_result()
    for bad in (estimated(5, Basis.LIST, note="x"), exact(5, Basis.LIST_EQUIVALENT)):
        total = dataclasses.replace(r.bill.total, exact=bad)
        with pytest.raises(ContractViolation):
            H.render_html(dataclasses.replace(r, bill=dataclasses.replace(r.bill, total=total)))
    raw = RawAggregate(group_by=("team",), rows=(), window=(0, 1))
    with pytest.raises(ContractViolation):
        H.render_html(dataclasses.replace(r, bill=BillSummary(
            total=r.bill.total, esr=None, breakdowns=(("team", raw),))))  # type: ignore
    with pytest.raises(ContractViolation):
        H.render_html("x")  # type: ignore[arg-type]


def test_extension_sections_are_included_and_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    extension.install(monkeypatch)
    r = full_result(copilot=extension.summary())
    assert '<section id="copilot">' in H.render_html(r)
    monkeypatch.setattr(extension.FakeSection, "html_text", "<section><script>x()</script>")
    with pytest.raises(ContractViolation):
        H.render_html(r)
    monkeypatch.setattr(extension.FakeSection, "html_text", "<img src='https://x.test/a.png'>")
    with pytest.raises(ContractViolation):
        H.render_html(r)


def test_empty_result_renders() -> None:
    doc = H.render_html(empty_result())
    parse(doc)
    assert "Evidence labels" in doc and "<svg" not in doc
    assert "Not reconciled against provider reports" not in doc     # no bill → no bill section


def test_deterministic() -> None:
    assert H.render_html(full_result()) == H.render_html(full_result())
