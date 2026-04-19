# Swing Tracker

Kisisel swing trading sinyal sistemi. BIST hisselerini teknik analizle tarar, skor bazli giris sinyalleri uretir, ATR tabanli TP/SL ile pozisyon takibi yapar. Web arayuzunden manuel alim/satim ve portfoy yonetimi, Telegram'dan gercek zamanli bildirim.

## Ozetle

- **Sinyal motoru**: XU100 rejim filtresi + coklu zaman dilimi (gunluk + saatlik) skor analizi. Skor >= 5 alim sinyali uretir.
- **Otomatik TP/SL**: ATR-14 ile Stop Loss ve 3 kademeli Take Profit (TP1/TP2/TP3) hesaplanir. Manuel alimda da sembol girince tek tikla doldurur.
- **Web dashboard**: FastAPI + HTMX + Tailwind. Canli fiyat, acik pozisyonlarda Acik K/Z, hedeflere ilerleme cubugu, nakit akisi, sembol detay sayfasi.
- **Telegram**: Yeni sinyal + TP/SL tetiklenmesi + gunluk rapor push bildirimleri. Interaktif komutlar da mevcut.
- **Backtest**: BIST (borsapy) ve US (yfinance) icin; piyasa filtreli/filtresiz, parametre karsilastirma grid'i.

## Kurulum

```bash
python3.11 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install yfinance pyarrow    # US borsa + cache icin (opsiyonel)
```

`.env` olustur:
```bash
cp .env.example .env
```

Degiskenler:
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` — Telegram bildirimi icin. Bos birakilirsa bildirim devre disi.
- `WEB_PASSWORD` — web arayuzunu sifreyle korur. Bos birakilirsa auth devre disi (yalniz yerel agda onerilir).
- `WEB_SECRET_KEY` — cookie imza anahtari. Bos olursa restart sonrasi oturumlar sifirlanir.

## Calistirma

Iki ayri process:

```bash
# Scheduler + Telegram bot
python -m swing_tracker.main

# Web dashboard (http://localhost:8000)
swing-tracker-web
```

Scheduler, BIST saatlerinde (Pzt–Cum) otomatik calisir:
- **Quick Scan** — her 30 dk, prefiltre + skor analizi
- **Deep Scan** — 18:30, XU100 tam tarama + gunluk rapor
- **Pozisyon Takip** — her 5 dk, TP/SL/trailing stop kontrol
- **Gunluk Snapshot** — 18:45

Windows servisi (nssm):
```bash
nssm install SwingTracker ".venv\Scripts\python.exe" "-m swing_tracker.main"
nssm install SwingTrackerWeb ".venv\Scripts\swing-tracker-web.exe"
```

## Strateji

### Giris (skor >= 5)

| Sinyal | Kosul | Puan |
|--------|-------|------|
| RSI Pullback | Gunluk RSI < 45 | +2 |
| MACD | MACD < Signal (momentum yavasladi) | +1 |
| Bollinger Band | Fiyat alt banda yakin (<%3) | +2 |
| RSI Donus | Saatlik RSI 40 alti → ustu (donus) | +2 |
| Hacim | Hacim > 20 gunluk ortalama | +1 |

Zorunlu filtreler:
- Hisse fiyati > SMA 50 (bireysel trend)
- Endeks > SMA 100 (piyasa rejimi)

### Cikis (ATR tabanli, config'den ayarlanabilir)

| Kademe | Seviye | Lot | Aciklama |
|--------|--------|-----|----------|
| Stop Loss | Giris - ATR × 2.5 | — | Tamami |
| TP1 | Giris + ATR × 2.0 | %50 | Ilk hedef |
| TP2 | Giris + ATR × 3.0 | %30 | Ikinci hedef |
| TP3 | Giris + ATR × 4.5 | %20 | Son hedef (opsiyonel) |

Trailing stop opsiyonel — kalan lot icin zirveden geri cekilme esigi.

## Backtest Sonuclari

BIST, 15 hisse, 2024–2025, 100.000 TL baslangic:

| Metrik | Filtresiz | Piyasa Filtreli (SMA 100) |
|--------|-----------|---------------------------|
| Toplam Trade | 42 | 35 |
| Win Rate | %71 | %71 |
| Toplam Getiri | +%42 | +%38.5 |
| Max Drawdown | %8.3 | %8.4 |
| Ort. Pozisyon | 62 gun | 66 gun |

US (S&P 500, 15 hisse, $50K):

| Metrik | Sonuc |
|--------|-------|
| Toplam Trade | 21 |
| Win Rate | %67 |
| Toplam Getiri | +%15.5 |
| Max Drawdown | %4.5 |

## Backtest Calistirma

```bash
# Varsayilan (config.toml'dan)
python -m swing_tracker.backtest

# Ozel parametreler
python -m swing_tracker.backtest --symbols THYAO ASELS GARAN --start 2024-01-01 -v

# Parametre karsilastirmasi (RSI threshold × SL multiplier grid)
python -m swing_tracker.backtest --compare
```

US borsa icin:
```python
from swing_tracker.backtest.engine import run_backtest
from swing_tracker.backtest.models import BacktestConfig

result = run_backtest(BacktestConfig(
    symbols=["AAPL", "MSFT", "GOOGL", "NVDA", "JPM"],
    market="us",
    commission_fixed=1.5,
    market_index="^GSPC",
))
```

## Telegram Komutlari

Bildirimler otomatik gonderilir: yeni sinyal, TP/SL tetigi, gunluk rapor. Interaktif komutlar da mevcut:

| Komut | Aciklama |
|-------|----------|
| `/durum` | Piyasa rejimi, acik pozisyon sayisi, toplam PnL |
| `/portfoy` | Yatirilan, guncel deger, PnL, win rate |
| `/pozisyon` | Acik pozisyonlar (gruplanmis), canli fiyat, TP/SL, aksiyon onerileri |
| `/sinyal` | Son 10 sinyal |
| `/scan` | Manuel tarama baslat |
| `/yakin` | Sinyale yakin adaylar (skor 1-4) |
| `/al THYAO 315 100` | Alim kaydet (ATR ile TP/SL) |
| `/sat 3 50 328` | Kismi satis |
| `/yardim` | Komut listesi |

## Proje Yapisi

```
swing-tracker/
├── config.toml                     # Strateji parametreleri, scanner ayarlari
├── src/swing_tracker/
│   ├── main.py                     # APScheduler + Telegram bot entry
│   ├── config.py                   # TOML + .env yukleme
│   ├── core/
│   │   ├── signals.py              # Sinyal uretici, TradeSetup, ATR hesaplari
│   │   ├── scanner.py              # Skor bazli multi-TF tarama (quick + deep)
│   │   ├── monitor.py              # TP/SL/trailing stop takibi
│   │   ├── portfolio.py            # Portfoy yonetimi, snapshot
│   │   └── strategy.py             # config → strateji params
│   ├── bot/
│   │   └── telegram.py             # Bildirim + interaktif komutlar
│   ├── web/
│   │   ├── app.py                  # FastAPI app + auth middleware
│   │   ├── auth.py                 # Cookie tabanli sifre auth
│   │   ├── dependencies.py         # Shared state (repo, config)
│   │   ├── helpers.py              # Sermaye ozeti, nakit akisi
│   │   ├── auto_setup.py           # ATR tabanli SL/TP hesaplayici (cache'li)
│   │   ├── regime_cache.py         # Piyasa rejim (Boga/Ayi) cache
│   │   ├── price_cache.py          # Canli fiyat cache (60s TTL)
│   │   ├── indicator_cache.py      # Indikator cache (sembol sayfasi)
│   │   ├── routers/                # dashboard, portfolio, signals, trades, symbol
│   │   ├── templates/              # Jinja2 + HTMX
│   │   └── static/                 # CSS
│   ├── backtest/
│   │   ├── engine.py               # Simulasyon dongusu
│   │   ├── data.py                 # BIST (borsapy) + US (yfinance)
│   │   ├── exits.py                # Cikis kurallari
│   │   ├── metrics.py              # Win rate, drawdown, profit factor
│   │   └── runner.py               # CLI entry
│   └── db/
│       ├── schema.py               # Tablo DDL'leri
│       ├── connection.py           # SQLite WAL mode
│       └── repository.py           # Raw SQL CRUD
└── tests/
```

## Teknoloji

| Katman | Teknoloji |
|--------|-----------|
| Dil | Python 3.11+ |
| Web | FastAPI, Jinja2, HTMX, Tailwind CSS (CDN) |
| BIST Veri | [borsapy](https://github.com/saidsurucu/borsapy) |
| US Veri | yfinance |
| DB | SQLite (WAL mode, raw SQL) |
| Zamanlama | APScheduler 3.x |
| Bildirim | python-telegram-bot 21+ |
| Lint | Ruff |
| Test | Pytest |

## Yol Haritasi

- [x] Sinyal motoru + Telegram bildirim
- [x] Backtest engine (multi-TF + daily-only, BIST + US)
- [x] Piyasa filtresi (endeks > SMA 100)
- [x] Interaktif Telegram komutlari
- [x] Pozisyon takip + TP/SL/trailing stop bildirimleri
- [x] Web dashboard (FastAPI + HTMX)
- [x] Canli fiyat, Acik K/Z, piyasa rejim rozeti
- [x] Trade detay: canli durum, hedef uzakligi, dinamik ilerleme cubugu
- [x] Manuel/sinyal alim modalinda ATR otomatik TP/SL
- [ ] Test suite
- [ ] Trade gecmisi performans raporu (aylik/yillik)

## Lisans

MIT — bkz. [LICENSE](LICENSE)
