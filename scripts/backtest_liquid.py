"""BIST genis evrende backtest.

Kaynak siralamasi:
1. liquid_universe tablosundaki semboller (rebuild_universe sonrasi dolar)
2. Bossa: data/backtest_cache altindaki cache'li BIST sembolleri

Kullanim:
    python -m scripts.backtest_liquid                    # cache-only fast
    python -m scripts.backtest_liquid --use-liquid       # liquid_universe'den
    python -m scripts.backtest_liquid --start 2025-01-01 --end 2026-04-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from swing_tracker.backtest.engine import run_backtest
from swing_tracker.backtest.metrics import format_report
from swing_tracker.backtest.models import BacktestConfig
from swing_tracker.backtest.runner import parse_config_from_toml
from swing_tracker.config import load_config
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository


def _cached_bist_symbols() -> list[str]:
    """backtest_cache'deki BIST _do_ varyantlari."""
    cache_dir = Path(__file__).parent.parent / "data" / "backtest_cache"
    if not cache_dir.exists():
        return []
    symbols: set[str] = set()
    for f in cache_dir.glob("*_do_2024-01-01_2025-12-31_daily.parquet"):
        name = f.stem
        sym = name.split("_do_")[0]
        if sym.isalpha() and sym.isupper():
            symbols.add(sym)
    return sorted(symbols)


def _liquid_universe_symbols(repo: Repository, limit: int | None) -> list[str]:
    rows = repo.get_liquid_symbols()
    if not rows:
        return []
    rows.sort(key=lambda r: r.get("median_volume_tl") or 0, reverse=True)
    if limit:
        rows = rows[:limit]
    return [r["symbol"] for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-liquid", action="store_true",
                        help="liquid_universe tablosundan sembol cek")
    parser.add_argument("--limit", type=int, default=150,
                        help="Maksimum sembol sayisi (default 150)")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)

    # Sembol kaynagi
    if args.use_liquid:
        symbols = _liquid_universe_symbols(repo, limit=args.limit)
        if not symbols:
            print("HATA: liquid_universe bos. Once: python -m scripts.rebuild_universe")
            return 1
        source = f"liquid_universe ({len(symbols)} en likit)"
    else:
        symbols = _cached_bist_symbols()
        if args.limit:
            symbols = symbols[: args.limit]
        source = f"cache ({len(symbols)} sembol, 2024-01-01→2025-12-31 daily)"

    if not symbols:
        print("HATA: sembol bulunamadi.")
        return 1

    print(f"\n=== Backtest: {source} ===")
    print(f"Tarih: {args.start} → {args.end}")
    print(f"Semboller: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")

    # Backtest config
    bt_cfg = parse_config_from_toml()
    bt_cfg.symbols = symbols
    bt_cfg.start_date = args.start
    bt_cfg.end_date = args.end

    result = run_backtest(bt_cfg)

    # Metrikler
    print("\n" + format_report(result.metrics, result.params))

    # Sembol bazinda performans
    from collections import defaultdict
    sym_pnl: dict[str, float] = defaultdict(float)
    sym_count: Counter = Counter()
    for t in result.trades:
        sym_pnl[t.symbol] += (t.total_pnl or 0)
        sym_count[t.symbol] += 1

    if sym_pnl:
        sorted_syms = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)
        print(f"\n--- Sembol bazinda performans (top 15) ---")
        print(f"{'Sembol':<8} {'Trade':>6} {'Toplam PnL (TL)':>18}")
        print("-" * 36)
        for sym, pnl in sorted_syms[:15]:
            print(f"{sym:<8} {sym_count[sym]:>6} {pnl:>18,.0f}")

        if len(sorted_syms) > 15:
            print(f"\n--- En kotuleri (bottom 5) ---")
            for sym, pnl in sorted_syms[-5:]:
                print(f"{sym:<8} {sym_count[sym]:>6} {pnl:>18,.0f}")

    # Trade listesi (ilk 20 + son 20)
    if result.trades:
        print(f"\n--- Trade detayi ({len(result.trades)} toplam, ilk 20) ---")
        for t in result.trades[:20]:
            exit_str = ", ".join(f"{e.exit_type}@{e.price:.2f}" for e in t.exits) if t.exits else "acik"
            pnl = t.total_pnl or 0
            print(
                f"  {t.symbol:<7} {t.entry_date[:10]} @{t.entry_price:>7.2f} "
                f"x{int(t.shares):<5} → {exit_str:<30} PnL:{pnl:>+8,.0f} TL"
            )

    # Aylik dagilim
    if result.trades:
        monthly: dict[str, list[float]] = defaultdict(list)
        for t in result.trades:
            if t.entry_date and t.total_pnl is not None:
                ym = t.entry_date[:7]
                monthly[ym].append(t.total_pnl)
        print(f"\n--- Aylik PnL (giris tarihine gore) ---")
        for ym in sorted(monthly.keys()):
            pnls = monthly[ym]
            total = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            print(f"  {ym}: {len(pnls):>3} trade, {wins} kazanan, toplam {total:>+10,.0f} TL")

    return 0


if __name__ == "__main__":
    sys.exit(main())
