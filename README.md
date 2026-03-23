# Swing Tracker

BIST ve US borsalarinda teknik analiz tabanli swing trading sinyal sistemi. Otomatik tarama yapar, skor bazli giris sinyalleri uretir, pozisyon takibi yapar ve Telegram uzerinden bildirim gonderir.

## Backtest Sonuclari

BIST, 15 hisse, 2024-2025 donemi, 100.000 TL baslangic:

| Metrik | Sonuc |
|--------|-------|
| Toplam Trade | 35 |
| Win Rate | %71.4 |
| Toplam Getiri | +%38.5 |
| Max Drawdown | %8.4 |
| Ort. Pozisyon Suresi | 66 gun |

## Strateji

### Giris Kosullari (Skor Bazli)

Zorunlu filtreler:
- Fiyat > SMA 50 (trend)
- Endeks > SMA 50 (piyasa rejimi — ayi piyasasinda trade yok)

Skor sinyalleri (min. 5 puan gerekli):

| Sinyal | Kosul | Puan |
|--------|-------|------|
| RSI Pullback | Gunluk RSI < 45 | +2 |
| MACD Negatif | MACD < Signal | +1 |
| Bollinger Band | Fiyat alt banda yakin (<%3) | +2 |
| RSI Donus | Saatlik RSI 40 alti → ustu | +2 |
| Hacim | Hacim > 20 gunluk ortalama | +1 |

### Cikis Kurallari

- **Stop Loss:** ATR x 1.5
- **TP1 (%50):** ATR x 1.5
- **TP2 (%30):** ATR x 3.0
- **Trailing Stop (%20):** TP1 sonrasi aktif

## Kurulum

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`.env` dosyasini olustur:
```bash
cp .env.example .env
# TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID degerlerini gir
```

## Kullanim

### Canli Sistem

```bash
python -m swing_tracker.main
```

Pzt-Cum BIST saatlerinde otomatik calisir:
- **Quick Scan:** Her 30 dk (10:00-17:30)
- **Deep Scan:** 18:30 (XU100 tam tarama)
- **Pozisyon Takip:** Her 5 dk
- **Gunluk Snapshot:** 18:45

### Backtest

```bash
# Varsayilan parametrelerle
python -m swing_tracker.backtest

# Sembol ve tarih belirt
python -m swing_tracker.backtest --symbols THYAO ASELS GARAN --start 2024-01-01 --end 2025-12-31

# Parametre override
python -m swing_tracker.backtest --param min_entry_score=4 --param sl_atr_mult=2.0

# Parametre karsilastirmasi
python -m swing_tracker.backtest --compare

# Detayli log
python -m swing_tracker.backtest -v
```

### US Borsasi (Backtest)

```python
from swing_tracker.backtest.engine import run_backtest
from swing_tracker.backtest.models import BacktestConfig

config = BacktestConfig(
    symbols=["AAPL", "MSFT", "GOOGL", "NVDA", "JPM"],
    market="us",
    commission_pct=0,
    commission_fixed=1.5,
    market_index="^GSPC",
)
result = run_backtest(config)
```

## Proje Yapisi

```
swing-tracker/
├── config.toml                  # Strateji parametreleri
├── src/swing_tracker/
│   ├── main.py                  # Entry point: APScheduler
│   ├── config.py                # TOML + .env yukleme
│   ├── db/                      # SQLite (WAL mode)
│   ├── core/
│   │   ├── signals.py           # Sinyal uretici (RSI, MACD, BB, Stochastic)
│   │   ├── scanner.py           # BIST tarama (quick + deep)
│   │   ├── monitor.py           # Pozisyon takip (TP/SL/trailing)
│   │   └── portfolio.py         # Portfoy yonetimi
│   ├── bot/
│   │   └── telegram.py          # Telegram bildirimleri
│   └── backtest/
│       ├── engine.py            # Simulasyon dongusu
│       ├── data.py              # Multi-market veri (borsapy + yfinance)
│       ├── exits.py             # TP/SL/trailing stop kurallari
│       ├── metrics.py           # Performans metrikleri
│       ├── models.py            # Dataclass'lar
│       └── runner.py            # CLI entry point
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
| Test | Pytest |

## Yol Haritas

- [x] Faz 1: Sinyal motoru + Telegram bildirim + backtest engine
- [ ] Faz 2: Interaktif Telegram komutlari (/portfoy, /scan, /roi)
- [ ] Faz 3: Web dashboard

## Lisans

MIT
