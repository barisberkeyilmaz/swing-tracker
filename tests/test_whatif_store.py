"""Tests for whatif persistent store: config, schema/CRUD, job steps."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.config import WhatIfConfig, load_config
from swing_tracker.core.whatif_store import expire_stale, fill_pending, refresh_buyhold, row_to_bt, update_open
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


def _make_open(repo, symbol="THYAO", signal_time="2026-06-21 07:30:00",
               entry=100.0, sl=96.0, tp1=103.0, tp2=106.0,
               remaining=100, tp1_hit=0, highest=None, realized=0.0,
               last_update="2026-06-21"):
    rid = _make_pending(repo, symbol, signal_time)
    repo.update_whatif_trade(rid, {
        "status": "open", "entry_price": entry, "entry_source": "bar_1h",
        "stop_loss": sl, "tp1": tp1, "tp2": tp2,
        "remaining_shares": remaining, "tp1_hit": tp1_hit,
        "highest_price": highest or entry, "realized_pnl": realized,
        "last_update": last_update,
    })
    return rid


class TestUpdateOpen:
    def test_sl_closes(self, repo):
        _make_open(repo)
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0),
                                                 (100.0, 100.0, 90.0, 92.0)])
        counts = update_open(repo, {"THYAO": daily}, _bt_config())

        assert counts == {"updated": 0, "closed": 1}
        row = repo.get_whatif_trades(status="closed")[0]
        assert row["exit_type"] == "sl"
        assert row["exit_date"] == "2026-06-22"
        assert row["strategy_pnl_pct"] == pytest.approx(-4.0)  # (96-100)*100/10000
        assert row["holding_days"] == pytest.approx(1.0)

    def test_tp1_partial_stays_open_and_resumes(self, repo):
        _make_open(repo)
        # Gun 1 (06-22): high 103.5 -> TP1, 50 pay @103 (+150); kapanis 103
        daily1 = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0),
                                                  (103.0, 103.5, 102.0, 103.0)])
        update_open(repo, {"THYAO": daily1}, _bt_config())
        row = repo.get_whatif_trades(status="open")[0]
        assert row["remaining_shares"] == 50
        assert row["tp1_hit"] == 1
        assert row["last_update"] == "2026-06-22"
        # mark-to-market: (150 + (103-100)*50) / 10000 * 100 = 3.0
        assert row["strategy_pnl_pct"] == pytest.approx(3.0)

        # Gun 2 (06-23): ayni bar'lar + trailing tetikleyen dusus.
        # highest=103.5 -> trail 82.8; low 80 -> kalan 50 pay 82.8'den kapanir.
        daily2 = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0),
                                                  (103.0, 103.5, 102.0, 103.0),
                                                  (100.0, 100.0, 80.0, 81.0)])
        counts = update_open(repo, {"THYAO": daily2}, _bt_config())
        assert counts["closed"] == 1
        row = repo.get_whatif_trades(status="closed")[0]
        assert row["exit_type"] == "trailing"
        # 150 + (82.8-100)*50 = 150 - 860 = -710 -> -7.1%
        assert row["strategy_pnl_pct"] == pytest.approx(-7.1)

    def test_no_new_bars_idempotent(self, repo):
        _make_open(repo, last_update="2026-06-22")
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0),
                                                 (100.0, 100.0, 90.0, 92.0)])
        counts = update_open(repo, {"THYAO": daily}, _bt_config())
        assert counts == {"updated": 0, "closed": 0}

    def test_missing_data_skips_row(self, repo):
        _make_open(repo)
        counts = update_open(repo, {}, _bt_config())
        assert counts == {"updated": 0, "closed": 0}
        assert repo.get_whatif_trades(status="open")[0]["last_update"] == "2026-06-21"


class TestRefreshBuyhold:
    def test_updates_all_rows_with_entry(self, repo):
        _make_open(repo, "THYAO")
        rid2 = _make_open(repo, "ASELS", signal_time="2026-06-20 07:30:00")
        repo.update_whatif_trade(rid2, {"status": "closed", "exit_type": "sl",
                                        "exit_date": "2026-06-22"})
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 110.0)])

        n = refresh_buyhold(repo, {"THYAO": daily, "ASELS": daily})

        assert n == 2
        for row in repo.get_whatif_trades():
            assert row["last_close"] == 110.0
            assert row["buyhold_pnl_pct"] == pytest.approx(10.0)


class TestExpireStale:
    def test_open_expires_with_pnl(self, repo):
        rid = _make_open(repo, signal_time="2026-04-01 07:30:00", last_update="2026-04-01")
        repo.update_whatif_trade(rid, {"last_close": 95.0})

        n = expire_stale(repo, today="2026-07-10", max_holding_days=60)

        assert n == 1
        row = repo.get_whatif_trades(status="expired")[0]
        assert row["exit_type"] == "expired"
        assert row["exit_date"] == "2026-07-10"
        assert row["strategy_pnl_pct"] == pytest.approx(-5.0)  # (95-100)*100/10000
        assert row["remaining_shares"] == 0

    def test_pending_expires_without_pnl(self, repo):
        _make_pending(repo, signal_time="2026-04-01 07:30:00")
        n = expire_stale(repo, today="2026-07-10", max_holding_days=60)
        assert n == 1
        row = repo.get_whatif_trades(status="expired")[0]
        assert row["strategy_pnl_pct"] is None

    def test_fresh_rows_untouched(self, repo):
        _make_open(repo, signal_time="2026-07-01 07:30:00")
        assert expire_stale(repo, today="2026-07-10", max_holding_days=60) == 0


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

    def test_zero_remaining_raises(self):
        row = {
            "symbol": "THYAO", "signal_time": "2026-06-21 07:30:00",
            "entry_price": 102.0, "stop_loss": 98.0, "tp1": 105.0, "tp2": 108.0,
            "remaining_shares": 0, "highest_price": 106.0, "tp1_hit": 1,
        }
        with pytest.raises(ValueError):
            row_to_bt(row)
