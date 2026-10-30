"""What the model returns: each guidance statement in its literal shape.

Numbers are copied as written ("$108.0 billion" is value 108.0, scale billions)
so code can check them against the quote. Turning statements into absolute
ranges is done by code in `normalize.py`, where the conventions are explicit.
"""

from typing import Literal

from pydantic import BaseModel, Field

MetricName = Literal[
    "revenue",
    "gross_margin",
    "operating_expenses",
    "operating_margin",
    "eps_diluted",
    "tax_rate",
    "other",
]
Scale = Literal["units", "thousands", "millions", "billions"]


class ExtractedPromise(BaseModel):
    quote: str = Field(
        description="The exact text containing the statement, copied verbatim from the "
        "document: one sentence, bullet or table row. Keep table rows as they appear, "
        'including " | " separators.'
    )
    metric: MetricName
    metric_name: str = Field(
        description='The name used in the document, e.g. "Non-GAAP diluted EPS".'
    )
    basis: Literal["gaap", "non_gaap", "unspecified"]
    fiscal_year: int = Field(
        description="Fiscal year of the guided period, as the company numbers it."
    )
    fiscal_quarter: int | None = Field(description="1-4, or null for the full fiscal year.")
    kind: Literal["range", "point", "plus_minus", "bound", "relative", "qualitative"]
    unit: Literal["usd", "percent", "usd_per_share", "none"]
    scale: Scale = Field(
        description='Scale of USD amounts ("units" for percent and per-share values).'
    )
    value: float | None = Field(
        description="point: the value; plus_minus: the center; bound: the limit."
    )
    low: float | None = Field(description="range: the low end, as written.")
    high: float | None = Field(description="range: the high end, as written.")
    approximate: bool = Field(description='True for "approximately", "about", "around".')
    plus_minus: float | None = Field(description="plus_minus: the tolerance, as written.")
    plus_minus_unit: (
        Literal["percent_of_value", "basis_points", "percentage_points", "same_as_value"] | None
    ) = Field(
        description='"$13 billion +/- $300 million" is same_as_value, plus_minus_scale millions; '
        '"$108 billion +/- 2%" is percent_of_value; "74.0% +/- 50 basis points" is basis_points.'
    )
    plus_minus_scale: Scale | None
    bound_direction: Literal["at_least", "at_most"] | None
    relative_reference: Literal["prior_quarter", "prior_year_quarter", "prior_year"] | None
    change_low: float | None = Field(description="relative: low end of the change in percent.")
    change_high: float | None = Field(description="relative: high end of the change in percent.")
    change_words: str | None = Field(
        description='relative, when the change is in words: e.g. "mid single digits", "flat".'
    )
    confidence: float = Field(
        description="0-1: how sure you are this is a correct, forward-looking promise."
    )


class ExtractionOutput(BaseModel):
    promises: list[ExtractedPromise]
