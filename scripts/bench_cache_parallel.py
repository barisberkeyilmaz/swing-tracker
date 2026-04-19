"""Cold vs warm + 1 vs 10 worker karsilastirma.

- DB cache tablolarini temizler
- Cold run: run_quick_scan (tam fetch)
- Warm run: run_quick_scan (cache hit)
- Serial run: scanner_max_workers=1 ile aynisi
- Paralel run: scanner_max_workers=10 ile

Not: quick_scan prefilter'a gore sembolleri kisir. Gercekci "BIST100 tum"
olcumu icin deep_scan benzeri ama sinirli (ilk 30 sembol) ayri bench.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from swing_tracker.config import load_config
from swing_tracker.core.scanner import Scanner
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def _wipe_cache(repo: Repository) -> None:
    repo._conn.execute("DELETE FROM ohlcv_cache")
    repo._conn.execute("DELETE FROM ohlcv_cache_meta")
    repo._conn.commit()


def _bench_scan_30(scanner: Scanner, label: str) -> float:
    """Ilk 30 BIST100 sembolunu score et (deep_scan ama sinirli)."""
    import borsapy as bp
    from swing_tracker.core.strategy import get_strategy, get_strategy_params

    params = get_strategy_params(get_strategy(scanner._config))
    idx = bp.Index("XU100").components
    symbols = [s["symbol"] if isinstance(s, dict) else str(s) for s in idx][:30]

    start = time.perf_counter()
    results = list(scanner._executor.map(
        lambda s: scanner._score_symbol(s, 100_000, params),
        symbols,
    ))
    dur = time.perf_counter() - start
    found = sum(1 for r in results if r is not None)
    print(f"  {label}: {dur:.2f}s ({found}/{len(symbols)} sinyal, workers={scanner._executor._max_workers})")
    return dur


def main() -> None:
    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)

    print("### Cache wipe")
    _wipe_cache(repo)
    print("  ohlcv_cache + meta temizlendi")

    # 1. Sequential (1 worker) cold
    print("\n### 1 worker, COLD (cache bos)")
    config_s = replace(config, cache=replace(config.cache, scanner_max_workers=1))
    scanner_s = Scanner(repo, config_s)
    cold_s = _bench_scan_30(scanner_s, "1w cold")

    # 2. Sequential warm
    print("\n### 1 worker, WARM (cache dolu)")
    warm_s = _bench_scan_30(scanner_s, "1w warm")
    scanner_s.close()

    # 3. Parallel cold (wipe again)
    print("\n### Cache wipe")
    _wipe_cache(repo)
    print("\n### 5 worker, COLD")
    config_p = replace(config, cache=replace(config.cache, scanner_max_workers=5))
    scanner_p = Scanner(repo, config_p)
    cold_p = _bench_scan_30(scanner_p, "5w cold")

    print("\n### 5 worker, WARM")
    warm_p = _bench_scan_30(scanner_p, "5w warm")
    scanner_p.close()

    print("\n" + "=" * 70)
    print("Ozet (30 BIST100 sembol, skor hesabi dahil):")
    print(f"  1w cold:  {cold_s:.2f}s")
    print(f"  1w warm:  {warm_s:.2f}s  (hizlanma {cold_s/warm_s:.1f}x)")
    print(f"  5w cold:  {cold_p:.2f}s  (paralel hizlanma cold: {cold_s/cold_p:.1f}x)")
    print(f"  5w warm:  {warm_p:.2f}s  (paralel hizlanma warm: {warm_s/warm_p:.1f}x)")
    print(f"  Toplam kazanim (1w cold vs 5w warm): {cold_s/warm_p:.1f}x")
    print("=" * 70)


if __name__ == "__main__":
    main()
