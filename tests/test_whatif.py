"""Tests for what-if simulation: repository, entry price, simulation, stats."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_tracker.core.whatif import atr_from_daily, find_entry
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _log(repo, symbol, signal_type="buy", score=5, price=100.0, created_at=None):
    sid = repo.log_signal(
        symbol=symbol,
        signal_type=signal_type,
        indicator="score",
        strength="medium",
        price_at_signal=price,
        score=score,
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
