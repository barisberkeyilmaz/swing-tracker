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


from datetime import datetime, timedelta

from swing_tracker.config import AllocationTarget as _AT
from swing_tracker.core.allocation import check_rebalance, compute_weights as _cw


def _report_with_drift():
    holdings = [{"symbol": "VOO", "shares": 1.0}, {"symbol": "QTUM", "shares": 1.0}]
    prices = {"VOO": 300.0, "QTUM": 100.0}
    targets = {
        "VOO": _AT("VOO", 28, "AMEX", "core", ""),
        "QTUM": _AT("QTUM", 72, "NASDAQ", "satellite", ""),
    }
    return _cw(holdings, prices, targets)


def test_check_rebalance_flags_drifted_legs():
    rep = _report_with_drift()  # VOO 75% vs 28% -> +47 drift
    now = datetime(2026, 7, 24)
    alert = check_rebalance(rep, threshold_pct=5.0, last_review=None,
                            interval_days=91, now=now)
    assert {l.symbol for l in alert.drifted_legs} == {"VOO", "QTUM"}


def test_review_due_when_never_reviewed():
    rep = _report_with_drift()
    alert = check_rebalance(rep, 5.0, None, 91, datetime(2026, 7, 24))
    assert alert.review_due is True
    assert alert.next_review_date is None


def test_review_not_due_within_interval():
    rep = _report_with_drift()
    last = datetime(2026, 7, 1)
    alert = check_rebalance(rep, 5.0, last, 91, datetime(2026, 7, 24))
    assert alert.review_due is False
    assert alert.next_review_date == (last + timedelta(days=91)).date()


def test_review_due_after_interval():
    rep = _report_with_drift()
    last = datetime(2026, 1, 1)
    alert = check_rebalance(rep, 5.0, last, 91, datetime(2026, 7, 24))
    assert alert.review_due is True
