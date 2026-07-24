# Allocation & Rebalance Modülü Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swing-tracker'a, USD bazlı core/satellite ETF portföyünün gerçek ağırlıklarını hedefe göre izleyen, drift uyarısı + DCA (alım-only) ve tam-rebalance (sat+al) önerisi + ETA üreten ayrı bir `allocation` modülü eklemek.

**Architecture:** Mevcut katmanlı yapı: `config.toml`→`config.py` dataclass, `db/schema.py`+`repository.py` (raw SQL, dict dönüş), `core/` saf fonksiyonlar, `web/routers/` + Jinja template, `bot/telegram.py` bildirim, `main.py` APScheduler job. ETF fiyatları `borsapy._providers.tradingview.get_quote(symbol, exchange)` ile, USDTRY `bp.FX("USD")` ile çekilir. Web router ve scheduler aynı orkestrasyon fonksiyonunu (`build_report`) kullanır.

**Tech Stack:** Python 3.11, borsapy>=0.8.3 (yalnızca veri), SQLite (WAL, `sqlite3.Row`), FastAPI + Jinja2, APScheduler 3.x (`BackgroundScheduler`+`CronTrigger`), python-telegram-bot 21+, Pytest, Ruff.

## Global Constraints

- Python 3.11+; cross-platform (`pathlib.Path`, `zoneinfo`). Ruff line-length=100, target py311.
- borsapy yalnızca **veri kaynağı**; hesap/algoritma bu projede.
- Tüm `core/` fonksiyonları **pure** (I/O yok, network yok), **dataclass** tabanlı, test edilebilir.
- DB: ORM yok, raw SQL + `sqlite3.Row`; repository method'ları **`dict` döndürür**; upsert için `ON CONFLICT`.
- Türkçe UI metinleri; Telegram `ParseMode.HTML`, async method + `send_message()`.
- Scheduler timezone her zaman `Europe/Istanbul` (`config.timezone`).
- Hardcoded secret yok.
- Testler: `tests/test_<modul>.py`; DB testleri in-memory SQLite (`:memory:`); borsapy çağrıları mock.
- Nakit/para piyasası ağırlık hesabının **dışında** (bu modül nakit tutmaz).
- Modül **yalnızca öneri** üretir — otomatik emir yok.
- Hedef ağırlıklar: VOO 28 (AMEX, core), VXUS 12 (NASDAQ, core), QTUM 20 (NASDAQ, satellite), FIW 20 (AMEX, satellite), XLE 20 (AMEX, satellite). Drift eşiği ±5 puan; çeyreklik hatırlatma 91 gün; `fractional=true`.

---

### Task 1: Config — `[allocation]` bölümü

**Files:**
- Modify: `config.toml` (yeni `[allocation]` bölümü sona ekle)
- Modify: `src/swing_tracker/config.py` (dataclass'lar + parse + `Config` alanı)
- Test: `tests/test_config_allocation.py`

**Interfaces:**
- Produces:
  - `AllocationTarget(symbol: str, weight: float, exchange: str, group: str, note: str)`
  - `AllocationConfig(enabled: bool, base_currency: str, monthly_contribution_usd: float, drift_threshold_pct: float, review_interval_days: int, fractional: bool, targets: dict[str, AllocationTarget])`
  - `Config.allocation: AllocationConfig`

- [ ] **Step 1: config.toml'a bölüm ekle**

`config.toml` sonuna ekle:

```toml
[allocation]
enabled = true
base_currency = "USD"
monthly_contribution_usd = 500
drift_threshold_pct = 5.0
review_interval_days = 91
fractional = true

[allocation.targets.VOO]
weight = 28
exchange = "AMEX"
group = "core"
note = "S&P 500"

[allocation.targets.VXUS]
weight = 12
exchange = "NASDAQ"
group = "core"
note = "Ex-US"

[allocation.targets.QTUM]
weight = 20
exchange = "NASDAQ"
group = "satellite"
note = "Kuantum/AI"

[allocation.targets.FIW]
weight = 20
exchange = "AMEX"
group = "satellite"
note = "Su"

[allocation.targets.XLE]
weight = 20
exchange = "AMEX"
group = "satellite"
note = "Enerji"
```

- [ ] **Step 2: Failing test yaz**

`tests/test_config_allocation.py`:

```python
from swing_tracker.config import load_config


def test_allocation_config_parsed(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[allocation]
enabled = true
monthly_contribution_usd = 750
drift_threshold_pct = 5.0
review_interval_days = 91
fractional = true

[allocation.targets.VOO]
weight = 28
exchange = "AMEX"
group = "core"
note = "S&P 500"

[allocation.targets.QTUM]
weight = 20
exchange = "NASDAQ"
group = "satellite"
note = "Kuantum/AI"
""",
        encoding="utf-8",
    )
    config = load_config(cfg_file)
    assert config.allocation.enabled is True
    assert config.allocation.monthly_contribution_usd == 750
    assert config.allocation.fractional is True
    assert set(config.allocation.targets) == {"VOO", "QTUM"}
    voo = config.allocation.targets["VOO"]
    assert voo.weight == 28
    assert voo.exchange == "AMEX"
    assert voo.group == "core"


def test_allocation_config_defaults_when_missing(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[general]\n", encoding="utf-8")
    config = load_config(cfg_file)
    assert config.allocation.enabled is True
    assert config.allocation.targets == {}
```

- [ ] **Step 3: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_config_allocation.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'allocation'`

- [ ] **Step 4: config.py'ye dataclass'lar ekle**

`src/swing_tracker/config.py`, `WhatIfConfig` dataclass'ından sonra ekle:

```python
@dataclass
class AllocationTarget:
    symbol: str
    weight: float
    exchange: str
    group: str  # "core" | "satellite"
    note: str = ""


@dataclass
class AllocationConfig:
    enabled: bool = True
    base_currency: str = "USD"
    monthly_contribution_usd: float = 500.0
    drift_threshold_pct: float = 5.0
    review_interval_days: int = 91
    fractional: bool = True
    targets: dict[str, AllocationTarget] = field(default_factory=dict)
```

- [ ] **Step 5: Config dataclass'ına alan ekle**

`src/swing_tracker/config.py`, `Config` içinde `whatif: WhatIfConfig = ...` satırından sonra:

```python
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
```

- [ ] **Step 6: load_config'e parse bloğu ekle**

`src/swing_tracker/config.py`, `load_config` içinde "What-if" bloğundan sonra, `# Strategies` bloğundan önce:

```python
    # Allocation
    al = raw.get("allocation", {})
    targets: dict[str, AllocationTarget] = {}
    for sym, tv in al.get("targets", {}).items():
        symu = sym.upper()
        targets[symu] = AllocationTarget(
            symbol=symu,
            weight=float(tv.get("weight", 0)),
            exchange=tv.get("exchange", ""),
            group=tv.get("group", ""),
            note=tv.get("note", ""),
        )
    total_w = sum(t.weight for t in targets.values())
    if targets and abs(total_w - 100.0) > 0.01:
        import logging
        logging.getLogger(__name__).warning(
            "Allocation hedef agirliklari toplami %.1f (100 degil)", total_w
        )
    config.allocation = AllocationConfig(
        enabled=al.get("enabled", True),
        base_currency=al.get("base_currency", "USD"),
        monthly_contribution_usd=float(al.get("monthly_contribution_usd", 500)),
        drift_threshold_pct=float(al.get("drift_threshold_pct", 5.0)),
        review_interval_days=int(al.get("review_interval_days", 91)),
        fractional=al.get("fractional", True),
        targets=targets,
    )
```

- [ ] **Step 7: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_config_allocation.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add config.toml src/swing_tracker/config.py tests/test_config_allocation.py
git commit -m "feat(allocation): config [allocation] bolumu + dataclass'lar"
```

---

### Task 2: DB şema + repository CRUD

**Files:**
- Modify: `src/swing_tracker/db/schema.py` (`TABLES` listesine 3 tablo)
- Modify: `src/swing_tracker/db/repository.py` (allocation method'ları)
- Test: `tests/test_repository_allocation.py`

**Interfaces:**
- Produces (Repository method'ları):
  - `upsert_allocation_holding(symbol: str, exchange: str, shares: float, cost_per_share: float | None = None, notes: str | None = None) -> int`
  - `get_allocation_holdings() -> list[dict]`
  - `get_allocation_holding(symbol: str) -> dict | None`
  - `delete_allocation_holding(symbol: str) -> None`
  - `log_allocation_review(note: str | None = None) -> int`
  - `get_last_allocation_review() -> dict | None`
  - `get_allocation_setting(key: str, default: str | None = None) -> str | None`
  - `set_allocation_setting(key: str, value: str) -> None`

- [ ] **Step 1: schema.py'ye tabloları ekle**

`src/swing_tracker/db/schema.py`, `TABLES` listesinde `whatif_trades` DDL'inden sonra (kapanış `]`'dan önce) üç string ekle:

```python
    """
    CREATE TABLE IF NOT EXISTS allocation_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL UNIQUE,
        exchange TEXT NOT NULL,
        shares REAL NOT NULL DEFAULT 0,
        cost_per_share REAL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allocation_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reviewed_at TEXT DEFAULT (datetime('now')),
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allocation_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
```

- [ ] **Step 2: Failing test yaz**

`tests/test_repository_allocation.py`:

```python
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


def test_upsert_and_get_holding(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0, cost_per_share=650.0, notes="core")
    got = repo.get_allocation_holding("VOO")
    assert got["symbol"] == "VOO"
    assert got["exchange"] == "AMEX"
    assert got["shares"] == 4.0
    assert got["cost_per_share"] == 650.0


def test_upsert_updates_existing(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    repo.upsert_allocation_holding("VOO", "AMEX", 6.5)
    rows = repo.get_allocation_holdings()
    assert len(rows) == 1
    assert rows[0]["shares"] == 6.5


def test_delete_holding(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    repo.delete_allocation_holding("VOO")
    assert repo.get_allocation_holdings() == []


def test_review_log(repo):
    assert repo.get_last_allocation_review() is None
    repo.log_allocation_review("ceyreklik")
    last = repo.get_last_allocation_review()
    assert last["note"] == "ceyreklik"
    assert last["reviewed_at"] is not None


def test_settings_upsert(repo):
    assert repo.get_allocation_setting("last_contribution_usd") is None
    assert repo.get_allocation_setting("last_contribution_usd", "0") == "0"
    repo.set_allocation_setting("last_contribution_usd", "750")
    assert repo.get_allocation_setting("last_contribution_usd") == "750"
    repo.set_allocation_setting("last_contribution_usd", "800")
    assert repo.get_allocation_setting("last_contribution_usd") == "800"
```

- [ ] **Step 3: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_repository_allocation.py -v`
Expected: FAIL — `AttributeError: 'Repository' object has no attribute 'upsert_allocation_holding'`

- [ ] **Step 4: repository.py'ye method'ları ekle**

`src/swing_tracker/db/repository.py`, sınıfın sonuna ekle:

```python
    # ── Allocation ──

    def upsert_allocation_holding(
        self,
        symbol: str,
        exchange: str,
        shares: float,
        cost_per_share: float | None = None,
        notes: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO allocation_holdings
               (symbol, exchange, shares, cost_per_share, notes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 exchange = excluded.exchange,
                 shares = excluded.shares,
                 cost_per_share = excluded.cost_per_share,
                 notes = excluded.notes,
                 updated_at = datetime('now')""",
            (symbol, exchange, shares, cost_per_share, notes),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_allocation_holdings(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM allocation_holdings ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_allocation_holding(self, symbol: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM allocation_holdings WHERE symbol = ?", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def delete_allocation_holding(self, symbol: str) -> None:
        self._conn.execute(
            "DELETE FROM allocation_holdings WHERE symbol = ?", (symbol,)
        )
        self._conn.commit()

    def log_allocation_review(self, note: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO allocation_reviews (note) VALUES (?)", (note,)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_last_allocation_review(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM allocation_reviews ORDER BY reviewed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_allocation_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM allocation_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_allocation_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO allocation_settings (key, value)
               VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value,
                 updated_at = datetime('now')""",
            (key, value),
        )
        self._conn.commit()
```

- [ ] **Step 5: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_repository_allocation.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add src/swing_tracker/db/schema.py src/swing_tracker/db/repository.py tests/test_repository_allocation.py
git commit -m "feat(allocation): allocation_holdings/reviews/settings tablolari + CRUD"
```

---

### Task 3: ETF fiyat katmanı — `core/etf_prices.py`

**Files:**
- Create: `src/swing_tracker/core/etf_prices.py`
- Test: `tests/test_etf_prices.py`

**Interfaces:**
- Produces:
  - `class EtfPriceCache` — TTL+LRU cache, `price_cache.py` deseninde.
  - `fetch_etf_prices(symbol_exchange: dict[str, str], max_workers: int = 5) -> dict[str, float]` — {symbol: usd_price}; çekilemeyen sembol sonuç dict'inde yer almaz.
  - `fetch_usdtry() -> float | None`
  - Modül singleton: `etf_price_cache = EtfPriceCache()`
- Consumes: `borsapy._providers.tradingview.get_tradingview_provider().get_quote(symbol, exchange)` → dict (`last` alanı USD fiyat); `borsapy.FX("USD").price` → USDTRY.

- [ ] **Step 1: Failing test yaz** (borsapy mock'lanır — network yok)

`tests/test_etf_prices.py`:

```python
from swing_tracker.core import etf_prices


def test_fetch_etf_prices_maps_symbol_to_price(monkeypatch):
    calls = []

    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            calls.append((symbol, exchange))
            return {"VOO": {"last": 682.24}, "VXUS": {"last": 83.9}}[symbol]

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    out = cache.fetch_many({"VOO": "AMEX", "VXUS": "NASDAQ"})
    assert out == {"VOO": 682.24, "VXUS": 83.9}
    assert ("VOO", "AMEX") in calls and ("VXUS", "NASDAQ") in calls


def test_fetch_skips_failing_symbol(monkeypatch):
    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            if symbol == "BAD":
                raise RuntimeError("no data")
            return {"last": 100.0}

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    out = cache.fetch_many({"VOO": "AMEX", "BAD": "AMEX"})
    assert out == {"VOO": 100.0}


def test_cache_hit_within_ttl(monkeypatch):
    n = {"count": 0}

    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            n["count"] += 1
            return {"last": 50.0}

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    cache.fetch_many({"XLE": "AMEX"})
    cache.fetch_many({"XLE": "AMEX"})
    assert n["count"] == 1  # ikinci cagri cache'ten
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_etf_prices.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.core.etf_prices'`

- [ ] **Step 3: etf_prices.py yaz**

`src/swing_tracker/core/etf_prices.py`:

```python
"""US ETF fiyat katmani — TradingView (exchange destekli) + USDTRY.

Mevcut web/price_cache.py deseni (TTL + LRU + paralel fetch) ile ayni,
fark: BIST'e sabit bp.Ticker yerine exchange parametreli get_quote kullanir.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import borsapy as bp
from borsapy._providers.tradingview import get_tradingview_provider

logger = logging.getLogger(__name__)

TTL = 300  # saniye — ETF fiyatlari icin 5 dk
USDTRY_TTL = 300
MAX_SIZE = 200
MAX_WORKERS = 5


class EtfPriceCache:
    def __init__(self, max_size: int = MAX_SIZE):
        self._cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._usdtry: tuple[float, float] | None = None

    def _get(self, symbol: str) -> float | None:
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and (time.monotonic() - entry[1]) < TTL:
                self._cache.move_to_end(symbol)
                return entry[0]
        return None

    def _set(self, symbol: str, price: float) -> None:
        with self._lock:
            self._cache[symbol] = (price, time.monotonic())
            self._cache.move_to_end(symbol)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def fetch_one(self, symbol: str, exchange: str) -> float | None:
        cached = self._get(symbol)
        if cached is not None:
            return cached
        try:
            quote = get_tradingview_provider().get_quote(symbol, exchange=exchange)
            price = float(quote.get("last") or 0)
            if price <= 0:
                logger.warning("ETF fiyati alinamadi: %s:%s", exchange, symbol)
                return None
            self._set(symbol, price)
            return price
        except Exception:
            logger.warning("ETF fiyat cekme hatasi: %s:%s", exchange, symbol, exc_info=True)
            return None

    def fetch_many(
        self, symbol_exchange: dict[str, str], max_workers: int = MAX_WORKERS
    ) -> dict[str, float]:
        if not symbol_exchange:
            return {}
        items = list(symbol_exchange.items())
        workers = min(max_workers, len(items))
        result: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            prices = pool.map(lambda p: self.fetch_one(p[0], p[1]), items)
            for (symbol, _exchange), price in zip(items, prices):
                if price is not None:
                    result[symbol] = price
        return result

    def fetch_usdtry(self) -> float | None:
        if self._usdtry and (time.monotonic() - self._usdtry[1]) < USDTRY_TTL:
            return self._usdtry[0]
        try:
            rate = float(bp.FX("USD").price)
            if rate <= 0:
                return None
            self._usdtry = (rate, time.monotonic())
            return rate
        except Exception:
            logger.warning("USDTRY cekme hatasi", exc_info=True)
            return None


etf_price_cache = EtfPriceCache()
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_etf_prices.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/etf_prices.py tests/test_etf_prices.py
git commit -m "feat(allocation): US ETF fiyat katmani (TradingView exchange + USDTRY)"
```

---

### Task 4: `core/allocation.py` — dataclass'lar + `compute_weights`

**Files:**
- Create: `src/swing_tracker/core/allocation.py`
- Test: `tests/test_allocation.py`

**Interfaces:**
- Consumes: `swing_tracker.config.AllocationTarget`.
- Produces:
  - `AllocationLeg(symbol, exchange, group, target_pct, shares, price_usd, value_usd, weight_pct, drift_pct, price_stale)`
  - `AllocationReport(legs: list[AllocationLeg], total_value_usd, core_weight_pct, satellite_weight_pct, usdtry: float | None)`
  - `compute_weights(holdings: list[dict], prices: dict[str, float], targets: dict[str, AllocationTarget], usdtry: float | None = None) -> AllocationReport`

- [ ] **Step 1: Failing test yaz**

`tests/test_allocation.py`:

```python
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
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.core.allocation'`

- [ ] **Step 3: allocation.py yaz (dataclass'lar + compute_weights)**

`src/swing_tracker/core/allocation.py`:

```python
"""Allocation & rebalance — saf hesap fonksiyonlari (I/O yok, network yok)."""

from __future__ import annotations

from dataclasses import dataclass

from swing_tracker.config import AllocationTarget


@dataclass
class AllocationLeg:
    symbol: str
    exchange: str
    group: str
    target_pct: float
    shares: float
    price_usd: float | None
    value_usd: float
    weight_pct: float
    drift_pct: float
    price_stale: bool


@dataclass
class AllocationReport:
    legs: list[AllocationLeg]
    total_value_usd: float
    core_weight_pct: float
    satellite_weight_pct: float
    usdtry: float | None = None


def compute_weights(
    holdings: list[dict],
    prices: dict[str, float],
    targets: dict[str, AllocationTarget],
    usdtry: float | None = None,
) -> AllocationReport:
    shares_by_sym = {h["symbol"]: (h.get("shares") or 0.0) for h in holdings}

    raw: list[tuple[AllocationTarget, float, float | None, float, bool]] = []
    total = 0.0
    for sym, tgt in targets.items():
        shares = shares_by_sym.get(sym, 0.0)
        price = prices.get(sym)
        stale = price is None
        value = 0.0 if stale else shares * price
        if not stale:
            total += value
        raw.append((tgt, shares, price, value, stale))

    legs: list[AllocationLeg] = []
    core_w = 0.0
    sat_w = 0.0
    for tgt, shares, price, value, stale in raw:
        weight = (value / total * 100.0) if (total > 0 and not stale) else 0.0
        drift = weight - tgt.weight
        legs.append(
            AllocationLeg(
                symbol=tgt.symbol,
                exchange=tgt.exchange,
                group=tgt.group,
                target_pct=tgt.weight,
                shares=shares,
                price_usd=price,
                value_usd=value,
                weight_pct=weight,
                drift_pct=drift,
                price_stale=stale,
            )
        )
        if not stale:
            if tgt.group == "core":
                core_w += weight
            elif tgt.group == "satellite":
                sat_w += weight

    return AllocationReport(
        legs=legs,
        total_value_usd=total,
        core_weight_pct=core_w,
        satellite_weight_pct=sat_w,
        usdtry=usdtry,
    )
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation.py tests/test_allocation.py
git commit -m "feat(allocation): compute_weights + dataclass'lar"
```

---

### Task 5: `core/allocation.py` — `check_rebalance`

**Files:**
- Modify: `src/swing_tracker/core/allocation.py`
- Test: `tests/test_allocation.py`

**Interfaces:**
- Consumes: `AllocationReport`, `AllocationLeg` (Task 4).
- Produces:
  - `RebalanceAlert(drifted_legs: list[AllocationLeg], review_due: bool, next_review_date: date | None, last_review_date: date | None)`
  - `check_rebalance(report: AllocationReport, threshold_pct: float, last_review: datetime | None, interval_days: int, now: datetime) -> RebalanceAlert`

- [ ] **Step 1: Failing test ekle**

`tests/test_allocation.py` sonuna:

```python
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
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -k rebalance -v`
Expected: FAIL — `ImportError: cannot import name 'check_rebalance'`

- [ ] **Step 3: check_rebalance ekle**

`src/swing_tracker/core/allocation.py` üstüne import ve dataclass + fonksiyon ekle. `from __future__` altındaki import satırına ekle:

```python
from datetime import date, datetime, timedelta
```

Dosya sonuna:

```python
@dataclass
class RebalanceAlert:
    drifted_legs: list[AllocationLeg]
    review_due: bool
    next_review_date: date | None
    last_review_date: date | None


def check_rebalance(
    report: AllocationReport,
    threshold_pct: float,
    last_review: datetime | None,
    interval_days: int,
    now: datetime,
) -> RebalanceAlert:
    drifted = [
        leg
        for leg in report.legs
        if not leg.price_stale and abs(leg.drift_pct) >= threshold_pct
    ]
    if last_review is None:
        return RebalanceAlert(drifted, True, None, None)
    next_date = (last_review + timedelta(days=interval_days)).date()
    review_due = now.date() >= next_date
    return RebalanceAlert(drifted, review_due, next_date, last_review.date())
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation.py tests/test_allocation.py
git commit -m "feat(allocation): check_rebalance (drift esigi + ceyreklik vade)"
```

---

### Task 6: `core/allocation.py` — `plan_dca` (water-fill, alım-only)

**Files:**
- Modify: `src/swing_tracker/core/allocation.py`
- Test: `tests/test_allocation.py`

**Interfaces:**
- Consumes: `AllocationReport` (Task 4).
- Produces:
  - `DcaItem(symbol: str, buy_usd: float, buy_shares: float)`
  - `DcaPlan(items: list[DcaItem], deployed_usd: float, leftover_usd: float)`
  - `plan_dca(report: AllocationReport, contribution_usd: float, fractional: bool) -> DcaPlan`
  - `_waterfill(values: dict[str, float], target_frac: dict[str, float], budget: float) -> dict[str, float]` — modül-içi yardımcı (Task 8 tekrar kullanır). Alım-only; en düşük `value/target` oranındaki bacaklara para döker.

- [ ] **Step 1: Failing test ekle**

`tests/test_allocation.py` sonuna:

```python
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
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -k dca -v`
Expected: FAIL — `ImportError: cannot import name 'plan_dca'`

- [ ] **Step 3: `_waterfill` + `plan_dca` ekle**

`src/swing_tracker/core/allocation.py` üstündeki import'a ekle:

```python
import math
```

Dosya sonuna:

```python
_EPS = 1e-9


def _waterfill(
    values: dict[str, float], target_frac: dict[str, float], budget: float
) -> dict[str, float]:
    """Alim-only water-filling: en dusuk value/target oranli bacaklara para doker.
    Dondurur: {symbol: eklenecek_usd}. Toplam ~= budget (butce > 0 ise)."""
    add = {s: 0.0 for s in target_frac}
    syms = [s for s in target_frac if target_frac[s] > 0]
    if budget <= _EPS or not syms:
        return add
    remaining = budget
    while remaining > _EPS:
        ratios = {s: (values[s] + add[s]) / target_frac[s] for s in syms}
        min_r = min(ratios.values())
        group = [s for s in syms if ratios[s] <= min_r + _EPS]
        higher = [ratios[s] for s in syms if ratios[s] > min_r + _EPS]
        tsum = sum(target_frac[s] for s in group)
        if higher:
            target_r = min(higher)
            cost = sum(target_frac[s] * (target_r - ratios[s]) for s in group)
            if cost <= remaining + _EPS:
                for s in group:
                    add[s] += target_frac[s] * (target_r - ratios[s])
                remaining -= cost
                continue
        # ya hepsi esit (higher yok) ya da butce bir sonraki seviyeye yetmiyor:
        # kalan butceyi grup icinde target agirligina gore dagit
        for s in group:
            add[s] += remaining * (target_frac[s] / tsum)
        remaining = 0.0
    return add


def plan_dca(
    report: AllocationReport, contribution_usd: float, fractional: bool
) -> DcaPlan:
    legs = [l for l in report.legs if not l.price_stale and l.target_pct > 0]
    if contribution_usd <= 0 or not legs:
        return DcaPlan(items=[], deployed_usd=0.0,
                       leftover_usd=max(contribution_usd, 0.0))
    values = {l.symbol: l.value_usd for l in legs}
    target_frac = {l.symbol: l.target_pct / 100.0 for l in legs}
    prices = {l.symbol: l.price_usd for l in legs}
    add = _waterfill(values, target_frac, contribution_usd)

    items: list[DcaItem] = []
    deployed = 0.0
    leftover = 0.0
    for sym, amt in add.items():
        if amt <= _EPS:
            continue
        price = prices[sym]
        if fractional:
            shares = amt / price
            spend = amt
        else:
            shares = float(math.floor(amt / price))
            spend = shares * price
        if shares <= 0:
            leftover += amt
            continue
        deployed += spend
        leftover += amt - spend
        items.append(DcaItem(symbol=sym, buy_usd=round(spend, 2),
                             buy_shares=round(shares, 4)))
    return DcaPlan(items=items, deployed_usd=round(deployed, 2),
                   leftover_usd=round(leftover, 2))
```

Ayrıca `DcaItem`/`DcaPlan` dataclass'larını dosyada `RebalanceAlert`'ten sonra tanımla:

```python
@dataclass
class DcaItem:
    symbol: str
    buy_usd: float
    buy_shares: float


@dataclass
class DcaPlan:
    items: list[DcaItem]
    deployed_usd: float
    leftover_usd: float
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation.py tests/test_allocation.py
git commit -m "feat(allocation): plan_dca water-filling (alim-only, kesirli/tam lot)"
```

---

### Task 7: `core/allocation.py` — `plan_rebalance` (sat + al)

**Files:**
- Modify: `src/swing_tracker/core/allocation.py`
- Test: `tests/test_allocation.py`

**Interfaces:**
- Consumes: `AllocationReport` (Task 4).
- Produces:
  - `RebalanceItem(symbol: str, action: str, amount_usd: float, shares: float)` — `action` ∈ {"BUY","SELL","HOLD"}; `amount_usd`/`shares` pozitif büyüklük.
  - `RebalancePlan(items: list[RebalanceItem], net_cash_usd: float)`
  - `plan_rebalance(report: AllocationReport, contribution_usd: float, fractional: bool, min_trade_usd: float = 1.0) -> RebalancePlan`

- [ ] **Step 1: Failing test ekle**

`tests/test_allocation.py` sonuna:

```python
from swing_tracker.core.allocation import plan_rebalance


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
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -k rebalance -v`
Expected: FAIL — `ImportError: cannot import name 'plan_rebalance'`

- [ ] **Step 3: plan_rebalance ekle**

`src/swing_tracker/core/allocation.py` sonuna:

```python
@dataclass
class RebalanceItem:
    symbol: str
    action: str  # "BUY" | "SELL" | "HOLD"
    amount_usd: float
    shares: float


@dataclass
class RebalancePlan:
    items: list[RebalanceItem]
    net_cash_usd: float


def plan_rebalance(
    report: AllocationReport,
    contribution_usd: float,
    fractional: bool,
    min_trade_usd: float = 1.0,
) -> RebalancePlan:
    legs = [l for l in report.legs if not l.price_stale and l.target_pct > 0]
    if not legs:
        return RebalancePlan(items=[], net_cash_usd=0.0)
    total = sum(l.value_usd for l in legs)
    t_prime = total + max(contribution_usd, 0.0)

    items: list[RebalanceItem] = []
    net = 0.0
    for l in legs:
        target_val = (l.target_pct / 100.0) * t_prime
        delta = target_val - l.value_usd
        if abs(delta) < min_trade_usd:
            items.append(RebalanceItem(l.symbol, "HOLD", 0.0, 0.0))
            continue
        price = l.price_usd
        if fractional:
            shares = abs(delta) / price
            amount = abs(delta)
        else:
            shares = float(math.floor(abs(delta) / price))
            amount = shares * price
            if shares <= 0:
                items.append(RebalanceItem(l.symbol, "HOLD", 0.0, 0.0))
                continue
        if delta > 0:
            items.append(RebalanceItem(l.symbol, "BUY", round(amount, 2),
                                       round(shares, 4)))
            net += amount
        else:
            items.append(RebalanceItem(l.symbol, "SELL", round(amount, 2),
                                       round(shares, 4)))
            net -= amount
    return RebalancePlan(items=items, net_cash_usd=round(net, 2))
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation.py tests/test_allocation.py
git commit -m "feat(allocation): plan_rebalance (sat+al, katki+satis ile tam hedef)"
```

---

### Task 8: `core/allocation.py` — `estimate_months_to_core_target`

**Files:**
- Modify: `src/swing_tracker/core/allocation.py`
- Test: `tests/test_allocation.py`

**Interfaces:**
- Consumes: `AllocationReport` (Task 4), `_waterfill` (Task 6), `AllocationTarget`.
- Produces:
  - `TargetEta(months: int | None, target_date: date | None, already_met: bool, note: str)`
  - `estimate_months_to_core_target(report: AllocationReport, contribution_usd: float, targets: dict[str, AllocationTarget], now: datetime, target_core_pct: float = 40.0, max_months: int = 600) -> TargetEta`

- [ ] **Step 1: Failing test ekle**

`tests/test_allocation.py` sonuna:

```python
from swing_tracker.core.allocation import estimate_months_to_core_target


def test_eta_reaches_core_target():
    # core VOO deger 100, satellite QTUM deger 300 -> core %25; hedef %40
    rep = _report([_leg("VOO", "core", 28, 100, 10.0),
                   _leg("QTUM", "satellite", 72, 300, 10.0)])
    targets = {
        "VOO": _AT("VOO", 28, "AMEX", "core", ""),
        "QTUM": _AT("QTUM", 72, "NASDAQ", "satellite", ""),
    }
    eta = estimate_months_to_core_target(rep, 100.0, targets, datetime(2026, 7, 24))
    assert eta.already_met is False
    assert eta.months is not None and eta.months > 0
    assert eta.target_date is not None


def test_eta_already_met():
    rep = _report([_leg("VOO", "core", 40, 500, 10.0),
                   _leg("QTUM", "satellite", 60, 300, 10.0)])
    targets = {
        "VOO": _AT("VOO", 40, "AMEX", "core", ""),
        "QTUM": _AT("QTUM", 60, "NASDAQ", "satellite", ""),
    }
    eta = estimate_months_to_core_target(rep, 100.0, targets, datetime(2026, 7, 24))
    assert eta.already_met is True
    assert eta.months == 0


def test_eta_zero_contribution_unknown():
    rep = _report([_leg("VOO", "core", 40, 100, 10.0),
                   _leg("QTUM", "satellite", 60, 300, 10.0)])
    targets = {
        "VOO": _AT("VOO", 40, "AMEX", "core", ""),
        "QTUM": _AT("QTUM", 60, "NASDAQ", "satellite", ""),
    }
    eta = estimate_months_to_core_target(rep, 0.0, targets, datetime(2026, 7, 24))
    assert eta.months is None
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -k eta -v`
Expected: FAIL — `ImportError: cannot import name 'estimate_months_to_core_target'`

- [ ] **Step 3: Fonksiyonu ekle**

`src/swing_tracker/core/allocation.py` sonuna:

```python
def _add_months(d: date, months: int) -> date:
    total = (d.month - 1) + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


@dataclass
class TargetEta:
    months: int | None
    target_date: date | None
    already_met: bool
    note: str


def estimate_months_to_core_target(
    report: AllocationReport,
    contribution_usd: float,
    targets: dict[str, AllocationTarget],
    now: datetime,
    target_core_pct: float = 40.0,
    max_months: int = 600,
) -> TargetEta:
    core_syms = {s for s, t in targets.items() if t.group == "core"}
    values = {l.symbol: l.value_usd for l in report.legs
              if not l.price_stale and l.target_pct > 0}
    target_frac = {s: targets[s].weight / 100.0 for s in values}

    def core_weight() -> float:
        tot = sum(values.values())
        if tot <= 0:
            return 0.0
        return 100.0 * sum(v for s, v in values.items() if s in core_syms) / tot

    if core_weight() >= target_core_pct:
        return TargetEta(0, now.date(), True, "Core zaten hedefte.")
    if contribution_usd <= 0:
        return TargetEta(None, None, False, "Katki 0 — tahmin yok.")

    months = 0
    while months < max_months:
        add = _waterfill(values, target_frac, contribution_usd)
        for s, a in add.items():
            values[s] += a
        months += 1
        if core_weight() >= target_core_pct:
            return TargetEta(
                months, _add_months(now.date(), months), False,
                "Fiyat hareketleri haric, mevcut katki temposuyla.",
            )
    return TargetEta(None, None, False, "Mevcut tempoyla ulasilamiyor.")
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation.py tests/test_allocation.py
git commit -m "feat(allocation): estimate_months_to_core_target (ileri sim)"
```

---

### Task 9: `core/allocation_service.py` — okuma orkestrasyonu (`build_report`)

**Files:**
- Create: `src/swing_tracker/core/allocation_service.py`
- Test: `tests/test_allocation_service.py`

**Interfaces:**
- Consumes: `Repository` (Task 2 method'lari), `AllocationConfig`, `etf_price_cache` (Task 3), tüm core fonksiyonlar (Task 4-8).
- Produces:
  - `AllocationView(report, alert, dca, rebalance, eta, contribution_usd)` (dataclass)
  - `build_report(repo, config, now: datetime | None = None, contribution_override: float | None = None, price_cache=etf_price_cache) -> AllocationView`

- [ ] **Step 1: Failing test yaz** (fiyat çekimi fake cache ile — network yok)

`tests/test_allocation_service.py`:

```python
import sqlite3
from datetime import datetime

import pytest

from swing_tracker.config import AllocationConfig, AllocationTarget
from swing_tracker.core.allocation_service import build_report
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


class FakePriceCache:
    def __init__(self, prices, usdtry=47.0):
        self._prices = prices
        self._usdtry = usdtry

    def fetch_many(self, symbol_exchange, max_workers=5):
        return {s: self._prices[s] for s in symbol_exchange if s in self._prices}

    def fetch_usdtry(self):
        return self._usdtry


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _config():
    return AllocationConfig(
        enabled=True,
        monthly_contribution_usd=100.0,
        drift_threshold_pct=5.0,
        review_interval_days=91,
        fractional=True,
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        },
    )


def test_build_report_uses_config_contribution(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 10.0)
    repo.upsert_allocation_holding("QTUM", "NASDAQ", 30.0)
    cache = FakePriceCache({"VOO": 10.0, "QTUM": 10.0})
    view = build_report(repo, _config(), now=datetime(2026, 7, 24), price_cache=cache)
    assert view.contribution_usd == 100.0
    assert view.report.total_value_usd == 400.0
    assert view.report.usdtry == 47.0
    assert view.dca is not None and view.rebalance is not None and view.eta is not None


def test_build_report_prefers_saved_contribution(repo):
    repo.set_allocation_setting("last_contribution_usd", "250")
    cache = FakePriceCache({"VOO": 10.0})
    view = build_report(repo, _config(), now=datetime(2026, 7, 24), price_cache=cache)
    assert view.contribution_usd == 250.0


def test_build_report_override_wins(repo):
    repo.set_allocation_setting("last_contribution_usd", "250")
    cache = FakePriceCache({"VOO": 10.0})
    view = build_report(repo, _config(), now=datetime(2026, 7, 24),
                        contribution_override=999.0, price_cache=cache)
    assert view.contribution_usd == 999.0
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'swing_tracker.core.allocation_service'`

- [ ] **Step 3: allocation_service.py yaz**

`src/swing_tracker/core/allocation_service.py`:

```python
"""Allocation okuma orkestrasyonu — web router ve scheduler ayni yolu kullanir."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from swing_tracker.config import AllocationConfig
from swing_tracker.core import etf_prices
from swing_tracker.core.allocation import (
    AllocationReport,
    DcaPlan,
    RebalanceAlert,
    RebalancePlan,
    TargetEta,
    check_rebalance,
    compute_weights,
    estimate_months_to_core_target,
    plan_dca,
    plan_rebalance,
)
from swing_tracker.db.repository import Repository


@dataclass
class AllocationView:
    report: AllocationReport
    alert: RebalanceAlert
    dca: DcaPlan
    rebalance: RebalancePlan
    eta: TargetEta
    contribution_usd: float


def _resolve_contribution(
    repo: Repository, config: AllocationConfig, override: float | None
) -> float:
    if override is not None:
        return float(override)
    saved = repo.get_allocation_setting("last_contribution_usd")
    if saved is not None:
        try:
            return float(saved)
        except ValueError:
            pass
    return float(config.monthly_contribution_usd)


def build_report(
    repo: Repository,
    config: AllocationConfig,
    now: datetime | None = None,
    contribution_override: float | None = None,
    price_cache=etf_prices.etf_price_cache,
) -> AllocationView:
    now = now or datetime.now()
    holdings = repo.get_allocation_holdings()
    symbol_exchange = {t.symbol: t.exchange for t in config.targets.values()}
    prices = price_cache.fetch_many(symbol_exchange)
    usdtry = price_cache.fetch_usdtry()

    report = compute_weights(holdings, prices, config.targets, usdtry=usdtry)

    last_row = repo.get_last_allocation_review()
    last_review = None
    if last_row and last_row.get("reviewed_at"):
        try:
            last_review = datetime.fromisoformat(last_row["reviewed_at"])
        except ValueError:
            last_review = None

    alert = check_rebalance(
        report, config.drift_threshold_pct, last_review,
        config.review_interval_days, now,
    )
    contribution = _resolve_contribution(repo, config, contribution_override)
    dca = plan_dca(report, contribution, config.fractional)
    rebalance = plan_rebalance(report, contribution, config.fractional)
    eta = estimate_months_to_core_target(report, contribution, config.targets, now)

    return AllocationView(report, alert, dca, rebalance, eta, contribution)
```

- [ ] **Step 4: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_service.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/swing_tracker/core/allocation_service.py tests/test_allocation_service.py
git commit -m "feat(allocation): build_report orkestrasyonu (katki hafizasi dahil)"
```

---

### Task 10: Web router + template + nav

**Files:**
- Create: `src/swing_tracker/web/routers/allocation.py`
- Create: `src/swing_tracker/web/templates/allocation.html`
- Modify: `src/swing_tracker/web/app.py` (router register)
- Modify: `src/swing_tracker/web/templates/base.html` (nav linki + bottom nav)
- Test: `tests/test_allocation_router.py`

**Interfaces:**
- Consumes: `build_report` (Task 9), `get_repo`/`get_config` (`web/dependencies.py`), `templates`.

- [ ] **Step 1: Failing test yaz** (fiyat çekimini monkeypatch ile fake'le)

`tests/test_allocation_router.py`:

```python
import sqlite3

import pytest
from fastapi.testclient import TestClient

from swing_tracker.config import AllocationConfig, AllocationTarget, Config
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.web import dependencies
from swing_tracker.core import allocation_service


class FakePriceCache:
    def fetch_many(self, symbol_exchange, max_workers=5):
        return {s: 100.0 for s in symbol_exchange}

    def fetch_usdtry(self):
        return 47.0


@pytest.fixture
def client(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    repo = Repository(conn)
    config = Config()
    config.allocation = AllocationConfig(
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        }
    )
    dependencies.init_state(repo, config)
    monkeypatch.setattr(allocation_service.etf_prices, "etf_price_cache", FakePriceCache())
    from swing_tracker.web.app import app
    return TestClient(app), repo


def test_allocation_page_renders(client):
    tc, repo = client
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    resp = tc.get("/allocation")
    assert resp.status_code == 200
    assert "VOO" in resp.text


def test_add_holding_redirects(client):
    tc, repo = client
    resp = tc.post("/allocation/holding",
                   data={"symbol": "QTUM", "exchange": "NASDAQ", "shares": "10"},
                   follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert repo.get_allocation_holding("QTUM")["shares"] == 10.0


def test_review_marks_done(client):
    tc, repo = client
    tc.post("/allocation/review", follow_redirects=False)
    assert repo.get_last_allocation_review() is not None


def test_dca_persists_contribution(client):
    tc, repo = client
    tc.post("/allocation/dca", data={"contribution": "750"}, follow_redirects=False)
    assert repo.get_allocation_setting("last_contribution_usd") == "750.0"
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_router.py -v`
Expected: FAIL — `assert 404 == 200` (route yok)

- [ ] **Step 3: Router yaz**

`src/swing_tracker/web/routers/allocation.py`:

```python
"""Allocation router — hedef vs gercek agirlik, DCA + rebalance onerileri."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.core.allocation_service import build_report
from swing_tracker.web.dependencies import get_config, get_repo, templates

router = APIRouter(prefix="/allocation")


@router.get("", response_class=HTMLResponse)
async def allocation_page(request: Request):
    repo = get_repo()
    config = get_config()
    view = await asyncio.to_thread(build_report, repo, config.allocation)
    return templates.TemplateResponse(
        request,
        "allocation.html",
        context={"view": view, "config": config.allocation},
    )


@router.post("/holding")
async def add_holding(
    symbol: str = Form(...),
    exchange: str = Form(...),
    shares: float = Form(...),
    cost_per_share: float | None = Form(None),
    notes: str | None = Form(None),
):
    repo = get_repo()
    repo.upsert_allocation_holding(
        symbol.strip().upper(), exchange.strip().upper(), shares, cost_per_share, notes
    )
    return RedirectResponse("/allocation", status_code=303)


@router.post("/holding/delete")
async def delete_holding(symbol: str = Form(...)):
    get_repo().delete_allocation_holding(symbol.strip().upper())
    return RedirectResponse("/allocation", status_code=303)


@router.post("/dca")
async def set_contribution(contribution: float = Form(...)):
    get_repo().set_allocation_setting("last_contribution_usd", str(float(contribution)))
    return RedirectResponse("/allocation", status_code=303)


@router.post("/review")
async def mark_reviewed(note: str | None = Form(None)):
    get_repo().log_allocation_review(note)
    return RedirectResponse("/allocation", status_code=303)
```

- [ ] **Step 4: app.py'ye register ekle**

`src/swing_tracker/web/app.py`, router import ve include bölümüne ekle. Import satırına `allocation` ekle (mevcut `from swing_tracker.web.routers import ...` satırına), ve include bloğuna:

```python
app.include_router(allocation.router)
```

(Not: import satırı örn. `from swing_tracker.web.routers import dashboard, portfolio, signals, trades, symbol, whatif, allocation` şeklinde güncellenir.)

- [ ] **Step 5: Template yaz**

`src/swing_tracker/web/templates/allocation.html` — mevcut tema (Tailwind util sınıfları, `bg-surface-raised`, `text-txt-primary`, `border-border`, `font-mono tabular-nums`) ve `base.html` extend deseniyle. Değişkenler: `view` (`AllocationView`), `config` (`AllocationConfig`).

```html
{% extends "base.html" %}
{% block title %}Allocation{% endblock %}
{% block nav_allocation %}text-txt-primary bg-accent/10{% endblock %}
{% block bottom_nav_allocation %}text-txt-primary{% endblock %}
{% block content %}
<div class="max-w-5xl mx-auto px-4 py-6 space-y-6">

  <div class="flex items-center justify-between">
    <h1 class="text-xl font-bold text-txt-primary">Allocation &amp; Rebalance</h1>
    {% if view.report.usdtry %}
    <span class="text-sm text-txt-muted">USDTRY
      <span class="font-mono tabular-nums">{{ "%.2f"|format(view.report.usdtry) }}</span>
    </span>
    {% endif %}
  </div>

  {% if view.alert.review_due %}
  <form action="/allocation/review" method="post"
        class="flex items-center gap-3 bg-warning/10 border border-warning/30 rounded-lg px-4 py-3">
    <span class="text-sm text-warning flex-1">Ceyreklik kontrol zamani geldi.</span>
    <button class="text-xs px-3 py-1.5 rounded-lg border border-border">Kontrol yapildi</button>
  </form>
  {% endif %}

  <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
    <div class="bg-surface-raised rounded-lg p-3">
      <div class="text-xs text-txt-muted">Toplam deger</div>
      <div class="text-lg font-mono tabular-nums text-txt-primary">
        ${{ "{:,.0f}".format(view.report.total_value_usd) }}</div>
      {% if view.report.usdtry %}
      <div class="text-xs text-txt-muted font-mono">
        ₺{{ "{:,.0f}".format(view.report.total_value_usd * view.report.usdtry) }}</div>
      {% endif %}
    </div>
    <div class="bg-surface-raised rounded-lg p-3">
      <div class="text-xs text-txt-muted">Core (hedef 40%)</div>
      <div class="text-lg font-mono tabular-nums text-txt-primary">
        {{ "%.1f"|format(view.report.core_weight_pct) }}%</div>
    </div>
    <div class="bg-surface-raised rounded-lg p-3">
      <div class="text-xs text-txt-muted">Satellite (hedef 60%)</div>
      <div class="text-lg font-mono tabular-nums text-txt-primary">
        {{ "%.1f"|format(view.report.satellite_weight_pct) }}%</div>
    </div>
    <div class="bg-surface-raised rounded-lg p-3">
      <div class="text-xs text-txt-muted">Core %40 ETA</div>
      <div class="text-lg font-mono tabular-nums text-txt-primary">
        {% if view.eta.already_met %}Hedefte{% elif view.eta.months %}≈ {{ view.eta.months }} ay{% else %}—{% endif %}</div>
      {% if view.eta.target_date and not view.eta.already_met %}
      <div class="text-xs text-txt-muted">~{{ view.eta.target_date.strftime('%m/%Y') }}</div>
      {% endif %}
    </div>
  </div>

  <table class="w-full text-sm">
    <thead><tr class="text-txt-muted text-xs text-right">
      <th class="text-left font-normal py-2">ETF</th><th class="font-normal">Hedef</th>
      <th class="font-normal">Gercek</th><th class="font-normal">Drift</th>
      <th class="font-normal">Deger $</th><th class="font-normal">Deger ₺</th>
    </tr></thead>
    <tbody>
    {% for leg in view.report.legs %}
      <tr class="border-t border-border text-right">
        <td class="text-left py-2">
          <a href="/symbol/{{ leg.symbol }}" class="font-medium text-txt-primary">{{ leg.symbol }}</a>
          <span class="text-xs text-txt-muted">{{ leg.group }}</span>
        </td>
        <td class="font-mono tabular-nums">{{ "%.0f"|format(leg.target_pct) }}%</td>
        {% if leg.price_stale %}
          <td colspan="4" class="text-txt-muted">— fiyat alinamadi</td>
        {% else %}
          <td class="font-mono tabular-nums">{{ "%.1f"|format(leg.weight_pct) }}%</td>
          <td>
            {% set d = leg.drift_pct %}
            {% set cls = 'bg-danger/15 text-danger' if d|abs >= config.drift_threshold_pct else ('bg-warning/15 text-warning' if d|abs >= 3 else 'text-txt-muted') %}
            <span class="px-2 py-0.5 rounded font-mono tabular-nums text-xs {{ cls }}">
              {{ "%+.1f"|format(d) }}</span>
          </td>
          <td class="font-mono tabular-nums">{{ "{:,.0f}".format(leg.value_usd) }}</td>
          <td class="font-mono tabular-nums text-txt-muted">
            {% if view.report.usdtry %}{{ "{:,.0f}".format(leg.value_usd * view.report.usdtry) }}{% else %}—{% endif %}</td>
        {% endif %}
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <form action="/allocation/dca" method="post"
        class="flex items-center gap-3 bg-surface-raised rounded-lg px-4 py-3">
    <label class="text-sm text-txt-secondary">Aylik katki $</label>
    <input type="number" step="1" name="contribution" value="{{ '%.0f'|format(view.contribution_usd) }}"
           class="w-28 bg-surface border border-border rounded-lg px-2 py-1 font-mono" />
    <button class="text-xs px-3 py-1.5 rounded-lg border border-border">Hesapla</button>
    {% if view.report.usdtry %}
    <span class="text-xs text-txt-muted">≈ ₺{{ "{:,.0f}".format(view.contribution_usd * view.report.usdtry) }}</span>
    {% endif %}
  </form>

  <div class="grid sm:grid-cols-2 gap-4">
    <div class="bg-surface-raised rounded-lg p-4">
      <div class="text-sm font-medium text-txt-primary mb-2">DCA onerisi
        <span class="text-xs text-success">sadece alim</span></div>
      <table class="w-full text-sm">
        {% for it in view.dca.items %}
        <tr class="text-right"><td class="text-left py-1 text-txt-secondary">{{ it.symbol }}</td>
          <td class="text-success font-mono">+${{ "%.0f"|format(it.buy_usd) }}</td>
          <td class="text-txt-muted font-mono text-xs">{{ "%.2f"|format(it.buy_shares) }} lot</td></tr>
        {% else %}
        <tr><td class="text-txt-muted py-1">Katki gir.</td></tr>
        {% endfor %}
      </table>
    </div>
    <div class="bg-surface-raised rounded-lg p-4">
      <div class="text-sm font-medium text-txt-primary mb-2">Tam rebalance
        <span class="text-xs text-accent">sat + al</span></div>
      <table class="w-full text-sm">
        {% for it in view.rebalance.items %}{% if it.action != 'HOLD' %}
        <tr class="text-right"><td class="text-left py-1 text-txt-secondary">{{ it.symbol }}</td>
          <td class="font-mono {{ 'text-success' if it.action == 'BUY' else 'text-danger' }}">
            {{ 'AL' if it.action == 'BUY' else 'SAT' }} ${{ "%.0f"|format(it.amount_usd) }}</td></tr>
        {% endif %}{% endfor %}
      </table>
      <div class="text-xs text-txt-muted mt-2">Net nakit ${{ "%.0f"|format(view.rebalance.net_cash_usd) }}</div>
    </div>
  </div>

  <form action="/allocation/holding" method="post"
        class="flex flex-wrap items-center gap-2 border-t border-border pt-4">
    <span class="text-sm text-txt-secondary">Pozisyon ekle/guncelle</span>
    <input name="symbol" placeholder="Sembol" class="w-24 bg-surface border border-border rounded-lg px-2 py-1" required />
    <input name="exchange" placeholder="Borsa" class="w-24 bg-surface border border-border rounded-lg px-2 py-1" required />
    <input name="shares" type="number" step="0.0001" placeholder="Lot" class="w-24 bg-surface border border-border rounded-lg px-2 py-1" required />
    <input name="cost_per_share" type="number" step="0.01" placeholder="Ort. maliyet $" class="w-32 bg-surface border border-border rounded-lg px-2 py-1" />
    <button class="text-xs px-3 py-1.5 rounded-lg border border-border">Kaydet</button>
  </form>

  <p class="text-xs text-txt-muted">Sadece oneri — otomatik emir yok. Fiyatlar canli, ETA fiyat hareketleri haric.</p>
</div>
{% endblock %}
```

- [ ] **Step 6: base.html nav'a link ekle**

`src/swing_tracker/web/templates/base.html` — desktop nav'da `/whatif` linkinden sonra ekle:

```html
                    <a href="/allocation" class="px-3 py-1.5 rounded-lg text-sm font-medium
                        {% block nav_allocation %}text-txt-muted hover:text-txt-primary hover:bg-accent/5{% endblock %}">
                        Allocation
                    </a>
```

Ve bottom nav'da `/whatif` bloğundan sonra ekle (mevcut bottom nav item yapısıyla, uygun bir `ti-` ikon; whatif item'ının markup'ını referans al):

```html
            <a href="/allocation" class="flex-1 flex flex-col items-center gap-0.5 py-2.5
                {% block bottom_nav_allocation %}text-txt-muted{% endblock %}">
                <svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M11 3.055A9.001 9.001 0 1020.945 13H11V3.055z"/><path stroke-linecap="round" stroke-linejoin="round" d="M20.488 9H15V3.512A9.025 9.025 0 0120.488 9z"/></svg>
                <span class="text-[10px]">Allocation</span>
            </a>
```

- [ ] **Step 7: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_router.py -v`
Expected: PASS (4 passed)

- [ ] **Step 8: Tarayıcıda doğrula**

`.claude/launch.json` yoksa web uygulaması için oluştur (uvicorn ile). Preview başlat, `/allocation` sayfasını aç: hedef/gerçek tablosu, drift rozetleri, DCA + rebalance kartları, ETA görünüyor mu; konsol/log hatası yok. Bir holding ekleyip formun çalıştığını doğrula. Ekran görüntüsü al.

- [ ] **Step 9: Commit**

```bash
git add src/swing_tracker/web/routers/allocation.py src/swing_tracker/web/templates/allocation.html src/swing_tracker/web/app.py src/swing_tracker/web/templates/base.html tests/test_allocation_router.py
git commit -m "feat(allocation): /allocation web sayfasi + router + nav"
```

---

### Task 11: Telegram bildirim + `run_allocation_check`

**Files:**
- Modify: `src/swing_tracker/bot/telegram.py` (pure format helper'lar + notify method'lari)
- Modify: `src/swing_tracker/core/allocation_service.py` (`run_allocation_check`)
- Test: `tests/test_allocation_notify.py`

**Interfaces:**
- Consumes: `AllocationView` (Task 9), `TelegramNotifier.send_message` (mevcut).
- Produces:
  - `swing_tracker.bot.telegram.build_drift_message(view) -> str` (pure)
  - `swing_tracker.bot.telegram.build_review_message(next_date) -> str` (pure)
  - `TelegramNotifier.notify_allocation_drift(view) -> None`
  - `TelegramNotifier.notify_allocation_review(next_date) -> None`
  - `swing_tracker.core.allocation_service.run_allocation_check(repo, config, notifier, now=None, price_cache=...) -> None`

- [ ] **Step 1: Failing test yaz** (pure formatter + orkestrasyon; notifier fake)

`tests/test_allocation_notify.py`:

```python
import sqlite3
from datetime import datetime

import pytest

from swing_tracker.bot.telegram import build_drift_message, build_review_message
from swing_tracker.config import AllocationConfig, AllocationTarget
from swing_tracker.core import allocation_service
from swing_tracker.core.allocation import AllocationLeg, AllocationReport, RebalanceAlert
from swing_tracker.core.allocation_service import AllocationView, run_allocation_check
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


def _view_with_drift():
    leg = AllocationLeg("VOO", "AMEX", "core", 28, 1, 300.0, 300.0, 75.0, 47.0, False)
    report = AllocationReport([leg], 300.0, 75.0, 0.0, 47.0)
    alert = RebalanceAlert([leg], True, None, None)
    return AllocationView(report, alert, None, None, None, 100.0)


def test_build_drift_message_lists_legs():
    msg = build_drift_message(_view_with_drift())
    assert "VOO" in msg
    assert "+47" in msg or "47.0" in msg


def test_build_review_message():
    from datetime import date
    msg = build_review_message(date(2026, 10, 1))
    assert "2026" in msg


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_message_sync(self, text):
        self.sent.append(text)


class FakePriceCache:
    def fetch_many(self, se, max_workers=5):
        return {s: 100.0 for s in se}

    def fetch_usdtry(self):
        return 47.0


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _config():
    return AllocationConfig(
        drift_threshold_pct=5.0,
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        },
    )


def test_run_check_sends_when_drifted(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 1.0)  # tek bacak -> %100, drift
    notifier = FakeNotifier()
    run_allocation_check(repo, _config(), notifier,
                         now=datetime(2026, 7, 24), price_cache=FakePriceCache())
    assert len(notifier.sent) >= 1


def test_run_check_silent_when_no_drift_and_reviewed(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 40.0)
    repo.upsert_allocation_holding("QTUM", "NASDAQ", 60.0)
    repo.log_allocation_review("init")
    notifier = FakeNotifier()
    run_allocation_check(repo, _config(), notifier,
                         now=datetime(2026, 7, 24), price_cache=FakePriceCache())
    assert notifier.sent == []
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_notify.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_drift_message'`

- [ ] **Step 3: telegram.py'ye pure formatter + notify + sync helper ekle**

`src/swing_tracker/bot/telegram.py` modül düzeyine (sınıf dışına) ekle:

```python
def build_drift_message(view) -> str:
    lines = ["<b>Allocation drift uyarisi</b>"]
    for leg in view.alert.drifted_legs:
        lines.append(f"{leg.symbol}: {leg.weight_pct:.1f}% (hedef {leg.target_pct:.0f}%, "
                     f"drift {leg.drift_pct:+.1f})")
    lines.append("\nSadece oneri — otomatik emir yok.")
    return "\n".join(lines)


def build_review_message(next_date) -> str:
    return (f"<b>Allocation ceyreklik kontrol</b>\n"
            f"Rebalance gozden gecirme zamani ({next_date.isoformat()}).")
```

`TelegramNotifier` sınıfına method ekle (mevcut `send_message` async ve `_run_async`/sync gönderim desenini kullan; sınıfta senkron sarmalayıcı yoksa `send_message_sync` ekle):

```python
    def send_message_sync(self, text: str) -> None:
        self._run_async(self.send_message(text))

    def notify_allocation_drift(self, view) -> None:
        if not self.enabled or not view.alert.drifted_legs:
            return
        self.send_message_sync(build_drift_message(view))

    def notify_allocation_review(self, next_date) -> None:
        if not self.enabled:
            return
        self.send_message_sync(build_review_message(next_date))
```

(Not: `_run_async` mevcut helper; yoksa dosyadaki sync-gönderim deseniyle uyumlu hale getir. `self.enabled` alanı mevcut.)

- [ ] **Step 4: allocation_service.py'ye run_allocation_check ekle**

`src/swing_tracker/core/allocation_service.py` sonuna:

```python
def run_allocation_check(
    repo: Repository,
    config: AllocationConfig,
    notifier,
    now: datetime | None = None,
    price_cache=etf_prices.etf_price_cache,
) -> None:
    """Scheduler girisi: drift/vade kontrolu, gerekiyorsa Telegram bildirimi."""
    if not config.enabled:
        return
    view = build_report(repo, config, now=now, price_cache=price_cache)
    if view.alert.drifted_legs:
        notifier.notify_allocation_drift(view)
    if view.alert.review_due:
        notifier.notify_allocation_review(view.alert.next_review_date)
```

Not: test'teki `FakeNotifier.send_message_sync` çağrılır; `notify_allocation_drift`/`notify_allocation_review` gerçek notifier'da tanımlı. Test `FakeNotifier`'a bu iki method'u da eklemeli — **test'i şu şekilde güncelle**: `FakeNotifier`'a ekle:

```python
    def notify_allocation_drift(self, view):
        self.send_message_sync("drift")

    def notify_allocation_review(self, next_date):
        self.send_message_sync("review")
```

(Bu satırları Step 1 test dosyasındaki `FakeNotifier`'a dahil et.)

- [ ] **Step 5: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_allocation_notify.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add src/swing_tracker/bot/telegram.py src/swing_tracker/core/allocation_service.py tests/test_allocation_notify.py
git commit -m "feat(allocation): Telegram drift/vade bildirimi + run_allocation_check"
```

---

### Task 12: `main.py` scheduler job

**Files:**
- Modify: `src/swing_tracker/main.py` (job fonksiyonu + `add_job` wiring)
- Test: `tests/test_main_allocation_job.py`

**Interfaces:**
- Consumes: `run_allocation_check` (Task 11), `config.allocation`.
- Produces: `job_allocation_check(repo, config, notifier)` — exception yutan sarmalayıcı (whatif deseni).

- [ ] **Step 1: Failing test yaz**

`tests/test_main_allocation_job.py`:

```python
import sqlite3

from swing_tracker.config import AllocationConfig, AllocationTarget, Config
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.main import job_allocation_check


class FakeNotifier:
    def __init__(self):
        self.calls = 0

    def notify_allocation_drift(self, view):
        self.calls += 1

    def notify_allocation_review(self, next_date):
        self.calls += 1


def test_job_allocation_check_runs_without_error(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    repo = Repository(conn)
    repo.upsert_allocation_holding("VOO", "AMEX", 1.0)
    config = Config()
    config.allocation = AllocationConfig(
        targets={"VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
                 "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", "")}
    )

    from swing_tracker.core import allocation_service

    class FakeCache:
        def fetch_many(self, se, max_workers=5):
            return {s: 100.0 for s in se}

        def fetch_usdtry(self):
            return 47.0

    monkeypatch.setattr(allocation_service.etf_prices, "etf_price_cache", FakeCache())
    # exception yutulmali, patlamamali
    job_allocation_check(repo, config, FakeNotifier())
```

- [ ] **Step 2: Test'i çalıştır, fail gördüğünü doğrula**

Run: `.venv/bin/python -m pytest tests/test_main_allocation_job.py -v`
Expected: FAIL — `ImportError: cannot import name 'job_allocation_check'`

- [ ] **Step 3: main.py'ye job fonksiyonu ekle**

`src/swing_tracker/main.py`, `job_whatif_update`'ten sonra:

```python
def job_allocation_check(repo, config, notifier):
    """Gunluk allocation drift/vade kontrolu -> gerekiyorsa Telegram."""
    from swing_tracker.core.allocation_service import run_allocation_check
    try:
        run_allocation_check(repo, config.allocation, notifier)
    except Exception:
        logger.exception("allocation_check job hatasi")
```

- [ ] **Step 4: Scheduler wiring ekle**

`src/swing_tracker/main.py`, whatif job wiring'inden sonra (aynı `if config.<x>.enabled` deseniyle):

```python
    if config.allocation.enabled:
        _scheduler.add_job(
            job_allocation_check,
            CronTrigger(hour=17, minute=0, timezone=tz),
            args=[repo, config, _notifier],
            id="allocation_check",
            name="Allocation Check",
        )
```

(Not: `repo`, `config`, `_notifier` bu kapsamda mevcut değişkenler; whatif/monitor job'larıyla aynı kaynaklardan verilir. Gerçek isimlerini o bölümdeki mevcut `add_job` çağrılarından teyit et.)

- [ ] **Step 5: Test'i çalıştır, geçtiğini doğrula**

Run: `.venv/bin/python -m pytest tests/test_main_allocation_job.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Tüm test paketini çalıştır**

Run: `.venv/bin/python -m pytest -q`
Expected: Tüm testler PASS (mevcut testler dahil kırılma yok).

- [ ] **Step 7: Ruff kontrol**

Run: `.venv/bin/ruff check src/swing_tracker/core/allocation.py src/swing_tracker/core/allocation_service.py src/swing_tracker/core/etf_prices.py src/swing_tracker/web/routers/allocation.py`
Expected: Hata yok (varsa düzelt).

- [ ] **Step 8: Commit**

```bash
git add src/swing_tracker/main.py tests/test_main_allocation_job.py
git commit -m "feat(allocation): gunluk scheduler job (drift/vade -> Telegram)"
```

---

## Self-Review Notları

- **Spec kapsamı:** (1) config hedefleri → Task 1; (2) gerçek ağırlık + drift → Task 4; (3) ±5 uyarı + çeyreklik → Task 5/11/12; (4) DCA alım-only → Task 6; (5) ETA → Task 8; (6) nakit hariç → tabloda nakit yok (Task 2), hesap yalnız targets (Task 4); (7) emir yok → yalnız öneri (her task); (8) sat+al rebalance → Task 7; (9) TRY gösterim → Task 3 (USDTRY) + Task 10 template. ETF fiyat çekimi → Task 3. Katkı hafızası → Task 2/9/10.
- **Kapsam dışı (spec):** ETF holdings overlap — hiçbir task'ta yok (bilerek).
- **Tip tutarlılığı:** `_waterfill` Task 6'da tanımlı, Task 8'de kullanılıyor; `AllocationView` Task 9'da tanımlı, Task 10/11/12'de tüketiliyor; `build_report`/`run_allocation_check` imzaları tutarlı.
- **Not:** Task 10 Step 4/6 ve Task 12 Step 4, mevcut dosyalardaki tam satırlara göre uyarlanmalı — plan importlar/değişken adları için "mevcut desenden teyit et" diyor; router register ve nav ekleme dışında yapısal değişiklik yok.
