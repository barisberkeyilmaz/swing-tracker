from __future__ import annotations

import sqlite3

from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.web.helpers import build_cash_flows, calc_capital_summary


def make_repo() -> Repository:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def test_sale_is_not_counted_twice_in_capital_summary() -> None:
    repo = make_repo()

    repo.add_cash_transaction(10_000, "deposit", description="Baslangic")

    trade_id = repo.create_trade(
        symbol="THYAO",
        direction="long",
        status="open",
        entry_price=100,
        entry_date="2026-04-15 10:00",
        shares=10,
    )
    repo.add_cash_transaction(-1_000, "buy", related_trade_id=trade_id, description="THYAO alis")

    repo.record_exit(
        trade_id=trade_id,
        exit_type="manual",
        shares=5,
        price=110,
        pnl=50,
        pnl_pct=10,
    )
    repo.add_cash_transaction(550, "sell", related_trade_id=trade_id, description="THYAO satis")
    repo.update_trade_status(trade_id, "partial_exit")

    capital = calc_capital_summary(repo)

    assert capital.deposits == 10_000
    assert capital.total_bought == 1_000
    assert capital.total_sold == 550
    assert capital.available_cash == 9_550
    assert capital.open_cost == 500
    assert capital.realized_pnl == 50
    assert capital.total_portfolio == 10_050


def test_cash_flow_log_does_not_duplicate_buy_and_sell_transactions() -> None:
    repo = make_repo()

    repo.add_cash_transaction(10_000, "deposit", description="Baslangic")

    trade_id = repo.create_trade(
        symbol="THYAO",
        direction="long",
        status="closed",
        entry_price=100,
        entry_date="2026-04-15 10:00",
        shares=10,
    )
    repo.add_cash_transaction(-1_000, "buy", related_trade_id=trade_id, description="THYAO alis")

    repo.record_exit(
        trade_id=trade_id,
        exit_type="manual",
        shares=10,
        price=110,
        pnl=100,
        pnl_pct=10,
    )
    repo.add_cash_transaction(1_100, "sell", related_trade_id=trade_id, description="THYAO satis")

    flows = build_cash_flows(repo)

    assert len(flows) == 3
    amounts_by_type = {flow.flow_type: flow.amount for flow in flows}
    assert amounts_by_type == {"deposit": 10_000, "buy": -1_000, "sell": 1_100}
    assert flows[-1].balance == 10_100
