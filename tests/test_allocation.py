from swing_tracker.config import AllocationTarget
from swing_tracker.core.allocation import compute_weights, plan_rebalance


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


def test_compute_weights_zero_price_is_stale():
    holdings = [{"symbol": "VOO", "shares": 5.0}, {"symbol": "QTUM", "shares": 1.0}]
    prices = {"VOO": 0.0, "QTUM": 100.0, "VXUS": 80.0, "FIW": 100.0, "XLE": 60.0}
    rep = compute_weights(holdings, prices, _targets())
    voo = next(leg for leg in rep.legs if leg.symbol == "VOO")
    assert voo.price_stale is True
    assert voo.value_usd == 0.0
    assert rep.total_value_usd == 100.0  # only QTUM counts


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


from swing_tracker.core.allocation import plan_dca, AllocationReport as _AR, AllocationLeg as _AL


def _leg(sym, group, target, value, price):
    return _AL(sym, "AMEX", group, target, value / price if price else 0,
               price, value, 0.0, 0.0, False)


def _report(legs):
    total = sum(l.value_usd for l in legs)
    return _AR(legs=legs, total_value_usd=total, core_weight_pct=0.0,
               satellite_weight_pct=0.0, usdtry=None)


def test_plan_dca_all_to_most_underweight():
    # A hedef 50% deger 100 (oran 200), B hedef 50% deger 300 (oran 600)
    rep = _report([_leg("A", "core", 50, 100, 10.0), _leg("B", "core", 50, 300, 10.0)])
    plan = plan_dca(rep, contribution_usd=100.0, fractional=True)
    buys = {i.symbol: i.buy_usd for i in plan.items}
    assert buys.get("A") == 100.0  # tumu A'ya (en geride)
    assert "B" not in buys
    assert plan.deployed_usd == 100.0


def test_plan_dca_equal_ratio_splits_by_target():
    rep = _report([_leg("A", "core", 50, 100, 10.0), _leg("B", "core", 50, 100, 10.0)])
    plan = plan_dca(rep, 100.0, fractional=True)
    buys = {i.symbol: round(i.buy_usd, 2) for i in plan.items}
    assert buys == {"A": 50.0, "B": 50.0}


def test_plan_dca_whole_share_rounds_and_leaves_leftover():
    # 100$ butce, fiyat 60$, tam lot -> 1 lot (60$), 40$ artik
    rep = _report([_leg("A", "core", 100, 0, 60.0)])
    plan = plan_dca(rep, 100.0, fractional=False)
    assert plan.items[0].buy_shares == 1
    assert plan.items[0].buy_usd == 60.0
    assert round(plan.leftover_usd, 2) == 40.0


def test_plan_dca_zero_contribution_empty():
    rep = _report([_leg("A", "core", 100, 100, 10.0)])
    plan = plan_dca(rep, 0.0, fractional=True)
    assert plan.items == []


def test_plan_dca_ignores_zero_price_leg():
    stale_leg = _AL("ZERO", "AMEX", "core", 50.0, 0.0, 0.0, 0.0, 0.0, 0.0, True)
    normal_leg = _leg("GOOD", "core", 50.0, 100.0, 10.0)
    rep = _report([stale_leg, normal_leg])
    plan = plan_dca(rep, 100.0, fractional=True)
    # Should not allocate to zero-price stale leg
    assert not any(item.symbol == "ZERO" for item in plan.items)
    # Should allocate to normal leg
    assert any(item.symbol == "GOOD" for item in plan.items)
    assert plan.deployed_usd == 100.0


def test_rebalance_net_cash_equals_contribution():
    # A hedef 50 deger 100, B hedef 50 deger 300; katki 100 -> T'=500
    rep = _report([_leg("A", "core", 50, 100, 10.0), _leg("B", "core", 50, 300, 10.0)])
    plan = plan_rebalance(rep, contribution_usd=100.0, fractional=True)
    acts = {i.symbol: (i.action, round(i.amount_usd, 2)) for i in plan.items}
    assert acts["A"] == ("BUY", 150.0)   # 250 - 100
    assert acts["B"] == ("SELL", 50.0)   # 250 - 300
    assert round(plan.net_cash_usd, 2) == 100.0  # 150 - 50


def test_rebalance_zero_contribution_is_cash_neutral():
    rep = _report([_leg("A", "core", 50, 100, 10.0), _leg("B", "core", 50, 300, 10.0)])
    plan = plan_rebalance(rep, 0.0, fractional=True)
    assert round(plan.net_cash_usd, 2) == 0.0
    acts = {i.symbol: i.action for i in plan.items}
    assert acts["A"] == "BUY" and acts["B"] == "SELL"


def test_rebalance_on_target_holds():
    rep = _report([_leg("A", "core", 50, 200, 10.0), _leg("B", "core", 50, 200, 10.0)])
    plan = plan_rebalance(rep, 0.0, fractional=True)
    assert all(i.action == "HOLD" for i in plan.items)


def test_rebalance_whole_share_mode():
    # Test fractional=False mode with two legs
    rep = _report([_leg("A", "core", 50, 100, 10.0), _leg("B", "core", 50, 300, 10.0)])
    plan = plan_rebalance(rep, contribution_usd=100.0, fractional=False)
    # Assert no exception raised
    assert plan is not None
    # Assert every item's shares is a whole number
    for item in plan.items:
        assert item.shares == int(item.shares), f"Shares {item.shares} is not whole"
    # Assert all actions are in valid set
    valid_actions = {"BUY", "SELL", "HOLD"}
    for item in plan.items:
        assert item.action in valid_actions, f"Action {item.action} not in {valid_actions}"
