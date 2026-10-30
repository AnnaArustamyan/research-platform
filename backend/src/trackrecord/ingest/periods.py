"""Derive each company's fiscal calendar from its 10-Q and 10-K filings.

Fiscal calendars differ by company (NVIDIA's year ends in late January, many
others use 52/53-week years ending on a Saturday), so periods are built from the
filing dates themselves:

- every 10-K gives the end of a fiscal year (and of its Q4); every 10-Q gives
  the end of Q1, Q2 or Q3, numbered by how far it falls after the last year end;
- a quarter starts the day after the previous quarter ends;
- future quarters are projected from the same quarter a year earlier and marked
  as estimated until a filing confirms them.

The fiscal year/period labels in SEC's XBRL data are not used: filers get them
wrong often enough (e.g. a December quarter labeled Q3 of the year before) that
dates are the more reliable source.
"""

import calendar
import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

QUARTER_DAYS = 91.3


@dataclass(frozen=True, slots=True)
class PeriodicReport:
    """An original (non-amended) 10-Q or 10-K."""

    accession: str
    report_date: date
    is_annual: bool


@dataclass(frozen=True, slots=True)
class PeriodSpec:
    fiscal_year: int
    fiscal_quarter: int | None  # None for the full fiscal year
    start: date
    end: date
    is_estimated: bool


@dataclass
class Calendar:
    periods: list[PeriodSpec]
    warnings: list[str] = field(default_factory=list)


Label = tuple[int, int]  # (fiscal year, quarter 1-4)


def fiscal_year_of(year_end: date) -> int:
    """Companies name a fiscal year after the calendar year it ends in, except
    52/53-week years that spill a few days into January, which keep the number of
    the year they mostly cover (Onto Innovation's fiscal 2025 ended 2026-01-03)."""
    return year_end.year - 1 if year_end.month == 1 and year_end.day <= 7 else year_end.year


def _next_label(label: Label) -> Label:
    year, quarter = label
    return (year + 1, 1) if quarter == 4 else (year, quarter + 1)


def _previous_label(label: Label) -> Label:
    year, quarter = label
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def _is_month_end(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def _month_end_after(d: date, months: int) -> date:
    """The last day of the month `months` after the month of `d`."""
    index = d.year * 12 + d.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, calendar.monthrange(year, month + 1)[1])


def _is_month_based(ends: Iterable[date]) -> bool:
    """Whether a calendar ends periods on month ends (vs. 52/53-week years)."""
    ends = list(ends)
    return sum(map(_is_month_end, ends)) >= 0.9 * len(ends)


def _shift_year(d: date, years: int, month_based: bool) -> date:
    return _month_end_after(d, 12 * years) if month_based else d + timedelta(days=364 * years)


def _label_reports(reports: list[PeriodicReport], warnings: list[str]) -> dict[Label, date]:
    year_ends = sorted({r.report_date for r in reports if r.is_annual})
    quarterlies = sorted((r for r in reports if not r.is_annual), key=lambda r: r.report_date)
    if not year_ends:
        if quarterlies:
            warnings.append("no 10-K in range, cannot number quarters")
        return {}

    # Bracket every 10-Q with year ends, projecting where no 10-K exists (yet).
    month_based = _is_month_based(year_ends)
    if quarterlies:
        while quarterlies[-1].report_date > year_ends[-1]:
            year_ends.append(_shift_year(year_ends[-1], 1, month_based))
        while quarterlies[0].report_date <= year_ends[0]:
            year_ends.insert(0, _shift_year(year_ends[0], -1, month_based))

    quarter_ends: dict[Label, date] = {}
    for report in reports:
        if report.is_annual:
            quarter_ends[(fiscal_year_of(report.report_date), 4)] = report.report_date

    for report in quarterlies:
        d = report.report_date
        next_end = next(end for end in year_ends if end > d)
        previous_end = max(end for end in year_ends if end < d)
        quarter = round((d - previous_end).days / QUARTER_DAYS)
        if not 1 <= quarter <= 3:
            warnings.append(f"{report.accession}: {d} is not a Q1-Q3 end, skipping")
            continue
        label = (fiscal_year_of(next_end), quarter)
        if label in quarter_ends and quarter_ends[label] != d:
            warnings.append(
                f"{report.accession}: FY{label[0]} Q{label[1]} already ends "
                f"{quarter_ends[label]}, ignoring {d}"
            )
            continue
        quarter_ends[label] = d
    return quarter_ends


def derive_calendar(reports: list[PeriodicReport], *, project_quarters: int = 4) -> Calendar:
    warnings: list[str] = []
    quarter_ends = _label_reports(reports, warnings)

    if not quarter_ends:
        return Calendar(periods=[], warnings=warnings)

    labels = sorted(quarter_ends, key=quarter_ends.__getitem__)
    for earlier, later in itertools.pairwise(labels):
        if later < earlier:
            warnings.append(
                f"fiscal labels out of order: FY{earlier[0]} Q{earlier[1]} ends after "
                f"FY{later[0]} Q{later[1]}"
            )

    # Project from the most recent year, so a company that changed its calendar
    # (Onto moved to month-end quarters in 2026) is projected on the new one.
    month_based = _is_month_based(sorted(quarter_ends.values())[-2:])

    def project_end(label: Label, previous_end: date) -> date:
        if month_based:
            return _month_end_after(previous_end, months=3)
        year_ago = quarter_ends.get((label[0] - 1, label[1]))
        if year_ago is None:
            return previous_end + timedelta(days=91)
        return year_ago + timedelta(days=364)

    estimated: set[Label] = set()
    last = max(quarter_ends, key=quarter_ends.__getitem__)
    for _ in range(project_quarters):
        label = _next_label(last)
        quarter_ends[label] = project_end(label, quarter_ends[last])
        estimated.add(label)
        last = label

    quarters: dict[Label, PeriodSpec] = {}
    for label in sorted(quarter_ends):
        end = quarter_ends[label]
        previous = quarter_ends.get(_previous_label(label))
        if previous is not None and timedelta(days=70) <= end - previous <= timedelta(days=112):
            start, start_known = previous + timedelta(days=1), True
        else:
            start, start_known = end - timedelta(days=90), False
        quarters[label] = PeriodSpec(
            fiscal_year=label[0],
            fiscal_quarter=label[1],
            start=start,
            end=end,
            is_estimated=label in estimated or not start_known,
        )

    years: list[PeriodSpec] = []
    for year in sorted({y for y, q in quarters if q == 4}):
        q4 = quarters[(year, 4)]
        q1 = quarters.get((year, 1))
        if q1 is not None and not q1.is_estimated:
            start, year_estimated = q1.start, q4.is_estimated
        else:
            # No confirmed Q1 (the first year in range): assume a full year.
            start = _shift_year(q4.end, -1, month_based) + timedelta(days=1)
            year_estimated = True
        if not 350 <= (q4.end - start).days <= 380:
            warnings.append(f"FY{year}: {start} to {q4.end} is not a fiscal year, skipping")
            continue
        years.append(PeriodSpec(year, None, start, q4.end, year_estimated))

    periods = sorted([*quarters.values(), *years], key=lambda p: (p.end, p.fiscal_quarter or 5))
    return Calendar(periods=periods, warnings=warnings)
