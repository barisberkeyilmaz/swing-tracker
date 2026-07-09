# What-If Sayfası — Design

**Tarih:** 2026-07-09
**Amaç:** Üretilen buy sinyallerini alsaydım şu anki performansım ne olurdu sorusunu cevaplayan yeni web sayfası.

## Problem

Sistem sinyal üretiyor ama sinyallerin gerçek hayattaki değeri ölçülmüyor. Ayrıca sinyaller 15 dk gecikmeli veriyle üretiliyor: sinyaldeki fiyat, kullanıcının gerçekte alabileceği fiyat değil. Sayfa bu gecikmeyi modelleyerek gerçekçi bir what-if simülasyonu sunar.

## Kapsam

- Sadece **buy** sinyalleri (BIST spot'ta short yok).
- Skor eşiği: `MIN_ENTRY_SCORE` (scanner ile aynı, şu an 4).
- İki mod yan yana: **strateji kurallı** (TP/SL/trailing) ve **al-tut**.
- İşlem bazında eşit ağırlıklı % getiri; sanal portföy / nakit kısıtı yok.
- Hesaplama sayfa isteğinde anlık yapılır; yeni DB tablosu ve scheduler job'u yok.

## Simülasyon çekirdeği — `core/whatif.py`

Pure function'lar, `signals.py` pattern'inde. Dataclass'lar: `WhatIfTrade`, `WhatIfStats`, `WhatIfResult`.

### Sinyal seçimi ve dedup

`signals_log`'dan `signal_type='buy'` ve `score >= MIN_ENTRY_SCORE` sinyaller kronolojik işlenir. Sembol bazında sanal pozisyon açıkken gelen yeni buy sinyalleri atlanır (atlanan sayısı raporlanır). Pozisyon strateji kurallarıyla kapanınca o sembole yeni sinyal tekrar pozisyon açabilir. Dedup, strateji modunun pozisyon durumuna göre yürür; al-tut sonuçları aynı işlem listesi üzerinden hesaplanır.

### Giriş fiyatı (15 dk gecikme düzeltmesi)

1. Sinyalin `created_at`'inden **sonraki ilk 1h bar'ın kapanışı** (`ohlcv_cache` üzerinden `get_ohlcv`).
2. O gün bar kalmadıysa: bir sonraki işlem gününün ilk 1h bar'ı.
3. 1h veri hiç yoksa: `price_at_signal` fallback.

Kullanılan kaynak (`gerçek` / `fallback`) işlemde işaretlenir ve UI'da gösterilir.

### TP/SL hesabı

Sinyal tarihine kadarki günlük bar'lardan ATR hesaplanır; config.toml'daki backtest çarpanları (`sl_atr_mult`, `tp1_atr_mult`, `tp2_atr_mult`) giriş fiyatına uygulanır. Backtest engine ile birebir aynı setup mantığı.

### Simülasyon

- **Strateji modu:** Giriş sonrası günlük bar'lar sırayla `backtest/exits.py::check_exits`'e verilir. Sanal 100 pay, komisyon 0 (% getiri ölçülür). Kapanan işlemde gerçekleşen P&L%; açık işlemde kalan paylar güncel fiyattan (`price_cache`) mark-to-market.
- **Al-tut modu:** `(güncel fiyat − giriş) / giriş × 100`.

### Hata toleransı

Sembol için OHLCV/güncel fiyat alınamazsa işlem "veri yok" işaretlenir, simülasyon devam eder. Hiç sinyal yoksa boş durum döner.

## İstatistikler — `WhatIfStats`

İki mod için ayrı ayrı (uygun olanlar):

- **Özet:** işlem sayısı (açık/kapalı), win-rate, ortalama getiri, toplam kümülatif getiri.
- **Dağılım:** medyan getiri, en iyi / en kötü işlem, profit factor (toplam kazanç ÷ toplam kayıp; hiç kayıp yoksa "∞" gösterilir), ortalama tutma süresi (gün, sadece kapanan işlemler).
- **Çıkış tipi dağılımı** (sadece strateji): TP1 / TP2 / trailing / SL / açık adetleri.
- **Skor dilimi performansı:** skor aralıklarına göre (4–5, 6–7, 8+) ortalama getiri + win-rate.
- **15 dk gecikme maliyeti:** `price_at_signal` ile gerçek giriş arasındaki ortalama fark (%).
- **Kümülatif getiri eğrisi:** kapanan işlemler kronolojik, eşit ağırlıklı; çizgi grafik verisi.

## Web katmanı

### Router — `web/routers/whatif.py`

- `GET /whatif` → skeleton'lı iskelet sayfa, hesaplama yok, anında açılır.
- `GET /whatif/results` → htmx fragment; simülasyonu koşturur, özet + istatistik + tablo döndürür.

### Template'ler

- `templates/whatif.html`: başlık + `hx-get="/whatif/results" hx-trigger="load"` skeleton.
- `templates/fragments/whatif_results.html`:
  1. Özet kartları (strateji / al-tut yan yana).
  2. İstatistik bölümü (dağılım, çıkış tipleri, skor dilimleri, gecikme maliyeti, kümülatif eğri grafiği).
  3. Sanal işlem tablosu: sembol (`/symbol/X` linki), sinyal tarihi (İstanbul saati), skor, giriş fiyatı (+fallback işareti), strateji P&L% + durum rozeti (açık/TP/SL/trailing), al-tut P&L%. Kâr yeşil, zarar kırmızı; mevcut tasarım sistemi.
  4. Alt not: dedup ile atlanan sinyal sayısı + 15 dk gecikme modellemesi açıklaması.

### Nav

Desktop üst nav ve mobil bottom tab bar'a "What-if" girişi (bottom bar 3→4 öğe).

## Test — `tests/test_whatif.py`

In-memory SQLite + sahte sinyaller + sahte OHLCV (borsapy mock, network yok):

- Dedup: açık pozisyonda yeni sinyal atlanır, kapanınca tekrar açılabilir.
- Giriş fiyatı seçimi: sonraki 1h bar / ertesi gün ilk bar / `price_at_signal` fallback.
- Strateji modu: TP1, TP2, trailing, SL senaryoları; açık pozisyon mark-to-market.
- Al-tut hesabı.
- İstatistikler: win-rate, profit factor, skor dilimleri, gecikme maliyeti.
- Boş sinyal listesi ve veri alınamayan sembol.

## Kararlar (soru-cevap özeti)

| Soru | Karar |
|------|-------|
| Simülasyon modeli | Strateji kurallı + al-tut, ikisi yan yana |
| Sinyal dedup | Pozisyon açıkken yeni sinyal yok sayılır |
| 15 dk giriş fiyatı | 1h bar'dan yaklaşık (cache'te mevcut) |
| Sunum | İşlem bazında eşit ağırlıklı % getiri |
| Short | Dahil değil |
| Hesaplama | Sayfa isteğinde anlık, yeni tablo/job yok |
