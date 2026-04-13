"""Symbol detail router — comprehensive stock profile page."""

from __future__ import annotations

import logging
from datetime import datetime

import borsapy as bp
import pandas as pd
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from swing_tracker.core.signals import _add_all_indicators, _get_indicators
from swing_tracker.web.dependencies import templates, get_repo, get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/symbol")


def _safe_get(info, key, default=None):
    """Safely get a value from ticker info, handling NaN and None."""
    try:
        val = info[key]
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return val
    except (KeyError, TypeError):
        return default


def _format_short(val: float) -> str:
    """Format large numbers for mobile display."""
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1_000_000_000:
        return f"{sign}{abs_val / 1_000_000_000:.1f} milyar"
    if abs_val >= 1_000_000:
        return f"{sign}{abs_val / 1_000_000:.1f} milyon"
    if abs_val >= 1_000:
        return f"{sign}{abs_val / 1_000:.0f} bin"
    return f"{sign}{abs_val:,.0f}"


def _format_market_cap(val: float | None) -> str:
    """Format market cap to human-readable Turkish format."""
    if val is None:
        return "-"
    if val >= 1_000_000_000_000:
        return f"{val / 1_000_000_000_000:.1f}T TL"
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B TL"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M TL"
    return f"{val:,.0f} TL"


def _technical_summary(df: pd.DataFrame) -> dict:
    """Build technical indicator summary from OHLCV data."""
    if df is None or len(df) < 50:
        return {}

    df = _add_all_indicators(df)
    ind = _get_indicators(df)
    last = df.iloc[-1]
    price = float(last["Close"])

    summary = {"price": price}

    # RSI
    rsi = ind.get("rsi_14") or ind.get("rsi")
    if rsi is not None:
        summary["rsi"] = round(rsi, 1)
        if rsi < 30:
            summary["rsi_label"] = "Asiri Satim"
        elif rsi < 45:
            summary["rsi_label"] = "Dusuk"
        elif rsi > 70:
            summary["rsi_label"] = "Asiri Alim"
        elif rsi > 55:
            summary["rsi_label"] = "Yuksek"
        else:
            summary["rsi_label"] = "Notr"

    # MACD
    macd = ind.get("macd")
    signal = ind.get("signal")
    if macd is not None and signal is not None:
        summary["macd"] = round(macd, 4)
        summary["macd_signal"] = round(signal, 4)
        summary["macd_label"] = "Pozitif" if macd > signal else "Negatif"

    # Stochastic
    stoch_k = ind.get("stoch_k")
    if stoch_k is not None:
        summary["stoch_k"] = round(stoch_k, 1)
        if stoch_k < 20:
            summary["stoch_label"] = "Asiri Satim"
        elif stoch_k > 80:
            summary["stoch_label"] = "Asiri Alim"
        else:
            summary["stoch_label"] = "Notr"

    # SMA positions
    for period in (50, 100, 200):
        sma_key = f"sma_{period}"
        sma_val = ind.get(sma_key)
        if sma_val is not None:
            summary[sma_key] = round(sma_val, 2)
            summary[f"sma_{period}_above"] = price > sma_val

    # Bollinger Band position
    bb_upper = ind.get("bb_upper")
    bb_lower = ind.get("bb_lower")
    if bb_upper is not None and bb_lower is not None:
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            bb_pos = (price - bb_lower) / bb_range
            summary["bb_position"] = round(bb_pos * 100, 1)
            if bb_pos < 0.2:
                summary["bb_label"] = "Alt Bant"
            elif bb_pos > 0.8:
                summary["bb_label"] = "Ust Bant"
            else:
                summary["bb_label"] = "Orta"

    return summary


@router.get("/{symbol}", response_class=HTMLResponse)
async def symbol_detail(request: Request, symbol: str):
    """Hisse detay sayfasi — fiyat, temel gostergeler, teknik gorunum, analist, ortaklik."""
    symbol = symbol.upper()
    repo = get_repo()
    config = get_config()

    try:
        ticker = bp.Ticker(symbol)
        info = ticker.info
        price = _safe_get(info, "last") or _safe_get(info, "close", 0)
    except Exception:
        logger.exception(f"{symbol}: Veri alinamadi")
        now = datetime.now(config.timezone)
        return templates.TemplateResponse(
            request,
            "symbol_detail.html",
            context={
                "symbol": symbol,
                "error": True,
                "market": {}, "fundamentals": {}, "technical": {},
                "chart_data": {}, "analyst": {}, "holders": [],
                "user_positions": [], "last_signal": None, "now": now,
            },
            status_code=200,
        )

    # Fiyat & piyasa verileri
    market = {
        "price": price,
        "change": _safe_get(info, "change", 0),
        "change_pct": _safe_get(info, "change_percent", 0),
        "open": _safe_get(info, "open"),
        "high": _safe_get(info, "high"),
        "low": _safe_get(info, "low"),
        "prev_close": _safe_get(info, "close"),
        "volume": _safe_get(info, "volume"),
        "amount": _safe_get(info, "amount"),
        "week52_high": _safe_get(info, "fiftyTwoWeekHigh"),
        "week52_low": _safe_get(info, "fiftyTwoWeekLow"),
        "market_cap": _safe_get(info, "marketCap"),
        "market_cap_fmt": _format_market_cap(_safe_get(info, "marketCap")),
    }

    # Temel gostergeler
    fundamentals = {
        "pe": _safe_get(info, "trailingPE"),
        "pb": _safe_get(info, "priceToBook"),
        "ev_ebitda": _safe_get(info, "enterpriseToEbitda"),
        "free_float": _safe_get(info, "floatShares"),
        "foreign_ratio": _safe_get(info, "foreignRatio"),
        "dividend_yield": _safe_get(info, "dividendYield"),
        "sector": _safe_get(info, "sector", "-"),
        "industry": _safe_get(info, "industry", "-"),
    }

    # Teknik gorunum
    try:
        df = ticker.history(period="6mo", interval="1d")
        technical = _technical_summary(df)
    except Exception:
        logger.warning(f"{symbol}: Teknik veri alinamadi")
        technical = {}

    # Grafik verisi (varsayilan 6 ay)
    chart_data = {}
    if df is not None and not df.empty:
        chart_data = {
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "prices": [round(float(row["Close"]), 2) for _, row in df.iterrows()],
            "volumes": [int(row["Volume"]) for _, row in df.iterrows()],
        }

    # Analist gorusleri
    analyst = {}
    try:
        rec = ticker.recommendations
        if rec:
            analyst["recommendation"] = rec.get("recommendation")
            analyst["target_price"] = rec.get("target_price")
            analyst["upside"] = rec.get("upside_potential")

        targets = ticker.analyst_price_targets
        if targets:
            analyst["target_low"] = targets.get("low")
            analyst["target_high"] = targets.get("high")
            analyst["target_mean"] = targets.get("mean")
            analyst["target_median"] = targets.get("median")
            analyst["analyst_count"] = targets.get("numberOfAnalysts")

        rec_summary = ticker.recommendations_summary
        if rec_summary:
            analyst["strong_buy"] = rec_summary.get("strongBuy", 0)
            analyst["buy"] = rec_summary.get("buy", 0)
            analyst["hold"] = rec_summary.get("hold", 0)
            analyst["sell"] = rec_summary.get("sell", 0)
            analyst["strong_sell"] = rec_summary.get("strongSell", 0)
            total = sum([
                analyst["strong_buy"], analyst["buy"], analyst["hold"],
                analyst["sell"], analyst["strong_sell"],
            ])
            analyst["total_rec"] = total
    except Exception:
        logger.warning(f"{symbol}: Analist verisi alinamadi")

    # Ortaklik yapisi
    holders = []
    try:
        mh = ticker.major_holders
        if mh is not None and not mh.empty:
            for name, row in mh.iterrows():
                holders.append({"name": name, "pct": round(row["Percentage"], 2)})
    except Exception:
        logger.warning(f"{symbol}: Ortaklik verisi alinamadi")

    # Acik pozisyon var mi?
    open_trades = repo.get_open_trades()
    user_positions = [t for t in open_trades if t["symbol"] == symbol]

    # Son sinyal
    signals = repo.get_recent_signals(limit=50)
    last_signal = next((s for s in signals if s["symbol"] == symbol), None)

    now = datetime.now(config.timezone)

    return templates.TemplateResponse(
        request,
        "symbol_detail.html",
        context={
            "symbol": symbol,
            "market": market,
            "fundamentals": fundamentals,
            "technical": technical,
            "chart_data": chart_data,
            "analyst": analyst,
            "holders": holders,
            "user_positions": user_positions,
            "last_signal": last_signal,
            "now": now,
        },
    )


@router.get("/{symbol}/chart-data")
async def chart_data(symbol: str, period: str = Query("6mo")):
    """Grafik verisi JSON endpoint — periyod degisiminde HTMX/fetch ile cagirilir."""
    symbol = symbol.upper()
    valid_periods = {"1mo", "3mo", "6mo", "1y", "2y"}
    if period not in valid_periods:
        period = "6mo"

    try:
        ticker = bp.Ticker(symbol)
        df = ticker.history(period=period, interval="1d")
        if df is None or df.empty:
            return JSONResponse({"dates": [], "prices": [], "volumes": []})

        return JSONResponse({
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "prices": [round(float(row["Close"]), 2) for _, row in df.iterrows()],
            "volumes": [int(row["Volume"]) for _, row in df.iterrows()],
        })
    except Exception:
        return JSONResponse({"dates": [], "prices": [], "volumes": []})


HIGHLIGHT_LABELS = {
    # Gelir tablosu
    "Satış Gelirleri": "Sirketin ana faaliyetlerinden elde ettigi toplam gelir",
    "BRÜT KAR (ZARAR)": "Satis gelirinden uretim maliyeti dusuldukten sonra kalan kar",
    "FAALİYET KARI (ZARARI)": "Ana faaliyetlerden elde edilen kar, finansman ve vergi oncesi",
    "SÜRDÜRÜLEN FAALİYETLER VERGİ ÖNCESİ KARI (ZARARI)": "Vergi oncesi toplam kar/zarar",
    "SÜRDÜRÜLEN FAALİYETLER DÖNEM KARI/ZARARI": "Vergi sonrasi net kar/zarar",
    "DÖNEM KARI (ZARARI)": "Donemin toplam net kari veya zarari",
    "Ana Ortaklık Payları": "Ana ortakliga dusen net kar payi, F/K hesabinda kullanilir",
    "Hisse Başına Kazanç": "Hisse basina dusen net kar (EPS)",
    # Bilanco
    "Dönen Varlıklar": "1 yil icinde nakde donusebilecek varliklar",
    "Duran Varlıklar": "Uzun vadeli varliklar (fabrika, makine, yatirimlar)",
    "TOPLAM VARLIKLAR": "Sirketin sahip oldugu tum varliklarin toplami",
    "Nakit ve Nakit Benzerleri": "Sirketin elindeki likit nakit ve kisa vadeli yatirimlar",
    "Kısa Vadeli Yükümlülükler": "1 yil icinde odenmesi gereken borclar",
    "Uzun Vadeli Yükümlülükler": "1 yildan uzun vadeli borclar",
    "Özkaynaklar": "Sirketin net degeri (varliklar - borclar). PD/DD hesabinda kullanilir",
    "TOPLAM KAYNAKLAR": "Borc + ozkaynak toplami, toplam varliklara esit olmali",
    "Ödenmiş Sermaye": "Ortaklarin sirkete koydugu sermaye",
    # Nakit akis
    "İşletme Faaliyetlerinden Kaynaklanan Net Nakit": "Ana faaliyetlerden gelen nakit. Pozitif olmasi saglikli isletme gostergesi",
    "Yatırım Faaliyetlerinden Kaynaklanan Nakit": "Yatirim harcamalari. Negatif olmasi buyume yatirimina isaret eder",
    "Serbest Nakit Akım": "Isletme nakdi - yatirim harcamasi. Temettu ve borc odeme kapasitesini gosterir",
    "Finansman Faaliyetlerden Kaynaklanan Nakit": "Borc alma/odeme ve sermaye islemlerinden nakit",
    "Nakit ve Benzerlerindeki Değişim": "Donem icerisinde nakitteki toplam artis veya azalis",
}


@router.get("/{symbol}/financials", response_class=HTMLResponse)
async def financials_fragment(
    request: Request,
    symbol: str,
    tab: str = Query("income_stmt"),
    quarterly: int = Query(0),
):
    """Finansal tablolar HTMX fragment."""
    symbol = symbol.upper()

    try:
        ticker = bp.Ticker(symbol)
        if tab == "balance_sheet":
            df = ticker.quarterly_balance_sheet if quarterly else ticker.balance_sheet
            title = "Bilanco"
        elif tab == "cashflow":
            df = ticker.quarterly_cashflow if quarterly else ticker.cashflow
            title = "Nakit Akis"
        else:
            df = ticker.quarterly_income_stmt if quarterly else ticker.income_stmt
            title = "Gelir Tablosu"

        if df is None or df.empty:
            return HTMLResponse('<p class="text-gray-500 text-sm py-4">Veri bulunamadi</p>')

        # Son 5 donem
        cols = list(df.columns[:5])
        rows = []
        for idx, row in df.iterrows():
            label = str(idx).strip()
            vals = []
            vals_short = []
            for c in cols:
                v = row[c]
                if pd.notna(v):
                    vals.append(f"{v:,.0f}")
                    vals_short.append(_format_short(v))
                else:
                    vals.append("-")
                    vals_short.append("-")
            rows.append({
                "label": label,
                "cells": vals,
                "cells_short": vals_short,
                "highlight": label in HIGHLIGHT_LABELS,
                "tooltip": HIGHLIGHT_LABELS.get(label, ""),
            })

    except Exception:
        logger.warning(f"{symbol}: Finansal tablo alinamadi ({tab})")
        return HTMLResponse('<p class="text-gray-500 text-sm py-4">Veri yuklenemedi</p>')

    return templates.TemplateResponse(
        request,
        "fragments/financials.html",
        context={
            "symbol": symbol,
            "title": title,
            "tab": tab,
            "quarterly": quarterly,
            "columns": cols,
            "rows": rows,
        },
    )


@router.get("/{symbol}/news", response_class=HTMLResponse)
async def news_fragment(request: Request, symbol: str):
    """KAP haberleri + takvim HTMX fragment."""
    symbol = symbol.upper()
    news_items = []
    calendar_items = []

    try:
        ticker = bp.Ticker(symbol)

        news_df = ticker.news
        if news_df is not None and not news_df.empty:
            for _, row in news_df.head(10).iterrows():
                news_items.append({
                    "date": row.get("Date", ""),
                    "title": row.get("Title", ""),
                    "url": row.get("URL", ""),
                })

        cal_df = ticker.calendar
        if cal_df is not None and not cal_df.empty:
            for _, row in cal_df.iterrows():
                calendar_items.append({
                    "start": row.get("StartDate", ""),
                    "end": row.get("EndDate", ""),
                    "subject": row.get("Subject", ""),
                    "period": row.get("Period", ""),
                })
    except Exception:
        logger.warning(f"{symbol}: Haber/takvim alinamadi")

    return templates.TemplateResponse(
        request,
        "fragments/news.html",
        context={
            "symbol": symbol,
            "news_items": news_items,
            "calendar_items": calendar_items,
        },
    )


@router.get("/{symbol}/etf-holders", response_class=HTMLResponse)
async def etf_holders_fragment(request: Request, symbol: str):
    """ETF pozisyonlari HTMX fragment."""
    symbol = symbol.upper()
    etfs = []

    try:
        ticker = bp.Ticker(symbol)
        df = ticker.etf_holders
        if df is not None and not df.empty:
            for _, row in df.head(15).iterrows():
                etfs.append({
                    "symbol": row.get("symbol", ""),
                    "name": row.get("name", ""),
                    "weight": row.get("holding_weight_pct"),
                    "issuer": row.get("issuer", ""),
                    "aum": row.get("aum_usd"),
                })
    except Exception:
        logger.warning(f"{symbol}: ETF verisi alinamadi")

    return templates.TemplateResponse(
        request,
        "fragments/etf_holders.html",
        context={"symbol": symbol, "etfs": etfs},
    )
