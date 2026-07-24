from swing_tracker.config import AllocationTarget
from swing_tracker.core.allocation import compute_weights


def _targets():
    return {
        "VOO": AllocationTarget("VOO", 28, "AMEX", "core", "S&P 500"),
        "VXUS": AllocationTarget("VXUS", 12, "NASDAQ", "core", "Ex-US"),
        "QTUM": AllocationTarget("QTUM", 20, "NASDAQ", "satellite", "AI"),
        "FIW": AllocationTarget("FIW", 20, "AMEX", "satellite", "Su"),
        "XLE": AllocationTarget("XLE", 20, "AMEX", "satellite", "Enerji"),
    }


def test_compute_weights_basic():
    holdings = [
        {"symbol": "VOO", "shares": 1.0},
        {"symbol": "QTUM", "shares": 1.0},
    ]
    prices = {"VOO": 300.0, "QTUM": 100.0, "VXUS": 80.0, "FIW": 100.0, "XLE": 60.0}
    rep = compute_weights(holdings, prices, _targets())
    assert rep.total_value_usd == 400.0
    voo = next(l for l in rep.legs if l.symbol == "VOO")
    assert voo.value_usd == 300.0
    assert round(voo.weight_pct, 1) == 75.0
    assert round(voo.drift_pct, 1) == 47.0  # 75 - 28
    assert round(rep.core_weight_pct, 1) == 75.0
    assert round(rep.satellite_weight_pct, 1) == 25.0


def test_compute_weights_marks_stale_price():
    holdings = [{"symbol": "VOO", "shares": 2.0}, {"symbol": "QTUM", "shares": 1.0}]
    prices = {"QTUM": 100.0}  # VOO fiyati yok
    rep = compute_weights(holdings, prices, _targets())
    voo = next(l for l in rep.legs if l.symbol == "VOO")
    assert voo.price_stale is True
    assert voo.value_usd == 0.0
    assert rep.total_value_usd == 100.0  # sadece QTUM


def test_compute_weights_empty_holdings():
    rep = compute_weights([], {}, _targets())
    assert rep.total_value_usd == 0.0
    assert all(l.weight_pct == 0.0 for l in rep.legs)
    assert rep.core_weight_pct == 0.0
