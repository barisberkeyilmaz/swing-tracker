# Swing Tracker

Kisisel swing trading sinyal sistemi. BIST ve US borsalarini teknik analizle tarar, skor bazli giris sinyalleri uretir, pozisyon takibi yapar ve Telegram uzerinden hem bildirim gonderir hem interaktif komutlarla yonetilir.

## Nasil Calisiyor

```
XU100 > SMA 100 mi?
├── Hayir → Ayi piyasasi, bekle
└── Evet → Hisseleri tara
                ├── Gunluk: RSI < 45? MACD < Signal? BB alt banda yakin?
                ├── Saatlik: RSI yukari donus yapti mi?
                └── Hacim ortalama ustu mu?
                        ├── Skor < 5 → Atla
                        └── Skor >= 5 → AL sinyali
                                ├── SL: ATR x 1.5
                                ├── TP1: ATR x 1.5 (%50 sat)
                                ├── TP2: ATR x 3.0 (%30 sat)
                                └── Trailing Stop (%20 kalan)
```

## Backtest Sonuclari

BIST, 15 hisse, 2024-2025, 100.000 TL baslangic:

| Metrik | Filtresiz | Piyasa Filtreli (SMA 100) |
|--------|-----------|---------------------------|
| Toplam Trade | 42 | 35 |
| Win Rate | %71 | %71 |
| Toplam Getiri | +%42 | +%38.5 |
| Max Drawdown | %8.3 | %8.4 |
| Ort. Pozisyon | 62 gun | 66 gun |

US borsa (S&P 500, 15 hisse, $50K):

| Metrik | Sonuc |
|--------|-------|
| Toplam Trade | 21 |
| Win Rate | %67 |
| Toplam Getiri | +%15.5 |
| Max Drawdown | %4.5 |

## Strateji

### Giris (Skor Bazli, min. 5 puan)

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

### Cikis

- **Stop Loss:** Giris - ATR x 1.5
- **TP1:** Giris + ATR x 1.5 → pozisyonun %50'sini sat
- **TP2:** Giris + ATR x 3.0 → pozisyonun %30'unu sat
- **Trailing Stop:** Kalan %20, zirve fiyattan %20 duserse cik

## Telegram Komutlari

| Komut | Aciklama |
|-------|----------|
| `/durum` | Piyasa rejimi, acik pozisyon, toplam PnL |
| `/portfoy` | Yatirilan, guncel deger, gerceklesmis/gerceklesmemis PnL, win rate |
| `/pozisyon` | Acik pozisyonlar (gruplanmis), canli fiyat, TP/SL seviyeleri, aksiyon onerileri |
| `/sinyal` | Son 10 sinyal |
| `/scan` | Manuel tarama baslat |
| `/al THYAO 315 100` | Alis kaydet — otomatik TP/SL hesaplar, takibe alir |
| `/sat 3 50 328` | Trade #3'ten 50 lot sat @ 328 TL |
| `/yardim` | Komut listesi |

## Kurulum

```bash
python3.11 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install yfinance pyarrow    # US borsa + cache icin
```

`.env` olustur:
```bash
cp .env.example .env
# TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID ekle
```

Baslat:
```bash
python -m swing_tracker.main
```

Pzt-Cum BIST saatlerinde otomatik calisir:
- **Quick Scan:** Her 30 dk — prefiltre + skor analizi
- **Deep Scan:** 18:30 — XU100 tam tarama
- **Pozisyon Takip:** Her 5 dk — TP/SL kontrol
- **Gunluk Rapor:** 18:45

Windows servis olarak:
```bash
nssm install SwingTracker ".venv\Scripts\python.exe" "-m swing_tracker.main"
```

## Backtest

```bash
# Varsayilan (config.toml'dan)
python -m swing_tracker.backtest

# Ozel parametreler
python -m swing_tracker.backtest --symbols THYAO ASELS GARAN --start 2024-01-01 -v

# Parametre karsilastirmasi (RSI threshold x SL multiplier grid)
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

## Proje Yapisi

```
swing-tracker/
├── config.toml                     # Strateji parametreleri
├── src/swing_tracker/
│   ├── main.py                     # APScheduler + graceful shutdown
│   ├── config.py                   # TOML + .env
│   ├── core/
│   │   ├── signals.py              # Sinyal uretici (680 satir)
│   │   ├── scanner.py              # Skor bazli multi-TF tarama
│   │   ├── monitor.py              # TP/SL/trailing stop takibi
│   │   └── portfolio.py            # Portfoy yonetimi
│   ├── bot/
│   │   └── telegram.py             # Bildirim + interaktif komutlar
│   ├── backtest/
│   │   ├── engine.py               # Multi-TF simulasyon dongusu
│   │   ├── data.py                 # BIST (borsapy) + US (yfinance)
│   │   ├── exits.py                # Cikis kurallari
│   │   ├── metrics.py              # Win rate, drawdown, profit factor
│   │   ├── models.py               # Dataclass'lar
│   │   └── runner.py               # CLI entry point
│   └── db/
│       ├── schema.py               # 6 tablo DDL
│       ├── connection.py           # SQLite WAL mode
│       └── repository.py           # Raw SQL CRUD
└── tests/
```

## Teknoloji

| Katman | Teknoloji |
|--------|-----------|
| Dil | Python 3.11+ |
| BIST Veri | [borsapy](https://github.com/saidsurucu/borsapy) |
| US Veri | yfinance |
| DB | SQLite (WAL mode) |
| Zamanlama | APScheduler 3.x |
| Bildirim | python-telegram-bot 21+ |
| Lint | Ruff |

## Yol Haritasi

- [x] Sinyal motoru + Telegram bildirim
- [x] Backtest engine (multi-TF + daily-only, BIST + US)
- [x] Piyasa filtresi (endeks > SMA 100)
- [x] Interaktif Telegram komutlari (al/sat/pozisyon/portfoy/scan)
- [x] Pozisyon takip + TP/SL/trailing stop bildirimleri
- [ ] Test suite
- [ ] Trade gecmisi ve performans raporu
- [ ] Web dashboard

## Lisans

MIT
