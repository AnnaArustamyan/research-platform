import json
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trackrecord.config import Settings
from trackrecord.db import make_engine, make_sessionmaker
from trackrecord.edgar.client import EdgarClient

FIXTURES = Path(__file__).parent / "fixtures"
BACKEND = Path(__file__).parent.parent
NVDA_CIK = 1045810


def load_fixture(name: str) -> Any:
    path = FIXTURES / name
    return json.loads(path.read_text()) if path.suffix == ".json" else path.read_text()


def index_html(entries: list[tuple[str, str]]) -> str:
    """A minimal `-index.htm` page listing (doc_type, filename) entries."""
    rows = "".join(
        f'<tr><td scope="row">{i}</td><td scope="row">{doc_type} doc</td>'
        f'<td scope="row"><a href="/Archives/x/{filename}">{filename}</a></td>'
        f'<td scope="row">{doc_type}</td><td scope="row">100</td></tr>'
        for i, (doc_type, filename) in enumerate(entries, 1)
    )
    return (
        '<table class="tableFile" summary="Document Format Files">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>"
        f"{rows}</table>"
    )


class FakeEdgar:
    """Serves NVIDIA fixtures for EDGAR URLs and records every request."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        submissions = load_fixture("nvda_submissions.json")
        recent = submissions["filings"]["recent"]
        self.forms = dict(zip(recent["accessionNumber"], recent["form"], strict=True))
        self.routes: dict[str, Callable[[], httpx.Response]] = {
            f"/submissions/CIK{NVDA_CIK:010d}.json": lambda: httpx.Response(200, json=submissions),
            f"/api/xbrl/companyfacts/CIK{NVDA_CIK:010d}.json": lambda: httpx.Response(
                200, json=load_fixture("nvda_companyfacts.json")
            ),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path in self.routes:
            return self.routes[path]()
        if match := re.search(r"/(\d{10}-\d{2}-\d{6})-index\.htm$", path):
            accession = match.group(1)
            form = self.forms[accession]
            if form == "8-K":
                entries = [("8-K", "main.htm"), ("EX-99.1", "pr.htm"), ("EX-99.2", "cfo.htm")]
            else:
                entries = [(form, "report.htm"), ("EX-31.1", "cert.htm")]
            return httpx.Response(200, text=index_html(entries))
        if path.startswith("/Archives/edgar/data/"):
            return httpx.Response(200, content=f"<html>{path}</html>".encode())
        return httpx.Response(404)

    def client(self) -> EdgarClient:
        return EdgarClient(
            "TrackRecord tests test@example.com",
            max_requests_per_second=1000,
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture
def fake_edgar() -> FakeEdgar:
    return FakeEdgar()


# --- Database -------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("TR_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """Reset the test database and apply all migrations once per test session."""
    if not TEST_DATABASE_URL:
        pytest.skip("set TR_TEST_DATABASE_URL to run database tests")
    sync_engine = create_engine(TEST_DATABASE_URL)
    with sync_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    sync_engine.dispose()

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield TEST_DATABASE_URL


@pytest.fixture(scope="session")
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(migrated_database)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE companies, procrastinate_jobs RESTART IDENTITY CASCADE"))
    yield make_sessionmaker(engine)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL or "postgresql+psycopg://unused/unused",
        sec_user_agent="TrackRecord tests test@example.com",
        data_dir=tmp_path,
        history_start=date(2024, 1, 1),
    )
