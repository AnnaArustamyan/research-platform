import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from trackrecord.edgar.client import EdgarClient, EdgarError, RateLimiter


def client_for(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> EdgarClient:
    return EdgarClient(
        "TrackRecord tests test@example.com",
        max_requests_per_second=1000,
        backoff_seconds=0.001,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_user_agent_with_contact_is_required() -> None:
    with pytest.raises(EdgarError, match="User-Agent"):
        EdgarClient(None)
    with pytest.raises(EdgarError, match="User-Agent"):
        EdgarClient("no contact details")


async def test_sends_user_agent() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={})

    async with client_for(handler) as client:
        await client.get_json("https://data.sec.gov/x.json")

    assert seen == ["TrackRecord tests test@example.com"]


async def test_rate_limiter_spaces_requests() -> None:
    limiter = RateLimiter(rate=50)
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    # 6 requests at 50/s: the 6th may start no earlier than 5 intervals (0.1s) later.
    assert time.monotonic() - start >= 0.095


async def test_retries_server_errors_then_succeeds() -> None:
    statuses = iter([503, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={"ok": True})

    async with client_for(handler) as client:
        assert await client.get_json("https://data.sec.gov/x.json") == {"ok": True}


async def test_gives_up_after_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with client_for(handler, max_retries=2) as client:
        with pytest.raises(EdgarError, match="503"):
            await client.get_json("https://data.sec.gov/x.json")
    assert calls == 3


async def test_retries_transport_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[])

    async with client_for(handler) as client:
        assert await client.get_json("https://data.sec.gov/x.json") == []


async def test_forbidden_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    async with client_for(handler) as client:
        with pytest.raises(EdgarError, match="403"):
            await client.submissions(1)
    assert calls == 1


async def test_company_without_xbrl_returns_none() -> None:
    async with client_for(lambda request: httpx.Response(404)) as client:
        assert await client.company_facts(1) is None
