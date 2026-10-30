"""A small async client for SEC EDGAR.

SEC's fair-access rules: at most 10 requests per second and a User-Agent that
identifies the requester. Requests over the limit get HTTP 403/429.
"""

import asyncio
import logging
import random
import time
from typing import Any, Self

import httpx

from trackrecord.edgar.parsing import IndexEntry, parse_filing_index

log = logging.getLogger(__name__)

DATA_BASE = "https://data.sec.gov"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class EdgarError(Exception):
    pass


class RateLimiter:
    """Spaces requests evenly so no more than `rate` start per second."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


def archive_url(cik: int, accession: str, filename: str) -> str:
    return f"{ARCHIVES_BASE}/{cik}/{accession.replace('-', '')}/{filename}"


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None,
        *,
        max_requests_per_second: float = 8.0,
        max_retries: int = 5,
        backoff_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise EdgarError(
                "SEC requires a User-Agent with contact details, e.g. "
                "'TrackRecord you@example.com'. Set TR_SEC_USER_AGENT."
            )
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = RateLimiter(max_requests_per_second)
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, *, allow_404: bool = False) -> httpx.Response | None:
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise EdgarError(f"GET {url} failed: {exc}") from exc
                await self._sleep_before_retry(attempt, None)
                continue

            if response.status_code == 404 and allow_404:
                return None
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code == 403:
                raise EdgarError(
                    f"GET {url} returned 403. SEC blocks requests without a valid "
                    "User-Agent or above the rate limit."
                )
            if response.is_error:
                raise EdgarError(f"GET {url} returned {response.status_code}")
            return response
        raise AssertionError("unreachable")

    async def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff * 2**attempt * (1 + random.random() / 4)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        log.warning("EDGAR request failed, retrying in %.1fs (attempt %d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    async def get_json(self, url: str) -> Any:
        response = await self._get(url)
        assert response is not None
        return response.json()

    async def get_bytes(self, url: str) -> bytes:
        response = await self._get(url)
        assert response is not None
        return response.content

    async def submissions(self, cik: int) -> dict[str, Any]:
        """Company metadata plus its most recent ~1000 filings."""
        return await self.get_json(f"{DATA_BASE}/submissions/CIK{cik:010d}.json")

    async def submissions_page(self, name: str) -> dict[str, Any]:
        """An older page of filings, named in `filings.files` of the submissions JSON."""
        return await self.get_json(f"{DATA_BASE}/submissions/{name}")

    async def company_facts(self, cik: int) -> dict[str, Any] | None:
        """All XBRL facts for a company, or None if it has never filed XBRL."""
        response = await self._get(
            f"{DATA_BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json", allow_404=True
        )
        return response.json() if response is not None else None

    async def filing_index(self, cik: int, accession: str) -> list[IndexEntry]:
        url = archive_url(cik, accession, f"{accession}-index.htm")
        response = await self._get(url)
        assert response is not None
        return parse_filing_index(response.text)
