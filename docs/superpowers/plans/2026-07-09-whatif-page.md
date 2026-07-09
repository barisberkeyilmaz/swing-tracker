# What-If Sayfası Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Üretilen buy sinyallerini alsaydım performansım ne olurdu sorusunu cevaplayan `/whatif` web sayfası — strateji kurallı + al-tut simülasyonu, istatistikler, 15 dk gecikme düzeltmesi.

**Architecture:** `core/whatif.py` pure-function simülasyon çekirdeği `signals_log`'daki buy sinyallerini kronolojik işler; giriş fiyatını sinyalden sonraki ilk 1h bar'dan alır, TP/SL'i günlük ATR'den hesaplar, `backtest/exits.py::check_exits`'i yeniden kullanır. Web katmanı htmx fragment pattern'iyle skeleton → sonuç yükler.

**Tech Stack:** Python 3.11, FastAPI + Jinja2 + htmx, pandas, SQLite (raw SQL), Chart.js 4 (CDN), pytest.

**Spec:** `docs/superpowers/specs/2026-07-09-whatif-page-design.md`

## Global Constraints

- Ruff: line-length=100, target py311. Commit öncesi `ruff check src tests` temiz olmalı.
- UI metinleri Türkçe, ASCII karakterlerle (mevcut şablonlardaki gibi: "Portfoy", "Sinyaller", "icin").
- Repository method'ları `dict` döndürür (Row → dict). ORM yok, raw SQL.
- `core/` fonksiyonları pure function — network ve global state yok; veri erişimi parametreyle enjekte edilir.
- Testler network'e çıkmaz; DB testleri `:memory:` SQLite kullanır.
- Zaman damgaları: `signals_log.created_at` **UTC** `"YYYY-MM-DD HH:MM:SS"`; `ohlcv_cache.bar_ts` **naive UTC ISO** string (DataFrame index'i naive UTC `DatetimeIndex`). Karşılaştırmalar naive UTC'de yapılır; sadece görüntüleme İstanbul'a çevrilir (`localize_signal_timestamps`).
- Skor eşiği tek kaynak: `swing_tracker.core.scanner.MIN_ENTRY_SCORE` (şu an 4).

---

### Task 1: Repository — buy sinyallerini kronolojik çeken method

**Files:**
- Modify: `src/swing_tracker/db/repository.py` (get_recent_signals civarı, ~satır 242)
- Test: `tests/test_whatif.py` (yeni dosya)

**Interfaces:**
- Produces: `Repository.get_buy_signals_asc(min_score: int) -> list[dict]` — `signal_type='buy'` ve `score >= min_score` sinyaller, `created_at` artan sırada, tüm kolonlar dict olarak.

- [ ] **Step 1: Write the failing test**

`tests/test_whatif.py` dosyasını oluştur:

```python
"""Tests for what-if simulation: repository, entry price, simulation, stats."""

from __future__ import annotations

import sqlite3

import pytest

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
```

Not: kurulum pattern'i `tests/test_signal_logging.py` ile aynıdır (`create_all_tables` + `Repository`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `AttributeError: 'Repository' object has no attribute 'get_buy_signals_asc'`

- [ ] **Step 3: Write minimal implementation**

`repository.py`'de `get_recent_signals`'ın hemen altına ekle:

```python
    def get_buy_signals_asc(self, min_score: int) -> list[dict]:
        """What-if simulasyonu icin: esik ustu buy sinyalleri, kronolojik sirada."""
        rows = self._conn.execute(
            """SELECT * FROM signals_log
               WHERE signal_type = 'buy' AND score >= ?
               ORDER BY created_at ASC, id ASC""",
            (min_score,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_whatif.py src/swing_tracker/db/repository.py
git commit -m "feat(whatif): buy sinyallerini kronolojik ceken repo methodu"
```

---

### Task 2: core/whatif.py — dataclass'lar, giriş fiyatı seçimi, ATR/TP/SL

**Files:**
- Create: `src/swing_tracker/core/whatif.py`
- Test: `tests/test_whatif.py` (ekleme)

**Interfaces:**
- Consumes: yok (pure pandas).
- Produces:
  - `@dataclass WhatIfTrade` (alanlar aşağıda).
  - `find_entry(df_1h: pd.DataFrame | None, signal_ts: str, price_at_signal: float | None) -> tuple[float, str] | None` — `(entry_price, source)`; source `"bar_1h"` veya `"fallback"`; fiyat bulunamazsa `None`.
  - `atr_from_daily(df_1d: pd.DataFrame, upto_ts: str, period: int = 14) -> float | None` — sinyal tarihine kadarki günlük bar'lardan ATR; yetersiz veri → `None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_whatif.py` başına import'ları ekle ve dosya sonuna test sınıflarını ekle:

```python
import pandas as pd

from swing_tracker.core.whatif import atr_from_daily, find_entry


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.core.whatif'` (Task 1 testleri PASS kalır)

- [ ] **Step 3: Write implementation**

`src/swing_tracker/core/whatif.py` oluştur:

```python
"""What-if simulasyonu: uretilen buy sinyalleri alinsaydi performans ne olurdu.

Pure function'lar — veri erisimi (OHLCV, guncel fiyat) parametreyle enjekte edilir.
Giris fiyati sinyalden sonraki ilk 1h bar'in kapanisi (15 dk veri gecikmesi modeli).
Cikis kurallari backtest/exits.py ile ortak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

ATR_PERIOD = 14
VIRTUAL_SHARES = 100  # yuzde getiri olculuyor; sabit sanal lot


@dataclass
class WhatIfTrade:
    signal_id: int
    symbol: str
    signal_time: str            # UTC "YYYY-MM-DD HH:MM:SS"
    score: int
    price_at_signal: float | None
    entry_price: float
    entry_source: Literal["bar_1h", "fallback"]
    stop_loss: float
    tp1: float
    tp2: float
    status: Literal["open", "closed", "no_data"]
    strategy_pnl_pct: float | None = None
    exit_type: str | None = None      # kapali islemde son cikisin tipi
    exit_date: str | None = None      # kapali islemde son cikisin tarihi (ISO)
    holding_days: float | None = None  # sadece kapali islemler
    buyhold_pnl_pct: float | None = None
    current_price: float | None = None
    delay_cost_pct: float | None = None  # (entry - price_at_signal) / price_at_signal * 100


def find_entry(
    df_1h: pd.DataFrame | None,
    signal_ts: str,
    price_at_signal: float | None,
) -> tuple[float, str] | None:
    """Sinyalden sonraki ilk 1h bar'in kapanisini giris fiyati olarak sec.

    1h bar yoksa veya sinyal son bar'dan sonraysa price_at_signal'a duser.
    Hicbir fiyat yoksa None.
    """
    ts = pd.Timestamp(signal_ts)
    if df_1h is not None and not df_1h.empty:
        later = df_1h[df_1h.index >= ts]
        if not later.empty:
            close = later.iloc[0]["Close"]
            if pd.notna(close) and float(close) > 0:
                return float(close), "bar_1h"
    if price_at_signal is not None and price_at_signal > 0:
        return float(price_at_signal), "fallback"
    return None


def atr_from_daily(
    df_1d: pd.DataFrame, upto_ts: str, period: int = ATR_PERIOD
) -> float | None:
    """Sinyal gununden ONCEKI gunluk bar'lardan ATR (basit rolling mean TR).

    Sinyal gununun kendi bar'i dahil edilmez: gunun tam araligi sinyal aninda
    henuz bilinemez (lookahead onlemi).
    """
    ts = pd.Timestamp(upto_ts).normalize()
    df = df_1d[df_1d.index < ts]
    if len(df) < period + 1:
        return None
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None
    return float(atr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: tümü PASS

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/whatif.py tests/test_whatif.py
git commit -m "feat(whatif): giris fiyati secimi (15dk gecikme modeli) ve gunluk ATR"
```

---

### Task 3: Simülasyon — dedup + strateji kurallı + al-tut

**Files:**
- Modify: `src/swing_tracker/core/whatif.py`
- Test: `tests/test_whatif.py` (ekleme)

**Interfaces:**
- Consumes: `find_entry`, `atr_from_daily` (Task 2); `swing_tracker.backtest.exits.check_exits`; `swing_tracker.backtest.models.BacktestTrade, BacktestConfig`.
- Produces:
  - `simulate_whatif(signals: list[dict], ohlcv_1h: dict[str, pd.DataFrame | None], ohlcv_1d: dict[str, pd.DataFrame | None], current_prices: dict[str, float], bt_config: BacktestConfig) -> tuple[list[WhatIfTrade], int]` — `(trades, skipped_dedup)`. `signals` Task 1'in dict formatı (`id`, `symbol`, `created_at`, `score`, `price_at_signal` alanları kullanılır).

- [ ] **Step 1: Write the failing tests**

`tests/test_whatif.py`'a ekle:

```python
from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.core.whatif import simulate_whatif


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

    def test_delay_cost(self):
        # price_at_signal 100, 1h giris 102 → gecikme maliyeti +2%
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 103.0, 99.5, 102.0)] * 2)
        hourly = _df_1h("2026-06-21 06:00:00", [102.0] * 5)
        signals = [_signal(1, "THYAO", "2026-06-21 07:30:00", price=100.0)]

        trades, _ = simulate_whatif(
            signals, {"THYAO": hourly}, {"THYAO": daily}, {"THYAO": 102.0}, _bt_config()
        )
        assert trades[0].delay_cost_pct == pytest.approx(2.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `ImportError: cannot import name 'simulate_whatif'`

- [ ] **Step 3: Write implementation**

`core/whatif.py`'a ekle (import bölümüne `import dataclasses` ve backtest import'ları):

```python
import dataclasses

from swing_tracker.backtest.exits import check_exits
from swing_tracker.backtest.models import BacktestConfig, BacktestTrade


def _simulate_strategy(
    trade: WhatIfTrade,
    df_1d: pd.DataFrame,
    current_price: float | None,
    bt_config: BacktestConfig,
) -> None:
    """Gunluk bar'lari check_exits'e vererek strateji sonucunu WhatIfTrade'e yazar."""
    bt = BacktestTrade(
        symbol=trade.symbol,
        direction="long",
        entry_price=trade.entry_price,
        entry_date=trade.signal_time,
        shares=VIRTUAL_SHARES,
        stop_loss=trade.stop_loss,
        tp1=trade.tp1,
        tp2=trade.tp2,
    )
    # Lookahead onlemi: giris gununun KENDI bar'i exit tetiklemez,
    # ertesi gunden itibaren bakilir.
    entry_day = pd.Timestamp(trade.signal_time).normalize()
    later = df_1d[df_1d.index.normalize() > entry_day]

    for ts, row in later.iterrows():
        check_exits(
            bt, ts.date().isoformat(),
            float(row["High"]), float(row["Low"]), float(row["Close"]),
            bt_config,
        )
        if bt.status == "closed":
            break

    cost = trade.entry_price * VIRTUAL_SHARES
    if bt.status == "closed":
        trade.status = "closed"
        trade.strategy_pnl_pct = round(bt.total_pnl / cost * 100, 2)
        last_exit = bt.exits[-1]
        trade.exit_type = last_exit.exit_type
        trade.exit_date = last_exit.date
        trade.holding_days = float(
            (pd.Timestamp(last_exit.date) - entry_day).days
        )
    else:
        trade.status = "open"
        unrealized = 0.0
        if current_price is not None:
            unrealized = (current_price - trade.entry_price) * bt.remaining_shares
        trade.strategy_pnl_pct = round((bt.total_pnl + unrealized) / cost * 100, 2)


def simulate_whatif(
    signals: list[dict],
    ohlcv_1h: dict[str, pd.DataFrame | None],
    ohlcv_1d: dict[str, pd.DataFrame | None],
    current_prices: dict[str, float],
    bt_config: BacktestConfig,
) -> tuple[list[WhatIfTrade], int]:
    """Sinyalleri kronolojik isler; (islemler, dedup ile atlanan sayisi) doner.

    Dedup: sembolde acik sanal pozisyon varken (veya kapanis sinyalden sonraysa)
    yeni buy sinyali atlanir.
    """
    trades: list[WhatIfTrade] = []
    skipped = 0
    # symbol -> son islemin kapanis Timestamp'i (None = hala acik/no_data)
    position_until: dict[str, pd.Timestamp | None] = {}

    for sig in signals:
        symbol = sig["symbol"]
        signal_ts = sig["created_at"]

        if symbol in position_until:
            closed_at = position_until[symbol]
            if closed_at is None or pd.Timestamp(signal_ts) <= closed_at:
                skipped += 1
                continue

        entry = find_entry(ohlcv_1h.get(symbol), signal_ts, sig.get("price_at_signal"))
        if entry is None:
            continue  # fiyat yok: islem uretilemez, dedup'a da girmez
        entry_price, source = entry

        price_at_signal = sig.get("price_at_signal")
        delay_cost = None
        if source == "bar_1h" and price_at_signal:
            delay_cost = round((entry_price - price_at_signal) / price_at_signal * 100, 2)

        current = current_prices.get(symbol)
        df_1d = ohlcv_1d.get(symbol)
        atr = atr_from_daily(df_1d, signal_ts) if df_1d is not None else None

        trade = WhatIfTrade(
            signal_id=sig["id"],
            symbol=symbol,
            signal_time=signal_ts,
            score=sig.get("score") or 0,
            price_at_signal=price_at_signal,
            entry_price=entry_price,
            entry_source=source,
            stop_loss=round(entry_price - (atr or 0) * bt_config.sl_atr_mult, 2),
            tp1=round(entry_price + (atr or 0) * bt_config.tp1_atr_mult, 2),
            tp2=round(entry_price + (atr or 0) * bt_config.tp2_atr_mult, 2),
            status="no_data",
            current_price=current,
            delay_cost_pct=delay_cost,
        )

        if current is not None:
            trade.buyhold_pnl_pct = round((current - entry_price) / entry_price * 100, 2)

        if df_1d is not None and atr is not None:
            _simulate_strategy(trade, df_1d, current, bt_config)

        trades.append(trade)
        if trade.status == "closed":
            position_until[symbol] = pd.Timestamp(trade.exit_date)
        else:
            position_until[symbol] = None  # acik veya no_data: sembol blokeli

    return trades, skipped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/swing_tracker/core/whatif.py tests/test_whatif.py
git add src/swing_tracker/core/whatif.py tests/test_whatif.py
git commit -m "feat(whatif): dedup'li strateji + al-tut simulasyonu (backtest exits ortak)"
```

---

### Task 4: İstatistikler — WhatIfStats

**Files:**
- Modify: `src/swing_tracker/core/whatif.py`
- Test: `tests/test_whatif.py` (ekleme)

**Interfaces:**
- Consumes: `WhatIfTrade` (Task 2/3).
- Produces:
  - `@dataclass ModeStats`: `trade_count: int, open_count: int, closed_count: int, win_rate: float, avg_pnl_pct: float, median_pnl_pct: float, total_pnl_pct: float, best: tuple[str, float] | None, worst: tuple[str, float] | None, profit_factor: float | None` (None = hiç kayıp yok → UI'da "∞").
  - `@dataclass ScoreBucket`: `label: str, trade_count: int, win_rate: float, avg_pnl_pct: float`.
  - `@dataclass WhatIfStats`: `strategy: ModeStats, buyhold: ModeStats, exit_counts: dict[str, int], score_buckets: list[ScoreBucket], avg_delay_cost_pct: float | None, avg_holding_days: float | None, cumulative_curve: list[tuple[str, float]], skipped_dedup: int, no_data_count: int`.
  - `compute_stats(trades: list[WhatIfTrade], skipped_dedup: int) -> WhatIfStats`.

Kurallar:
- `ModeStats` ilgili modun `pnl_pct`'si `None` olmayan işlemler üzerinden hesaplanır. Strateji modunda `open_count`/`closed_count` status'ten; al-tut modunda tümü "açık" sayılır (`open_count = trade_count, closed_count = 0`).
- `win_rate`: pnl > 0 oranı, yüzde. `total_pnl_pct`: pnl'lerin toplamı (eşit ağırlık).
- `profit_factor`: kazançların toplamı / kayıpların mutlak toplamı; kayıp yoksa `None`.
- `best`/`worst`: `(symbol, pnl_pct)`.
- `exit_counts`: kapalı strateji işlemlerinin `exit_type` sayımı + `"open"` anahtarıyla açık sayısı.
- `score_buckets`: sabit dilimler `4-5`, `6-7`, `8+` (skor 4'ten küçükse `4-5`'e girmez, atlanır); her dilim strateji pnl'iyle hesaplanır, boş dilimler listeye girmez.
- `avg_delay_cost_pct`: `delay_cost_pct` dolu işlemlerin ortalaması; hiç yoksa `None`.
- `avg_holding_days`: kapalı strateji işlemlerinin ortalaması; yoksa `None`.
- `cumulative_curve`: kapalı strateji işlemleri `exit_date` artan sırada, `strategy_pnl_pct` kümülatif toplamı → `[(exit_date, cum), ...]`.
- `no_data_count`: status `no_data` işlem sayısı.

- [ ] **Step 1: Write the failing tests**

```python
from swing_tracker.core.whatif import WhatIfTrade, compute_stats


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_stats'`

- [ ] **Step 3: Write implementation**

`core/whatif.py`'a ekle (`import statistics` üste):

```python
import statistics

_SCORE_BUCKETS = [("4-5", 4, 5), ("6-7", 6, 7), ("8+", 8, 10**9)]


@dataclass
class ModeStats:
    trade_count: int = 0
    open_count: int = 0
    closed_count: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    median_pnl_pct: float = 0.0
    total_pnl_pct: float = 0.0
    best: tuple[str, float] | None = None
    worst: tuple[str, float] | None = None
    profit_factor: float | None = None


@dataclass
class ScoreBucket:
    label: str
    trade_count: int
    win_rate: float
    avg_pnl_pct: float


@dataclass
class WhatIfStats:
    strategy: ModeStats
    buyhold: ModeStats
    exit_counts: dict[str, int] = field(default_factory=dict)
    score_buckets: list[ScoreBucket] = field(default_factory=list)
    avg_delay_cost_pct: float | None = None
    avg_holding_days: float | None = None
    cumulative_curve: list[tuple[str, float]] = field(default_factory=list)
    skipped_dedup: int = 0
    no_data_count: int = 0


def _mode_stats(
    pairs: list[tuple[WhatIfTrade, float]], open_count: int, closed_count: int
) -> ModeStats:
    """pairs: (islem, o modun pnl_pct'si)."""
    if not pairs:
        return ModeStats()
    pnls = [p for _, p in pairs]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    best = max(pairs, key=lambda x: x[1])
    worst = min(pairs, key=lambda x: x[1])
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and wins else (None if not losses else 0.0)
    return ModeStats(
        trade_count=len(pairs),
        open_count=open_count,
        closed_count=closed_count,
        win_rate=round(len(wins) / len(pnls) * 100, 2),
        avg_pnl_pct=round(sum(pnls) / len(pnls), 2),
        median_pnl_pct=round(statistics.median(pnls), 2),
        total_pnl_pct=round(sum(pnls), 2),
        best=(best[0].symbol, best[1]),
        worst=(worst[0].symbol, worst[1]),
        profit_factor=pf,
    )


def compute_stats(trades: list[WhatIfTrade], skipped_dedup: int) -> WhatIfStats:
    """Islem listesinden sayfa istatistiklerini uret."""
    strat = [(t, t.strategy_pnl_pct) for t in trades if t.strategy_pnl_pct is not None]
    buyhold = [(t, t.buyhold_pnl_pct) for t in trades if t.buyhold_pnl_pct is not None]
    closed = [t for t, _ in strat if t.status == "closed"]
    opened = [t for t, _ in strat if t.status == "open"]

    exit_counts: dict[str, int] = {}
    for t in closed:
        exit_counts[t.exit_type] = exit_counts.get(t.exit_type, 0) + 1
    if opened:
        exit_counts["open"] = len(opened)

    buckets = []
    for label, lo, hi in _SCORE_BUCKETS:
        in_bucket = [(t, p) for t, p in strat if lo <= t.score <= hi]
        if not in_bucket:
            continue
        pnls = [p for _, p in in_bucket]
        wins = [p for p in pnls if p > 0]
        buckets.append(ScoreBucket(
            label=label,
            trade_count=len(in_bucket),
            win_rate=round(len(wins) / len(pnls) * 100, 2),
            avg_pnl_pct=round(sum(pnls) / len(pnls), 2),
        ))

    delays = [t.delay_cost_pct for t in trades if t.delay_cost_pct is not None]
    holdings = [t.holding_days for t in closed if t.holding_days is not None]

    curve: list[tuple[str, float]] = []
    cum = 0.0
    for t in sorted(closed, key=lambda t: t.exit_date or ""):
        cum = round(cum + (t.strategy_pnl_pct or 0.0), 2)
        curve.append((t.exit_date or "", cum))

    return WhatIfStats(
        strategy=_mode_stats(strat, open_count=len(opened), closed_count=len(closed)),
        buyhold=_mode_stats(
            buyhold, open_count=len(buyhold), closed_count=0
        ),
        exit_counts=exit_counts,
        score_buckets=buckets,
        avg_delay_cost_pct=round(sum(delays) / len(delays), 2) if delays else None,
        avg_holding_days=round(sum(holdings) / len(holdings), 2) if holdings else None,
        cumulative_curve=curve,
        skipped_dedup=skipped_dedup,
        no_data_count=sum(1 for t in trades if t.status == "no_data"),
    )
```

Not: `_mode_stats`'daki profit factor kuralı — kayıp yoksa `None` ("∞" gösterimi), kazanç yokken kayıp varsa `0.0`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/swing_tracker/core/whatif.py tests/test_whatif.py
git add src/swing_tracker/core/whatif.py tests/test_whatif.py
git commit -m "feat(whatif): istatistikler — win-rate, profit factor, skor dilimleri, gecikme maliyeti"
```

---

### Task 5: Web router + veri toplama

**Files:**
- Create: `src/swing_tracker/web/routers/whatif.py`
- Modify: `src/swing_tracker/web/app.py:27` (import) ve `:120-124` (include_router)
- Test: `tests/test_whatif.py` (ekleme)

**Interfaces:**
- Consumes: `Repository.get_buy_signals_asc` (Task 1), `simulate_whatif`, `compute_stats` (Task 3/4), `swing_tracker.backtest.runner.parse_config_from_toml`, `swing_tracker.core.ohlcv_cache.get_ohlcv`, `swing_tracker.web.price_cache.price_cache`, `swing_tracker.core.scanner.MIN_ENTRY_SCORE`.
- Produces:
  - `GET /whatif` → `whatif.html` (Task 6'da yazılacak; bu task'ta template'ler henüz yokken endpoint testi template'i mock'lamaz — Step sırasına dikkat: bu task yalnızca `build_whatif_data`'yı test eder, endpoint'ler Task 6 ile birlikte doğrulanır).
  - `build_whatif_data(repo, config) -> tuple[list[WhatIfTrade], WhatIfStats]` — sync, network'e çıkan toplama fonksiyonu (testte tamamı mock'lanabilir parçalardan oluşur).

- [ ] **Step 1: Write the failing test**

```python
class TestBuildWhatIfData:
    def test_assembles_from_injected_fetchers(self, repo, monkeypatch):
        from swing_tracker.web.routers import whatif as whatif_router

        _log(repo, "THYAO", score=5, price=100.0, created_at="2026-06-21 07:30:00")

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
        assert stats.strategy.trade_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whatif.py::TestBuildWhatIfData -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.web.routers.whatif'`

- [ ] **Step 3: Write implementation**

`src/swing_tracker/web/routers/whatif.py` oluştur:

```python
"""What-if router — sinyaller alinsaydi performans simulasyonu."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from swing_tracker.backtest.runner import parse_config_from_toml
from swing_tracker.core.ohlcv_cache import get_ohlcv
from swing_tracker.core.scanner import MIN_ENTRY_SCORE
from swing_tracker.core.whatif import WhatIfStats, WhatIfTrade, compute_stats, simulate_whatif
from swing_tracker.web.dependencies import get_config, get_repo, templates
from swing_tracker.web.helpers import localize_signal_timestamps
from swing_tracker.web.price_cache import price_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatif")

# Simulasyon icin veri pencereleri
_DAILY_PERIOD = "1y"
_HOURLY_PERIOD = "3mo"


def build_whatif_data(repo, config) -> tuple[list[WhatIfTrade], WhatIfStats]:
    """Sinyalleri cek, OHLCV + guncel fiyatlari topla, simulasyonu kostur. Sync/blocking."""
    signals = repo.get_buy_signals_asc(min_score=MIN_ENTRY_SCORE)
    symbols = list(dict.fromkeys(s["symbol"] for s in signals))

    ohlcv_1h = {}
    ohlcv_1d = {}
    for sym in symbols:
        try:
            ohlcv_1h[sym] = get_ohlcv(
                sym, interval="1h", period=_HOURLY_PERIOD,
                repo=repo, cache_cfg=config.cache,
            )
        except Exception:
            logger.warning("whatif: 1h veri alinamadi: %s", sym, exc_info=True)
            ohlcv_1h[sym] = None
        try:
            ohlcv_1d[sym] = get_ohlcv(
                sym, interval="1d", period=_DAILY_PERIOD,
                repo=repo, cache_cfg=config.cache,
            )
        except Exception:
            logger.warning("whatif: 1d veri alinamadi: %s", sym, exc_info=True)
            ohlcv_1d[sym] = None

    current_prices = price_cache.fetch_many(symbols)
    # Komisyon sifirlanir: sanal islemlerde yuzde getiri olculur (spec karari)
    bt_config = dataclasses.replace(
        parse_config_from_toml(), commission_pct=0.0, commission_fixed=0.0
    )

    trades, skipped = simulate_whatif(signals, ohlcv_1h, ohlcv_1d, current_prices, bt_config)
    stats = compute_stats(trades, skipped)
    return trades, stats


@router.get("", response_class=HTMLResponse)
async def whatif_page(request: Request):
    """Skeleton sayfa — hesaplama yok, fragment htmx ile yuklenir."""
    return templates.TemplateResponse(request, "whatif.html", context={})


@router.get("/results", response_class=HTMLResponse)
async def whatif_results(request: Request):
    """Simulasyonu kosturup sonuc fragment'ini dondurur."""
    repo = get_repo()
    config = get_config()

    trades, stats = await asyncio.to_thread(build_whatif_data, repo, config)

    # Sinyal saatlerini goruntuleme icin Istanbul'a cevir
    display = []
    for t in trades:
        d = t.__dict__.copy()
        d["signal_time_local"] = localize_signal_timestamps(
            [{"created_at": t.signal_time}], config.timezone
        )[0]["created_at"]
        display.append(d)

    # En yeni sinyal ustte
    display.sort(key=lambda d: d["signal_time"], reverse=True)

    return templates.TemplateResponse(
        request,
        "fragments/whatif_results.html",
        context={"trades": display, "stats": stats},
    )
```

`app.py`'de iki değişiklik:

```python
from swing_tracker.web.routers import dashboard, portfolio, signals, symbol, trades, whatif
```

ve router kayıtlarının sonuna:

```python
app.include_router(whatif.router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whatif.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
ruff check src/swing_tracker/web/routers/whatif.py tests/test_whatif.py
git add src/swing_tracker/web/routers/whatif.py src/swing_tracker/web/app.py tests/test_whatif.py
git commit -m "feat(whatif): /whatif router — veri toplama + simulasyon endpoint'leri"
```

---

### Task 6: Template'ler + nav

**Files:**
- Create: `src/swing_tracker/web/templates/whatif.html`
- Create: `src/swing_tracker/web/templates/fragments/whatif_results.html`
- Modify: `src/swing_tracker/web/templates/base.html` (desktop nav ~satır 76-79 sonrası; bottom nav ~satır 132-139 sonrası)
- Test: manuel doğrulama (Step 4) — template render'ı Python testiyle değil çalışan app ile doğrulanır.

**Interfaces:**
- Consumes: Task 5 context'i — `trades: list[dict]` (WhatIfTrade alanları + `signal_time_local`), `stats: WhatIfStats`.
- Not: mevcut şablonlardaki Tailwind sınıf düzenini (`bg-surface-raised`, `border-border`, `text-txt-*`, `text-accent`) ve `STATUS_TR` global'ini kullan. Örnek yapı için `signals.html`'e bak.

- [ ] **Step 1: Skeleton sayfa**

`templates/whatif.html`:

```html
{% extends "base.html" %}

{% block title %}What-if — Swing Tracker{% endblock %}
{% block nav_whatif %}text-accent bg-accent/10{% endblock %}
{% block bottom_nav_whatif %}text-accent{% endblock %}

{% block content %}
<div class="mb-6">
    <h1 class="text-2xl font-bold text-txt-primary">What-if</h1>
    <p class="text-sm text-txt-muted mt-1">
        Uretilen sinyalleri alsaydim performansim ne olurdu?
    </p>
</div>

<div hx-get="/whatif/results" hx-trigger="load" hx-swap="outerHTML">
    <div class="bg-surface-raised border border-border rounded-xl p-8 text-center">
        <div class="animate-pulse text-txt-muted text-sm">
            Simulasyon calisiyor, fiyat verileri yukleniyor...
        </div>
    </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Sonuç fragment'i**

`templates/fragments/whatif_results.html`:

```html
<div>
{% if not trades %}
<div class="bg-surface-raised border border-border rounded-xl p-8 text-center text-txt-muted">
    Henuz esik ustu buy sinyali yok. Sinyaller uretildikce burada simulasyon gorunecek.
</div>
{% else %}

<!-- Ozet kartlari: Strateji vs Al-Tut -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
    {% for mode, label in [(stats.strategy, "Strateji Kurallari"), (stats.buyhold, "Al ve Tut")] %}
    <div class="bg-surface-raised border border-border rounded-xl p-4">
        <h2 class="text-sm font-semibold text-txt-secondary mb-3">{{ label }}</h2>
        <div class="grid grid-cols-2 gap-3">
            <div>
                <div class="text-[11px] text-txt-ghost uppercase">Toplam Getiri</div>
                <div class="text-xl font-bold {{ 'text-emerald-400' if mode.total_pnl_pct >= 0 else 'text-red-400' }}">
                    {{ "%+.1f"|format(mode.total_pnl_pct) }}%
                </div>
            </div>
            <div>
                <div class="text-[11px] text-txt-ghost uppercase">Win Rate</div>
                <div class="text-xl font-bold text-txt-primary">{{ "%.0f"|format(mode.win_rate) }}%</div>
            </div>
            <div>
                <div class="text-[11px] text-txt-ghost uppercase">Ort. Getiri</div>
                <div class="text-sm font-semibold {{ 'text-emerald-400' if mode.avg_pnl_pct >= 0 else 'text-red-400' }}">
                    {{ "%+.2f"|format(mode.avg_pnl_pct) }}%
                </div>
            </div>
            <div>
                <div class="text-[11px] text-txt-ghost uppercase">Islem</div>
                <div class="text-sm font-semibold text-txt-primary">
                    {{ mode.trade_count }}
                    {% if mode.closed_count %}<span class="text-txt-ghost">({{ mode.closed_count }} kapali)</span>{% endif %}
                </div>
            </div>
        </div>
    </div>
    {% endfor %}
</div>

<!-- Istatistikler -->
<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
    <div class="bg-surface-raised border border-border rounded-xl p-4">
        <h2 class="text-sm font-semibold text-txt-secondary mb-3">Dagilim (Strateji)</h2>
        <dl class="space-y-2 text-sm">
            <div class="flex justify-between">
                <dt class="text-txt-muted">Medyan getiri</dt>
                <dd class="text-txt-primary font-medium">{{ "%+.2f"|format(stats.strategy.median_pnl_pct) }}%</dd>
            </div>
            <div class="flex justify-between">
                <dt class="text-txt-muted">Profit factor</dt>
                <dd class="text-txt-primary font-medium">
                    {% if stats.strategy.profit_factor is none %}&infin;{% else %}{{ stats.strategy.profit_factor }}{% endif %}
                </dd>
            </div>
            {% if stats.strategy.best %}
            <div class="flex justify-between">
                <dt class="text-txt-muted">En iyi</dt>
                <dd class="text-emerald-400 font-medium">
                    <a href="/symbol/{{ stats.strategy.best[0] }}" class="hover:underline">{{ stats.strategy.best[0] }}</a>
                    {{ "%+.1f"|format(stats.strategy.best[1]) }}%
                </dd>
            </div>
            <div class="flex justify-between">
                <dt class="text-txt-muted">En kotu</dt>
                <dd class="text-red-400 font-medium">
                    <a href="/symbol/{{ stats.strategy.worst[0] }}" class="hover:underline">{{ stats.strategy.worst[0] }}</a>
                    {{ "%+.1f"|format(stats.strategy.worst[1]) }}%
                </dd>
            </div>
            {% endif %}
            {% if stats.avg_holding_days is not none %}
            <div class="flex justify-between">
                <dt class="text-txt-muted">Ort. tutma suresi</dt>
                <dd class="text-txt-primary font-medium">{{ stats.avg_holding_days }} gun</dd>
            </div>
            {% endif %}
            {% if stats.avg_delay_cost_pct is not none %}
            <div class="flex justify-between">
                <dt class="text-txt-muted">15 dk gecikme maliyeti (ort.)</dt>
                <dd class="{{ 'text-red-400' if stats.avg_delay_cost_pct > 0 else 'text-emerald-400' }} font-medium">
                    {{ "%+.2f"|format(stats.avg_delay_cost_pct) }}%
                </dd>
            </div>
            {% endif %}
        </dl>
    </div>

    <div class="bg-surface-raised border border-border rounded-xl p-4">
        <h2 class="text-sm font-semibold text-txt-secondary mb-3">Cikis Tipleri &amp; Skor Dilimleri</h2>
        <div class="flex flex-wrap gap-2 mb-4">
            {% for etype, count in stats.exit_counts.items() %}
            <span class="px-2 py-1 rounded-lg text-xs font-medium bg-accent/10 text-txt-secondary">
                {{ STATUS_TR.get(etype, etype)|upper }}: {{ count }}
            </span>
            {% endfor %}
        </div>
        <table class="w-full text-sm">
            <thead>
                <tr class="text-[11px] text-txt-ghost uppercase text-left">
                    <th class="pb-2">Skor</th><th class="pb-2">Islem</th>
                    <th class="pb-2">Win Rate</th><th class="pb-2">Ort. Getiri</th>
                </tr>
            </thead>
            <tbody>
                {% for b in stats.score_buckets %}
                <tr class="border-t border-border">
                    <td class="py-1.5 text-txt-primary font-medium">{{ b.label }}</td>
                    <td class="py-1.5 text-txt-muted">{{ b.trade_count }}</td>
                    <td class="py-1.5 text-txt-muted">{{ "%.0f"|format(b.win_rate) }}%</td>
                    <td class="py-1.5 {{ 'text-emerald-400' if b.avg_pnl_pct >= 0 else 'text-red-400' }}">
                        {{ "%+.2f"|format(b.avg_pnl_pct) }}%
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>

<!-- Kumulatif getiri egrisi -->
{% if stats.cumulative_curve|length >= 2 %}
<div class="bg-surface-raised border border-border rounded-xl p-4 mb-6">
    <h2 class="text-sm font-semibold text-txt-secondary mb-3">Kumulatif Getiri (kapanan islemler)</h2>
    <div class="h-48"><canvas id="whatifCurve"></canvas></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
(function() {
    const curve = {{ stats.cumulative_curve|tojson }};
    const ctx = document.getElementById('whatifCurve').getContext('2d');
    new Chart(ctx, {
        type: 'line',
        data: {
            labels: curve.map(p => p[0]),
            datasets: [{
                data: curve.map(p => p[1]),
                borderColor: '#66bb6a',
                borderWidth: 1.5,
                fill: true,
                backgroundColor: 'rgba(102, 187, 106, 0.05)',
                pointRadius: 2,
                tension: 0.1,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { ticks: { callback: v => v + '%' } }
            }
        }
    });
})();
</script>
{% endif %}

<!-- Islem tablosu -->
<div class="bg-surface-raised border border-border rounded-xl overflow-hidden">
    <div class="overflow-x-auto">
        <table class="w-full text-sm">
            <thead>
                <tr class="text-[11px] text-txt-ghost uppercase text-left border-b border-border">
                    <th class="px-4 py-2.5">Sembol</th>
                    <th class="px-4 py-2.5">Sinyal</th>
                    <th class="px-4 py-2.5">Skor</th>
                    <th class="px-4 py-2.5">Giris</th>
                    <th class="px-4 py-2.5">Strateji</th>
                    <th class="px-4 py-2.5">Durum</th>
                    <th class="px-4 py-2.5">Al-Tut</th>
                </tr>
            </thead>
            <tbody>
                {% for t in trades %}
                <tr class="border-b border-border/50">
                    <td class="px-4 py-2.5">
                        <a href="/symbol/{{ t.symbol }}" class="text-accent font-medium hover:underline">{{ t.symbol }}</a>
                    </td>
                    <td class="px-4 py-2.5 text-txt-muted whitespace-nowrap">{{ t.signal_time_local }}</td>
                    <td class="px-4 py-2.5 text-txt-primary">{{ t.score }}</td>
                    <td class="px-4 py-2.5 text-txt-primary whitespace-nowrap">
                        {{ "%.2f"|format(t.entry_price) }}
                        {% if t.entry_source == 'fallback' %}
                        <span class="text-txt-ghost text-xs" title="1h bar bulunamadi, sinyal fiyati kullanildi">*</span>
                        {% endif %}
                    </td>
                    <td class="px-4 py-2.5 font-medium
                        {% if t.strategy_pnl_pct is none %}text-txt-ghost
                        {% elif t.strategy_pnl_pct >= 0 %}text-emerald-400{% else %}text-red-400{% endif %}">
                        {% if t.strategy_pnl_pct is none %}—{% else %}{{ "%+.1f"|format(t.strategy_pnl_pct) }}%{% endif %}
                    </td>
                    <td class="px-4 py-2.5">
                        {% if t.status == 'no_data' %}
                        <span class="px-2 py-0.5 rounded text-xs bg-border/50 text-txt-ghost">VERI YOK</span>
                        {% elif t.status == 'open' %}
                        <span class="px-2 py-0.5 rounded text-xs bg-accent/10 text-accent">ACIK</span>
                        {% else %}
                        <span class="px-2 py-0.5 rounded text-xs bg-border/50 text-txt-secondary">{{ STATUS_TR.get(t.exit_type, t.exit_type)|upper }}</span>
                        {% endif %}
                    </td>
                    <td class="px-4 py-2.5 font-medium
                        {% if t.buyhold_pnl_pct is none %}text-txt-ghost
                        {% elif t.buyhold_pnl_pct >= 0 %}text-emerald-400{% else %}text-red-400{% endif %}">
                        {% if t.buyhold_pnl_pct is none %}—{% else %}{{ "%+.1f"|format(t.buyhold_pnl_pct) }}%{% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>

<p class="text-xs text-txt-ghost mt-4">
    Giris fiyatlari sinyalden sonraki ilk saatlik bar'in kapanisidir (15 dk veri gecikmesi modeli).
    {% if stats.skipped_dedup %}{{ stats.skipped_dedup }} sinyal acik pozisyon nedeniyle atlandi.{% endif %}
    {% if stats.no_data_count %}{{ stats.no_data_count }} islemde veri eksik.{% endif %}
    * isaretli girislerde saatlik veri yoktu, sinyal fiyati kullanildi.
</p>
{% endif %}
</div>
```

Not: renk sınıfları (`text-emerald-400`/`text-red-400`) mevcut şablonlarda kâr/zarar için ne kullanılıyorsa ona uyarlanmalı — `grep -o 'text-[a-z]*-400' src/swing_tracker/web/templates/portfolio.html | sort -u` ile kontrol et ve aynısını kullan.

- [ ] **Step 3: Nav girişleri**

`base.html` desktop nav'da `/signals` linkinin kapanışından sonra (satır ~79) ekle:

```html
                    <a href="/whatif" class="px-3 py-1.5 rounded-lg text-sm font-medium
                        {% block nav_whatif %}text-txt-muted hover:text-txt-primary hover:bg-accent/5{% endblock %}">
                        What-if
                    </a>
```

Bottom nav'da `/signals` girişinin kapanış `</a>`'sından sonra ekle:

```html
            <a href="/whatif" class="flex-1 flex flex-col items-center gap-0.5 py-2.5
                {% block bottom_nav_whatif %}text-txt-muted{% endblock %}">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-5 h-5">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636" />
                </svg>
                <span class="text-[10px] font-medium">What-if</span>
            </a>
```

- [ ] **Step 4: Manuel doğrulama**

```bash
python -m pytest tests/ -v          # tum suite yesil
ruff check src tests                # temiz
```

Sonra app'i başlat (`.claude/launch.json` varsa preview aracıyla, yoksa `python -m swing_tracker.web.app`) ve doğrula:

1. `GET /whatif` → skeleton görünür, sonra sonuçlar yüklenir (canlı DB'de sinyal yoksa boş durum mesajı görünmeli).
2. Nav'da What-if linki hem desktop hem mobil genişlikte görünür ve aktif sayfada vurgulanır.
3. `/symbol/X` linkleri çalışır.

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/web/templates/whatif.html \
        src/swing_tracker/web/templates/fragments/whatif_results.html \
        src/swing_tracker/web/templates/base.html
git commit -m "feat(whatif): what-if sayfasi — ozet, istatistik, kumulatif egri, islem tablosu"
```

---

### Task 7: Son doğrulama

**Files:** yok (doğrulama).

- [ ] **Step 1: Tüm suite + lint**

```bash
python -m pytest tests/ -v
ruff check src tests
```

Expected: tümü PASS, lint temiz.

- [ ] **Step 2: Gerçek veriyle smoke test**

Gerçek `data/` DB'si varsa app'i başlatıp `/whatif`'i aç; sinyaller varsa tabloda İstanbul saatiyle göründüğünü, strateji/al-tut kolonlarının dolu olduğunu, gecikme maliyeti istatistiğinin çıktığını gözle doğrula.

- [ ] **Step 3: Branch'i tamamla**

`superpowers:finishing-a-development-branch` skill'i ile devam et (PR → main, kullanıcının branch workflow tercihi).
