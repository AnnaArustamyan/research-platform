"""Score stored promises against the hand-labeled golden set (docs/design.md §8).

A predicted promise matches a golden one when metric, basis and target period
agree; it is then value-correct when low and high agree within tolerance.
Guidance on `other` metrics is not scored.
"""

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from trackrecord.models import Filing, Metric, Promise, ReviewStatus

GOLDEN_PATH = Path(__file__).resolve().parents[3] / "evals" / "extraction" / "golden.yaml"
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 0.005

Key = tuple[str, str, str]  # (metric, basis, period label)


@dataclass(frozen=True)
class Labeled:
    metric: str
    basis: str
    period: str
    low: float | None
    high: float | None

    @property
    def key(self) -> Key:
        # Revenue has one basis; a statement without a basis is GAAP.
        basis = "any" if self.metric == "revenue" else self.basis.replace("unspecified", "gaap")
        return (self.metric, basis, self.period)


@dataclass
class GoldFiling:
    accession: str
    ticker: str
    reported: str
    promises: list[Labeled]


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldFiling]:
    data = yaml.safe_load(path.read_text())
    return [
        GoldFiling(
            accession=f["accession"],
            ticker=str(f["ticker"]),
            reported=f["reported"],
            promises=[_labeled(p) for p in f["promises"]],
        )
        for f in data["filings"]
    ]


def _labeled(raw: dict[str, Any]) -> Labeled:
    # YAML 1.1 reads exponents without a sign ("6.37e9") as strings.
    def number(value: Any) -> float | None:
        return None if value is None else float(value)

    return Labeled(
        metric=raw["metric"],
        basis=raw["basis"],
        period=raw["period"],
        low=number(raw.get("low")),
        high=number(raw.get("high")),
    )


def values_match(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE)


@dataclass
class FilingScore:
    accession: str
    ticker: str
    true_positives: int = 0
    value_correct: int = 0
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    value_errors: list[str] = field(default_factory=list)
    calendar_error: str | None = None


@dataclass
class Report:
    filings: list[FilingScore]

    @property
    def true_positives(self) -> int:
        return sum(f.true_positives for f in self.filings)

    @property
    def precision(self) -> float:
        predicted = self.true_positives + sum(len(f.false_positives) for f in self.filings)
        return self.true_positives / predicted if predicted else 1.0

    @property
    def recall(self) -> float:
        expected = self.true_positives + sum(len(f.false_negatives) for f in self.filings)
        return self.true_positives / expected if expected else 1.0

    @property
    def value_accuracy(self) -> float:
        """Share of matched promises whose values are also right."""
        matched = self.true_positives
        return sum(f.value_correct for f in self.filings) / matched if matched else 1.0

    def summary(self) -> dict[str, Any]:
        return {
            "filings": len(self.filings),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "value_accuracy": round(self.value_accuracy, 4),
            "calendar_errors": sum(f.calendar_error is not None for f in self.filings),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "filings": [asdict(f) for f in self.filings]}


def score_filing(gold: GoldFiling, predicted: list[Labeled], reported: str | None) -> FilingScore:
    score = FilingScore(gold.accession, gold.ticker)
    if reported != gold.reported:
        score.calendar_error = f"reports {reported}, expected {gold.reported}"

    expected = {p.key: p for p in gold.promises}
    seen: set[Key] = set()
    for p in predicted:
        if p.key in seen:
            continue  # e.g. revenue given with both bases
        seen.add(p.key)
        label = expected.get(p.key)
        if label is None:
            score.false_positives.append(_describe(p))
            continue
        score.true_positives += 1
        if values_match(p.low, label.low) and values_match(p.high, label.high):
            score.value_correct += 1
        else:
            score.value_errors.append(f"{_describe(label)}, got {p.low} to {p.high}")
    score.false_negatives = [_describe(p) for k, p in expected.items() if k not in seen]
    return score


def _describe(p: Labeled) -> str:
    return f"{p.period} {p.basis} {p.metric} {p.low} to {p.high}"


def as_labeled(p: Promise) -> Labeled:
    period = f"FY{p.target_fiscal_year}" + (
        f" Q{p.target_fiscal_quarter}" if p.target_fiscal_quarter else ""
    )
    return Labeled(
        metric=p.metric.value,
        basis=p.basis.value,
        period=period,
        low=float(p.low) if p.low is not None else None,
        high=float(p.high) if p.high is not None else None,
    )


async def score_database(session: AsyncSession, golden: list[GoldFiling]) -> Report:
    scores = []
    for gold in golden:
        filing = await session.scalar(
            select(Filing)
            .where(Filing.accession == gold.accession)
            .options(selectinload(Filing.period))
        )
        promises = []
        if filing is not None:
            promises = (
                await session.scalars(
                    select(Promise)
                    .where(
                        Promise.filing_id == filing.id,
                        Promise.metric != Metric.OTHER,
                        Promise.review_status != ReviewStatus.REJECTED,
                    )
                    .order_by(Promise.id)
                )
            ).all()
        reported = filing.period.label if filing is not None and filing.period else None
        scores.append(score_filing(gold, [as_labeled(p) for p in promises], reported))
    return Report(scores)


async def golden_filing_ids(session: AsyncSession, golden: list[GoldFiling]) -> list[int]:
    accessions = [g.accession for g in golden]
    return list(
        (await session.scalars(select(Filing.id).where(Filing.accession.in_(accessions)))).all()
    )
