import pytest

from factor.broker.ctrader import lots_to_protocol_volume, price_distance_to_relative


def test_lots_to_volume_xau():
    # 0.01 lot × 100 oz = 1 oz → protocol 100
    assert lots_to_protocol_volume(0.01, 100) == 100


def test_lots_to_volume_eur():
    # 0.01 lot × 100_000 = 1000 units → protocol 100_000
    assert lots_to_protocol_volume(0.01, 100_000) == 100_000


def test_relative_from_price_distance():
    # 300 pts × 0.01 = $3.00 → relative 300_000
    assert price_distance_to_relative(3.0) == 300_000


def test_relative_rejects_zero():
    with pytest.raises(ValueError):
        price_distance_to_relative(0)
