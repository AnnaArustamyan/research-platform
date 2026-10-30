from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from `TR_*` environment variables or `.env`."""

    model_config = SettingsConfigDict(env_prefix="TR_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://trackrecord:trackrecord@localhost:5432/trackrecord"

    # SEC requires a User-Agent that identifies you, e.g. "TrackRecord you@example.com".
    # https://www.sec.gov/os/accessing-edgar-data
    sec_user_agent: str | None = None
    sec_max_requests_per_second: float = Field(default=8.0, gt=0, le=10)

    data_dir: Path = Path("data")
    history_start: date = date(2021, 1, 1)
    xbrl_taxonomies: tuple[str, ...] = ("us-gaap", "dei", "srt")

    # When set, every API request must send `Authorization: Bearer <token>`.
    api_token: str | None = None

    # Claude. The API key is read by the SDK from ANTHROPIC_API_KEY.
    llm_monthly_budget_usd: Decimal = Decimal(30)
    extraction_model: str = "claude-haiku-5-5"
    extraction_effort: Literal["low", "medium", "high"] = "medium"
    # Extract promises from new earnings releases in the background worker.
    auto_extract: bool = True

    @property
    def libpq_url(self) -> str:
        """The database URL without SQLAlchemy's driver suffix, for psycopg/procrastinate."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


@lru_cache
def get_settings() -> Settings:
    return Settings()
