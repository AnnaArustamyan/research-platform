"""Validate an extracted statement against its document and normalize it.

Checks are code, not model judgment (docs/design.md §5.2):
- the quote must be found in the document, which rejects invented promises;
- the numbers in the structured fields must appear in the quote;
- units must fit the metric.
Failures of the first kind reject the statement; the others send it to review.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from trackrecord.models import Basis, Metric, PromiseKind, Unit
from trackrecord.promises.schema import ExtractedPromise, Scale

CONFIDENCE_THRESHOLD = 0.8

SCALES: dict[Scale, Decimal] = {
    "units": Decimal(1),
    "thousands": Decimal(1_000),
    "millions": Decimal(1_000_000),
    "billions": Decimal(1_000_000_000),
}

# Documented conventions for changes given in words (design §5.2), in percent.
CHANGE_WORDS: dict[str, tuple[Decimal, Decimal]] = {
    "flat": (Decimal(-1), Decimal(1)),
    "low single digits": (Decimal(1), Decimal(3)),
    "low single digit": (Decimal(1), Decimal(3)),
    "mid single digits": (Decimal(4), Decimal(6)),
    "mid single digit": (Decimal(4), Decimal(6)),
    "high single digits": (Decimal(7), Decimal(9)),
    "high single digit": (Decimal(7), Decimal(9)),
    "low double digits": (Decimal(10), Decimal(13)),
    "low double digit": (Decimal(10), Decimal(13)),
}

EXPECTED_UNIT = {
    Metric.REVENUE: Unit.USD,
    Metric.OPERATING_EXPENSES: Unit.USD,
    Metric.GROSS_MARGIN: Unit.PERCENT,
    Metric.OPERATING_MARGIN: Unit.PERCENT,
    Metric.TAX_RATE: Unit.PERCENT,
    Metric.EPS_DILUTED: Unit.USD_PER_SHARE,
}

_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


class Rejected(Exception):
    pass


@dataclass
class NormalizedPromise:
    metric: Metric
    metric_name: str
    basis: Basis
    fiscal_year: int
    fiscal_quarter: int | None
    kind: PromiseKind
    unit: Unit | None
    low: Decimal | None
    high: Decimal | None
    approximate: bool
    relative_reference: str | None
    change_low: Decimal | None
    change_high: Decimal | None
    interpretation: str | None
    quote: str
    span_start: int
    span_end: int
    confidence: float
    review_reasons: list[str] = field(default_factory=list)

    def dedup_key(self) -> tuple[object, ...]:
        name = self.metric_name.lower() if self.metric == Metric.OTHER else ""
        return (self.metric, name, self.basis, self.fiscal_year, self.fiscal_quarter)


def find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Locate the quote exactly, or else ignoring case, whitespace and "|" separators."""
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    tokens = [t for t in re.split(r"[\s|]+", quote) if t]
    if not tokens:
        return None
    pattern = r"[\s|]+".join(re.escape(t) for t in tokens)
    match = re.search(pattern, text, re.IGNORECASE)
    return (match.start(), match.end()) if match else None


def numbers_in(text: str) -> set[Decimal]:
    return {Decimal(n.replace(",", "")) for n in _NUMBER.findall(text)}


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def normalize(p: ExtractedPromise, document_text: str) -> NormalizedPromise:
    span = find_quote(document_text, p.quote)
    if span is None:
        raise Rejected("quote not found in document")
    if p.fiscal_quarter is not None and not 1 <= p.fiscal_quarter <= 4:
        raise Rejected(f"invalid fiscal quarter {p.fiscal_quarter}")

    reasons: list[str] = []
    metric = Metric(p.metric)
    unit = None if p.unit == "none" else Unit(p.unit)
    scale = SCALES[p.scale] if unit == Unit.USD else Decimal(1)
    value, low, high = _dec(p.value), _dec(p.low), _dec(p.high)
    approximate = p.approximate
    interpretation: str | None = None
    change_low = change_high = None
    relative_reference = None

    match p.kind:
        case "range":
            if low is None or high is None:
                raise Rejected("range without low and high")
            low, high = low * scale, high * scale
            kind = PromiseKind.RANGE
        case "point":
            if value is None:
                raise Rejected("point without value")
            low = high = value * scale
            kind = PromiseKind.POINT
        case "plus_minus":
            low, high = _plus_minus(p, value, scale, unit)
            kind = PromiseKind.RANGE
        case "bound":
            if value is None or p.bound_direction is None:
                raise Rejected("bound without value or direction")
            low, high = (
                (value * scale, None) if p.bound_direction == "at_least" else (None, value * scale)
            )
            kind = PromiseKind.BOUND
        case "relative":
            relative_reference = p.relative_reference
            if relative_reference is None:
                raise Rejected("relative without reference period")
            change_low, change_high = _dec(p.change_low), _dec(p.change_high)
            if change_low is None or change_high is None:
                words = (p.change_words or "").strip().lower()
                if words not in CHANGE_WORDS:
                    raise Rejected(f"relative change without numbers or a known phrase: {words!r}")
                change_low, change_high = CHANGE_WORDS[words]
                interpretation = f'"{words}" read as {change_low}% to {change_high}%'
                reasons.append("interpreted")
            low = high = None
            kind = PromiseKind.RELATIVE
        case "qualitative":
            low = high = None
            kind = PromiseKind.QUALITATIVE

    if low is not None and high is not None and low > high:
        raise Rejected(f"low {low} is above high {high}")

    quoted = numbers_in(p.quote)
    stated = [p.value, p.low, p.high, p.plus_minus, p.change_low, p.change_high]
    for number in (_dec(n) for n in stated if n is not None):
        assert number is not None
        if abs(number) not in quoted:
            reasons.append(f"number not in quote: {number}")

    expected = EXPECTED_UNIT.get(metric)
    if (
        expected
        and kind != PromiseKind.QUALITATIVE
        and unit != expected
        and kind != PromiseKind.RELATIVE
    ):
        reasons.append(f"unit {unit} unexpected for {metric}")
    if unit == Unit.PERCENT and any(v is not None and not 0 <= v <= 100 for v in (low, high)):
        reasons.append("percent outside 0-100")
    if p.confidence < CONFIDENCE_THRESHOLD:
        reasons.append(f"low confidence {p.confidence:.2f}")

    return NormalizedPromise(
        metric=metric,
        metric_name=p.metric_name.strip(),
        basis=Basis(p.basis),
        fiscal_year=p.fiscal_year,
        fiscal_quarter=p.fiscal_quarter,
        kind=kind,
        unit=unit,
        low=low,
        high=high,
        approximate=approximate,
        relative_reference=relative_reference,
        change_low=change_low,
        change_high=change_high,
        interpretation=interpretation,
        quote=document_text[span[0] : span[1]],
        span_start=span[0],
        span_end=span[1],
        confidence=p.confidence,
        review_reasons=reasons,
    )


def _plus_minus(
    p: ExtractedPromise, value: Decimal | None, scale: Decimal, unit: Unit | None
) -> tuple[Decimal, Decimal]:
    tolerance = _dec(p.plus_minus)
    if value is None or tolerance is None or p.plus_minus_unit is None:
        raise Rejected("plus_minus without value, tolerance or tolerance unit")
    center = value * scale
    match p.plus_minus_unit:
        case "percent_of_value":
            delta = center * tolerance / 100
        case "basis_points":
            if unit != Unit.PERCENT:
                raise Rejected("basis points on a non-percent value")
            delta = tolerance / 100
        case "percentage_points":
            delta = tolerance
        case "same_as_value":
            tolerance_scale = SCALES[p.plus_minus_scale] if p.plus_minus_scale else scale
            delta = tolerance * (tolerance_scale if unit == Unit.USD else Decimal(1))
    return center - delta, center + delta
