# What-If Faz 2 — Kalıcı Tablo + Günlük Job — Design

**Tarih:** 2026-07-10
**Öncül:** Faz 1 (PR #11) — `/whatif` sayfası her açılışta tüm sinyalleri OHLCV çekip yeniden simüle ediyor.

## Problem

Faz 1'de sayfa açılışı pahalı (sembol başına OHLCV + fiyat çekimi, soğuk açılışta dakikalar) ve sinyal sayısı büyüdükçe kötüleşiyor. Ayrıca 1h veri penceresi (~3 ay) eskiyen sinyallerin giriş fiyatını hesaplanamaz kılıyor. Amaç: hissenin yolu DB'de **önceden yaşanmış** olsun; sayfa saf okuma yapsın.

## Kapsam

- Yeni `whatif_trades` tablosu: eşik üstü **her** buy sinyali bağımsız sanal işlem olarak yaşar (dedup artık veri değil, görünüm).
- Scanner hook: sinyal loglanınca `pending` satır INSERT.
- Günlük job (`whatif_update`, 18:40 İstanbul): pending doldurma + open güncelleme (incremental) + zaman aşımı.
- Tek seferlik idempotent backfill CLI.
- Sayfa: DB okuması + iki modlu görünüm (dedup'lu "Takip edilebilir" / "Tüm sinyaller"); sadece açık pozisyonlar için canlı fiyat.
- Faz 1 motoru (`find_entry`, `atr_from_daily`, `check_exits`, `compute_stats`) yeniden kullanılır; sayfadaki `simulate_whatif` çağrısı kalkar.

## Şema — `whatif_trades`

```sql
CREATE TABLE IF NOT EXISTS whatif_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals_log(id),
    symbol TEXT NOT NULL,
    signal_time TEXT NOT NULL,          -- UTC "YYYY-MM-DD HH:MM:SS"
    score INTEGER NOT NULL,             -- entry_score olcegi (0-10)
    price_at_signal REAL,
    entry_price REAL,
    entry_source TEXT CHECK(entry_source IN ('bar_1h','fallback')),
    stop_loss REAL, tp1 REAL, tp2 REAL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','open','closed','expired','no_data')),
    remaining_shares INTEGER,           -- 100 sanal pay uzerinden
    realized_pnl REAL DEFAULT 0,        -- ara cikislarin birikimi (TL, 100 pay)
    highest_price REAL,                 -- trailing icin tepe takibi
    tp1_hit INTEGER DEFAULT 0,
    exit_type TEXT,                     -- tp1/tp2/trailing/sl/expired
    exit_date TEXT,
    strategy_pnl_pct REAL,              -- kapali/expired: nihai; open: son gunlenen
    buyhold_pnl_pct REAL,               -- al-tut "su ana kadar": job her gun gunceller
    last_close REAL,                    -- son bilinen gunluk kapanis (buyhold + expiry icin)
    delay_cost_pct REAL,                -- (entry - price_at_signal) / price_at_signal * 100
    holding_days REAL,
    last_update TEXT,                   -- job'un isledigi son bar tarihi (ISO gun)
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_whatif_trades_status ON whatif_trades(status);
CREATE INDEX IF NOT EXISTS idx_whatif_trades_symbol ON whatif_trades(symbol, signal_time);
```

Durum alanları (`remaining_shares`, `tp1_hit`, `highest_price`, `realized_pnl`) günlük job'un pozisyonu `BacktestTrade`'e geri yükleyip **sadece yeni bar'ları** işlemesini sağlar — her gün baştan simülasyon yok.

## Yazma yolu — scanner hook

`scanner.py::_log_scored_signal` sinyali logladıktan sonra `whatif_trades`'e tek satır ekler: `signal_id`, `symbol`, `signal_time`, `score` (entry_score), `price_at_signal`, `status='pending'`. Simülasyon mantığı scanner'a girmez; hook hatası sinyal akışını bozmaz (try/except + log).

## Günlük job — `whatif_update` (CronTrigger, Pzt-Cum 18:40 Europe/Istanbul)

deep_scan (18:30) OHLCV cache'i tazeledikten sonra çalışır; üç adım:

1. **Pending doldurma:** her `pending` satır için sinyalden sonraki ilk 1h bar kapanışı (2 gün penceresi, Faz 1 kuralı) → `entry_price/entry_source`; sinyal gününden önceki günlük bar'lardan ATR → SL/TP1/TP2; `status='open'`, `remaining_shares=100`, `highest_price=entry`. 1h bar yoksa `fallback` girişle devam; ATR için günlük veri yoksa `status='no_data'`.
2. **Open güncelleme:** her `open` satır durum alanlarından `BacktestTrade`'e yüklenir; `last_update`'ten sonraki günlük bar'lar sırayla `check_exits`'e verilir (komisyon 0). Kapanırsa `closed` + `exit_type/exit_date/strategy_pnl_pct/holding_days`; kapanmazsa durum alanları + `strategy_pnl_pct` (son kapanışla mark-to-market) + `last_update` güncellenir.
   **2b. Al-tut güncelleme:** al-tut "şu ana kadar" tanımlı olduğundan strateji durumundan bağımsızdır: tablodaki **tüm** girişli satırların `buyhold_pnl_pct` ve `last_close` değerleri o günün kapanışıyla güncellenir (kapalı işlemler dahil — al-tut kolonu onlar için de yaşamaya devam eder). Semboller cache'li günlük OHLCV'den okunur.
3. **Zaman aşımı:** `signal_time` üzerinden `max_holding_days`'i (config, varsayılan 60) aşan `open/pending/no_data` satırlar `expired` yapılır: `exit_type='expired'`, `exit_date` = job'un koştuğu gün (dedup filtresinin blok düşürmesi buna dayanır). Open satırda kalan paylar son bilinen kapanıştan realize edilir; pending/no_data'da `entry` olmadığından `strategy_pnl_pct` NULL kalır (istatistiklere zaten girmez).

Hata toleransı: sembol bazlı hata satırı atlar (log), `last_update` ilerlemez → ertesi gün yeniden dener. Job Telegram bildirimi göndermez.

Config: `config.toml`'a `[whatif]` bölümü → `enabled = true`, `max_holding_days = 60`; `config.py`'de dataclass.

## Backfill — `python -m swing_tracker.whatif_backfill`

Tek seferlik, idempotent CLI. Ayrı bir retrospektif simülasyon yolu YOKTUR — backfill, günlük job'un aynı pipeline'ını kullanır:
1. `signals_log`'daki eşik üstü buy sinyallerini okur (skor normalizasyonu: `indicator_values.entry_score`, fallback `score//10`) ve her birini `pending` satır olarak ekler (`INSERT OR IGNORE` — `signal_id` UNIQUE).
2. `run_whatif_update`'i bir kez çalıştırır: pending doldurma tüm tarihî girişleri üretir, open güncelleme sinyal gününden bugüne bar replay yapar, expiry eski açıkları kapatır.
- Bağımsız mod gereği dedup'suz: **tüm** sinyaller yazılır. Eski sinyallerde 1h penceresi aşıldığından girişler `fallback` işaretli olur — kabul edilmiş kusur, UI'da zaten işaretleniyor.
- Deploy sonrası bir kez elle çalıştırılır; tekrar çalıştırmak zararsız.

## Okuma yolu + UI

- `build_whatif_data`: `whatif_trades` SELECT → `WhatIfTrade` map → mevcut `compute_stats`. OHLCV çekimi ve `simulate_whatif` çağrısı sayfadan kalkar.
- **Canlı fiyat:** yalnızca `open` satırların sembolleri `price_cache.fetch_many` ile çekilir; açık pozisyonların `strategy_pnl_pct`'si görüntüleme anında canlı fiyatla yeniden hesaplanır (DB'deki değer son kapanış bazlı kalır). Fiyat alınamazsa DB'deki son değer gösterilir.
- **İki mod (toggle, query param `?mode=`):**
  - `takip` (varsayılan): kronolojik dedup filtresi okuma anında — sembolde daha erken bir satır hâlâ açıkken (veya kapanışı bu sinyalden sonrayken) gelen satırlar istatistik ve tabloya girmez; atlanan sayısı dipnotta.
  - `tum`: her satır bağımsız işlem, filtre yok.
- **Pending görünümü:** girişi doldurulmamış satırlar "BEKLEMEDE" rozetiyle listelenir (P&L boş), istatistiklere girmez.
- `expired` durumu tabloda kendi rozetiyle görünür ve kapalı işlem istatistiklerine dahildir (`exit_type='expired'`).
- Fragment/skeleton yapısı korunur; sayfa artık anında yüklenir.

## Test

In-memory SQLite + sahte bar'lar (Faz 1 test altyapısı):
- Scanner hook: sinyal → pending satır; hook hatası sinyal loglamayı bozmaz.
- Job adım 1: giriş doldurma (bar_1h / fallback / no_data yolları).
- Job adım 2: incremental güncelleme — durum geri yükleme doğruluğu (TP1 kısmi çıkış sonrası ertesi gün devam), kapanış, mark-to-market; `last_update` ilerleyişi; iki kez koşmanın idempotent olması (aynı bar iki kez işlenmez).
- Job adım 3: expiry (open/pending/no_data).
- Backfill: idempotency (iki koşu = tek satır seti), skor normalizasyonu.
- Okuma: dedup filtresi (açık blok, kapanış sonrası izin, expired blok düşürme), iki modun istatistik farkı, pending'in istatistik dışı kalması.

## Kararlar (soru-cevap özeti)

| Soru | Karar |
|------|-------|
| Tablo granülaritesi | Her sinyal = bir satır; dedup okuma filtresi (iki mod tek tablodan) |
| Giriş fiyatı yazımı | Günlük job doldurur (pending → open) |
| Dedup sızıntıları | max_holding_days=60 zaman aşımı → expired |
| Sayfa tazeliği | Sadece açık pozisyonlara canlı fiyat; gerisi saf DB |
| Bildirim | Job sessiz, Telegram'a dokunmaz |
