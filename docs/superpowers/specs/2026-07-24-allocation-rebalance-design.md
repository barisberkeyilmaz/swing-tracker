# Allocation & Rebalance Modülü — Tasarım Dokümanı

**Tarih:** 2026-07-24
**Durum:** Onay bekliyor
**Branch:** `feature/allocation-rebalance`

## Amaç

Swing-tracker'a, USD bazlı uzun-vadeli core/satellite ETF portföyünü izleyen ayrı bir
allocation (varlık dağılımı) ve rebalance modülü eklemek. Modül hedef ağırlıklara göre gerçek
ağırlıkları hesaplar, sapmayı (drift) gösterir, aylık katkı için alım önerisi (DCA) ve
gerektiğinde satış dahil tam-rebalance önerisi üretir. **Sadece uyarı/öneri üretir —
otomatik emir göndermez.**

Bu kitap BIST swing trading kitabından **tamamen ayrıdır**: farklı para birimi (USD),
farklı strateji (satış-yapmadan-biriktir + periyodik rebalance), farklı veri kaynağı
(ABD borsaları). Mevcut `swing_trades` / `portfolio_holdings` tablolarına dokunmaz.

## Portföy Modeli (kullanıcı hedefi)

Core/satellite yapı, hedef ağırlıklar toplam yatırılan sermayenin (nakit hariç) yüzdesi:

| Grup      | ETF  | Borsa  | Hedef | Tema         |
|-----------|------|--------|-------|--------------|
| Core      | VOO  | AMEX   | %28   | S&P 500      |
| Core      | VXUS | NASDAQ | %12   | Ex-US        |
| Satellite | QTUM | NASDAQ | %20   | Kuantum/AI   |
| Satellite | FIW  | AMEX   | %20   | Su           |
| Satellite | XLE  | AMEX   | %20   | Enerji       |

Core %40 (kendi içinde ~70/30 VOO/VXUS), Satellite %60 (üç eşit %20 bacak).
Şu an core hedefin altında (birikim aşaması).

## Gereksinimler

1. Hedef ağırlıklar config'de tanımlı (hardcode yok).
2. Güncel fiyatlarla gerçek ağırlıklar + hedeften sapma (drift, yüzde puan).
3. Rebalance kuralı: bir bacak ±5 puan saparsa uyarı; ayrıca çeyreklik kontrol hatırlatması.
4. DCA-to-target: aylık katkı tutarını girince, **satış yapmadan sadece alımla** hedefe en
   hızlı yaklaştıracak bölüm önerisi.
5. Mevcut katkı temposuyla core'un %40'a kaç ayda ulaşacağı tahmini.
6. Nakit/para piyasası ağırlık hesabının **dışında** (ayrı acil durum fonu).
7. Sadece uyarı/öneri; otomatik emir yok.
8. **(Ek)** Satış dahil tam-rebalance önerisi — DCA'dan ayrı bir mod.
9. USD bazlı; TRY gösterimi de olsun.

### Kapsam dışı (bu faz)

- **ETF holdings overlap (çakışma) analizi:** borsapy ABD ETF'lerinin içindeki hisseleri
  veremiyor (yalnızca BIST endeks bileşenleri ve TEFAS fon holdingleri var). Ayrı veri
  kaynağı gerektirir → sonraki faz.
- Otomatik emir gönderimi, vergi-lot takibi, USD/TRY dışı döviz, tarihsel tahsis
  snapshot grafiği.

## Mimari

Katmanlar mevcut proje desenlerini birebir izler.

```
config.toml [allocation]  ──► config.py: AllocationConfig / AllocationTarget
                                    │
core/etf_prices.py  ── TradingViewProvider.get_quote(sym, exchange) + bp.FX("USD")
   (TTL+LRU cache, price_cache.py deseni)     │
                                    ▼
db: allocation_holdings, allocation_reviews  ──► repository CRUD
                                    │
                                    ▼
core/allocation.py  (saf fonksiyonlar, dataclass tabanlı)
   compute_weights · check_rebalance · plan_dca · plan_rebalance
   · estimate_months_to_core_target
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
   web/routers/allocation.py   bot/telegram.py     main.py scheduler
   + templates/allocation.html  notify_*()          günlük drift/vade job
```

### Veri kaynağı — ETF fiyatları

Mevcut `web/price_cache.py` `bp.Ticker` kullanır ve BIST'e sabittir (TradingView
`BIST:` öneki). ABD ETF'leri için düşük seviye sağlayıcı kullanılır:

- **ETF spot fiyat:** `borsapy._providers.tradingview.get_tradingview_provider().get_quote(symbol, exchange=...)` → USD `last`. Canlı doğrulandı (VOO/VXUS/QTUM/FIW/XLE hepsi çalışıyor, 2026-07-24).
- **USDTRY:** `bp.FX("USD")` → `last`. Canlı doğrulandı (47.29).
- Yeni `core/etf_prices.py`: `price_cache.py` desenini (TTL + LRU + `ThreadPoolExecutor` paralel fetch, thread-safe) kopyalar; sembol→exchange eşlemesi config'den gelir. USDTRY için ayrı TTL'li tek-değer cache.

## Bileşenler

### 1. `config.toml` — `[allocation]` bölümü

```toml
[allocation]
enabled = true
base_currency = "USD"
monthly_contribution_usd = 500      # DCA varsayılanı (UI'da override)
drift_threshold_pct = 5.0           # ±5 puan uyarı eşiği
review_interval_days = 91           # çeyreklik hatırlatma
fractional = true                   # kesirli lot alım

[allocation.targets.VOO]
weight = 28
exchange = "AMEX"
group = "core"
note = "S&P 500"
# ... VXUS, QTUM, FIW, XLE benzer
```

Yükleme sırasında ağırlıklar toplamı 100'den saparsa log uyarısı (normalize edilmez,
sadece uyarılır — kullanıcı bilinçli karar versin).

### 2. `config.py` — dataclass'lar

- `AllocationTarget(symbol, weight, exchange, group, note)`
- `AllocationConfig(enabled, base_currency, monthly_contribution_usd,
  drift_threshold_pct, review_interval_days, fractional, targets: dict[str, AllocationTarget])`
- `load_config()` içinde `[allocation]` parse edilir, `Config`'e eklenir.

### 3. `db/schema.py` — yeni tablolar

```sql
CREATE TABLE IF NOT EXISTS allocation_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,        -- kesirli lot destekli
    cost_per_share REAL,                    -- USD ort. maliyet (opsiyonel)
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS allocation_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewed_at TEXT DEFAULT (datetime('now')),
    note TEXT
);

CREATE TABLE IF NOT EXISTS allocation_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
-- key='last_contribution_usd' → değişken aylık katkının son girilen değeri;
-- ekran açılışında config varsayılanı yerine bu gelir.
```

Nakit tabloda tutulmaz → ağırlık hesabının tamamen dışında.

### 4. `db/repository.py` — CRUD

- `upsert_allocation_holding(symbol, exchange, shares, cost_per_share, notes)` — `ON CONFLICT(symbol) DO UPDATE`.
- `get_allocation_holdings() -> list[dict]`
- `delete_allocation_holding(symbol)`
- `log_allocation_review(note=None)`
- `get_last_allocation_review() -> dict | None`
- `get_allocation_setting(key, default=None)` / `set_allocation_setting(key, value)` —
  değişken aylık katkı hafızası (`last_contribution_usd`) için `ON CONFLICT(key)` upsert.

Tüm method'lar `dict` döndürür (Row → dict), raw SQL.

### 5. `core/allocation.py` — iş mantığı (saf fonksiyonlar)

Dataclass'lar:

- `AllocationLeg(symbol, exchange, group, target_pct, shares, price_usd, value_usd, weight_pct, drift_pct, price_stale)`
- `AllocationReport(legs, total_value_usd, core_weight_pct, satellite_weight_pct, usdtry)`
- `RebalanceAlert(drifted_legs, review_due, next_review_date)`
- `DcaItem(symbol, buy_usd, buy_shares)` ; `DcaPlan(items, deployed_usd, leftover_usd)`
- `RebalanceItem(symbol, action, amount_usd, shares)` (action: BUY/SELL/HOLD) ; `RebalancePlan(items, net_cash_usd)`
- `TargetEta(months, target_date, assumption_note)`

Fonksiyonlar:

- **`compute_weights(holdings, prices, targets) -> AllocationReport`**
  Her bacak: `value_usd = shares * price_usd`; `weight_pct = value/total*100`;
  `drift_pct = weight_pct - target_pct`. Fiyatı çekilemeyen bacak `price_stale=True`,
  toplamdan ve drift'ten dışlanır (çökme yok).

- **`check_rebalance(report, threshold, last_review, interval_days) -> RebalanceAlert`**
  `|drift| >= threshold` bacakları listeler; `next_review = last_review + interval`,
  `review_due = now >= next_review` (veya hiç review yoksa due).

- **`plan_dca(report, contribution_usd, targets, fractional) -> DcaPlan`** — **ALIM-ONLY.**
  Water-filling: her bacağın `value/target` oranı hesaplanır; para en düşük orandan
  başlayarak, o bacağı bir sonraki en düşük orana eşitleyene kadar dökülür; bütçe
  bitene dek tekrar. Hedefin üstündeki bacaklar 0 alır → **satış yok**.
  `fractional=false` ise adetler tam sayıya yuvarlanır, artık `leftover_usd`.

- **`plan_rebalance(report, contribution_usd, targets, fractional) -> RebalancePlan`** —
  **SAT + AL.** Katkı + satış birlikte. `T' = total + contribution`;
  her bacak `delta_i = target_i * T' - value_i`. `delta>0` → BUY, `delta<0` → SELL,
  ~0 → HOLD. Matematiksel olarak `Σdelta = contribution` (satışlar alımları + katkıyı
  finanse eder, portföy tam hedefe oturur). `net_cash_usd = contribution`.

- **`estimate_months_to_core_target(report, contribution_usd, targets, fractional,
  target_core_pct=40) -> TargetEta`**
  Fiyatlar sabit varsayımıyla ileri simülasyon: her ay `plan_dca` uygulanır (alım-only
  tempo), core (VOO+VXUS) ağırlığı `>= target_core_pct` olana dek. `assumption_note`:
  "Fiyat hareketleri hariç, mevcut katkı temposuyla." Katkı 0 veya core zaten hedefteyse
  uygun mesaj.

Hepsi I/O'suz, network'süz, tam test edilebilir.

### 6. `web/routers/allocation.py` + `templates/allocation.html`

Route prefix `/allocation` (mevcut router deseni: `dependencies.templates/get_repo/get_config`).
Nav/başlık etiketi: "Allocation".

- `GET /allocation`: holdingleri çek → ETF fiyatları + USDTRY → `compute_weights` →
  `check_rebalance` → aylık katkıyla `plan_dca` + `plan_rebalance` + ETA. Katkı değeri:
  `get_allocation_setting('last_contribution_usd')` varsa o, yoksa config varsayılanı.
- `POST /allocation/holding`: holding upsert (symbol/shares/cost/notes) → redirect.
- `POST /allocation/holding/delete`: sil.
- `POST /allocation/dca`: kullanıcının girdiği aylık tutarla DCA + rebalance + ETA yeniden
  hesapla; tutarı `set_allocation_setting('last_contribution_usd', ...)` ile hatırla.
- `POST /allocation/review`: `log_allocation_review` → çeyreklik hatırlatmayı sıfırla.

Şablon (`base.html` + mevcut tasarım sistemi, nötr koyu tema):
- Hedef vs Gerçek vs Drift tablosu; drift `>= threshold` kırmızı rozet, yakın sarı.
- Core/Satellite gruplu görsel (bar). Her satır USD + **TRY sütunu** (USDTRY ile).
- DCA öneri kartı: aylık tutar input → alım-only bölüm tablosu (canlı yeniden hesap).
- Tam-rebalance kartı: SAT/AL/TUT aksiyon tablosu.
- ETA satırı: "≈ N ay (yaklaşık AY YIL)" + varsayım notu.
- Çeyreklik hatırlatma banner'ı + "Kontrol yapıldı" butonu.
- Aylık katkı input'u: son girilen değer (DB) ön-dolu gelir; değişince DCA + rebalance +
  ETA yeniden hesaplanır ve değer hatırlanır.
- Manuel holding giriş/güncelleme formu; fiyat çekilemeyen bacak "—" + uyarı.
- Sembol linkleri / navigasyon tüm sayfalarla tutarlı; nav'a `/allocation` ("Allocation")
  eklenir (mobil fixed bottom tab bar + desktop).

### 7. `bot/telegram.py` — bildirim (sadece)

- `notify_allocation_drift(drifted_legs)` — drift eden bacaklar + öneri özeti.
- `notify_allocation_review_due(next_date)` — çeyreklik hatırlatma.
- HTML parse mode, `_run_async()` deseni. Emir yok.

### 8. `main.py` — scheduler job

- Günlük `CronTrigger` (Europe/Istanbul, örn. 17:00): ETF fiyatları + USDTRY çek →
  `compute_weights` + `check_rebalance` → drift ≥ eşik veya review due ise Telegram.
- `allocation.enabled=false` ise job eklenmez.

## Veri Akışı

`allocation_holdings` (DB, manuel giriş) + canlı ETF/USDTRY fiyatları → `core/allocation`
saf fonksiyonları → `AllocationReport`/planlar → router (şablon) **ve** scheduler
(Telegram). Web ve bot aynı çekirdek fonksiyonları kullanır.

## Hata Yönetimi

- Bir ETF fiyatı çekilemezse: `price_stale=True`, "—" gösterilir, drift/toplamdan dışlanır,
  log'a warn (mevcut "Fiyat alinamadi" deseni). Modül çökmez.
- USDTRY çekilemezse: TRY sütunu "—", USD normal çalışır.
- Boş holdings: dostça boş durum + "ETF ekle" çağrısı.
- Config hedef toplamı ≠ 100: load'da warn.
- Katkı 0: DCA/rebalance boş plan; ETA uygun mesaj.

## Test (`tests/test_allocation.py`)

- `compute_weights`: bilinen shares/price → beklenen weight/drift.
- `plan_dca`: water-fill doğruluğu; underweight core'a öncelik; alım-only (overweight 0);
  kesirli vs tam-lot yuvarlama + leftover.
- `plan_rebalance`: `Σdelta == contribution`; overweight SELL, underweight BUY; tam hedef.
- `estimate_months_to_core_target`: sabit fiyat sim, doğru ay sayısı; katkı 0 / zaten hedef.
- `check_rebalance`: eşik sınırları; review-due tarih mantığı.
- Repository: in-memory SQLite (`:memory:`) upsert/get/delete/review.
- ETF fiyatları mock'lanır (network yok).

## Uygulama Sırası (CLAUDE.md "Yeni Özellik Eklerken" deseni)

1. `config.toml` + `config.py` dataclass'lar.
2. `db/schema.py` tablolar + `repository.py` CRUD.
3. `core/etf_prices.py` fiyat katmanı.
4. `core/allocation.py` saf fonksiyonlar + dataclass'lar (+ testler, TDD).
5. `web/routers/allocation.py` + `templates/allocation.html` + nav.
6. `bot/telegram.py` notify fonksiyonları.
7. `main.py` scheduler job.
8. Manuel doğrulama (web preview) + testler yeşil.
