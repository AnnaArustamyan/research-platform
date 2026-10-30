from datetime import date
from decimal import Decimal

from tests.conftest import load_fixture
from trackrecord.edgar.parsing import iter_facts, parse_filing_index, parse_filings_table


def test_parse_filings_table() -> None:
    table = load_fixture("nvda_submissions.json")["filings"]["recent"]
    records = parse_filings_table(table)

    assert len(records) == len(table["accessionNumber"])
    release = next(r for r in records if r.accession == "0001045810-26-000073")
    assert release.form == "8-K"
    assert release.items == ("2.02", "9.01")
    assert release.filed_at == date(2026, 8, 26)
    assert release.accepted_at is not None and release.accepted_at.tzinfo is not None

    report = next(r for r in records if r.accession == "0001045810-26-000075")
    assert report.form == "10-Q"
    assert report.items == ()
    assert report.report_date == date(2026, 7, 26)


def test_parse_filing_index() -> None:
    entries = parse_filing_index(load_fixture("nvda_8k_index.htm"))

    by_type = {e.doc_type: e for e in entries}
    assert by_type["EX-99.1"].filename == "q2fy27pr.htm"
    assert by_type["EX-99.1"].size_bytes == 341113
    assert by_type["EX-99.2"].filename == "q2fy27cfocommentary.htm"
    assert by_type["8-K"].filename == "nvda-20260826.htm"
    # Only the "Document Format Files" table, not the XBRL "Data Files" table.
    assert not any(e.doc_type.startswith("EX-101") for e in entries)


def test_parse_filing_index_without_table() -> None:
    assert parse_filing_index("<html><body>not an index</body></html>") == []


def test_iter_facts_filters_by_taxonomy_and_filing_date() -> None:
    facts_json = load_fixture("nvda_companyfacts.json")

    facts = list(iter_facts(facts_json, ("us-gaap",), date(2026, 1, 1)))

    assert facts, "fixture should contain facts filed in 2026"
    assert all(f.filed_at >= date(2026, 1, 1) for f in facts)
    assert {f.taxonomy for f in facts} == {"us-gaap"}
    q2_revenue = next(
        f
        for f in facts
        if f.concept == "Revenues"
        and f.period_start == date(2026, 4, 27)
        and f.period_end == date(2026, 7, 26)
    )
    assert q2_revenue.value == Decimal(96221000000)
    assert (q2_revenue.fiscal_year, q2_revenue.fiscal_period) == (2027, "Q2")


def test_iter_facts_instant_values_have_no_start() -> None:
    facts_json = load_fixture("nvda_companyfacts.json")

    shares = [f for f in iter_facts(facts_json, ("dei",), date(2025, 1, 1))]

    assert shares and all(f.period_start is None for f in shares)
