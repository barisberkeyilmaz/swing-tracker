from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.web.helpers import _utc_to_local, build_cash_flows, calc_capital_summary


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

    assert capital.net_deposits == 10_000
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

    page = build_cash_flows(repo)

    assert page.total == 3
    assert page.total_pages == 1
    amounts_by_type = {flow.flow_type: flow.amount for flow in page.items}
    assert amounts_by_type == {"deposit": 10_000, "buy": -1_000, "sell": 1_100}
    # En yeni ustte: ilk item son satis, bakiye 10_100
    assert page.items[0].flow_type == "sell"
    assert page.items[0].balance == 10_100


def test_cash_flow_pagination_slices_correctly() -> None:
    repo = make_repo()

    for i in range(25):
        repo.add_cash_transaction(
            100,
            "deposit",
            description=f"Yatirma {i}",
        )

    page1 = build_cash_flows(repo, page=1, per_page=10)
    assert page1.total == 25
    assert page1.total_pages == 3
    assert page1.page == 1
    assert len(page1.items) == 10

    page3 = build_cash_flows(repo, page=3, per_page=10)
    assert page3.page == 3
    assert len(page3.items) == 5  # son sayfa kismi dolu

    # Sinirlari asan sayfa son sayfaya clamp edilir
    page_overflow = build_cash_flows(repo, page=99, per_page=10)
    assert page_overflow.page == 3


def test_utc_timestamps_are_converted_to_local_tz() -> None:
    tr = ZoneInfo("Europe/Istanbul")
    # Nisan 2026 -> TSI = UTC+3
    assert _utc_to_local("2026-04-15 16:01:08", tr) == "2026-04-15 19:01"
    # Saniyesiz format da desteklenir
    assert _utc_to_local("2026-04-15 16:01", tr) == "2026-04-15 19:01"
    # Bos/gecersiz timestamp kirmaz
    assert _utc_to_local("", tr) == ""
