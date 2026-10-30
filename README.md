# Track Record

A desktop research terminal that records what semiconductor companies promise
investors, checks those promises against what they later report, and answers
research questions with cited, code-computed numbers.

See [docs/design.md](docs/design.md) for the v1.0 design.

## Status

| Milestone | State |
|---|---|
| M1 Foundation: EDGAR sync, XBRL facts, fiscal calendars, API, worker, CI | done |
| M2 Promise extraction: extractor, validation, review queue, cost ledger, eval | built; first eval run needs a Claude API key |
| M3 Resolution and credibility: XBRL and press-release actuals, outcomes, scores | built; GAAP side runs on real data, non-GAAP needs the API key |
| M4 Desktop app | next |

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

Promise extraction (needs `ANTHROPIC_API_KEY`; spend is capped by `TR_LLM_MONTHLY_BUDGET_USD`):

```sh
uv run trackrecord eval-extraction --run        # extract the 19 golden releases and score them
uv run trackrecord extract --batch              # backfill every release via the Batch API
uv run trackrecord collect                      # store finished batch results (re-run until done)
uv run trackrecord read-results                 # reported results incl. non-GAAP, then resolve
uv run trackrecord spend                        # month-to-date Claude spend
```

Resolution and credibility (no API calls; also run after every sync):

```sh
uv run trackrecord resolve                      # GAAP actuals from XBRL, then resolve promises
uv run trackrecord credibility                  # scores, best first
```

After that, the worker extracts each new earnings release on its own. Promises
it is unsure about appear at `GET /review`.

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
    promises/     guidance extraction: schema, prompt, validation, batch backfill
    actuals/      reported results: from XBRL (GAAP) and from press releases (incl. non-GAAP)
    resolve.py    promises vs. actuals: outcome, hit or miss, surprise
    credibility.py  per-company scores
    evals/        scoring against hand-labeled data
    documents.py  HTML to structured plain text
    llm.py        Claude calls with a cost ledger and monthly budget
    api/          FastAPI app
    worker.py     background jobs (procrastinate)
    models.py     database schema
    universe.yaml the 40 tracked companies
  evals/          golden datasets (extraction/golden.yaml)
  migrations/     Alembic migrations
  tests/          unit and database tests, with trimmed real EDGAR fixtures
docs/design.md    v1.0 design
```
