from tests.conftest import load_fixture
from trackrecord.documents import html_to_text


def test_table_rows_become_lines_with_cells() -> None:
    html = (
        "<table><tr><td>Revenues</td><td>$9.7B - $10.5B</td></tr>"
        "<tr><td>GAAP diluted EPS</td><td>$1.22 - $1.42</td></tr></table>"
    )
    assert html_to_text(html) == "Revenues | $9.7B - $10.5B\nGAAP diluted EPS | $1.22 - $1.42"


def test_dollar_and_percent_cells_join_their_numbers() -> None:
    html = (
        "<table><tr><td>Revenue</td><td>$</td><td>9,947</td><td>(</td><td>4</td><td>%)</td></tr>"
        "<tr><td>Margin</td><td>52.5</td><td>%</td></tr></table>"
    )
    assert html_to_text(html) == "Revenue | $9,947 | (4%)\nMargin | 52.5%"


def test_typography_is_normalized_to_ascii() -> None:
    html = "<p>NVIDIA\N{RIGHT SINGLE QUOTATION MARK}s \N{LEFT DOUBLE QUOTATION MARK}outlook"
    html += "\N{RIGHT DOUBLE QUOTATION MARK} \N{EN DASH} $9.2\N{NO-BREAK SPACE}billion</p>"
    assert html_to_text(html) == 'NVIDIA\'s "outlook" - $9.2 billion'


def test_scripts_and_styles_are_dropped() -> None:
    html = "<html><head><style>p {}</style></head><body><script>x()</script><p>Text</p></body>"
    assert html_to_text(html) == "Text"


def test_real_release_keeps_bullets_on_their_own_lines() -> None:
    text = html_to_text(load_fixture("nvda_q2fy27_release.htm"))
    lines = text.splitlines()

    assert "Outlook" in lines
    assert (
        "• Revenue is expected to be $108.0 billion, plus or minus 2%. NVIDIA is not assuming "
        "any Data Center compute revenue from China in its outlook."
    ) in lines
    assert "Revenue | $96,221 | $81,615 | 18%" in lines


def test_paragraphs_inside_cells_stay_on_the_row() -> None:
    # Intel's outlook table puts each value in its own <p>.
    html = (
        "<table><tr><td><p>Q1 2024</p></td><td><p>GAAP</p></td><td><p>Non-GAAP</p></td></tr>"
        "<tr><td><p>Revenue</p></td><td><p>$12.2-13.2 billion</p></td>"
        "<td><p>$12.2-13.2</p><p>billion</p></td></tr></table>"
    )
    assert html_to_text(html) == (
        "Q1 2024 | GAAP | Non-GAAP\nRevenue | $12.2-13.2 billion | $12.2-13.2 billion"
    )
