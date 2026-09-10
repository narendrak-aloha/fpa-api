import pytest

from fpa_be.registry.measures import (
    INTERNAL_MEASURES,
    MEASURES,
    AccountAmountMeasure,
    ComputedAdditiveMeasure,
    DistinctCountMeasure,
    MeasureKind,
    RatioMeasure,
    public_measures,
)
from fpa_be.registry.reference import ACCOUNTS_BY_CODE


def test_every_account_reference_resolves():
    for measure in MEASURES.values():
        if isinstance(measure, (AccountAmountMeasure, DistinctCountMeasure)):
            for account in measure.accounts:
                assert account in ACCOUNTS_BY_CODE, f"{measure.name} references unknown account {account}"


def test_every_computed_and_ratio_reference_resolves():
    for measure in MEASURES.values():
        if isinstance(measure, ComputedAdditiveMeasure):
            for _sign, ref in measure.terms:
                assert ref in MEASURES, f"{measure.name} references unknown measure {ref}"
        if isinstance(measure, RatioMeasure):
            assert measure.numerator in MEASURES, f"{measure.name} numerator {measure.numerator} unknown"
            assert measure.denominator in MEASURES, f"{measure.name} denominator {measure.denominator} unknown"


def test_ratio_measures_are_never_additive():
    for measure in MEASURES.values():
        if isinstance(measure, RatioMeasure):
            assert measure.kind == MeasureKind.RATIO


@pytest.mark.parametrize("name", ["gross_margin_pct", "utilisation", "realisation"])
def test_known_ratio_measures(name):
    assert MEASURES[name].kind == MeasureKind.RATIO


@pytest.mark.parametrize("name", ["services_revenue", "total_revenue", "gross_margin", "delivery_cost"])
def test_known_additive_measures(name):
    assert MEASURES[name].kind == MeasureKind.ADDITIVE


def test_headcount_is_semi_additive():
    assert MEASURES["headcount"].kind == MeasureKind.SEMI_ADDITIVE


def test_internal_measures_hidden_from_public_list():
    public = public_measures()
    assert INTERNAL_MEASURES.isdisjoint(public.keys())
    assert "utilisation" in public  # public-facing ratio still resolvable


def test_gross_margin_is_revenue_minus_cogs():
    gm = MEASURES["gross_margin"]
    assert gm.terms == (("+", "total_revenue"), ("-", "total_cogs"))
