# Track Record

A desktop research terminal that records what semiconductor companies promise
investors, checks those promises against what they later report, and answers
research questions with cited, code-computed numbers.

See [docs/design.md](docs/design.md) for the v1.0 design.

## Status

| Milestone | State |
|---|---|
| M1 Foundation: EDGAR sync, XBRL facts, fiscal calendars, API, worker, CI | done |
| M2 Promise extraction | next |

## Run it

Requirements: Docker with Compose.

```sh
cp .env.example .env          # then set TR_SEC_USER_AGENT to your name and email
docker compose up -d          # database, migrations, API, background worker
curl -X POST http://127.0.0.1:8000/sync -H 'content-type: application/json' -d '{}'
```

The worker syncs all 40 companies (filings, XBRL facts, fiscal calendars,
press releases and reports), then re-syncs every two hours. Progress:
`GET /sync/runs`, totals: `GET /stats`. API docs: http://127.0.0.1:8000/docs.

## Develop

Requirements: [uv](https://docs.astral.sh/uv/) and Docker (for PostgreSQL).

```sh
docker compose up -d db
cd backend
uv sync
uv run alembic upgrade head
uv run trackrecord seed
uv run trackrecord sync NVDA AMD      # sync in-process, no worker needed
uv run trackrecord check-guidance     # flag companies that publish quarterly guidance
uv run trackrecord api --reload
```

Checks (the same ones CI runs):

```sh
docker compose exec db createdb -U trackrecord trackrecord_test   # once
export TR_TEST_DATABASE_URL=postgresql+psycopg://trackrecord:trackrecord@localhost:5432/trackrecord_test
uv run ruff format --check . && uv run ruff check . && uv run pyright && uv run pytest
```

Database tests are skipped when `TR_TEST_DATABASE_URL` is not set. They drop
and recreate the `public` schema of that database, so never point it at real data.

## Layout

```
backend/
  src/trackrecord/
    edgar/        SEC EDGAR client (rate limit, retries) and response parsers
    ingest/       sync of filings, facts, fiscal calendars and documents; data checks
    api/          FastAPI app
    worker.py     background jobs (procrastinate)
    models.py     database schema
    universe.yaml the 40 tracked companies
  migrations/     Alembic migrations
  tests/          unit and database tests, with trimmed real EDGAR fixtures
docs/design.md    v1.0 design
```
