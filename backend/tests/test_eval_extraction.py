import re

from trackrecord.evals.extraction import GoldFiling, Labeled, Report, load_golden, score_filing
from trackrecord.models import Metric


def test_golden_set_is_well_formed() -> None:
    golden = load_golden()
    assert len(golden) >= 19
    assert len({g.accession for g in golden}) == len(golden)
    metrics = {m.value for m in Metric} - {"other"}
    for filing in golden:
        assert re.fullmatch(r"FY\d{4} Q[1-4]", filing.reported)
        keys = [p.key for p in filing.promises]
        assert len(keys) == len(set(keys)), filing.accession
        for p in filing.promises:
            assert p.metric in metrics
            assert p.basis in {"gaap", "non_gaap", "unspecified"}
            assert re.fullmatch(r"FY\d{4}( Q[1-4])?", p.period)
            assert isinstance(p.low, float) and isinstance(p.high, float)
            assert p.low <= p.high


def gold(*promises: Labeled) -> GoldFiling:
    return GoldFiling("acc", "X", "FY2026 Q2", list(promises))


REVENUE = Labeled("revenue", "unspecified", "FY2026 Q3", 5.8e9, 6.3e9)
GM_GAAP = Labeled("gross_margin", "gaap", "FY2026 Q3", 52.0, 54.0)


def test_perfect_prediction() -> None:
    predicted = [
        Labeled("revenue", "gaap", "FY2026 Q3", 5.8e9, 6.3e9),  # basis ignored for revenue
        Labeled("revenue", "non_gaap", "FY2026 Q3", 5.8e9, 6.3e9),  # same key, counted once
        Labeled("gross_margin", "gaap", "FY2026 Q3", 52.0, 54.0),
    ]
    score = score_filing(gold(REVENUE, GM_GAAP), predicted, "FY2026 Q2")
    report = Report([score])

    assert (report.precision, report.recall, report.value_accuracy) == (1.0, 1.0, 1.0)
    assert score.calendar_error is None


def test_misses_extras_and_wrong_values() -> None:
    predicted = [
        Labeled("revenue", "unspecified", "FY2026 Q3", 5.8e9, 6.4e9),  # wrong high
        Labeled("gross_margin", "non_gaap", "FY2026 Q3", 53.0, 55.0),  # not in gold
    ]
    score = score_filing(gold(REVENUE, GM_GAAP), predicted, "FY2026 Q1")
    report = Report([score])

    assert report.precision == 0.5
    assert report.recall == 0.5
    assert report.value_accuracy == 0.0
    assert score.false_negatives == ["FY2026 Q3 gaap gross_margin 52.0 to 54.0"]
    assert score.calendar_error == "reports FY2026 Q1, expected FY2026 Q2"


def test_values_within_half_a_percent_match() -> None:
    predicted = [Labeled("revenue", "gaap", "FY2026 Q3", 5.81e9, 6.29e9)]
    assert score_filing(gold(REVENUE), predicted, "FY2026 Q2").value_correct == 1


def test_no_guidance_and_no_predictions_is_perfect() -> None:
    report = Report([score_filing(gold(), [], "FY2026 Q2")])
    assert (report.precision, report.recall) == (1.0, 1.0)
