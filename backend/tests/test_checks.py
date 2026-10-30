import pytest

from trackrecord.ingest.checks import GuidanceCheck, has_quarterly_guidance, html_to_text


@pytest.mark.parametrize(
    "text",
    [
        # NVIDIA, Q2 FY2027 release.
        "Outlook NVIDIA's outlook for the third quarter of fiscal 2027 is as follows: "
        "• Revenue is expected to be $108.0 billion, plus or minus 2%.",
        "For the fourth quarter, we expect net sales to be between $1.55 billion and $1.65 billion",
        "Fourth Quarter 2026 Outlook: revenue in the range of $280 million to $300 million",
        "The company anticipates first quarter gross margin of 52% to 54%",
        # Lam Research: guidance as a table, after "U.S. GAAP".
        "Outlook For the quarter ending December 27, 2026, Lam is providing the following "
        "guidance: U.S. GAAP Reconciling Items Non-GAAP Revenue $8.60 Billion +/- $400 Million",
    ],
)
def test_detects_forward_quarterly_guidance(text: str) -> None:
    assert has_quarterly_guidance(text)


@pytest.mark.parametrize(
    "text",
    [
        "Revenue for the quarter was $5.4 billion, up 12% from a year ago.",
        "We expect continued strength in data center demand.",
        "Our outlook remains positive. The quarter had record bookings of $2 billion.",
        "We expect growth to continue; the quarter's revenue was $2.1 billion.",
        # SiTime gives guidance only on its call.
        "SiTime plans to discuss its business outlook as part of today's scheduled conference "
        "call. Revenue for the third quarter was $90.1 million.",
    ],
)
def test_ignores_text_without_forward_numeric_guidance(text: str) -> None:
    assert not has_quarterly_guidance(text)


def test_html_to_text_collapses_whitespace() -> None:
    html = b"<html><body><p>Outlook</p>\n<table><tr><td>Revenue</td><td>$1.0</td></tr></table>"
    assert html_to_text(html) == "Outlook Revenue $1.0"


@pytest.mark.parametrize(
    ("checked", "with_guidance", "expected"),
    [(0, 0, None), (8, 4, True), (8, 3, False), (2, 2, True)],
)
def test_guides_quarterly_threshold(
    checked: int, with_guidance: int, expected: bool | None
) -> None:
    assert GuidanceCheck("X", checked, with_guidance).guides_quarterly is expected
