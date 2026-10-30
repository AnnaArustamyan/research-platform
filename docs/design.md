# Track Record: v1.0 Design

> Working name. A desktop research terminal that records what semiconductor
> companies promise investors, checks those promises against what they later
> report, and answers research questions with cited, code-computed numbers.

Status: approved; M1 done · Owner: Anna Arustamyan · Last updated: 2026-10-30

---

## 1. Product

### 1.1 Problem

Every quarter, companies publish guidance: "next quarter we expect revenue of
$5.4B to $5.6B and non-GAAP gross margin of about 52%." Whether management hits
its own numbers is one of the most useful signals an investor has, yet checking
it means digging through old press releases by hand. Existing AI research tools
summarize what was said; none keep a structured, verifiable scorecard of
whether it came true.

### 1.2 What v1.0 does

1. **Ingests** SEC filings for 40 semiconductor companies (5 years of history,
   then continuous sync).
2. **Extracts** every quantitative guidance statement into a structured
   `Promise` record, with the exact source quote.
3. **Resolves** each promise when results arrive: provisionally from the next
   earnings press release, then confirmed against XBRL data from the 10-Q/10-K.
4. **Scores** each company's credibility: hit rate, bias, consistency, with the
   components shown, not one opaque number.
5. **Answers research questions** through an agent that searches filings,
   queries financial tables and the promise ledger, and returns a cited report
   in which **every number is computed by code, never written by the model**.
6. **Alerts** you through desktop notifications when promises resolve.

### 1.3 Who it is for

Single user (you) in v1.0: a self-directed investor or analyst who wants an
honest view of management credibility. Multi-user and hosting are post-v1.0.

### 1.4 Non-goals for v1.0

- Earnings call transcripts (licensing; guidance is in press releases anyway).
  The design keeps a pluggable source so they can be added later.
- Sectors beyond semiconductors; segment-level guidance; real-time prices.
- Trading signals (the backtest is a post-v1.0 stretch, see §11).

---

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Company** | One of the 40 tracked issuers, keyed by SEC CIK. |
| **Fiscal period** | A company-specific quarter or year (`FY2025 Q1`) with real start/end dates. Fiscal calendars differ (e.g. NVIDIA's year ends in late January), so periods are always stored with dates. |
| **Document** | A filing or exhibit: 8-K earnings release (EX-99.1), 10-Q, 10-K. |
| **Metric** | A canonical financial quantity from a fixed dictionary (§4.3), with a basis (GAAP / non-GAAP). |
| **Promise** | One forward-looking quantitative statement about one metric for one future period. |
| **Actual** | The reported value of a metric for a period, from a named source. |
| **Resolution** | The comparison of a promise against its actual: outcome, surprise, status. |
| **Credibility** | Aggregated statistics over a company's resolved promises. |

---

## 3. Architecture

```
┌────────────────────── Desktop app (Tauri 2 + React + TS) ──────────────────────┐
│ Dashboard · Company · Promise detail · Research chat · Review queue · Settings │
└──────────────────────────────┬─────────────────────────────────────────────────┘
                               │ HTTP + SSE (typed client generated from OpenAPI)
┌──────────────────────────────▼─────────────────────────────────────────────────┐
│ API (FastAPI)                                                                   │
├────────────────┬────────────────┬────────────────┬──────────────┬──────────────┤
│ Ingestion      │ Extraction     │ Resolution     │ Research     │ Trust        │
│ EDGAR sync,    │ guidance →     │ actuals, rules,│ agent loop,  │ evals, traces│
│ parsing,       │ Promise records│ credibility    │ tools,       │ cost ledger, │
│ chunking       │ (LLM)          │ (deterministic)│ verifier     │ budget cap   │
├────────────────┴────────────────┴────────────────┴──────────────┴──────────────┤
│ Workers: Postgres-backed job queue (Procrastinate): periodic + retries + locks │
├─────────────────────────────────────────────────────────────────────────────────┤
│ PostgreSQL 17 + pgvector: relational data, XBRL facts, chunks, embeddings, FTS │
└─────────────────────────────────────────────────────────────────────────────────┘
        External: SEC EDGAR (free) · Claude API · local embedding models
        Observability: OpenTelemetry → Arize Phoenix (local container)
```

All backend services run with `docker compose up` on your machine. The same
compose file can later move to a small VPS so syncing continues while the
laptop is off.

### 3.1 Technology choices

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.12, managed with `uv` | Best ecosystem for data and LLM work |
| API | FastAPI + Pydantic v2 | Typed, async, OpenAPI for free, SSE streaming |
| DB | PostgreSQL 17 + pgvector, SQLAlchemy 2 + Alembic | One database for relational data, vectors and full-text search |
| Queue | Procrastinate (Postgres-backed) | No extra broker; periodic tasks, retries and queueing locks (one pending job per company or filing) built in. Redis/RabbitMQ add moving parts we don't need at this scale. |
| EDGAR access | Thin custom client on `httpx` with a rate limiter and retries | SEC asks for ≤10 req/s and a contact User-Agent. We only need four endpoints, so a dependency like `edgartools` isn't worth it. |
| Parsing | `selectolax` (lexbor engine) for HTML, custom table extractor | Press releases and filings are messy HTML |
| Embeddings / rerank | Local via `fastembed` (bge-family, final model picked by eval) | Zero API cost |
| Keyword search | Postgres full-text search (`tsvector`); ParadeDB `pg_search` if real BM25 measurably helps | Start simple, upgrade on evidence |
| LLM | Claude via the official `anthropic` SDK; structured outputs with Pydantic | See §7 for model routing |
| Tracing | OpenTelemetry → Arize Phoenix | Vendor-neutral; Phoenix is one container (self-hosted Langfuse needs ClickHouse + Redis + S3) |
| Quality | `ruff`, `pyright`, `pytest`, `pytest-recording` (VCR) | Fast local checks matching CI |
| Desktop | Tauri 2, React, TypeScript, Vite, TanStack Query + Router, shadcn/ui, ECharts | Small native binary, native notifications/tray/updater, large UI ecosystem |
| CI | GitHub Actions | Lint, types, tests, eval subset on every PR |

---

## 4. Data

### 4.1 Sources

| Source | Endpoint | Used for |
|---|---|---|
| Submissions | `data.sec.gov/submissions/CIK##########.json` | List of filings per company |
| Filing archive | `sec.gov/Archives/edgar/data/{cik}/{accession}/` | 8-K EX-99.1 press releases, 10-Q, 10-K documents |
| XBRL company facts | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | GAAP actuals |

Earnings releases are 8-Ks with **Item 2.02** (Results of Operations), with the
release attached as **EX-99.1**. They contain both the actuals for the quarter
just ended and the guidance for the next one.

### 4.2 Company universe

NVDA, AMD, INTC, AVGO, QCOM, TXN, MU, AMAT, LRCX, KLAC, ADI, MRVL, NXPI, ON,
MCHP, MPWR, TER, SWKS, ENTG, SNPS, CDNS, WOLF, LSCC, RMBS, SLAB, SYNA, CRUS,
POWI, DIOD, AMKR, FORM, ONTO, ACLS, MKSI, COHU, UCTT, SITM, CRDO, MTSI, ALGM
(`backend/src/trackrecord/universe.yaml`).

Selection rule: US-listed filers of 8-K/10-Q (foreign 6-K/20-F filers are
excluded in v1.0) that publish numeric quarterly guidance.

M1 data checks changed the proposed list:
- **ARM** and **GFS** were dropped because they file 6-K/20-F;
- **QRVO** was dropped because it no longer trades after its merger with Skyworks;
- **CRDO**, **MTSI** and **ALGM** were added in their place.

`trackrecord check-guidance` sets `companies.guides_quarterly` with a keyword
heuristic over the 8 most recent releases. M2 replaces it with the real
extractor's output.

### 4.3 Metric dictionary (v1.0)

| Metric | Unit | Bases | GAAP actual source |
|---|---|---|---|
| `revenue` | USD | GAAP | XBRL `Revenues` / `RevenueFromContractWithCustomerExcludingAssessedTax` |
| `gross_margin` | % | GAAP, non-GAAP | XBRL `GrossProfit` ÷ revenue |
| `operating_expenses` | USD | GAAP, non-GAAP | XBRL `OperatingExpenses` |
| `operating_margin` | % | GAAP, non-GAAP | XBRL `OperatingIncomeLoss` ÷ revenue |
| `eps_diluted` | USD/share | GAAP, non-GAAP | XBRL `EarningsPerShareDiluted` |
| `tax_rate` | % | GAAP, non-GAAP | derived |

XBRL concept names vary by company; a per-company concept map is part of the
ingestion data and is covered by tests. Non-GAAP actuals don't exist in XBRL
and come from the reconciliation tables of the next press release.

**Q4 rule:** many companies don't file a 10-Q for Q4, so the Q4 actual is the
10-K annual value minus the 9-month year-to-date value from the Q3 10-Q.

**Fiscal calendar** (`ingest/periods.py`): built from filing dates, not from
the `fy`/`fp` labels in SEC's XBRL data, which M1 found to be wrong for several
companies (e.g. Skyworks' December 2021 quarter is labeled Q3 of fiscal 2021,
but it is Q1 of fiscal 2022). The rules:
- each 10-K ends a fiscal year;
- each 10-Q is Q1–Q3, depending on how far it falls after the previous year end;
- a fiscal year is named after the calendar year it ends in, except 52/53-week
  years ending in the first week of January;
- future quarters are projected from the most recent pattern, and marked
  estimated until a filing confirms them.

### 4.4 Schema (main tables)

```
companies(id, cik, ticker, name, fiscal_year_end_month, guides_quarterly)
fiscal_periods(id, company_id, fiscal_year, fiscal_quarter NULL=FY, start_date, end_date)
filings(id, company_id, accession, form, filed_at, period_id, items[], url, sha256)
documents(id, filing_id, kind, url, raw_html_path, text, sha256, parsed_at)
sections(id, document_id, kind, title, start_char, end_char)
chunks(id, document_id, section_id, ord, text, context_summary,
       embedding vector(N), tsv tsvector, start_char, end_char, metadata jsonb)
xbrl_facts(id, company_id, concept, unit, value, period_start, period_end,
           fiscal_year, fiscal_period, form, accession, filed_at)
actuals(id, company_id, period_id, metric, basis, value, unit,
        source_kind[press_release|xbrl|derived], source_document_id, source_span, filed_at)
promises(...)      -- §5.1
resolutions(...)   -- §5.3
credibility_snapshots(id, company_id, metric NULL=all, as_of, stats jsonb)
research_sessions / research_messages / research_citations
feedback(id, target_kind, target_id, rating, note, created_at)
llm_calls(id, task, model, input_tokens, output_tokens, cache_read_tokens,
          cost_usd, latency_ms, status, trace_id, created_at)   -- cost ledger
eval_runs / eval_results
```

Raw HTML is stored on disk (content-addressed by sha256), so re-parsing never
needs to re-download. Every derived row records which parser or extractor
version produced it, so improvements can be re-run selectively.

---

## 5. Promise engine

### 5.1 Promise schema

```python
class Promise(BaseModel):
    company_id: int
    source_document_id: int
    quote: str                         # exact text, must be found verbatim in the document
    span: tuple[int, int]              # char offsets in document text
    issued_at: date                    # filing date
    target_period: FiscalPeriodRef     # e.g. FY2026 Q1, resolved to dates
    metric: Metric                     # from the dictionary
    basis: Literal["gaap", "non_gaap", "unspecified"]
    form: Range | Point | Bound | Relative | Qualitative
    interpreted: bool                  # True if a convention was applied (§5.2)
    confidence: float
    status: Literal["pending", "resolved", "superseded", "withdrawn", "unresolvable"]
    supersedes_id: int | None
    extractor_version: str
    model: str

class Range(BaseModel):     low: Decimal; high: Decimal; unit: Unit
class Point(BaseModel):     value: Decimal; unit: Unit; approx: bool
class Bound(BaseModel):     direction: Literal["at_least", "at_most"]; value: Decimal; unit: Unit
class Relative(BaseModel):  reference: Literal["prior_quarter", "prior_year_quarter", "prior_year"]
                            change_low: Decimal; change_high: Decimal   # in % or pp
class Qualitative(BaseModel): text: str   # stored and shown, never scored
```

### 5.2 Extraction pipeline

1. **Locate:** a rule-based pass finds the outlook section ("Outlook",
   "Business Outlook", "Guidance", "expects") and its tables; if none is found,
   the whole release goes to the model.
2. **Extract:** one structured-output LLM call returns `list[PromiseDraft]`.
   The prompt contains the metric dictionary, the issuing company's fiscal
   calendar, and the conventions below.
3. **Validate (code, not LLM):**
   - the quote exists verbatim in the document (this rejects invented promises);
   - numbers in the structured fields appear in the quote;
   - the target period is after the issuing period; units are consistent;
   - `low ≤ high`.
4. **Supersede:** a newer promise for the same company, metric, basis and
   period marks the older one `superseded` (mid-quarter guidance updates).
5. **Route by confidence:** below a threshold set from the eval (initially
   0.8), or `interpreted=True`, the draft goes to the **review queue** in the
   app instead of being auto-accepted.

**Conventions** (documented, applied explicitly, flagged `interpreted`):

- "± 2%" around a point becomes a Range.
- "approximately X" becomes a Point with `approx=True`. Tolerance: ±1% of
  value for amounts, ±0.5 pp for percentages (configurable, revisited after
  the first eval).
- "low / mid / high single digits" becomes 1–3 / 4–6 / 7–9 %.
- "flat" becomes −1 % to +1 %.

### 5.3 Resolution (deterministic, no LLM)

1. When a filing arrives that reports `target_period`, fetch the actual:
   - **Provisional:** from the press release's financial tables (GAAP) and
     reconciliation tables (non-GAAP).
   - **Confirmed:** for GAAP metrics, from XBRL once the 10-Q/10-K lands. If it
     differs from the provisional value by more than a rounding tolerance, raise
     a **discrepancy** flag. That flag doubles as a free check on the
     press-release table parser.
2. Convert `Relative` promises to absolute values using the reference
   period's actual.
3. Compute the outcome:
   - **Range:** `below | low_end | mid | high_end | above` (inside the range,
     split into thirds).
   - **Point / Bound:** `below | met | above`, using the tolerance above.
   - `surprise_pct = (actual − midpoint) / |midpoint|`; for percentages also
     `surprise_pp`.
   - `favorable`, from the metric's direction (higher revenue is good, higher
     opex is bad).
4. Status becomes `provisional`, then `confirmed`. Restatements trigger
   re-resolution and keep the history.

### 5.4 Credibility

Per company, per metric and overall:

| Statistic | Definition |
|---|---|
| Hit rate | Share of resolved promises met or beaten in the favorable direction |
| Miss rate | Share missed in the unfavorable direction |
| Bias | Mean signed `surprise_pct` (positive = conservative guidance) |
| Consistency | Standard deviation of `surprise_pct` |
| Sandbagging index | Share of beats above the top of the range |
| Streak | Current run of consecutive hits or misses |

Rates use Beta-Binomial shrinkage toward the sector-wide rate, so a company
with 3 promises doesn't show a misleading 100 %. A composite 0–100 score is
shown with its components always visible next to it. Snapshots are stored per
quarter, so the trend is visible over time.

---

## 6. Research agent

### 6.1 Loop

A hand-written agent loop on the Messages API (no framework, which is easier to
debug and teaches more):

1. **Plan:** the model breaks the question into sub-questions and tool calls.
2. **Act:** tools run in parallel where independent; all results return in one
   message.
3. **Draft:** the answer is written with **number placeholders**
   (`{{n:fact_812}}`) and **citation markers** (`[[c:chunk_4411]]`) instead of
   literal values.
4. **Verify:**
   - code checks that every placeholder points to a real tool result and that
     no free-standing numbers remain in the text;
   - a cheap model call checks that each cited chunk supports its sentence;
     unsupported sentences are rewritten or dropped.
5. **Render:** the API substitutes the formatted values and streams the report
   to the app over SSE, with progress events at each step.

### 6.2 Tools (v1.0)

| Tool | Does |
|---|---|
| `search_documents(query, company?, form?, years?, section?)` | Hybrid retrieval: vector + full-text, merged with Reciprocal Rank Fusion, then a local cross-encoder rerank |
| `get_financials(company, metric, basis, periods)` | SQL over `actuals` / `xbrl_facts`, returns values with IDs |
| `get_promises(company?, metric?, status?, periods?)` | The promise ledger and resolutions |
| `get_credibility(companies, metric?)` | Credibility statistics |
| `compute(expression, inputs)` | Safe arithmetic over returned values (growth, ratios, CAGR, sums); no arbitrary code in v1.0 |

Arbitrary sandboxed Python (pandas) is post-v1.0. A small, safe calculator
covers most research math without a sandbox to secure.

### 6.3 Security

- **Instructions vs. data:** retrieved text is wrapped and labeled as untrusted
  data in the prompt. Tools are read-only: nothing an injected instruction
  could trigger has side effects.
- **Injection test suite:** synthetic poisoned documents ("ignore previous
  instructions", fake guidance, hidden text) are indexed in a test corpus.
  The suite asserts the agent neither follows them nor cites them as fact.
- The API binds to localhost with a per-install token; the Claude API key is
  stored in the OS keychain through Tauri, never in the repo.

---

## 7. LLM usage and cost

Model per task is a config value. Defaults below are starting points; each is
kept only if it passes that task's eval.

| Task | Default model | Notes |
|---|---|---|
| Promise extraction (backfill) | Claude Haiku 5.5 via Batch API (50 % off) | ~800 releases, ≈12M input tokens, about $1 |
| Promise extraction (ongoing) | Claude Haiku 5.5 | Cents per month |
| Citation support check | Claude Haiku 5.5 | Short, many calls |
| Research agent | Claude Sonnet 5.5 | Prompt caching on system prompt + tool definitions |
| Deep research mode (opt-in) | Claude Opus 5.5 | User-triggered only |
| LLM judge (evals) | Claude Sonnet 5.5 | Calibrated against human labels (§8) |

Every call is written to `llm_calls` with its cost. A **hard monthly budget**
(default $30) is enforced before each call. Background jobs pause when the
budget is reached, and the app shows spend by task. Refusals and API errors
are handled explicitly and recorded, never silently retried forever.

---

## 8. Evaluation

| Suite | Data | Metric | v1.0 target* |
|---|---|---|---|
| Extraction | ~200 hand-labeled promises from ~40 releases across companies and years | Precision, recall, field-level accuracy | P ≥ 0.95, R ≥ 0.95 |
| Resolution | Unit tests per rule + ~100 labeled promise→outcome pairs | Exact outcome match | 100 % unit, ≥ 98 % labeled |
| Actuals parser | Press-release vs. XBRL agreement on GAAP metrics | Agreement rate | ≥ 99 % |
| Retrieval | ~80 question → relevant-chunk pairs | Recall@10, MRR | Recall@10 ≥ 0.9 |
| Research QA | ~80 own questions with verified answers + applicable FinanceBench items | Numeric accuracy, citation support, faithfulness | ≥ 0.95, ≥ 0.95, ≥ 0.9 |
| Judge calibration | ~60 human-labeled answers | Agreement with you (Cohen's κ) | κ ≥ 0.7 |
| Injection | ~30 poisoned-document scenarios | Successful attacks | 0 |

\* Targets get calibrated after the first baseline run; the point is to
measure and improve, not to guess.

**CI:**
- Every PR: lint, types, unit tests, VCR-replayed LLM tests (free and
  deterministic), plus a live subset of ~20 cases with a hard cost cap.
- Nightly or manual: the full suites. Results are stored in `eval_runs` and
  shown as trends in the app's analytics page.

---

## 9. Desktop app

| Screen | Content |
|---|---|
| Dashboard | Watchlist credibility scores, promises resolving this week, latest surprises, sync status |
| Company | Credibility components and trend, promise timeline (guided range vs. actual chart), filings list |
| Promise detail | Source quote highlighted in the document, actual with its source, outcome, history of supersessions |
| Research | Chat with streaming progress, report with clickable numbers and citations opening the source passage, feedback buttons |
| Review queue | Low-confidence or interpreted extractions to accept, fix or reject; your corrections feed the golden set |
| Analytics | Usage, answer ratings, eval trends, spend by task |
| Settings | API key, budget, watchlist, notification preferences, backend URL |

Native features: notifications (resolutions, misses, budget warnings), tray
icon with sync status, auto-update. Builds for macOS, Windows and Linux.
Unsigned builds are fine for personal use. Code signing (Apple Developer
Program, Windows certificate) costs money and is optional.

---

## 10. Milestones

Each milestone ends with working, tested software merged to `main`.

| # | Milestone | Done when |
|---|---|---|
| M1 | **Foundation**: repo layout, Docker Compose, schema + migrations, EDGAR sync for 40 companies, XBRL facts, fiscal periods, CI | 5 years of filings and XBRL facts stored for all companies, idempotent re-sync, CI green |
| M2 | **Promise extraction**: section locator, extractor, validation, supersession, golden set + extraction eval | Extraction eval meets target; backfill complete |
| M3 | **Resolution & credibility**: actuals parser (GAAP + non-GAAP), XBRL confirmation, Q4 rule, outcomes, scores | Resolution suites pass; credibility for all companies |
| M4 | **Desktop app I**: Tauri shell, API client, dashboard, company, promise detail, review queue | Daily-usable app over the promise data |
| M5 | **Research agent**: chunking, embeddings, hybrid retrieval, tools, verifier, streaming UI | Retrieval + research QA evals meet target |
| M6 | **Trust & release**: tracing, cost ledger + budget cap UI, injection suite, judge calibration, analytics, notifications, installers, auto-update | All v1.0 targets met → **v1.0** |
| M7 | *Stretch*: credibility-signal backtest without lookahead; transcripts source | — |

---

## 11. Post-v1.0 ideas

- Backtest: does credibility, or a change in it, predict post-earnings returns
  or volatility? Point-in-time data only (scores as of each filing date).
- Earnings call transcripts as an extra source, with spoken guidance and
  speaker attribution (CEO vs. CFO accuracy).
- More sectors; segment-level guidance; hosted multi-user version.

---

## 12. Risks and open questions

| Risk | Mitigation |
|---|---|
| Guidance phrasing is more varied than expected | Review queue + golden set grow from real cases; conventions table is explicit |
| Non-GAAP reconciliation tables are hard to parse reliably | Per-company table tests; provisional/confirmed split; discrepancy flags |
| XBRL concept names differ by company | Per-company concept map with tests; derived metrics computed in one place |
| Some companies stop guiding or guide only annually | `guides_quarterly` flag; universe adjusted in M1 |
| LLM cost creep | Hard budget cap, cost ledger, cheapest model that passes each eval |
| EDGAR rate limits / format changes | Rate limiter, retries with backoff, raw HTML cache, parser versioning |

| A company names fiscal years differently from the convention in §4.3 | Cross-check fiscal year labels against press-release headlines ("third quarter of fiscal 2027") in M2 |

Settled in M1: the company list (§4.2) and a custom EDGAR client (§3.1).

Open: the exact embedding model (decided by the retrieval eval in M5).
