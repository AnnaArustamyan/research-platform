from datetime import date

from trackrecord.ingest.periods import PeriodicReport, PeriodSpec, derive_calendar


def report(end: str, annual: bool = False) -> PeriodicReport:
    return PeriodicReport(f"acc-{end}", date.fromisoformat(end), annual)


def find(periods: list[PeriodSpec], fy: int, fq: int | None) -> PeriodSpec:
    return next(p for p in periods if p.fiscal_year == fy and p.fiscal_quarter == fq)


# NVIDIA: 52/53-week fiscal year ending on the last Sunday of January.
NVDA_REPORTS = [
    report("2025-01-26", annual=True),
    report("2025-04-27"),
    report("2025-07-27"),
    report("2025-10-26"),
    report("2026-01-25", annual=True),
    report("2026-04-26"),
    report("2026-07-26"),
]


def test_quarters_start_the_day_after_the_previous_quarter() -> None:
    cal = derive_calendar(NVDA_REPORTS)

    q2 = find(cal.periods, 2026, 2)
    assert (q2.start, q2.end, q2.is_estimated) == (date(2025, 4, 28), date(2025, 7, 27), False)
    assert cal.warnings == []


def test_q4_and_full_year_come_from_the_10k() -> None:
    cal = derive_calendar(NVDA_REPORTS)

    q4 = find(cal.periods, 2026, 4)
    assert (q4.start, q4.end) == (date(2025, 10, 27), date(2026, 1, 25))
    year = find(cal.periods, 2026, None)
    assert (year.start, year.end, year.is_estimated) == (
        date(2025, 1, 27),
        date(2026, 1, 25),
        False,
    )


def test_first_period_start_is_estimated() -> None:
    cal = derive_calendar(NVDA_REPORTS)

    first = find(cal.periods, 2025, 4)
    assert first.is_estimated
    # No Q1 for FY2025, so the full year is assumed to be 52 weeks.
    year = find(cal.periods, 2025, None)
    assert (year.start, year.end, year.is_estimated) == (date(2024, 1, 29), date(2025, 1, 26), True)


def test_week_based_calendar_projects_52_weeks_ahead() -> None:
    cal = derive_calendar(NVDA_REPORTS, project_quarters=4)

    q3 = find(cal.periods, 2027, 3)
    assert (q3.start, q3.end, q3.is_estimated) == (date(2026, 7, 27), date(2026, 10, 25), True)
    assert find(cal.periods, 2027, 4).end == date(2027, 1, 24)
    assert find(cal.periods, 2028, 2).end == date(2027, 7, 25)
    projected_year = find(cal.periods, 2027, None)
    assert projected_year.is_estimated
    assert (projected_year.start, projected_year.end) == (date(2026, 1, 26), date(2027, 1, 24))


def test_month_end_calendar_projects_to_month_ends() -> None:
    # Microchip: fiscal year ends March 31.
    reports = [
        report("2025-03-31", annual=True),
        report("2025-06-30"),
        report("2025-09-30"),
        report("2025-12-31"),
        report("2026-03-31", annual=True),
        report("2026-06-30"),
    ]

    cal = derive_calendar(reports, project_quarters=3)

    assert find(cal.periods, 2027, 2).end == date(2026, 9, 30)
    assert find(cal.periods, 2027, 3).end == date(2026, 12, 31)
    assert find(cal.periods, 2027, 4).end == date(2027, 3, 31)
    assert find(cal.periods, 2027, 2).start == date(2026, 7, 1)


def test_projection_without_year_ago_quarter_adds_13_weeks() -> None:
    cal = derive_calendar(
        [report("2026-01-25", annual=True), report("2026-04-26")], project_quarters=1
    )

    assert find(cal.periods, 2027, 2).end == date(2026, 7, 26)


def test_conflicting_labels_keep_the_first_and_warn() -> None:
    reports = [*NVDA_REPORTS, PeriodicReport("dup", date(2026, 8, 2), False)]

    cal = derive_calendar(reports)

    assert find(cal.periods, 2027, 2).end == date(2026, 7, 26)
    assert any("dup" in w for w in cal.warnings)


def test_unknown_label_is_skipped_with_warning() -> None:
    cal = derive_calendar([*NVDA_REPORTS, PeriodicReport("odd", date(2026, 2, 20), False)])

    assert any("odd" in w for w in cal.warnings)


def test_no_reports_gives_empty_calendar() -> None:
    assert derive_calendar([]).periods == []


def test_quarters_without_any_10k_cannot_be_numbered() -> None:
    cal = derive_calendar([report("2026-04-26"), report("2026-07-26")])

    assert cal.periods == []
    assert cal.warnings == ["no 10-K in range, cannot number quarters"]


def test_quarter_after_a_september_year_end_starts_the_next_fiscal_year() -> None:
    # Skyworks: the fiscal year ends on the Friday closest to September 30, so the
    # quarter ending 2021-12-31 is Q1 of fiscal 2022 (SEC's XBRL labels say Q3 2021).
    reports = [
        report("2021-10-01", annual=True),
        report("2021-12-31"),
        report("2022-04-01"),
        report("2022-07-01"),
        report("2022-09-30", annual=True),
    ]

    cal = derive_calendar(reports, project_quarters=0)

    assert find(cal.periods, 2022, 1).end == date(2021, 12, 31)
    assert find(cal.periods, 2022, 3).end == date(2022, 7, 1)
    year = find(cal.periods, 2022, None)
    assert (year.start, year.end) == (date(2021, 10, 2), date(2022, 9, 30))


def test_year_ending_in_early_january_keeps_the_previous_year_number() -> None:
    # Onto Innovation: fiscal 2025 was a 53-week year ending 2026-01-03.
    reports = [
        report("2024-12-28", annual=True),
        report("2025-03-29"),
        report("2025-06-28"),
        report("2025-09-27"),
        report("2026-01-03", annual=True),
    ]

    cal = derive_calendar(reports, project_quarters=0)

    q4 = find(cal.periods, 2025, 4)
    assert (q4.start, q4.end) == (date(2025, 9, 28), date(2026, 1, 3))
    assert not any(p.fiscal_year == 2026 for p in cal.periods)


def test_quarters_before_the_first_10k_are_numbered_from_a_projected_year_end() -> None:
    reports = [report("2021-04-25"), report("2021-07-25"), report("2022-01-30", annual=True)]

    cal = derive_calendar(reports, project_quarters=0)

    assert find(cal.periods, 2022, 1).end == date(2021, 4, 25)
    assert find(cal.periods, 2022, 2).end == date(2021, 7, 25)


def test_quarters_after_the_last_10k_belong_to_the_next_fiscal_year() -> None:
    reports = [report("2025-09-30", annual=True), report("2025-12-31"), report("2026-03-31")]

    cal = derive_calendar(reports, project_quarters=0)

    assert find(cal.periods, 2026, 2).end == date(2026, 3, 31)


def test_projection_follows_a_switch_to_month_end_quarters() -> None:
    # Onto Innovation: 52/53-week years until 2025, calendar quarters from 2026.
    reports = [
        report("2024-12-28", annual=True),
        report("2025-03-29"),
        report("2025-06-28"),
        report("2025-09-27"),
        report("2026-01-03", annual=True),
        report("2026-03-31"),
        report("2026-06-30"),
    ]

    cal = derive_calendar(reports, project_quarters=2)

    assert find(cal.periods, 2026, 3).end == date(2026, 9, 30)
    assert find(cal.periods, 2026, 4).end == date(2026, 12, 31)
