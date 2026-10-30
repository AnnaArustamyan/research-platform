"""Pure parsers for EDGAR responses. No I/O, so they are easy to test with fixtures."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from selectolax.lexbor import LexborHTMLParser


@dataclass(frozen=True, slots=True)
class FilingRecord:
    accession: str
    form: str
    filed_at: date
    accepted_at: datetime | None
    report_date: date | None
    items: tuple[str, ...]
    primary_document: str | None


@dataclass(frozen=True, slots=True)
class IndexEntry:
    sequence: str
    description: str
    filename: str
    doc_type: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class FactRecord:
    taxonomy: str
    concept: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    accession: str
    form: str
    filed_at: date
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _datetime(value: str | None) -> datetime | None:
    # EDGAR writes e.g. "2026-08-26T16:21:19.000Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_filings_table(table: dict[str, list[Any]]) -> list[FilingRecord]:
    """Parse the column-oriented filings table (`filings.recent`, or an older page)."""
    records = []
    accepted = table.get("acceptanceDateTime")
    for i, accession in enumerate(table["accessionNumber"]):
        filed_at = _date(table["filingDate"][i])
        assert filed_at is not None
        items = table["items"][i] if "items" in table else ""
        records.append(
            FilingRecord(
                accession=accession,
                form=table["form"][i],
                filed_at=filed_at,
                accepted_at=_datetime(accepted[i]) if accepted else None,
                report_date=_date(table["reportDate"][i] or None),
                items=tuple(item for item in items.split(",") if item),
                primary_document=table["primaryDocument"][i] or None,
            )
        )
    return records


def parse_filing_index(html: str) -> list[IndexEntry]:
    """Parse the "Document Format Files" table of a `{accession}-index.htm` page."""
    tree = LexborHTMLParser(html)
    table = tree.css_first('table.tableFile[summary="Document Format Files"]')
    if table is None:
        return []
    entries = []
    for row in table.css("tr"):
        cells = row.css("td")
        if len(cells) < 5:
            continue  # header row
        link = cells[2].css_first("a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        size = cells[4].text(strip=True)
        entries.append(
            IndexEntry(
                sequence=cells[0].text(strip=True),
                description=cells[1].text(strip=True),
                filename=href.rsplit("/", 1)[-1],
                doc_type=cells[3].text(strip=True),
                size_bytes=int(size) if size.isdigit() else None,
            )
        )
    return entries


def iter_facts(
    company_facts: dict[str, Any], taxonomies: tuple[str, ...], filed_since: date
) -> Iterator[FactRecord]:
    """Flatten the companyfacts JSON into one record per reported value."""
    for taxonomy, concepts in company_facts.get("facts", {}).items():
        if taxonomy not in taxonomies:
            continue
        for concept, body in concepts.items():
            for unit, values in body.get("units", {}).items():
                for v in values:
                    filed_at = date.fromisoformat(v["filed"])
                    if filed_at < filed_since:
                        continue
                    yield FactRecord(
                        taxonomy=taxonomy,
                        concept=concept,
                        unit=unit,
                        value=Decimal(str(v["val"])),
                        period_start=_date(v.get("start")),
                        period_end=date.fromisoformat(v["end"]),
                        accession=v["accn"],
                        form=v["form"],
                        filed_at=filed_at,
                        fiscal_year=v.get("fy"),
                        fiscal_period=v.get("fp"),
                        frame=v.get("frame"),
                    )
