"""Tests for what-if simulation: repository, entry price, simulation, stats."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.core.whatif import (
    WhatIfTrade,
    atr_from_daily,
    compute_stats,
    find_entry,
    simulate_whatif,
)
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _log(repo, symbol, signal_type="buy", score=5, price=100.0, created_at=None,
          indicator_values=None):
    sid = repo.log_signal(
        symbol=symbol,
        signal_type=signal_type,
        indicator="score",
        strength="medium",
        price_at_signal=price,
        score=score,
        indicator_values=indicator_values,
    )
    if created_at:
        repo._conn.execute(
            "UPDATE signals_log SET created_at = ? WHERE id = ?", (created_at, sid)
        )
        repo._conn.commit()
    return sid


class TestGetBuySignalsAsc:
    def test_filters_type_and_score_orders_asc(self, repo):
        _log(repo, "THYAO", score=5, created_at="2026-07-02 08:00:00")
        _log(repo, "ASELS", score=3, created_at="2026-07-01 08:00:00")  # skor dusuk
        _log(repo, "GARAN", signal_type="sell", score=8, created_at="2026-07-01 09:00:00")
        _log(repo, "KCHOL", score=4, created_at="2026-07-01 07:00:00")

        rows = repo.get_buy_signals_asc(min_score=4)

        assert [r["symbol"] for r in rows] == ["KCHOL", "THYAO"]
        assert all(isinstance(r, dict) for r in rows)

    def test_empty(self, repo):
        assert repo.get_buy_signals_asc(min_score=4) == []


def _df_1h(start: str, closes: list[float]) -> pd.DataFrame:
    """Saatlik bar DataFrame'i uret (naive UTC index, 1h aralik)."""
    idx = pd.date_range(start=start, periods=len(closes), freq="1h")
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1000},
        index=idx,
    )


def _df_1d(start: str, rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Gunluk bar DataFrame'i: rows = [(open, high, low, close), ...]."""
    idx = pd.date_range(start=start, periods=len(rows), freq="1D")
    o, h, low, c = zip(*rows)
    return pd.DataFrame(
        {"Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}, index=idx
    )


class TestFindEntry:
    def test_next_1h_bar_close(self):
        # Sinyal 10:30 UTC; sonraki bar 11:00 → close 102.0
        df = _df_1h("2026-07-01 08:00:00", [100.0, 101.0, 101.5, 102.0, 103.0])
        result = find_entry(df, "2026-07-01 10:30:00", 100.5)
        assert result == (102.0, "bar_1h")

    def test_signal_exactly_on_bar_ts(self):
        # bar_ts >= sinyal: 10:00 bar'i dahil
        df = _df_1h("2026-07-01 08:00:00", [100.0, 101.0, 101.5])
        result = find_entry(df, "2026-07-01 10:00:00", 100.5)
        assert result == (101.5, "bar_1h")

    def test_no_later_bar_falls_back(self):
        # Sinyal son bar'dan sonra (kapanisa yakin) → fallback
        df = _df_1h("2026-07-01 08:00:00", [100.0, 101.0])
        result = find_entry(df, "2026-07-01 15:00:00", 100.5)
        assert result == (100.5, "fallback")

    def test_no_1h_data_falls_back(self):
        assert find_entry(None, "2026-07-01 10:00:00", 99.0) == (99.0, "fallback")

    def test_nothing_available(self):
        assert find_entry(None, "2026-07-01 10:00:00", None) is None


class TestAtrFromDaily:
    def test_atr_simple(self):
        # 20 gun, her gun H-L = 2.0, gap yok → ATR(14) = 2.0
        rows = [(100.0, 101.0, 99.0, 100.0)] * 20
        df = _df_1d("2026-06-01", rows)
        atr = atr_from_daily(df, "2026-06-25 10:00:00")
        assert atr == pytest.approx(2.0)

    def test_signal_day_bar_excluded(self):
        # Sinyal gununun kendi bar'i (devasa aralik) ATR'ye girmemeli — lookahead onlemi.
        # Son bar 2026-06-21'de; sinyal ayni gun → sadece onceki 20 bar kullanilir.
        rows = [(100.0, 101.0, 99.0, 100.0)] * 20 + [(100.0, 200.0, 50.0, 150.0)]
        df = _df_1d("2026-06-01", rows)
        atr = atr_from_daily(df, "2026-06-21 10:00:00")
        assert atr == pytest.approx(2.0)

    def test_insufficient_bars(self):
        rows = [(100.0, 101.0, 99.0, 100.0)] * 5
        df = _df_1d("2026-06-01", rows)
        assert atr_from_daily(df, "2026-06-10 10:00:00") is None


def _bt_config():
    # sl = entry - 2*ATR, tp1 = entry + 1.5*ATR, tp2 = entry + 3*ATR
    return BacktestConfig(
        commission_pct=0.0, commission_fixed=0.0,
        sl_atr_mult=2.0, tp1_atr_mult=1.5, tp1_exit_pct=0.50,
        tp2_atr_mult=3.0, tp2_exit_pct=0.30, trailing_stop_pct=0.20,
    )


def _signal(sid, symbol, created_at, score=5, price=100.0):
    return {
        "id": sid, "symbol": symbol, "created_at": created_at,
        "score": score, "price_at_signal": price,
    }


# 20 gun sabit bar (ATR=2) + sinyal gunu; index 2026-06-01'den baslar.
_WARMUP = [(100.0, 101.0, 99.0, 100.0)] * 20


class TestSimulateWhatif:
    def test_open_trade_marks_to_market(self):
        # Giris 100 (1h bar), sonraki gunler TP1'e (103) ulasamiyor → acik, guncel 102
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0)] * 3)
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00")]

        trades, skipped = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 102.0}, _bt_config()
        )

        assert skipped == 0
        t = trades[0]
        assert t.status == "open"
        assert t.entry_price == 100.0
        assert t.entry_source == "bar_1h"
        assert t.stop_loss == pytest.approx(96.0)   # 100 - 2*2
        assert t.tp1 == pytest.approx(103.0)        # 100 + 1.5*2
        assert t.strategy_pnl_pct == pytest.approx(2.0)  # mark-to-market
        assert t.buyhold_pnl_pct == pytest.approx(2.0)

    def test_stop_loss_closes_trade(self):
        # Ertesi gun low 90 < SL 96 → SL'den kapanir, -4%
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 100.0, 90.0, 92.0)])
        hourly = _df_1h("2026-06-20 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-20 07:30:00")]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 92.0}, _bt_config()
        )

        t = trades[0]
        assert t.status == "closed"
        assert t.exit_type == "sl"
        assert t.strategy_pnl_pct == pytest.approx(-4.0)
        # Al-tut SL bilmez: guncel fiyattan -8%
        assert t.buyhold_pnl_pct == pytest.approx(-8.0)

    def test_entry_day_bars_not_used_for_exits(self):
        # Giris gununun kendi gunluk bar'i exit tetiklememeli (lookahead onlemi):
        # giris gunu low 90 SL'in altinda ama islem acik kalmali.
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 100.0, 90.0, 100.0)])
        # Sinyal son gunluk bar gununde (2026-06-21)
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00")]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 100.0}, _bt_config()
        )
        assert trades[0].status == "open"

    def test_dedup_skips_signal_while_open(self):
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0)] * 3)
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 10)
        signals = [
            _signal(1, "THYAO", "2026-06-21 07:30:00"),
            _signal(2, "THYAO", "2026-06-21 09:30:00"),  # pozisyon acikken → atla
        ]

        trades, skipped = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 102.0}, _bt_config()
        )
        assert len(trades) == 1
        assert skipped == 1

    def test_dedup_allows_after_close(self):
        # Ilk islem SL ile 2026-06-21'de kapanir; 2026-06-23 sinyali yeni islem acar.
        daily = _df_1d(
            "2026-06-01",
            _WARMUP + [(100.0, 100.0, 90.0, 92.0), (92.0, 93.0, 91.0, 92.0),
                       (92.0, 94.0, 91.5, 93.0)],
        )
        hourly = _df_1h("2026-06-20 06:00:00", [100.0] * 80)  # 3+ gun kapsar
        signals = [
            _signal(1, "THYAO", "2026-06-20 07:30:00"),
            _signal(2, "THYAO", "2026-06-23 07:30:00"),
        ]

        trades, skipped = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 93.0}, _bt_config()
        )
        assert len(trades) == 2
        assert skipped == 0

    def test_no_daily_data_marks_no_data(self):
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00")]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": None}, {"THYAO": 102.0}, _bt_config()
        )
        assert trades[0].status == "no_data"
        assert trades[0].strategy_pnl_pct is None
        # Al-tut guncel fiyatla yine hesaplanabilir
        assert trades[0].buyhold_pnl_pct == pytest.approx(2.0)

    def test_tp1_partial_exit_then_open(self):
        # Gun sonrasi bar: high 103.5 (TP1 vurur, TP2 vurmaz), low SL'in ustunde,
        # close 103. Islem acik kalir; TP1'de satilan 50 lot gerceklesir,
        # kalan 50 lot mark-to-market.
        daily = _df_1d(
            "2026-06-01",
            _WARMUP + [(100.0, 100.0, 100.0, 100.0), (100.0, 103.5, 102.0, 103.0)],
        )
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00")]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 103.0}, _bt_config()
        )

        t = trades[0]
        assert t.status == "open"
        # TP1: 50 lot @103 -> +150; kalan 50 lot (103-100)*50 -> +150
        # toplam 300 / 10000 maliyet * 100 = 3.0%
        assert t.strategy_pnl_pct == pytest.approx(3.0)

    def test_tp1_tp2_then_trailing_close(self):
        # Bar1: high 130 -> TP1 (50 lot @103, +150) ve TP2 (30 lot @106, +180)
        # ayni barda vurur; highest_price 130'a guncellenir, low 105 trailing'i
        # (130*0.8=104) tetiklemez.
        # Bar2: high 106 (highest guncellenmez), low 98 <= trail 104 -> kalan
        # 20 lot trailing'den 104'te kapanir: +80.
        daily = _df_1d(
            "2026-06-01",
            _WARMUP
            + [
                (100.0, 100.0, 100.0, 100.0),   # giris gunu (kullanilmaz)
                (100.0, 130.0, 105.0, 115.0),   # TP1 + TP2
                (100.0, 106.0, 98.0, 99.0),     # trailing kapanis
            ],
        )
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00")]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 99.0}, _bt_config()
        )

        t = trades[0]
        assert t.status == "closed"
        assert t.exit_type == "trailing"
        # realized = 150 (tp1) + 180 (tp2) + 80 (trailing) = 410 / 10000 * 100 = 4.1%
        # (her bacak yalnizca bir kez sayilirsa dogru deger budur; cift sayimda
        # TP1/TP2 iki katina cikip 5.9% gibi yanlis bir sonuc verirdi)
        assert t.strategy_pnl_pct == pytest.approx(4.1)

    def test_delay_cost(self):
        # price_at_signal 100, 1h giris 102 → gecikme maliyeti +2%
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 103.0, 99.5, 102.0)] * 2)
        hourly = _df_1h("2026-06-21 06:00:00", [102.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00", price=100.0)]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 102.0}, _bt_config()
        )
        assert trades[0].delay_cost_pct == pytest.approx(2.0)


def _trade(symbol="THYAO", score=5, status="closed", spnl=None, bpnl=None,
           exit_type=None, exit_date=None, holding=None, delay=None):
    return WhatIfTrade(
        signal_id=1, symbol=symbol, signal_time="2026-06-20 07:30:00", score=score,
        price_at_signal=100.0, entry_price=100.0, entry_source="bar_1h",
        stop_loss=96.0, tp1=103.0, tp2=106.0, status=status,
        strategy_pnl_pct=spnl, exit_type=exit_type, exit_date=exit_date,
        holding_days=holding, buyhold_pnl_pct=bpnl, delay_cost_pct=delay,
    )


class TestComputeStats:
    def test_mode_stats_and_profit_factor(self):
        trades = [
            _trade(symbol="A", spnl=6.0, bpnl=3.0, exit_type="tp2",
                   exit_date="2026-06-25", holding=5.0),
            _trade(symbol="B", spnl=-4.0, bpnl=-2.0, exit_type="sl",
                   exit_date="2026-06-22", holding=2.0),
            _trade(symbol="C", status="open", spnl=2.0, bpnl=2.0),
        ]
        stats = compute_stats(trades, skipped_dedup=3)

        s = stats.strategy
        assert s.trade_count == 3
        assert s.closed_count == 2 and s.open_count == 1
        assert s.win_rate == pytest.approx(66.67, abs=0.01)
        assert s.total_pnl_pct == pytest.approx(4.0)
        assert s.median_pnl_pct == pytest.approx(2.0)
        assert s.best == ("A", 6.0) and s.worst == ("B", -4.0)
        assert s.profit_factor == pytest.approx(8.0 / 4.0)

        assert stats.buyhold.trade_count == 3
        assert stats.exit_counts == {"tp2": 1, "sl": 1, "open": 1}
        assert stats.avg_holding_days == pytest.approx(3.5)
        assert stats.skipped_dedup == 3
        # Kumulatif egri exit_date sirasinda: B (-4), sonra A (+2)
        assert stats.cumulative_curve == [("2026-06-22", -4.0), ("2026-06-25", 2.0)]

    def test_profit_factor_none_when_no_losses(self):
        trades = [_trade(spnl=5.0, bpnl=5.0, exit_type="tp1",
                         exit_date="2026-06-25", holding=3.0)]
        assert compute_stats(trades, 0).strategy.profit_factor is None

    def test_score_buckets(self):
        trades = [
            _trade(symbol="A", score=4, spnl=2.0, exit_type="tp1",
                   exit_date="2026-06-25", holding=1.0),
            _trade(symbol="B", score=5, spnl=-2.0, exit_type="sl",
                   exit_date="2026-06-25", holding=1.0),
            _trade(symbol="C", score=8, spnl=6.0, exit_type="tp2",
                   exit_date="2026-06-25", holding=1.0),
        ]
        buckets = compute_stats(trades, 0).score_buckets
        assert [b.label for b in buckets] == ["4-5", "8+"]
        b45 = buckets[0]
        assert b45.trade_count == 2
        assert b45.win_rate == pytest.approx(50.0)
        assert b45.avg_pnl_pct == pytest.approx(0.0)

    def test_no_data_and_delay(self):
        trades = [
            _trade(status="no_data", bpnl=1.0, delay=1.5),
            _trade(spnl=2.0, status="open", delay=0.5),
        ]
        stats = compute_stats(trades, 0)
        assert stats.no_data_count == 1
        assert stats.avg_delay_cost_pct == pytest.approx(1.0)
        assert stats.strategy.trade_count == 1  # no_data haric

    def test_empty(self):
        stats = compute_stats([], 0)
        assert stats.strategy.trade_count == 0
        assert stats.cumulative_curve == []


class TestBuildWhatIfData:
    def test_assembles_from_injected_fetchers(self, repo, monkeypatch):
        from swing_tracker.web.routers import whatif as whatif_router

        _log(
            repo, "THYAO", score=50, price=100.0, created_at="2026-06-21 07:30:00",
            indicator_values={"entry_score": 5, "reasons": "test"},
        )

        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 102.0, 99.5, 101.0)] * 3)
        hourly = _df_1h("2026-06-21 06:00:00", [100.0] * 5)

        def fake_get_ohlcv(symbol, *, interval, **kwargs):
            return hourly if interval == "1h" else daily

        monkeypatch.setattr(whatif_router, "get_ohlcv", fake_get_ohlcv)
        monkeypatch.setattr(
            whatif_router.price_cache, "fetch_many", lambda syms: {"THYAO": 102.0}
        )

        class FakeConfig:
            cache = None  # get_ohlcv mock'landigi icin kullanilmaz

        trades, stats = whatif_router.build_whatif_data(repo, FakeConfig())

        assert len(trades) == 1
        assert trades[0].symbol == "THYAO"
        assert trades[0].score == 5
        assert stats.strategy.trade_count == 1


class TestEntryScore:
    def test_indicator_values_entry_score_wins(self):
        from swing_tracker.web.routers.whatif import _entry_score

        sig = {"score": 60, "indicator_values": '{"entry_score": 6, "reasons": "x"}'}
        assert _entry_score(sig) == 6

    def test_missing_entry_score_falls_back_to_score_div_10(self):
        from swing_tracker.web.routers.whatif import _entry_score

        sig = {"score": 50, "indicator_values": '{"reasons": "x"}'}
        assert _entry_score(sig) == 5

    def test_malformed_json_falls_back_to_score_div_10(self):
        from swing_tracker.web.routers.whatif import _entry_score

        sig = {"score": 40, "indicator_values": "not json"}
        assert _entry_score(sig) == 4

    def test_legacy_row_small_score_unscaled(self):
        from swing_tracker.web.routers.whatif import _entry_score

        sig = {"score": 5, "indicator_values": ""}
        assert _entry_score(sig) == 5

    def test_score_none_returns_zero(self):
        from swing_tracker.web.routers.whatif import _entry_score

        sig = {"score": None, "indicator_values": None}
        assert _entry_score(sig) == 0
