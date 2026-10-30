"""Credibility statistics over resolved promises (docs/design.md §5.4).

The headline score is the hit rate shrunk toward the sector-wide rate
(Beta-Binomial), so a company with few resolved promises isn't shown as 0% or
100%. The components are always reported next to it.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trackrecord.models import (
    Basis,
    Company,
    FiscalPeriod,
    Metric,
    Outcome,
    Promise,
    PromiseStatus,
    Resolution,
)
from trackrecord.resolve import LOWER_IS_BETTER

PRIOR_STRENGTH = 10  # the sector rate counts as this many promises
DEFAULT_PRIOR = 0.7  # used until any promise in the sector is resolved


@dataclass(frozen=True)
class ResolvedPromise:
    company_id: int
    metric: Metric
    basis: Basis
    period_end: date
    hit: bool
    outcome: Outcome
    surprise_pct: Decimal | None
    surprise_pp: Decimal | None

    @property
    def beat(self) -> bool:
        """Beyond the guided range in the good direction."""
        good = Outcome.BELOW if self.metric in LOWER_IS_BETTER else Outcome.ABOVE
        return self.outcome == good


@dataclass
class MetricStats:
    metric: Metric
    basis: Basis
    resolved: int
    hits: int
    beats: int
    # Mean and spread of the surprise vs. the midpoint, positive = better than guided.
    mean_surprise_pct: float | None
    stdev_surprise_pct: float | None
    mean_surprise_pp: float | None


@dataclass
class Credibility:
    company_id: int
    resolved: int
    hits: int
    hit_rate: float | None
    score: int | None  # 0-100: the shrunk hit rate
    beat_rate: float | None  # the "sandbagging index"
    streak: int  # +n consecutive hits, -n consecutive misses, most recent first
    by_metric: list[MetricStats] = field(default_factory=list)


def shrunk_rate(hits: int, n: int, prior: float, strength: int = PRIOR_STRENGTH) -> float:
    return (hits + prior * strength) / (n + strength)


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _streak(resolved: Sequence[ResolvedPromise]) -> int:
    # Within a period, misses come first, so a miss in the latest period ends a streak.
    ordered = sorted(resolved, key=lambda r: (r.period_end, not r.hit), reverse=True)
    if not ordered:
        return 0
    first = ordered[0].hit
    count = 0
    for r in ordered:
        if r.hit != first:
            break
        count += 1
    return count if first else -count


def summarize(company_id: int, resolved: Sequence[ResolvedPromise], prior: float) -> Credibility:
    n = len(resolved)
    hits = sum(r.hit for r in resolved)
    by_key: dict[tuple[Metric, Basis], list[ResolvedPromise]] = {}
    for r in resolved:
        by_key.setdefault((r.metric, r.basis), []).append(r)

    by_metric = []
    for (metric, basis), group in sorted(by_key.items()):
        pct = [float(r.surprise_pct) for r in group if r.surprise_pct is not None]
        pp = [float(r.surprise_pp) for r in group if r.surprise_pp is not None]
        by_metric.append(
            MetricStats(
                metric=metric,
                basis=basis,
                resolved=len(group),
                hits=sum(r.hit for r in group),
                beats=sum(r.beat for r in group),
                mean_surprise_pct=_mean(pct),
                stdev_surprise_pct=statistics.stdev(pct) if len(pct) > 1 else None,
                mean_surprise_pp=_mean(pp),
            )
        )
    return Credibility(
        company_id=company_id,
        resolved=n,
        hits=hits,
        hit_rate=hits / n if n else None,
        score=round(100 * shrunk_rate(hits, n, prior)) if n else None,
        beat_rate=sum(r.beat for r in resolved) / n if n else None,
        streak=_streak(resolved),
        by_metric=by_metric,
    )


async def load_resolved(
    session: AsyncSession, company_id: int | None = None
) -> list[ResolvedPromise]:
    stmt = (
        select(Promise, Resolution, FiscalPeriod.end_date)
        .join(Resolution, Resolution.promise_id == Promise.id)
        .join(FiscalPeriod, FiscalPeriod.id == Promise.target_period_id)
        .where(Promise.status == PromiseStatus.RESOLVED)
    )
    if company_id is not None:
        stmt = stmt.where(Promise.company_id == company_id)
    rows = (await session.execute(stmt)).all()
    return [
        ResolvedPromise(
            company_id=p.company_id,
            metric=p.metric,
            basis=p.basis,
            period_end=end,
            hit=r.hit,
            outcome=r.outcome,
            surprise_pct=r.surprise_pct,
            surprise_pp=r.surprise_pp,
        )
        for p, r, end in rows
    ]


async def sector_prior(session: AsyncSession) -> float:
    everything = await load_resolved(session)
    return sum(r.hit for r in everything) / len(everything) if everything else DEFAULT_PRIOR


async def company_credibility(session: AsyncSession, company: Company) -> Credibility:
    return summarize(
        company.id, await load_resolved(session, company.id), await sector_prior(session)
    )


async def all_credibility(session: AsyncSession) -> list[Credibility]:
    everything = await load_resolved(session)
    prior = sum(r.hit for r in everything) / len(everything) if everything else DEFAULT_PRIOR
    by_company: dict[int, list[ResolvedPromise]] = {}
    for r in everything:
        by_company.setdefault(r.company_id, []).append(r)
    company_ids = (await session.scalars(select(Company.id).where(Company.active))).all()
    return [summarize(cid, by_company.get(cid, []), prior) for cid in company_ids]
