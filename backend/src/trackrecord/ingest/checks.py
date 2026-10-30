"""Data checks on synced documents (docs/design.md §4.2).

`guides_quarterly`: whether a company publishes numeric guidance for the next
quarter in its earnings releases. This is a cheap keyword heuristic used to
choose the company universe. It is not the promise extractor (M2).
"""

import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from trackrecord.ingest.storage import RawStore
from trackrecord.models import Company, Filing, FilingKind

RECENT_RELEASES = 8
MIN_SHARE_WITH_GUIDANCE = 0.5

# A sentence with an expectation word, a quarter and a number, in any order.
_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+")
_EXPECTATION = re.compile(
    r"\b(outlook|guidance|expect\w*|anticipat\w*|forecast\w*|project\w*)\b", re.IGNORECASE
)
_QUARTER = re.compile(r"\bquarter\b", re.IGNORECASE)
_NUMBER = re.compile(r"\$\s?\d|\d+(\.\d+)?\s?(%|percent)", re.IGNORECASE)


def html_to_text(html: bytes) -> str:
    tree = LexborHTMLParser(html.decode("utf-8", errors="replace"))
    root = tree.body or tree.root
    text = root.text(separator=" ") if root is not None else ""
    return re.sub(r"\s+", " ", text).strip()


def has_quarterly_guidance(text: str) -> bool:
    return any(
        _EXPECTATION.search(sentence) and _QUARTER.search(sentence) and _NUMBER.search(sentence)
        for sentence in _SENTENCE_END.split(text)
    )


@dataclass
class GuidanceCheck:
    ticker: str
    releases_checked: int
    releases_with_guidance: int

    @property
    def guides_quarterly(self) -> bool | None:
        if self.releases_checked == 0:
            return None
        return self.releases_with_guidance / self.releases_checked >= MIN_SHARE_WITH_GUIDANCE


async def check_guidance(session: AsyncSession, store: RawStore, company: Company) -> GuidanceCheck:
    """Check the company's most recent releases and store the result on the company."""
    stmt = (
        select(Filing)
        .where(
            Filing.company_id == company.id,
            Filing.kind == FilingKind.EARNINGS_RELEASE,
            Filing.documents_fetched_at.is_not(None),
        )
        .options(selectinload(Filing.documents))
        .order_by(Filing.filed_at.desc())
        .limit(RECENT_RELEASES)
    )
    releases = (await session.scalars(stmt)).all()
    with_guidance = sum(
        any(
            has_quarterly_guidance(html_to_text(store.read(d.storage_path)))
            for d in filing.documents
            if d.storage_path
        )
        for filing in releases
    )
    result = GuidanceCheck(company.ticker, len(releases), with_guidance)
    company.guides_quarterly = result.guides_quarterly
    return result
