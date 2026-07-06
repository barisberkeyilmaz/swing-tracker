# Deploy — Homelab (Docker VM)

Swing Tracker, homelab'deki Ubuntu Docker VM'inde (`docker-host`, `192.168.1.202`, Proxmox VMID 101) iki container olarak calisir:

| Container | Gorev | Port |
|-----------|-------|------|
| `swing-tracker` | Scheduler: tarama + Telegram sinyalleri | - |
| `swing-tracker-web` | Web dashboard (FastAPI) | `8000` |

Erisim: evde `http://192.168.1.202:8000`, disarida Tailscale subnet route uzerinden ayni adres.

## Sunucudaki yerlesim

```
~/swing-tracker/          # rsync ile Mac'ten gelen repo kopyasi
├── .env                  # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEB_PASSWORD, WEB_SECRET_KEY
├── data/                 # SQLite DB (volume, kalici)
├── logs/                 # log dosyalari (volume, kalici)
└── docker-compose.yml
```

`.env` sadece sunucuda durur, rsync `--exclude` ile korunur; repoya girmez.

## Guncelleme (Mac'ten)

```bash
cd ~/Projects/swing-tracker
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '.env' \
  --exclude 'data' --exclude 'logs' --exclude '__pycache__' \
  ./ berke@192.168.1.202:~/swing-tracker/
ssh berke@192.168.1.202 'cd ~/swing-tracker && docker compose up -d --build'
```

## Sunucuda isletme

```bash
ssh berke@192.168.1.202
cd ~/swing-tracker
docker compose ps                 # durum
docker compose logs -f tracker    # scheduler loglari
docker compose logs -f web        # web loglari
docker compose restart            # yeniden baslat
docker compose down && docker compose up -d --build   # tam rebuild
```

DB yedegi:

```bash
ssh berke@192.168.1.202 'sqlite3 ~/swing-tracker/data/swing_tracker.db ".backup /tmp/swing_backup.db"'
scp berke@192.168.1.202:/tmp/swing_backup.db ./
```

(VM'in tamami ayrica Proxmox vzdump job'i ile haftalik yedeklenir.)

## Izleme

Uptime Kuma (`http://192.168.1.202:3001`) web dashboard'un `/login` sayfasini izler; dusunce Telegram bildirimi icin Kuma'da notification ayarlanabilir.

## Notlar

- `TZ=Europe/Istanbul` Dockerfile'da sabit — BIST saatli cron job'lari icin kritik.
- `config.toml` read-only volume: strateji parametresi degisikligi icin rebuild gerekmez, `docker compose restart` yeter.
- Eski Windows deploy (nssm) kullanim disi; Windows PC'de servis hala calisiyorsa `nssm stop SwingTracker` + `nssm remove SwingTracker confirm` ile kaldirilmali (cift Telegram sinyalini onlemek icin).
