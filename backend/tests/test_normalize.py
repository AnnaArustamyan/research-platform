from decimal import Decimal

import pytest

from tests.conftest import load_fixture
from tests.fakes import NVDA_Q2FY27_OUTPUT, promise
from trackrecord.documents import html_to_text
from trackrecord.models import Basis, Metric, PromiseKind, Unit
from trackrecord.promises.normalize import Rejected, find_quote, normalize, numbers_in

TEXT = html_to_text(load_fixture("nvda_q2fy27_release.htm"))
B = Decimal(1_000_000_000)


def test_plus_minus_percent_of_value() -> None:
    p = normalize(promise(), TEXT)

    assert p.kind == PromiseKind.RANGE
    assert (p.low, p.high) == (Decimal("105.84") * B, Decimal("110.16") * B)
    assert p.unit == Unit.USD
    assert p.review_reasons == []
    assert TEXT[p.span_start : p.span_end] == p.quote


def test_plus_minus_basis_points() -> None:
    p = normalize(NVDA_Q2FY27_OUTPUT.promises[1], TEXT)

    assert (p.low, p.high) == (Decimal("73.5"), Decimal("74.5"))
    assert (p.metric, p.basis) == (Metric.GROSS_MARGIN, Basis.GAAP)


def test_plus_minus_with_its_own_scale() -> None:
    # AMD: "approximately $13 billion, plus or minus $300 million"
    text = "AMD expects revenue to be approximately $13 billion, plus or minus $300 million."
    p = normalize(
        promise(
            quote=text,
            value=13,
            approximate=True,
            plus_minus=300,
            plus_minus_unit="same_as_value",
            plus_minus_scale="millions",
        ),
        text,
    )
    assert (p.low, p.high) == (Decimal("12.7") * B, Decimal("13.3") * B)


def test_approximate_point() -> None:
    p = normalize(NVDA_Q2FY27_OUTPUT.promises[3], TEXT)

    assert p.kind == PromiseKind.POINT
    assert p.low == p.high == Decimal("9.2") * B
    assert p.approximate


def test_full_year_range() -> None:
    p = normalize(NVDA_Q2FY27_OUTPUT.promises[5], TEXT)

    assert (p.fiscal_year, p.fiscal_quarter) == (2027, None)
    assert (p.low, p.high) == (Decimal("16.0"), Decimal("18.0"))


def test_bound() -> None:
    text = "We expect gross margin of at least 60% in the fourth quarter."
    p = normalize(
        promise(
            quote=text,
            metric="gross_margin",
            kind="bound",
            unit="percent",
            scale="units",
            value=60,
            plus_minus=None,
            plus_minus_unit=None,
            bound_direction="at_least",
        ),
        text,
    )
    assert (p.kind, p.low, p.high) == (PromiseKind.BOUND, Decimal(60), None)


def test_relative_with_numbers() -> None:
    # Microchip: "We expect net sales for the September quarter to be up sequentially
    # between 7% and 9%"
    text = "We expect net sales for the September quarter to be up sequentially between 7% and 9%"
    p = normalize(
        promise(
            quote=text,
            kind="relative",
            value=None,
            plus_minus=None,
            plus_minus_unit=None,
            relative_reference="prior_quarter",
            change_low=7,
            change_high=9,
        ),
        text,
    )
    assert p.kind == PromiseKind.RELATIVE
    assert (p.change_low, p.change_high) == (Decimal(7), Decimal(9))
    assert p.low is None and p.interpretation is None
    assert p.review_reasons == []


def test_relative_in_words_applies_the_convention_and_needs_review() -> None:
    text = "We expect revenue to grow mid single digits sequentially."
    p = normalize(
        promise(
            quote=text,
            kind="relative",
            value=None,
            plus_minus=None,
            plus_minus_unit=None,
            relative_reference="prior_quarter",
            change_words="Mid single digits",
        ),
        text,
    )
    assert (p.change_low, p.change_high) == (Decimal(4), Decimal(6))
    assert p.interpretation == '"mid single digits" read as 4% to 6%'
    assert "interpreted" in p.review_reasons


def test_unknown_change_words_are_rejected() -> None:
    text = "We expect revenue to grow nicely."
    with pytest.raises(Rejected, match="known phrase"):
        normalize(
            promise(
                quote=text,
                kind="relative",
                value=None,
                plus_minus=None,
                plus_minus_unit=None,
                relative_reference="prior_quarter",
                change_words="nicely",
            ),
            text,
        )


def test_quote_not_in_document_is_rejected() -> None:
    with pytest.raises(Rejected, match="quote not found"):
        normalize(promise(quote="Revenue is expected to be $200 billion."), TEXT)


def test_quote_matches_despite_whitespace_case_and_separators() -> None:
    text = "Revenues | $9.7B - $10.5B"
    assert find_quote(text, "revenues  $9.7B - $10.5B") == (0, len(text))
    assert find_quote(text, "Revenues | $9.7B - $10.5B") == (0, len(text))
    assert find_quote(text, "Revenues $9.8B") is None


def test_number_missing_from_quote_needs_review() -> None:
    p = normalize(promise(value=107.0), TEXT)
    assert "number not in quote: 107.0" in p.review_reasons


def test_unit_must_fit_metric() -> None:
    p = normalize(promise(metric="gross_margin"), TEXT)
    assert "unit usd unexpected for gross_margin" in p.review_reasons


def test_low_confidence_needs_review() -> None:
    assert "low confidence 0.50" in normalize(promise(confidence=0.5), TEXT).review_reasons


def test_inverted_range_is_rejected() -> None:
    text = "Revenue between $6.30 billion and $5.80 billion."
    with pytest.raises(Rejected, match="above high"):
        normalize(
            promise(
                quote=text,
                kind="range",
                value=None,
                low=6.30,
                high=5.80,
                plus_minus=None,
                plus_minus_unit=None,
            ),
            text,
        )


def test_invalid_quarter_is_rejected() -> None:
    with pytest.raises(Rejected, match="quarter"):
        normalize(promise(fiscal_quarter=5), TEXT)


def test_numbers_in_parses_thousands_separators() -> None:
    assert numbers_in("$9,947 and 74.0% and 50") == {Decimal(9947), Decimal("74.0"), Decimal(50)}
