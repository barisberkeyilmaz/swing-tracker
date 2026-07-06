# swing-tracker

Kisisel swing trading sinyal sistemi. BIST hisselerini teknik analizle tarar, giris/cikis sinyalleri uretir, Telegram'dan bildirim gonderir.

## Proje Yapisi

```
swing-tracker/
├── config.toml                    # Strateji parametreleri, tarama ayarlari
├── .env                           # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
├── src/swing_tracker/
│   ├── main.py                    # Entry point: APScheduler + graceful shutdown
│   ├── config.py                  # TOML + .env yukleme, dataclass'lar
│   ├── db/
│   │   ├── schema.py              # SQLite CREATE TABLE DDL'leri
│   │   ├── connection.py          # SQLite baglanti (WAL mode)
│   │   └── repository.py          # CRUD islemleri (tum tablolar)
│   ├── core/
│   │   ├── signals.py             # Sinyal uretici: detect_buy/sell_signals, calculate_score, build_trade_setup
│   │   ├── scanner.py             # BIST tarama: quick_scan (30dk), deep_scan (gunluk)
│   │   ├── monitor.py             # Pozisyon takip: TP/SL/trailing stop kontrolu
│   │   ├── portfolio.py           # Portfoy yonetimi: nakit, pozisyon boyutlandirma, snapshot
│   │   └── strategy.py            # config.toml'dan strateji yukleme
│   └── bot/
│       └── telegram.py            # Bildirim: sinyal, alert, gunluk rapor
└── tests/
```

## Calistirma

```bash
# Gelistirme (Mac)
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m swing_tracker.main

# Uretim (homelab Docker VM: berke@192.168.1.202, detay: DEPLOY.md)
rsync -az --delete --exclude '.git' --exclude '.venv' --exclude '.env' \
  --exclude 'data' --exclude 'logs' --exclude '__pycache__' \
  ./ berke@192.168.1.202:~/swing-tracker/
ssh berke@192.168.1.202 'cd ~/swing-tracker && docker compose up -d --build'
```

## Teknoloji

| Katman | Teknoloji |
|--------|-----------|
| Dil | Python 3.11+ |
| Veri kaynagi | borsapy >= 0.8.3 (sadece veri icin, sinyal mantigi burada) |
| DB | SQLite (WAL mode, pathlib tabanli) |
| Zamanlama | APScheduler 3.x (BackgroundScheduler) |
| Bildirim | python-telegram-bot 21+ |
| Lint | Ruff (line-length=100, target py311) |
| Test | Pytest |

## Kurallar

### Genel
- borsapy sadece **veri kaynagi** olarak kullanilir (fiyat, OHLCV, indikatör). Sinyal mantigi, skor, TP/SL, strateji bu projede yazilir.
- Cross-platform kod yaz: `pathlib.Path`, `zoneinfo`, `signal.SIGBREAK` (Windows).
- Turkce UI metinleri (Telegram mesajlari, log aciklamalari).
- Hardcoded secret olmasin, `.env` kullan.

### Sinyal Sistemi (`core/signals.py`)
- Tum veri yapilari **dataclass** tabanli: `Signal`, `TradeSetup`, `PriceLevel`, `AnalysisResult`.
- Fonksiyonlar pure function olarak yazilir (state tutmaz), test edilebilir olmali.
- `analyze_symbol()` tek entry point: sembol ver, tam analiz al.
- Yeni indikatör/sinyal eklerken: `detect_buy_signals()` veya `detect_sell_signals()` icine ekle, skor agirligini `calculate_score()` icinde tanimla.
- Skor -100 ile +100 arasinda, >= 30 long sinyal, <= -30 short sinyal.

### Veritabani (`db/`)
- ORM yok, raw SQL + `sqlite3.Row`.
- Schema degisikliklerinde `schema.py`'deki DDL'leri guncelle (CREATE IF NOT EXISTS pattern).
- Repository method'lari her zaman `dict` dondurur (Row -> dict).
- `ON CONFLICT` ile upsert kullan (holdings, snapshots).

### Zamanlama (`main.py`)
- BIST saatleri: Pzt-Cum 10:00-18:00 Istanbul zamani.
- `quick_scan`: Her 30 dk, piyasa acikken.
- `deep_scan`: 18:30'da, piyasa kapanisinda.
- `monitor_positions`: Her 5 dk, piyasa acikken.
- `daily_snapshot`: 18:45'te.
- Yeni zamanlanmis gorev eklerken `CronTrigger` kullan, timezone her zaman `Europe/Istanbul`.

### Telegram (`bot/telegram.py`)
- Faz 1: Sadece bildirim (notify_*). Interaktif komutlar Faz 2.
- HTML parse mode kullan (`ParseMode.HTML`).
- Async method'lar (`async def`), `_run_async()` helper ile sync context'ten cagir.

### Yeni Ozellik Eklerken
1. `config.toml`'a parametre ekle, `config.py`'de dataclass'a yansit.
2. `core/` altinda is mantigi yaz.
3. `repository.py`'de gerekli CRUD method'lari ekle.
4. `main.py`'de scheduler job'u ekle (gerekiyorsa).
5. `telegram.py`'de bildirim formati ekle.

### Test
- `tests/` altinda `test_<modul>.py` pattern'i.
- DB testlerinde in-memory SQLite kullan (`:memory:`).
- borsapy cagrilerini mock'la (network bagimliligi olmasin).

## Fazlar

- **Faz 1** (mevcut): Sinyal motoru + Telegram bildirim + portfoy takibi
- **Faz 2**: Interaktif Telegram komutlari (/portfoy, /swing, /roi, /al, /sat) + Claude MCP server
- **Faz 3**: Web dashboard (Streamlit veya benzeri)
