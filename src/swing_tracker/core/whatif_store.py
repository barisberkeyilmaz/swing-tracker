"""Kalici what-if islemleri: whatif_trades satirlarini gunluk ilerleten katman.

Sayfa hicbir simulasyon yapmaz; sinyal dusunce scanner 'pending' satir ekler,
gunluk job (fill_pending -> update_open -> refresh_buyhold -> expire_stale)
hissenin yolunu DB'de yasatir. OHLCV parametreyle enjekte edilir (network yok).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime

import pandas as pd

from swing_tracker.backtest.exits import check_exits
from swing_tracker.backtest.models import BacktestConfig, BacktestTrade
from swing_tracker.core.whatif import VIRTUAL_SHARES, atr_from_daily, find_entry
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

OhlcvMap = dict[str, "pd.DataFrame | None"]


def row_to_bt(row: dict) -> BacktestTrade:
    """Open satirin durum alanlarindan BacktestTrade kur (incremental replay icin)."""
    remaining = row["remaining_shares"]
    if not remaining or remaining <= 0:
        # BacktestTrade.__post_init__ remaining_shares=0'i sessizce shares'e
        # geri doldurur — bozuk 'open' satiri tam pozisyon olarak diriltmek
        # yerine yuksek sesle patla.
        raise ValueError(
            f"row_to_bt: {row['symbol']} acik satirda remaining_shares={remaining}"
        )
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
        remaining_shares=remaining,
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
