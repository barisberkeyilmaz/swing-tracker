"""Tests for whatif persistent store: config, schema/CRUD, job steps."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.config import WhatIfConfig, load_config
from swing_tracker.core.whatif_store import fill_pending, row_to_bt
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


class TestWhatIfConfig:
    def test_defaults(self):
        cfg = WhatIfConfig()
        assert cfg.enabled is True
        assert cfg.max_holding_days == 60

    def test_load_from_toml(self):
        config = load_config()
        assert isinstance(config.whatif, WhatIfConfig)
        assert config.whatif.max_holding_days == 60


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _insert_signal(repo, symbol="THYAO", created_at="2026-07-01 07:30:00",
                   score=50, price=100.0):
    sid = repo.log_signal(
        symbol=symbol, signal_type="buy", indicator="score", strength="medium",
        price_at_signal=price, score=score,
        indicator_values={"entry_score": score // 10, "reasons": "test"},
    )
    repo._conn.execute(
        "UPDATE signals_log SET created_at = ? WHERE id = ?", (created_at, sid)
    )
    repo._conn.commit()
    return sid


def _pending_fields(signal_id, symbol="THYAO", signal_time="2026-07-01 07:30:00",
                    score=5, price=100.0):
    return {
        "signal_id": signal_id, "symbol": symbol, "signal_time": signal_time,
        "score": score, "price_at_signal": price,
    }


class TestWhatIfTradeCrud:
    def test_insert_and_get(self, repo):
        sid = _insert_signal(repo)
        rowid = repo.insert_whatif_trade(_pending_fields(sid))
        assert rowid is not None

        rows = repo.get_whatif_trades()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["symbol"] == "THYAO"
        assert rows[0]["score"] == 5

    def test_insert_or_ignore_idempotent(self, repo):
        sid = _insert_signal(repo)
        assert repo.insert_whatif_trade(_pending_fields(sid)) is not None
        assert repo.insert_whatif_trade(_pending_fields(sid)) is None  # ayni signal_id
        assert len(repo.get_whatif_trades()) == 1

    def test_status_filter_and_order(self, repo):
        s1 = _insert_signal(repo, "AAA", "2026-07-02 08:00:00")
        s2 = _insert_signal(repo, "BBB", "2026-07-01 08:00:00")
        repo.insert_whatif_trade(_pending_fields(s1, "AAA", "2026-07-02 08:00:00"))
        rid2 = repo.insert_whatif_trade(_pending_fields(s2, "BBB", "2026-07-01 08:00:00"))
        repo.update_whatif_trade(rid2, {"status": "open", "entry_price": 101.0})

        assert [r["symbol"] for r in repo.get_whatif_trades()] == ["BBB", "AAA"]
        opens = repo.get_whatif_trades(status="open")
        assert len(opens) == 1 and opens[0]["entry_price"] == 101.0

    def test_update_whitelist(self, repo):
        sid = _insert_signal(repo)
        rid = repo.insert_whatif_trade(_pending_fields(sid))
        with pytest.raises(ValueError):
            repo.update_whatif_trade(rid, {"symbol; DROP TABLE": 1})


def _bt_config():
    return BacktestConfig(
        commission_pct=0.0, commission_fixed=0.0,
        sl_atr_mult=2.0, tp1_atr_mult=1.5, tp1_exit_pct=0.50,
        tp2_atr_mult=3.0, tp2_exit_pct=0.30, trailing_stop_pct=0.20,
    )


def _df_1h(start, closes):
    idx = pd.date_range(start=start, periods=len(closes), freq="1h")
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1000},
        index=idx,
    )


def _df_1d(start, rows):
    idx = pd.date_range(start=start, periods=len(rows), freq="1D")
    o, h, low, c = zip(*rows)
    return pd.DataFrame(
        {"Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}, index=idx
    )


_WARMUP = [(100.0, 101.0, 99.0, 100.0)] * 20  # ATR(14) = 2.0


def _make_pending(repo, symbol="THYAO", signal_time="2026-06-21 07:30:00",
                  score=5, price=100.0):
    sid = _insert_signal(repo, symbol, signal_time, score * 10, price)
    return repo.insert_whatif_trade(_pending_fields(sid, symbol, signal_time, score, price))


class TestFillPending:
    def test_fills_entry_and_levels(self, repo):
        rid = _make_pending(repo)  # sinyal 2026-06-21 07:30
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0)])
        hourly = _df_1h("2026-06-21 06:00:00", [102.0] * 5)  # ilk bar >= 07:30 → 08:00, close 102

        counts = fill_pending(repo, {"THYAO": hourly}, {"THYAO": daily}, _bt_config())

        assert counts == {"opened": 1, "no_data": 0, "left_pending": 0}
        row = repo.get_whatif_trades(status="open")[0]
        assert row["id"] == rid
        assert row["entry_price"] == 102.0
        assert row["entry_source"] == "bar_1h"
        assert row["stop_loss"] == pytest.approx(98.0)   # 102 - 2*2
        assert row["tp1"] == pytest.approx(105.0)
        assert row["remaining_shares"] == 100
        assert row["highest_price"] == 102.0
        assert row["last_update"] == "2026-06-21"
        assert row["delay_cost_pct"] == pytest.approx(2.0)  # (102-100)/100

    def test_no_hourly_falls_back(self, repo):
        _make_pending(repo)
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0)])

        counts = fill_pending(repo, {"THYAO": None}, {"THYAO": daily}, _bt_config())

        assert counts["opened"] == 1
        row = repo.get_whatif_trades(status="open")[0]
        assert row["entry_price"] == 100.0
        assert row["entry_source"] == "fallback"
        assert row["delay_cost_pct"] is None

    def test_no_daily_marks_no_data(self, repo):
        _make_pending(repo)
        hourly = _df_1h("2026-06-21 06:00:00", [102.0] * 5)

        counts = fill_pending(repo, {"THYAO": hourly}, {"THYAO": None}, _bt_config())

        assert counts == {"opened": 0, "no_data": 1, "left_pending": 0}
        row = repo.get_whatif_trades(status="no_data")[0]
        assert row["entry_price"] == 102.0  # al-tut icin giris yine yazilir

    def test_no_price_stays_pending(self, repo):
        sid = _insert_signal(repo, "THYAO", "2026-06-21 07:30:00", 50, None)
        repo.insert_whatif_trade({
            "signal_id": sid, "symbol": "THYAO",
            "signal_time": "2026-06-21 07:30:00", "score": 5,
        })
        counts = fill_pending(repo, {"THYAO": None}, {"THYAO": None}, _bt_config())
        assert counts == {"opened": 0, "no_data": 0, "left_pending": 1}
        assert len(repo.get_whatif_trades(status="pending")) == 1


class TestRowToBt:
    def test_reconstructs_state(self):
        row = {
            "symbol": "THYAO", "signal_time": "2026-06-21 07:30:00",
            "entry_price": 102.0, "stop_loss": 98.0, "tp1": 105.0, "tp2": 108.0,
            "remaining_shares": 50, "highest_price": 106.0, "tp1_hit": 1,
        }
        bt = row_to_bt(row)
        assert bt.remaining_shares == 50
        assert bt.tp1_hit is True
        assert bt.highest_price == 106.0
        assert bt.shares == 100  # VIRTUAL_SHARES — pnl_pct tabani
        assert bt.status == "open"
