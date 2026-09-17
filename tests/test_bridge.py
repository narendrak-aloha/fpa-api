from decimal import Decimal

from fpa_project.dsl.bridge import BridgeLine, decompose


def test_bridge_ties_and_volume_plus_mix_equals_quantity_variance():
    result = decompose(
        [
            BridgeLine("PL", Decimal("10"), Decimal("12"), Decimal("100"), Decimal("110"), Decimal("1.0"), Decimal("1.1")),
            BridgeLine("PL", Decimal("20"), Decimal("18"), Decimal("200"), Decimal("190"), Decimal("1.0"), Decimal("1.1")),
        ]
    )[0]
    assert abs(result.residual) < Decimal("0.000001")
    assert result.volume + result.mix == Decimal("-200")


def test_bridge_keeps_fx_separate_from_operational_legs():
    result = decompose(
        [BridgeLine("PL", Decimal("10"), Decimal("10"), Decimal("100"), Decimal("100"), Decimal("1.0"), Decimal("1.2"))]
    )[0]
    assert result.price == 0
    assert result.volume == 0
    assert result.mix == 0
    assert result.fx == Decimal("200")
    assert result.gap == result.fx
