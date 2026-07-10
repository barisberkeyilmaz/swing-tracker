# What-If Faz 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/whatif` sayfasını saf DB okumasına çevir: sinyaller `whatif_trades` tablosunda kalıcı sanal işlemler olarak yaşar, günlük job onları ilerletir.

**Architecture:** Yeni `core/whatif_store.py` kalıcı-işlem katmanı: satır ↔ `BacktestTrade` durum dönüşümü + job'un üç adımı (pending doldurma, incremental open güncelleme + al-tut yenileme, zaman aşımı). Scanner sinyal düşünce `pending` INSERT eder; backfill CLI eski sinyalleri pending ekleyip **aynı** job pipeline'ını çalıştırır (ayrı retrospektif yol yok). Router tabloyu okur; dedup artık `core/whatif.py`'de saf bir okuma filtresi, iki mod (takip/tüm) tek tablodan çıkar.

**Tech Stack:** Python 3.11, SQLite (raw SQL), APScheduler CronTrigger, FastAPI + Jinja2 + htmx, pandas, pytest.

**Spec:** `docs/superpowers/specs/2026-07-10-whatif-phase2-design.md`
**Branch:** `feature/whatif-phase2` (mevcut)

## Global Constraints

- Ruff scoped: değişen dosyalarda `ruff check <dosyalar>` temiz (repoda 19 pre-existing hata başka dosyalarda — onlara dokunma).
- Venv: `.venv/bin/python -m pytest ...`, `.venv/bin/ruff ...`.
- Repository method'ları `dict` döndürür; raw SQL + parametreli sorgular; upsert'te `ON CONFLICT`/`INSERT OR IGNORE`.
- `core/` pure function — network yok; veri erişimi (`repo`, DataFrame, fiyat) parametreyle enjekte edilir. `whatif_store` repo'ya yazar ama OHLCV/fiyatı parametre alır.
- Zaman: `signal_time` ve bar index'leri naive UTC; `last_update`/`exit_date` ISO gün (`YYYY-MM-DD`). Görüntüleme İstanbul.
- Skor `entry_score` ölçeği (0-10); `signals_log.score = entry_score*10` (scanner) — normalizasyon `normalize_signal_score` ile tek yerden.
- Sanal pozisyon 100 pay (`VIRTUAL_SHARES`), komisyon 0 (`dataclasses.replace(parse_config_from_toml(), commission_pct=0.0, commission_fixed=0.0)`).
- UI metinleri Türkçe, ASCII. Kâr/zarar renkleri `text-positive`/`text-negative`.
- Scheduler job'ları `CronTrigger`, timezone `Europe/Istanbul`; `whatif_update` Pzt-Cum 18:40.
- Config: `[whatif]` → `enabled = true`, `max_holding_days = 60`.

---

### Task 1: Config — WhatIfConfig

**Files:**
- Modify: `src/swing_tracker/config.py` (CacheConfig'in altına dataclass; `Config`'e alan; `load_config` içine yükleme bloğu)
- Modify: `config.toml` (dosya sonuna `[whatif]` bölümü)
- Test: `tests/test_whatif_store.py` (yeni dosya)

**Interfaces:**
- Produces: `WhatIfConfig(enabled: bool = True, max_holding_days: int = 60)`; `Config.whatif: WhatIfConfig`.

- [ ] **Step 1: Write the failing test**

`tests/test_whatif_store.py` oluştur:

```python
"""Tests for whatif persistent store: config, schema/CRUD, job steps."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from swing_tracker.config import WhatIfConfig, load_config
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'WhatIfConfig'`

- [ ] **Step 3: Implement**

`config.py` — `CacheConfig`'in hemen altına:

```python
@dataclass
class WhatIfConfig:
    enabled: bool = True
    max_holding_days: int = 60  # acik sanal pozisyon zaman asimi (gun)
```

`Config` dataclass'ına (`liquidity` alanının altına):

```python
    whatif: WhatIfConfig = field(default_factory=WhatIfConfig)
```

`load_config` içinde (cache bloğundan sonra, mevcut pattern'le):

```python
    # What-if
    wi = raw.get("whatif", {})
    config.whatif = WhatIfConfig(
        enabled=wi.get("enabled", True),
        max_holding_days=wi.get("max_holding_days", 60),
    )
```

`config.toml` sonuna:

```toml
[whatif]
enabled = true
max_holding_days = 60   # acik sanal pozisyon zaman asimi
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: 2 PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/config.py tests/test_whatif_store.py
git add src/swing_tracker/config.py config.toml tests/test_whatif_store.py
git commit -m "feat(whatif): [whatif] config bolumu — enabled + max_holding_days"
```

---

### Task 2: Şema + Repository CRUD

**Files:**
- Modify: `src/swing_tracker/db/schema.py` (`_TABLES`/DDL listesine yeni tablo + indexler)
- Modify: `src/swing_tracker/db/repository.py` (`get_buy_signals_asc`'in altına yeni bölüm)
- Test: `tests/test_whatif_store.py` (ekleme)

**Interfaces:**
- Produces:
  - `Repository.insert_whatif_trade(fields: dict) -> int | None` — `INSERT OR IGNORE`; eklenirse rowid, ignore edilirse None. Zorunlu key'ler: `signal_id, symbol, signal_time, score`; opsiyonel: `price_at_signal, status` (default 'pending').
  - `Repository.get_whatif_trades(status: str | None = None) -> list[dict]` — `signal_time ASC, id ASC`; status verilirse filtreli.
  - `Repository.update_whatif_trade(trade_id: int, fields: dict) -> None` — whitelist'li kolon güncelleme.

- [ ] **Step 1: Write the failing tests**

`tests/test_whatif_store.py`'a ekle:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: FAIL — `no such table: whatif_trades` veya `AttributeError: insert_whatif_trade`

- [ ] **Step 3: Implement**

`schema.py` DDL listesine (CREATE IF NOT EXISTS pattern'iyle):

```python
    """
    CREATE TABLE IF NOT EXISTS whatif_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL UNIQUE REFERENCES signals_log(id),
        symbol TEXT NOT NULL,
        signal_time TEXT NOT NULL,
        score INTEGER NOT NULL,
        price_at_signal REAL,
        entry_price REAL,
        entry_source TEXT CHECK(entry_source IN ('bar_1h','fallback')),
        stop_loss REAL, tp1 REAL, tp2 REAL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','open','closed','expired','no_data')),
        remaining_shares INTEGER,
        realized_pnl REAL DEFAULT 0,
        highest_price REAL,
        tp1_hit INTEGER DEFAULT 0,
        exit_type TEXT,
        exit_date TEXT,
        strategy_pnl_pct REAL,
        buyhold_pnl_pct REAL,
        last_close REAL,
        delay_cost_pct REAL,
        holding_days REAL,
        last_update TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_whatif_trades_status ON whatif_trades(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_whatif_trades_symbol
        ON whatif_trades(symbol, signal_time)
    """,
```

Not: `schema.py`'de DDL'lerin nasıl toplandığını (liste adı / `create_all_tables` döngüsü) dosyayı açıp gör; index'ler ayrı statement gerektiriyorsa mevcut pattern'e uy.

`repository.py` — `get_buy_signals_asc`'in altına:

```python
    # ── What-if trades ──

    _WHATIF_COLUMNS = {
        "signal_id", "symbol", "signal_time", "score", "price_at_signal",
        "entry_price", "entry_source", "stop_loss", "tp1", "tp2", "status",
        "remaining_shares", "realized_pnl", "highest_price", "tp1_hit",
        "exit_type", "exit_date", "strategy_pnl_pct", "buyhold_pnl_pct",
        "last_close", "delay_cost_pct", "holding_days", "last_update",
    }

    def insert_whatif_trade(self, fields: dict) -> int | None:
        """Sanal islem ekle (INSERT OR IGNORE — signal_id UNIQUE). None = zaten var."""
        bad = set(fields) - self._WHATIF_COLUMNS
        if bad:
            raise ValueError(f"Bilinmeyen whatif_trades kolonlari: {bad}")
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO whatif_trades ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(fields[c] for c in cols),
        )
        self._conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    def get_whatif_trades(self, status: str | None = None) -> list[dict]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM whatif_trades WHERE status = ? "
                "ORDER BY signal_time ASC, id ASC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM whatif_trades ORDER BY signal_time ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_whatif_trade(self, trade_id: int, fields: dict) -> None:
        bad = set(fields) - self._WHATIF_COLUMNS
        if bad:
            raise ValueError(f"Bilinmeyen whatif_trades kolonlari: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{c} = ?" for c in fields)
        self._conn.execute(
            f"UPDATE whatif_trades SET {sets} WHERE id = ?",
            (*fields.values(), trade_id),
        )
        self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/db/schema.py src/swing_tracker/db/repository.py tests/test_whatif_store.py
git add src/swing_tracker/db/schema.py src/swing_tracker/db/repository.py tests/test_whatif_store.py
git commit -m "feat(whatif): whatif_trades tablosu + repository CRUD"
```

---

### Task 3: core/whatif.py genişletmeleri — status'ler, dedup filtresi, skor normalizasyonu

**Files:**
- Modify: `src/swing_tracker/core/whatif.py`
- Modify: `src/swing_tracker/web/routers/whatif.py` (`_entry_score`'u kaldır, core'dan import et)
- Test: `tests/test_whatif.py` (ekleme; mevcut `TestEntryScore` sınıfının import'unu güncelle)

**Interfaces:**
- Produces:
  - `WhatIfTrade.status` Literal'ine `"pending"` ve `"expired"` eklenir.
  - `compute_stats`: `expired` işlemler kapalı sayılır (kapalı istatistikleri, çıkış dağılımı, kümülatif eğri, holding days); `pending` işlemler pnl'siz olduğundan doğal olarak istatistik dışıdır.
  - `dedup_filter(trades: list[WhatIfTrade]) -> tuple[list[WhatIfTrade], int]` — kronolojik gezip sembolde açık/pending/no_data satır (veya kapanışı sonraki) varken gelenleri atar; `(kalanlar, atlanan_sayisi)`.
  - `normalize_signal_score(sig: dict) -> int` — router'daki `_entry_score`'un core'a taşınmış hali (davranış birebir aynı).

- [ ] **Step 1: Write the failing tests**

`tests/test_whatif.py`'a ekle (mevcut `_trade` helper'ı kullan; `dedup_filter`, `normalize_signal_score` import'larını üstteki whatif import bloğuna ekle):

```python
class TestDedupFilter:
    def _t(self, symbol, signal_time, status="closed", exit_date=None):
        t = _trade(symbol=symbol, status=status, spnl=1.0 if status != "pending" else None,
                   exit_type="tp1" if status in ("closed", "expired") else None,
                   exit_date=exit_date, holding=1.0)
        t.signal_time = signal_time
        return t

    def test_open_blocks_later_signal(self):
        trades = [
            self._t("THYAO", "2026-07-01 08:00:00", status="open"),
            self._t("THYAO", "2026-07-02 08:00:00", status="open"),
            self._t("ASELS", "2026-07-02 09:00:00", status="open"),
        ]
        kept, skipped = dedup_filter(trades)
        assert [t.symbol for t in kept] == ["THYAO", "ASELS"]
        assert skipped == 1

    def test_closed_allows_after_exit(self):
        trades = [
            self._t("THYAO", "2026-07-01 08:00:00", exit_date="2026-07-03"),
            self._t("THYAO", "2026-07-04 08:00:00", status="open"),
        ]
        kept, skipped = dedup_filter(trades)
        assert len(kept) == 2 and skipped == 0

    def test_closed_blocks_before_exit(self):
        trades = [
            self._t("THYAO", "2026-07-01 08:00:00", exit_date="2026-07-10"),
            self._t("THYAO", "2026-07-05 08:00:00", status="open"),
        ]
        kept, skipped = dedup_filter(trades)
        assert len(kept) == 1 and skipped == 1

    def test_expired_releases_block(self):
        trades = [
            self._t("THYAO", "2026-07-01 08:00:00", status="expired", exit_date="2026-07-03"),
            self._t("THYAO", "2026-07-04 08:00:00", status="open"),
        ]
        kept, skipped = dedup_filter(trades)
        assert len(kept) == 2 and skipped == 0


class TestExpiredInStats:
    def test_expired_counts_as_closed(self):
        trades = [
            _trade(symbol="A", status="expired", spnl=-2.0, exit_type="expired",
                   exit_date="2026-07-05", holding=60.0),
        ]
        stats = compute_stats(trades, 0)
        assert stats.strategy.closed_count == 1
        assert stats.exit_counts == {"expired": 1}
        assert stats.cumulative_curve == [("2026-07-05", -2.0)]
```

Ayrıca mevcut `TestEntryScore` sınıfındaki import'u değiştir: `from swing_tracker.web.routers.whatif import _entry_score` yerine `from swing_tracker.core.whatif import normalize_signal_score` kullan ve çağrıları yeniden adlandır (davranış testleri aynı kalır).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `ImportError: cannot import name 'dedup_filter'`

- [ ] **Step 3: Implement**

`core/whatif.py`:

1. `import json` ekle. `WhatIfTrade.status` tipini genişlet:

```python
    status: Literal["pending", "open", "closed", "expired", "no_data"]
```

2. `compute_stats` içinde kapalı kümeyi genişlet (`closed`/`opened` satırları):

```python
    closed = [t for t, _ in strat if t.status in ("closed", "expired")]
    opened = [t for t, _ in strat if t.status == "open"]
```

3. Dosyaya ekle (compute_stats'in üstüne):

```python
def normalize_signal_score(sig: dict) -> int:
    """signals_log.score = entry_score * 10 (scanner boyle yazar).

    indicator_values JSON'daki entry_score birincil kaynak; yoksa score/10'a,
    o da yoksa score'un kendisine duser.
    """
    try:
        values = json.loads(sig.get("indicator_values") or "{}")
        if "entry_score" in values:
            return int(values["entry_score"])
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    score = sig.get("score") or 0
    return score // 10 if score >= 10 else score


def dedup_filter(trades: list[WhatIfTrade]) -> tuple[list[WhatIfTrade], int]:
    """'Takip edilebilir' gorunum: sembolde onceki islem hala aktifken
    (open/pending/no_data, ya da kapanisi bu sinyalden sonra) gelen islemleri atar.

    trades signal_time artan sirali olmali. (kalanlar, atlanan_sayisi) doner.
    """
    kept: list[WhatIfTrade] = []
    skipped = 0
    # symbol -> blok bitis Timestamp'i (None = suresiz aktif)
    blocked_until: dict[str, pd.Timestamp | None] = {}

    for t in trades:
        if t.symbol in blocked_until:
            until = blocked_until[t.symbol]
            if until is None or pd.Timestamp(t.signal_time) <= until:
                skipped += 1
                continue
        kept.append(t)
        if t.status in ("closed", "expired"):
            blocked_until[t.symbol] = pd.Timestamp(t.exit_date)
        else:
            blocked_until[t.symbol] = None
    return kept, skipped
```

4. `web/routers/whatif.py`: `_entry_score` fonksiyonunu ve `import json`'ı sil; `from swing_tracker.core.whatif import ...` satırına `normalize_signal_score` ekle; `build_whatif_data` içindeki `sig["score"] = _entry_score(sig)` çağrısını `normalize_signal_score(sig)` yap. (Bu task'ta router'ın geri kalanına dokunma — okuma yolu Task 7'de değişiyor.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif.py tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/core/whatif.py src/swing_tracker/web/routers/whatif.py tests/test_whatif.py
git add src/swing_tracker/core/whatif.py src/swing_tracker/web/routers/whatif.py tests/test_whatif.py
git commit -m "feat(whatif): dedup okuma filtresi + expired/pending statuleri + skor normalizasyonu core'a"
```

---

### Task 4: whatif_store — satır↔trade dönüşümü + pending doldurma

**Files:**
- Create: `src/swing_tracker/core/whatif_store.py`
- Test: `tests/test_whatif_store.py` (ekleme)

**Interfaces:**
- Consumes: `find_entry`, `atr_from_daily`, `VIRTUAL_SHARES` (core/whatif.py); `Repository.get_whatif_trades/update_whatif_trade` (Task 2); `BacktestConfig`.
- Produces:
  - `row_to_bt(row: dict) -> BacktestTrade` — open satırın durum alanlarından BacktestTrade kurar.
  - `fill_pending(repo, ohlcv_1h: dict[str, pd.DataFrame | None], ohlcv_1d: dict[str, pd.DataFrame | None], bt_config: BacktestConfig) -> dict` — `{"opened": n, "no_data": n, "left_pending": n}`.
  - `OhlcvMap = dict[str, pd.DataFrame | None]` type alias.

Kurallar:
- Giriş: `find_entry(df_1h, signal_time, price_at_signal)` (2 gün penceresi core'da). `None` dönerse satır `pending` KALIR (ertesi gün yeniden denenir; expiry eninde sonunda temizler).
- ATR: `atr_from_daily(df_1d, signal_time)`. `df_1d` yok veya ATR `None` → `status='no_data'` (giriş bulunduysa `entry_price/entry_source/delay_cost_pct` yine yazılır — al-tut için gerekli).
- Başarılı doldurma: `status='open'`, `remaining_shares=VIRTUAL_SHARES`, `highest_price=entry`, `realized_pnl=0`, `tp1_hit=0`, `last_update` = sinyal gününün ISO tarihi (lookahead önlemi: exit kontrolü ertesi günden başlar), SL/TP'ler yuvarlanmış (`round(x, 2)`).
- `delay_cost_pct` sadece `entry_source == 'bar_1h'` ve `price_at_signal` doluyken.

- [ ] **Step 1: Write the failing tests**

`tests/test_whatif_store.py`'a ekle (dosya başındaki import'lara `BacktestConfig`, `whatif_store` öğelerini ekle):

```python
from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.core.whatif_store import fill_pending, row_to_bt


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.core.whatif_store'`

- [ ] **Step 3: Implement**

`src/swing_tracker/core/whatif_store.py` oluştur:

```python
"""Kalici what-if islemleri: whatif_trades satirlarini gunluk ilerleten katman.

Sayfa hicbir simulasyon yapmaz; sinyal dusunce scanner 'pending' satir ekler,
gunluk job (fill_pending -> update_open -> refresh_buyhold -> expire_stale)
hissenin yolunu DB'de yasatir. OHLCV parametreyle enjekte edilir (network yok).
"""

from __future__ import annotations

import logging

import pandas as pd

from swing_tracker.backtest.models import BacktestConfig, BacktestTrade
from swing_tracker.core.whatif import VIRTUAL_SHARES, atr_from_daily, find_entry
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

OhlcvMap = dict[str, "pd.DataFrame | None"]


def row_to_bt(row: dict) -> BacktestTrade:
    """Open satirin durum alanlarindan BacktestTrade kur (incremental replay icin)."""
    return BacktestTrade(
        symbol=row["symbol"],
        direction="long",
        entry_price=row["entry_price"],
        entry_date=row["signal_time"],
        shares=VIRTUAL_SHARES,
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        status="open",
        highest_price=row["highest_price"] or row["entry_price"],
        tp1_hit=bool(row["tp1_hit"]),
        remaining_shares=row["remaining_shares"],
    )


def fill_pending(
    repo: Repository,
    ohlcv_1h: OhlcvMap,
    ohlcv_1d: OhlcvMap,
    bt_config: BacktestConfig,
) -> dict:
    """Pending satirlarin girisini doldur: pending -> open / no_data.

    Giris bulunamazsa satir pending kalir (ertesi gun yeniden denenir).
    """
    counts = {"opened": 0, "no_data": 0, "left_pending": 0}
    for row in repo.get_whatif_trades(status="pending"):
        symbol = row["symbol"]
        entry = find_entry(ohlcv_1h.get(symbol), row["signal_time"], row["price_at_signal"])
        if entry is None:
            counts["left_pending"] += 1
            continue
        entry_price, source = entry

        delay_cost = None
        if source == "bar_1h" and row["price_at_signal"]:
            delay_cost = round(
                (entry_price - row["price_at_signal"]) / row["price_at_signal"] * 100, 2
            )

        df_1d = ohlcv_1d.get(symbol)
        atr = atr_from_daily(df_1d, row["signal_time"]) if df_1d is not None else None
        if atr is None:
            repo.update_whatif_trade(row["id"], {
                "status": "no_data",
                "entry_price": entry_price,
                "entry_source": source,
                "delay_cost_pct": delay_cost,
            })
            counts["no_data"] += 1
            continue

        signal_day = pd.Timestamp(row["signal_time"]).date().isoformat()
        repo.update_whatif_trade(row["id"], {
            "status": "open",
            "entry_price": entry_price,
            "entry_source": source,
            "delay_cost_pct": delay_cost,
            "stop_loss": round(entry_price - atr * bt_config.sl_atr_mult, 2),
            "tp1": round(entry_price + atr * bt_config.tp1_atr_mult, 2),
            "tp2": round(entry_price + atr * bt_config.tp2_atr_mult, 2),
            "remaining_shares": VIRTUAL_SHARES,
            "realized_pnl": 0.0,
            "highest_price": entry_price,
            "tp1_hit": 0,
            "last_update": signal_day,  # exit kontrolu ertesi gunden (lookahead onlemi)
        })
        counts["opened"] += 1
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/core/whatif_store.py tests/test_whatif_store.py
git add src/swing_tracker/core/whatif_store.py tests/test_whatif_store.py
git commit -m "feat(whatif): whatif_store — pending doldurma + satir->BacktestTrade donusumu"
```

---

### Task 5: whatif_store — open güncelleme, al-tut yenileme, zaman aşımı, orkestratör

**Files:**
- Modify: `src/swing_tracker/core/whatif_store.py`
- Test: `tests/test_whatif_store.py` (ekleme)

**Interfaces:**
- Consumes: Task 4 fonksiyonları; `check_exits` (backtest/exits.py — dönen listeyi biriktir, `trade.exits`/`total_pnl` OKUMA: SL yolu erken döner ve `trade.exits`'e yazmaz, TP yolları yazar — çift sayım tuzağı).
- Produces:
  - `update_open(repo, ohlcv_1d: OhlcvMap, bt_config) -> dict` — `{"updated": n, "closed": n}`.
  - `refresh_buyhold(repo, ohlcv_1d: OhlcvMap) -> int` — güncellenen satır sayısı.
  - `expire_stale(repo, today: str, max_holding_days: int) -> int` — expire edilen sayı.
  - `run_whatif_update(repo, config) -> dict` — network'lü orkestratör: sembolleri toplar, `get_ohlcv` ile 1h/1d çeker, dört adımı sırayla koşar, özet dict döner.

Kurallar:
- `update_open`: `last_update`'ten SONRAKİ (`index.normalize() > last_update`) günlük bar'lar `check_exits`'e verilir; dönen exit'ler biriktirilir, `realized_pnl += sum(e.pnl)`. Kapanırsa: `status='closed'`, `exit_type/exit_date` son exit'ten, `strategy_pnl_pct = realized_pnl_total / (entry*100) * 100`, `holding_days = exit_date - sinyal günü`. Kapanmazsa: durum alanları (`remaining_shares/highest_price/tp1_hit/realized_pnl`) + `strategy_pnl_pct` (son bar kapanışıyla mark-to-market: `(realized + (close-entry)*remaining) / (entry*100) * 100`) + `last_update` = işlenen son bar günü. Bar yoksa satıra dokunulmaz (idempotency).
- `refresh_buyhold`: `entry_price` dolu TÜM satırlar (closed/expired dahil); sembolün son günlük kapanışı → `buyhold_pnl_pct = (close-entry)/entry*100`, `last_close = close`. DataFrame yoksa atla.
- `expire_stale`: `status IN ('pending','open','no_data')` ve `signal_time` günü `today - max_holding_days`'ten eski satırlar: open için `realized_pnl += (last_close - entry) * remaining` (last_close NULL ise entry kullan — P&L 0 katkı), `strategy_pnl_pct` finalize, `remaining_shares=0`; hepsi için `status='expired'`, `exit_type='expired'`, `exit_date=today`; open'da `holding_days = today - sinyal günü`.
- `run_whatif_update(repo, config)`: `config.whatif.enabled` False ise no-op `{}`. Semboller: `pending`+`open` satırların sembolleri 1h+1d için; buyhold için tüm distinct semboller 1d. `get_ohlcv(sym, interval=..., period="1y"/"3mo", repo=repo, cache_cfg=config.cache)` try/except ile (hata → None, log). `bt_config = dataclasses.replace(parse_config_from_toml(), commission_pct=0.0, commission_fixed=0.0)`. `today = datetime.now(config.timezone).date().isoformat()`. Sıra: fill → update → buyhold → expire. Dönen özet: adım sayaçlarının birleşimi.

- [ ] **Step 1: Write the failing tests**

```python
from swing_tracker.core.whatif_store import expire_stale, refresh_buyhold, update_open


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
        rid = _make_open(repo)
        daily = _df_1d("2026-06-01", _WARMUP + [(100.0, 100.0, 90.0, 92.0)])  # 06-21 sonrasi yok
        # _WARMUP 06-01..06-20; SL bari 06-21... last_update=06-21 oldugundan islenmez!
        # Bu yuzden SL barini 06-22'ye koy:
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
        rid = _make_open(repo)
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
```

Not: `test_sl_closes` içindeki ilk `daily` ataması bilinçli olarak üzerine yazılıyor — kopyalarken sadeleştir, tek atama bırak (yorumda açıklanan neden: `last_update` günü işlenmez).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'update_open'`

- [ ] **Step 3: Implement**

`whatif_store.py`'a ekle:

```python
import dataclasses
from datetime import datetime

from swing_tracker.backtest.exits import check_exits


def update_open(repo: Repository, ohlcv_1d: OhlcvMap, bt_config: BacktestConfig) -> dict:
    """Acik satirlari last_update'ten sonraki gunluk bar'larla ilerlet."""
    counts = {"updated": 0, "closed": 0}
    for row in repo.get_whatif_trades(status="open"):
        df = ohlcv_1d.get(row["symbol"])
        if df is None or df.empty:
            continue
        after = pd.Timestamp(row["last_update"])
        bars = df[df.index.normalize() > after]
        if bars.empty:
            continue

        bt = row_to_bt(row)
        # check_exits'in donen listesini biriktir; bt.exits/total_pnl OKUMA
        # (SL yolu trade.exits'e yazmaz, TP yollari yazar — cift sayim tuzagi).
        new_exits: list = []
        last_day = row["last_update"]
        for ts, bar in bars.iterrows():
            new_exits.extend(check_exits(
                bt, ts.date().isoformat(),
                float(bar["High"]), float(bar["Low"]), float(bar["Close"]),
                bt_config,
            ))
            last_day = ts.date().isoformat()
            if bt.status == "closed":
                break

        realized = row["realized_pnl"] + sum(e.pnl for e in new_exits)
        cost = row["entry_price"] * VIRTUAL_SHARES
        signal_day = pd.Timestamp(row["signal_time"]).normalize()

        if bt.status == "closed":
            last_exit = new_exits[-1]
            repo.update_whatif_trade(row["id"], {
                "status": "closed",
                "remaining_shares": 0,
                "realized_pnl": round(realized, 2),
                "highest_price": bt.highest_price,
                "tp1_hit": int(bt.tp1_hit),
                "exit_type": last_exit.exit_type,
                "exit_date": last_exit.date,
                "strategy_pnl_pct": round(realized / cost * 100, 2),
                "holding_days": float((pd.Timestamp(last_exit.date) - signal_day).days),
                "last_update": last_day,
            })
            counts["closed"] += 1
        else:
            last_close = float(bars.iloc[-1]["Close"])
            unrealized = (last_close - row["entry_price"]) * bt.remaining_shares
            repo.update_whatif_trade(row["id"], {
                "remaining_shares": bt.remaining_shares,
                "realized_pnl": round(realized, 2),
                "highest_price": bt.highest_price,
                "tp1_hit": int(bt.tp1_hit),
                "strategy_pnl_pct": round((realized + unrealized) / cost * 100, 2),
                "last_update": last_day,
            })
            counts["updated"] += 1
    return counts


def refresh_buyhold(repo: Repository, ohlcv_1d: OhlcvMap) -> int:
    """Girisli TUM satirlarin al-tut degerini gunun kapanisiyla yenile.

    Al-tut 'su ana kadar' tanimlidir: strateji kapansa da yasamaya devam eder.
    """
    updated = 0
    for row in repo.get_whatif_trades():
        if not row["entry_price"]:
            continue
        df = ohlcv_1d.get(row["symbol"])
        if df is None or df.empty:
            continue
        close = float(df.iloc[-1]["Close"])
        repo.update_whatif_trade(row["id"], {
            "last_close": close,
            "buyhold_pnl_pct": round((close - row["entry_price"]) / row["entry_price"] * 100, 2),
        })
        updated += 1
    return updated


def expire_stale(repo: Repository, today: str, max_holding_days: int) -> int:
    """max_holding_days'i asan pending/open/no_data satirlari expired yap."""
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=max_holding_days)
    expired = 0
    for status in ("pending", "open", "no_data"):
        for row in repo.get_whatif_trades(status=status):
            signal_day = pd.Timestamp(row["signal_time"]).normalize()
            if signal_day >= cutoff:
                continue
            fields = {"status": "expired", "exit_type": "expired", "exit_date": today}
            if status == "open":
                close = row["last_close"] or row["entry_price"]
                realized = row["realized_pnl"] + (close - row["entry_price"]) * row["remaining_shares"]
                cost = row["entry_price"] * VIRTUAL_SHARES
                fields.update({
                    "remaining_shares": 0,
                    "realized_pnl": round(realized, 2),
                    "strategy_pnl_pct": round(realized / cost * 100, 2),
                    "holding_days": float((pd.Timestamp(today) - signal_day).days),
                })
            repo.update_whatif_trade(row["id"], fields)
            expired += 1
    return expired


def run_whatif_update(repo: Repository, config) -> dict:
    """Gunluk job orkestratoru: OHLCV topla, dort adimi kostur. Sync/blocking."""
    if not config.whatif.enabled:
        return {}

    from swing_tracker.backtest.runner import parse_config_from_toml
    from swing_tracker.core.ohlcv_cache import get_ohlcv

    rows = repo.get_whatif_trades()
    active_symbols = sorted({
        r["symbol"] for r in rows if r["status"] in ("pending", "open")
    })
    all_symbols = sorted({r["symbol"] for r in rows})

    def _fetch(symbol: str, interval: str, period: str):
        try:
            return get_ohlcv(symbol, interval=interval, period=period,
                             repo=repo, cache_cfg=config.cache)
        except Exception:
            logger.warning("whatif_update: %s/%s veri alinamadi", symbol, interval,
                           exc_info=True)
            return None

    ohlcv_1h: OhlcvMap = {s: _fetch(s, "1h", "3mo") for s in active_symbols}
    ohlcv_1d: OhlcvMap = {s: _fetch(s, "1d", "1y") for s in all_symbols}

    bt_config = dataclasses.replace(
        parse_config_from_toml(), commission_pct=0.0, commission_fixed=0.0
    )
    today = datetime.now(config.timezone).date().isoformat()

    summary = {}
    summary.update(fill_pending(repo, ohlcv_1h, ohlcv_1d, bt_config))
    summary.update(update_open(repo, ohlcv_1d, bt_config))
    summary["buyhold_refreshed"] = refresh_buyhold(repo, ohlcv_1d)
    summary["expired"] = expire_stale(repo, today, config.whatif.max_holding_days)
    logger.info("whatif_update tamamlandi: %s", summary)
    return summary
```

Not: `run_whatif_update` içindeki import'lar bilinçli olarak fonksiyon içinde — `whatif_store`'un test edilen adımları network bağımlılığı olmadan import edilebilsin.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + full suite + commit**

```bash
.venv/bin/ruff check src/swing_tracker/core/whatif_store.py tests/test_whatif_store.py
.venv/bin/python -m pytest tests/ -q
git add src/swing_tracker/core/whatif_store.py tests/test_whatif_store.py
git commit -m "feat(whatif): incremental open guncelleme + al-tut yenileme + zaman asimi + orkestrator"
```

---

### Task 6: Scanner hook + scheduler job'u

**Files:**
- Modify: `src/swing_tracker/core/scanner.py` (`_log_scored_signal`, ~satır 517-530)
- Modify: `src/swing_tracker/main.py` (job fonksiyonu + `daily_snapshot` kaydından önce yeni `add_job`)
- Test: `tests/test_whatif_store.py` (ekleme)

**Interfaces:**
- Consumes: `Repository.insert_whatif_trade` (Task 2), `run_whatif_update` (Task 5).
- Produces: sinyal loglanınca `whatif_trades`'e pending satır; `whatif_update` job'u Pzt-Cum 18:40 Istanbul.

- [ ] **Step 1: Write the failing test**

`tests/test_whatif_store.py`'a ekle (import bloğuna `Config, ScannerConfig, CacheConfig, LiquidityConfig` ve `ScoredCandidate, Scanner` ekle — kurulum `tests/test_signal_logging.py` ile aynı pattern):

```python
from swing_tracker.config import CacheConfig, Config, LiquidityConfig, ScannerConfig
from swing_tracker.core.scanner import Scanner, ScoredCandidate


def _make_scanner(repo):
    c = Config()
    c.scanner = ScannerConfig(universe="XTUMY", market_regime_index="XU100")
    c.cache = CacheConfig(enabled=True)
    c.liquidity = LiquidityConfig(enabled=False)
    return Scanner(repo, c, universe_builder=None)


def _make_scored(symbol="THYAO", entry_score=5, price=100.0):
    return ScoredCandidate(
        symbol=symbol, price=price, entry_score=entry_score,
        reasons=["RSI=35"], analysis=None,
    )


class TestScannerWhatIfHook:
    def test_logged_signal_creates_pending_row(self, repo):
        scanner = _make_scanner(repo)

        assert scanner._log_scored_signal(_make_scored()) is True

        rows = repo.get_whatif_trades(status="pending")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "THYAO"
        assert rows[0]["score"] == 5           # entry_score olcegi
        assert rows[0]["price_at_signal"] == 100.0
        assert rows[0]["signal_id"] is not None

    def test_hook_failure_does_not_break_signal_logging(self, repo, monkeypatch):
        scanner = _make_scanner(repo)
        monkeypatch.setattr(
            repo, "insert_whatif_trade",
            lambda fields: (_ for _ in ()).throw(RuntimeError("db hatasi")),
        )
        assert scanner._log_scored_signal(_make_scored()) is True  # sinyal yine loglanir
```

Not: bu dosyanın `repo` fixture'ı Task 2'de tanımlandı; `check_same_thread=False` gerekmez (scanner testte thread kullanmaz).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py::TestScannerWhatIfHook -v`
Expected: FAIL — pending satır yok (`len(rows) == 0`)

- [ ] **Step 3: Implement**

`scanner.py::_log_scored_signal` — `self._repo.log_signal(...)` çağrısının dönüşünü alıp hook ekle:

```python
        signal_id = self._repo.log_signal(
            symbol=scored.symbol,
            signal_type="buy",
            indicator="multi_tf_score",
            strength="strong" if scored.entry_score >= 6 else "medium",
            price_at_signal=scored.price,
            indicator_values={"entry_score": scored.entry_score, "reasons": ", ".join(scored.reasons)},
            score=scored.entry_score * 10,
        )
        # What-if: sinyali kalici sanal islem olarak da baslat (pending; girisi
        # aksamki whatif_update doldurur). Hook hatasi sinyal akisini bozmaz.
        try:
            self._repo.insert_whatif_trade({
                "signal_id": signal_id,
                "symbol": scored.symbol,
                "signal_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "score": scored.entry_score,
                "price_at_signal": scored.price,
            })
        except Exception:
            logger.exception(f"{scored.symbol}: whatif pending kaydi eklenemedi")
```

`datetime`/`timezone` import'larının scanner.py'de mevcut olup olmadığını kontrol et; yoksa ekle. Not: `signal_time` burada uygulama saatiyle yazılır (signals_log'daki `datetime('now')` ile saniye farkı önemsiz).

`main.py` — diğer job fonksiyonlarının yanına:

```python
def job_whatif_update(repo, config):
    """Gunluk what-if guncellemesi: pending doldur, aciklari ilerlet, expire et."""
    from swing_tracker.core.whatif_store import run_whatif_update
    try:
        run_whatif_update(repo, config)
    except Exception:
        logger.exception("whatif_update job hatasi")
```

Scheduler kaydı (daily_snapshot bloğundan önce; `repo` ve `config` main'de job'lara nasıl geçiriliyorsa aynı yolla — mevcut `args=[...]` pattern'ine bak):

```python
    # What-if gunluk guncelleme: Pzt-Cum 18:40 (deep_scan 18:30'dan sonra)
    if config.whatif.enabled:
        _scheduler.add_job(
            job_whatif_update,
            CronTrigger(day_of_week="mon-fri", hour=18, minute=40, timezone=tz),
            args=[repo, config],
            id="whatif_update",
            name="What-If Update",
        )
```

`main.py`'de `repo` job scope'unda yoksa (scanner/portfolio objeleri üzerinden gidiyorsa) mevcut yapıya uy: ör. `scanner._repo` kullanma; `main()` içinde zaten oluşturulan `Repository` örneğini `args`'a ver — dosyada nasıl kurulduğuna bak ve aynı örneği paylaş.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py tests/test_signal_logging.py -v`
Expected: tümü PASS (mevcut scanner testleri kırılmamalı)

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/core/scanner.py src/swing_tracker/main.py tests/test_whatif_store.py
git add src/swing_tracker/core/scanner.py src/swing_tracker/main.py tests/test_whatif_store.py
git commit -m "feat(whatif): scanner pending hook + 18:40 whatif_update job'u"
```

---

### Task 7: Backfill CLI

**Files:**
- Create: `src/swing_tracker/whatif_backfill.py`
- Test: `tests/test_whatif_store.py` (ekleme)

**Interfaces:**
- Consumes: `Repository.get_buy_signals_asc` (Faz 1), `normalize_signal_score` (Task 3), `insert_whatif_trade` (Task 2), `run_whatif_update` (Task 5), `MIN_ENTRY_SCORE` (scanner).
- Produces: `backfill_signals(repo) -> dict` (`{"inserted": n, "skipped_existing": n}`) ve `python -m swing_tracker.whatif_backfill` entry point'i.

- [ ] **Step 1: Write the failing test**

```python
class TestBackfill:
    def test_inserts_pending_rows_idempotent(self, repo):
        from swing_tracker.whatif_backfill import backfill_signals
        _insert_signal(repo, "THYAO", "2026-04-01 07:30:00", score=50, price=100.0)
        _insert_signal(repo, "ASELS", "2026-04-02 07:30:00", score=60, price=50.0)
        _insert_signal(repo, "ZAYIF", "2026-04-03 07:30:00", score=30, price=10.0)  # esik alti

        first = backfill_signals(repo)
        assert first == {"inserted": 2, "skipped_existing": 0}
        rows = repo.get_whatif_trades(status="pending")
        assert {r["symbol"] for r in rows} == {"THYAO", "ASELS"}
        assert rows[0]["score"] == 5  # entry_score olcegi

        second = backfill_signals(repo)
        assert second == {"inserted": 0, "skipped_existing": 2}
        assert len(repo.get_whatif_trades()) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py::TestBackfill -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.whatif_backfill'`

- [ ] **Step 3: Implement**

`src/swing_tracker/whatif_backfill.py`:

```python
"""Tek seferlik backfill: signals_log'daki eski buy sinyallerini whatif_trades'e tasi.

Ayri bir retrospektif simulasyon yolu yoktur: sinyaller 'pending' eklenir,
ardindan gunluk job pipeline'i (run_whatif_update) bir kez kosturulur —
pending doldurma tarihi girisleri uretir, open guncelleme bugune kadar replay
yapar, expiry eski aciklari kapatir. INSERT OR IGNORE sayesinde idempotent.

Kullanim: python -m swing_tracker.whatif_backfill
"""

from __future__ import annotations

import logging

from swing_tracker.config import load_config
from swing_tracker.core.scanner import MIN_ENTRY_SCORE
from swing_tracker.core.whatif import normalize_signal_score
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


def backfill_signals(repo: Repository) -> dict:
    """Esik ustu buy sinyallerini pending satir olarak ekle (idempotent)."""
    counts = {"inserted": 0, "skipped_existing": 0}
    for sig in repo.get_buy_signals_asc(min_score=MIN_ENTRY_SCORE * 10):
        rowid = repo.insert_whatif_trade({
            "signal_id": sig["id"],
            "symbol": sig["symbol"],
            "signal_time": sig["created_at"],
            "score": normalize_signal_score(sig),
            "price_at_signal": sig["price_at_signal"],
        })
        counts["inserted" if rowid is not None else "skipped_existing"] += 1
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)
    try:
        counts = backfill_signals(repo)
        logger.info("Backfill: %s", counts)

        from swing_tracker.core.whatif_store import run_whatif_update
        summary = run_whatif_update(repo, config)
        logger.info("Ilk guncelleme: %s", summary)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/whatif_backfill.py tests/test_whatif_store.py
git add src/swing_tracker/whatif_backfill.py tests/test_whatif_store.py
git commit -m "feat(whatif): idempotent backfill CLI — pending ekle + job pipeline'ini kostur"
```

---

### Task 8: Okuma yolu — router DB'den okur, iki mod

**Files:**
- Modify: `src/swing_tracker/web/routers/whatif.py` (büyük yeniden yazım)
- Test: `tests/test_whatif.py` (`TestBuildWhatIfData` sınıfını değiştir)

**Interfaces:**
- Consumes: `Repository.get_whatif_trades`, `dedup_filter`, `compute_stats`, `WhatIfTrade`, `price_cache.fetch_many`, `localize_signal_timestamps`.
- Produces:
  - `row_to_trade(row: dict) -> WhatIfTrade` (router içinde module-level; template'in tükettiği alanlara map).
  - `build_whatif_data(repo, mode: str = "takip") -> tuple[list[WhatIfTrade], WhatIfStats]` — artık `config` parametresi ve OHLCV çekimi YOK; `simulate_whatif` çağrılmaz.
  - `GET /whatif/results?mode=takip|tum`.

Kurallar:
- `row_to_trade`: DB kolonları → `WhatIfTrade` alanları birebir (`signal_time`, `score`, `entry_price` — pending satırda `entry_price` NULL ise `0.0` ver ve `entry_source='fallback'`; pnl alanları zaten None). `current_price` ← `last_close`.
- Canlı fiyat: yalnızca `status == 'open'` satırların sembolleri `price_cache.fetch_many` ile; dönen fiyatla o trade'lerin `strategy_pnl_pct` (realized + unrealized mark-to-market: `(realized_pnl + (live-entry)*remaining) / (entry*100) * 100`) ve `buyhold_pnl_pct` yeniden hesaplanır, `current_price` güncellenir. Fiyat gelmezse DB değerleri kalır. Bunun için `row_to_trade` yeterli değil — open satırların `realized_pnl`/`remaining_shares`'ına ihtiyaç var; `build_whatif_data` row listesi üzerinde çalışıp trade'leri sonra üretir.
- `mode == "takip"` → `dedup_filter` uygulanır (skipped sayısı `compute_stats`'a gider); `mode == "tum"` → filtre yok, `skipped_dedup=0`.
- Pending trade'ler her iki modda tabloda görünür ama `compute_stats`'a pnl'siz girdikleri için istatistikleri etkilemez.

- [ ] **Step 1: Write the failing test**

`tests/test_whatif.py`'daki mevcut `TestBuildWhatIfData` sınıfını SİL ve yerine koy:

```python
class TestBuildWhatIfDataFromStore:
    def _seed(self, repo):
        # 1 kapali, 1 acik, 1 pending; ayni sembolde acik+sonraki sinyal (dedup testi)
        s1 = _log(repo, "THYAO", score=50, price=100.0, created_at="2026-06-01 07:30:00",
                  indicator_values={"entry_score": 5})
        s2 = _log(repo, "THYAO", score=50, price=100.0, created_at="2026-06-10 07:30:00",
                  indicator_values={"entry_score": 5})
        s3 = _log(repo, "ASELS", score=60, price=50.0, created_at="2026-06-05 07:30:00",
                  indicator_values={"entry_score": 6})
        repo.insert_whatif_trade({
            "signal_id": s1, "symbol": "THYAO", "signal_time": "2026-06-01 07:30:00",
            "score": 5, "price_at_signal": 100.0, "status": "open",
        })
        repo.update_whatif_trade(1, {
            "entry_price": 100.0, "entry_source": "bar_1h", "stop_loss": 96.0,
            "tp1": 103.0, "tp2": 106.0, "remaining_shares": 100, "realized_pnl": 0.0,
            "highest_price": 100.0, "strategy_pnl_pct": 1.0, "buyhold_pnl_pct": 1.0,
            "last_close": 101.0, "last_update": "2026-06-20",
        })
        repo.insert_whatif_trade({
            "signal_id": s2, "symbol": "THYAO", "signal_time": "2026-06-10 07:30:00",
            "score": 5, "price_at_signal": 100.0, "status": "open",
        })
        repo.update_whatif_trade(2, {
            "entry_price": 100.0, "entry_source": "bar_1h", "stop_loss": 96.0,
            "tp1": 103.0, "tp2": 106.0, "remaining_shares": 100, "realized_pnl": 0.0,
            "highest_price": 100.0, "strategy_pnl_pct": 1.0, "last_update": "2026-06-20",
        })
        repo.insert_whatif_trade({
            "signal_id": s3, "symbol": "ASELS", "signal_time": "2026-06-05 07:30:00",
            "score": 6, "price_at_signal": 50.0,
        })

    def test_reads_store_no_simulation(self, repo, monkeypatch):
        from swing_tracker.web.routers import whatif as whatif_router
        self._seed(repo)
        monkeypatch.setattr(
            whatif_router.price_cache, "fetch_many", lambda syms: {"THYAO": 110.0}
        )

        trades, stats = whatif_router.build_whatif_data(repo, mode="takip")

        # dedup: THYAO'nun 2. sinyali atlanir; pending ASELS gorunur
        assert len(trades) == 2
        assert stats.skipped_dedup == 1
        thyao = next(t for t in trades if t.symbol == "THYAO")
        # canli fiyatla mark-to-market: (0 + (110-100)*100)/10000*100 = 10.0
        assert thyao.strategy_pnl_pct == pytest.approx(10.0)
        assert thyao.buyhold_pnl_pct == pytest.approx(10.0)
        pending = next(t for t in trades if t.symbol == "ASELS")
        assert pending.status == "pending"
        assert pending.strategy_pnl_pct is None

    def test_mode_tum_no_dedup(self, repo, monkeypatch):
        from swing_tracker.web.routers import whatif as whatif_router
        self._seed(repo)
        monkeypatch.setattr(whatif_router.price_cache, "fetch_many", lambda syms: {})

        trades, stats = whatif_router.build_whatif_data(repo, mode="tum")

        assert len(trades) == 3
        assert stats.skipped_dedup == 0
        # fiyat gelmedi: DB'deki degerler korunur
        thyao1 = trades[0]
        assert thyao1.strategy_pnl_pct == pytest.approx(1.0)
```

`_log` helper'ının `indicator_values` parametresi Faz 1 skor fix'inde eklendi — dosyada mevcut haline uy.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_whatif.py -v`
Expected: FAIL — `build_whatif_data() got an unexpected keyword argument 'mode'` (veya eski imza hatası)

- [ ] **Step 3: Implement**

`web/routers/whatif.py`'yi yeniden yaz:

```python
"""What-if router — kalici whatif_trades tablosundan okur; simulasyon yapmaz.

Hesaplama sinyal aninda (scanner hook) ve gunluk 18:40 job'unda yasar
(core/whatif_store.py). Burada yalnizca: DB okumasi, acik pozisyonlara canli
fiyat, dedup gorunum filtresi ve istatistik toplama.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from swing_tracker.core.whatif import (
    VIRTUAL_SHARES,
    WhatIfStats,
    WhatIfTrade,
    compute_stats,
    dedup_filter,
)
from swing_tracker.web.dependencies import get_config, get_repo, templates
from swing_tracker.web.helpers import localize_signal_timestamps
from swing_tracker.web.price_cache import price_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatif")


def row_to_trade(row: dict) -> WhatIfTrade:
    """whatif_trades satirini template/istatistik modeli WhatIfTrade'e cevir."""
    return WhatIfTrade(
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        signal_time=row["signal_time"],
        score=row["score"],
        price_at_signal=row["price_at_signal"],
        entry_price=row["entry_price"] or 0.0,
        entry_source=row["entry_source"] or "fallback",
        stop_loss=row["stop_loss"] or 0.0,
        tp1=row["tp1"] or 0.0,
        tp2=row["tp2"] or 0.0,
        status=row["status"],
        strategy_pnl_pct=row["strategy_pnl_pct"],
        exit_type=row["exit_type"],
        exit_date=row["exit_date"],
        holding_days=row["holding_days"],
        buyhold_pnl_pct=row["buyhold_pnl_pct"],
        current_price=row["last_close"],
        delay_cost_pct=row["delay_cost_pct"],
    )


def build_whatif_data(repo, mode: str = "takip") -> tuple[list[WhatIfTrade], WhatIfStats]:
    """DB'den oku; yalnizca acik pozisyonlara canli fiyat uygula. Simulasyon yok."""
    rows = repo.get_whatif_trades()

    open_symbols = sorted({r["symbol"] for r in rows if r["status"] == "open"})
    live = price_cache.fetch_many(open_symbols) if open_symbols else {}

    trades: list[WhatIfTrade] = []
    for row in rows:
        trade = row_to_trade(row)
        price = live.get(row["symbol"])
        if row["status"] == "open" and price is not None and row["entry_price"]:
            cost = row["entry_price"] * VIRTUAL_SHARES
            unrealized = (price - row["entry_price"]) * (row["remaining_shares"] or 0)
            trade.strategy_pnl_pct = round(
                ((row["realized_pnl"] or 0.0) + unrealized) / cost * 100, 2
            )
            trade.buyhold_pnl_pct = round(
                (price - row["entry_price"]) / row["entry_price"] * 100, 2
            )
            trade.current_price = price
        trades.append(trade)

    if mode == "tum":
        stats = compute_stats(trades, skipped_dedup=0)
        return trades, stats

    kept, skipped = dedup_filter(trades)
    stats = compute_stats(kept, skipped_dedup=skipped)
    return kept, stats


@router.get("", response_class=HTMLResponse)
async def whatif_page(request: Request, mode: str = Query("takip")):
    """Skeleton sayfa — fragment htmx ile yuklenir."""
    return templates.TemplateResponse(request, "whatif.html", context={"mode": mode})


@router.get("/results", response_class=HTMLResponse)
async def whatif_results(request: Request, mode: str = Query("takip")):
    repo = get_repo()
    config = get_config()
    if mode not in ("takip", "tum"):
        mode = "takip"

    try:
        trades, stats = await asyncio.to_thread(build_whatif_data, repo, mode)
    except Exception:
        logger.exception("whatif: sonuc olusturulamadi")
        return HTMLResponse(
            '<div class="bg-surface-raised border border-border rounded-xl p-8 '
            'text-center text-txt-muted">Sonuclar yuklenemedi. '
            'Sayfayi yenileyip tekrar deneyin.</div>'
        )

    display = []
    for t in trades:
        d = t.__dict__.copy()
        d["signal_time_local"] = localize_signal_timestamps(
            [{"created_at": t.signal_time}], config.timezone
        )[0]["created_at"]
        display.append(d)
    display.sort(key=lambda d: d["signal_time"], reverse=True)

    return templates.TemplateResponse(
        request,
        "fragments/whatif_results.html",
        context={"trades": display, "stats": stats, "mode": mode},
    )
```

Silinenler: `build_whatif_data`'nın eski gövdesi, `get_ohlcv`/`parse_config_from_toml`/`MIN_ENTRY_SCORE`/`simulate_whatif`/`dataclasses` import'ları, `_DAILY_PERIOD`/`_HOURLY_PERIOD` sabitleri. `normalize_signal_score` import'u da artik router'da gereksizse kaldır (backfill core'dan alıyor).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_whatif.py tests/test_whatif_store.py -v`
Expected: tümü PASS

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check src/swing_tracker/web/routers/whatif.py tests/test_whatif.py
git add src/swing_tracker/web/routers/whatif.py tests/test_whatif.py
git commit -m "feat(whatif): okuma yolu DB'den — mode parametresi + acik pozisyona canli fiyat"
```

---

### Task 9: Template'ler — mod toggle + yeni rozetler

**Files:**
- Modify: `src/swing_tracker/web/templates/whatif.html`
- Modify: `src/swing_tracker/web/templates/fragments/whatif_results.html`
- Modify: `src/swing_tracker/web/dependencies.py` (STATUS_TR'ye iki anahtar)
- Test: manuel doğrulama (Step 3)

**Interfaces:**
- Consumes: Task 8 context'i — `trades` (dict listesi, `signal_time_local` dahil), `stats`, `mode` (`"takip"`/`"tum"`).

- [ ] **Step 1: Edits**

`dependencies.py` STATUS_TR'ye ekle:

```python
    "pending": "BEKLEMEDE",
    "expired": "SURE DOLDU",
```

`whatif.html` — başlık bloğunun altına, skeleton `div`inin üstüne toggle; `hx-get`'i mode'lu yap:

```html
<div class="flex gap-2 mb-4">
    <a href="/whatif?mode=takip" class="px-3 py-1.5 rounded-lg text-sm font-medium
        {{ 'bg-accent/10 text-accent' if mode == 'takip' else 'text-txt-muted hover:text-txt-primary' }}">
        Takip edilebilir
    </a>
    <a href="/whatif?mode=tum" class="px-3 py-1.5 rounded-lg text-sm font-medium
        {{ 'bg-accent/10 text-accent' if mode == 'tum' else 'text-txt-muted hover:text-txt-primary' }}">
        Tum sinyaller
    </a>
</div>
```

ve skeleton div'inde: `hx-get="/whatif/results?mode={{ mode }}"`.

`fragments/whatif_results.html` — işlem tablosundaki DURUM hücresine iki dal ekle (mevcut `no_data`/`open`/`closed` zincirine):

```html
                        {% if t.status == 'pending' %}
                        <span class="px-2 py-0.5 rounded text-xs bg-accent/10 text-txt-muted">BEKLEMEDE</span>
                        {% elif t.status == 'no_data' %}
                        ... (mevcut dallar aynen)
```

`expired` satırlar kapalı dalına düşer ve `STATUS_TR.get(t.exit_type, ...)` zaten "SURE DOLDU" gösterir — ek dal gerekmez. Strateji/Al-Tut hücrelerindeki `is none → —` mantığı pending için zaten çalışır. Alt dipnottaki dedup cümlesi yalnızca `mode == 'takip'` iken gösterilsin:

```html
    {% if mode == 'takip' and stats.skipped_dedup %}{{ stats.skipped_dedup }} sinyal acik pozisyon nedeniyle atlandi.{% endif %}
```

Giriş hücresinde pending için `—` göster: `{% if t.entry_price %}{{ "%.2f"|format(t.entry_price) }}...{% else %}—{% endif %}`.

- [ ] **Step 2: Run full suite (template'ler Python testini etkilemez, regresyon kontrolü)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: tümü PASS

- [ ] **Step 3: Manuel doğrulama (TestClient, commit'lenmeyen scratch script)**

Task 6'daki (Faz 1) TestClient yaklaşımı: in-memory repo + `create_all_tables` + `init_state`, `swing_tracker.web.auth.WEB_PASSWORD`'u `""` yap, Task 8 testindeki `_seed` benzeri satırlar ekle, `price_cache.fetch_many`'yi monkeypatch'le. Doğrula:
1. `GET /whatif` → 200, toggle iki link içeriyor, skeleton `mode`'u taşıyor.
2. `GET /whatif/results?mode=takip` → 200; dedup dipnotu var; BEKLEMEDE rozeti render oluyor.
3. `GET /whatif/results?mode=tum` → 200; üç satır; dedup dipnotu yok.
4. Bir satırı `expired` yapıp SURE DOLDU rozetini doğrula.

- [ ] **Step 4: Commit**

```bash
git add src/swing_tracker/web/templates/whatif.html \
        src/swing_tracker/web/templates/fragments/whatif_results.html \
        src/swing_tracker/web/dependencies.py
git commit -m "feat(whatif): mod toggle (takip/tum) + BEKLEMEDE ve SURE DOLDU rozetleri"
```

---

### Task 10: Son doğrulama

**Files:** yok.

- [ ] **Step 1: Suite + lint**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src/swing_tracker/core/whatif.py src/swing_tracker/core/whatif_store.py \
    src/swing_tracker/web/routers/whatif.py src/swing_tracker/whatif_backfill.py \
    src/swing_tracker/config.py src/swing_tracker/db/schema.py tests/test_whatif.py tests/test_whatif_store.py
```

Expected: tümü PASS, lint temiz (pre-existing hatalar başka dosyalarda kalabilir).

- [ ] **Step 2: Gerçek veriyle smoke**

Lokal `data/swing_tracker.db` üzerinde (önce yedek al: `cp data/swing_tracker.db /tmp/st-backup.db`):

```bash
.venv/bin/python -m swing_tracker.whatif_backfill
```

Beklenen: log'da `Backfill: {'inserted': ~47, ...}` + `Ilk guncelleme: {...}`; ikinci koşuda `inserted: 0`. Sonra web app'i başlat, `/whatif` aç: sayfa ANINDA yüklenmeli (OHLCV çekimi yok), iki mod arasında geçiş çalışmalı, eski sinyaller `expired`/`closed` görünmeli. Sorun çıkarsa yedeği geri koy.

- [ ] **Step 3: Branch'i tamamla**

`superpowers:finishing-a-development-branch` → PR → main. Deploy sonrası homelab'da bir kez `docker compose exec` ile backfill çalıştırmayı unutma (PR açıklamasına not düş).
