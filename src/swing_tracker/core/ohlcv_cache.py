"""SQLite-backed OHLCV cache with incremental refresh.

Scanner (and other data consumers) can call `get_ohlcv(...)` instead of
`bp.Ticker(sym).history(...)`. Cache:
- **Miss**: full `period` fetch, upsert bars, write meta.
- **Fresh** (meta.last_fetch_at within TTL): serve from DB, no network call.
- **Stale**: fetch a short tail window (5 days daily / 2 days hourly),
  upsert, update meta, serve from DB.

Thread-safety: shared `sqlite3.Connection` with WAL + `check_same_thread=False`
+ `busy_timeout=5000` → multiple readers + serialized writes. Idempotent upserts
make concurrent refreshes safe (last-writer-wins per bar).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import borsapy as bp
import pandas as pd

from swing_tracker.config import CacheConfig
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

FetchFn = Callable[[str, str, str], "pd.DataFrame | None"]

# Short tail windows used during stale refreshes.
_STALE_WINDOW = {
    "1d": "5d",
    "1h": "2d",
}

_OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _default_fetch(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """Default fetch: borsapy Ticker history."""
    try:
        return bp.Ticker(symbol).history(period=period, interval=interval)
    except Exception:
        logger.exception(f"{symbol}: borsapy fetch hatasi (period={period}, interval={interval})")
        return None


def _ttl_minutes(interval: str, cfg: CacheConfig) -> int:
    if interval == "1d":
        return cfg.daily_ttl_minutes
    if interval == "1h":
        return cfg.hourly_ttl_minutes
    return cfg.daily_ttl_minutes


def _df_to_bars(df: pd.DataFrame) -> list[dict]:
    """Convert borsapy DataFrame to cache rows. Assumes DatetimeIndex."""
    if df is None or df.empty:
        return []
    # Normalize index to naive UTC ISO strings for stable PK.
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        ts_strs = [t.isoformat() for t in idx]
    else:
        ts_strs = [str(t) for t in idx]

    bars: list[dict] = []
    for i, ts in enumerate(ts_strs):
        row = df.iloc[i]
        bars.append({
            "bar_ts": ts,
            "open": _safe_float(row.get("Open")),
            "high": _safe_float(row.get("High")),
            "low": _safe_float(row.get("Low")),
            "close": _safe_float(row.get("Close")),
            "volume": _safe_float(row.get("Volume")),
        })
    return bars


def _safe_float(v) -> float | None:
    if v is None or pd.isna(v):
        return None
    return float(v)


def _bars_to_df(rows: list[dict]) -> pd.DataFrame | None:
    """Reconstruct DataFrame with DatetimeIndex + OHLCV columns."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["bar_ts"] = pd.to_datetime(df["bar_ts"])
    df = df.set_index("bar_ts").sort_index()
    df = df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    })
    df.index.name = None
    return df[_OHLCV_COLUMNS]


def _write_df(
    symbol: str,
    interval: str,
    df: pd.DataFrame,
    repo: Repository,
    now: datetime,
) -> None:
    bars = _df_to_bars(df)
    if not bars:
        return
    repo.upsert_ohlcv_bars(symbol, interval, bars)
    repo.upsert_ohlcv_meta(
        symbol=symbol,
        interval=interval,
        last_fetch_at=now.isoformat(timespec="seconds"),
        last_bar_ts=bars[-1]["bar_ts"],
        bar_count=len(bars),
    )


def get_ohlcv(
    symbol: str,
    *,
    interval: str,
    period: str,
    repo: Repository,
    cache_cfg: CacheConfig,
    fetch_fn: FetchFn | None = None,
    now: datetime | None = None,
    ttl_override_minutes: int | None = None,
) -> pd.DataFrame | None:
    """Return OHLCV DataFrame for (symbol, interval), using cache when possible."""
    fn = fetch_fn or _default_fetch

    if not cache_cfg.enabled:
        return fn(symbol, period, interval)

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    ttl = ttl_override_minutes if ttl_override_minutes is not None else _ttl_minutes(interval, cache_cfg)
    meta = repo.get_ohlcv_meta(symbol, interval)

    if meta is None:
        df = fn(symbol, period, interval)
        if df is None or df.empty:
            return None
        _write_df(symbol, interval, df, repo, now)
        return df

    try:
        last_fetch = datetime.fromisoformat(meta["last_fetch_at"])
    except (ValueError, TypeError):
        last_fetch = datetime.min

    age_min = (now - last_fetch).total_seconds() / 60.0

    if age_min < ttl:
        cached = repo.get_cached_ohlcv(symbol, interval)
        df = _bars_to_df(cached)
        if df is not None and not df.empty:
            return df
        # Cache meta present but no bars — treat as miss.

    # Stale or empty cache rows: incremental refresh.
    tail = _STALE_WINDOW.get(interval, period)
    tail_df = fn(symbol, tail, interval)
    if tail_df is not None and not tail_df.empty:
        _write_df(symbol, interval, tail_df, repo, now)
    else:
        # Fetch failed — serve stale cache if we have anything.
        logger.warning(f"{symbol}/{interval}: stale refresh fetch bosu dondu, cache servis ediliyor")

    cached = repo.get_cached_ohlcv(symbol, interval)
    return _bars_to_df(cached)
