"""Likit evreni manuel yeniden kur.

Normalde scheduler her is gunu config.liquidity.build_time'da calistirir.
Manuel test / ilk fill icin bu script kullanilir.

Kullanim:
    python -m scripts.rebuild_universe              # tam build, DB'ye yaz
    python -m scripts.rebuild_universe --preview    # sadece konsola yaz
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from swing_tracker.config import load_config
from swing_tracker.core.universe import UniverseBuilder
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rebuild_universe")


def _preview(builder: UniverseBuilder) -> None:
    """DB'ye yazmadan, aday + filtre sonuclarini konsola bas."""
    import borsapy as bp

    cfg = builder._config.liquidity
    universe = builder._config.scanner.universe
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    idx = bp.Index(universe)
    components = idx.components
    symbols = [c["symbol"] if isinstance(c, dict) else str(c) for c in components]
    print(f"\n== Preview: {universe} evreninde {len(symbols)} aday ==\n")

    rows = []
    for s in symbols:
        r = builder._evaluate_symbol(s, now)
        if r is None:
            continue
        r["passes"] = builder._passes_filter(r)
        rows.append(r)

    kept = [r for r in rows if r["passes"]]
    rejected = [r for r in rows if not r["passes"]]

    print(f"Veri bulunan: {len(rows)}/{len(symbols)}")
    print(f"Filtreden gecen: {len(kept)} (medyan TL hacim >= {cfg.min_daily_volume_tl:,.0f})")
    print(f"Reddedilen: {len(rejected)}")
    print(f"Dislanan pazarlar: {cfg.excluded_markets}\n")

    print("-- Ilk 20 likit (hacme gore) --")
    kept.sort(key=lambda r: r["median_volume_tl"], reverse=True)
    for r in kept[:20]:
        print(
            f"  {r['symbol']:8} {r['market']:20} "
            f"medyan TL hacim: {r['median_volume_tl']:>15,.0f}  "
            f"gun: {r['volume_days']:2}  "
            f"son: {r['last_close']}"
        )

    # Pazar dagilimi
    from collections import Counter
    market_counts = Counter(r["market"] for r in kept)
    print("\n-- Pazar dagilimi (likit) --")
    for market, count in market_counts.most_common():
        print(f"  {market:30} {count}")

    # Rejected sebepler
    print("\n-- Reddedilen ilk 10 sembol (sebep) --")
    for r in rejected[:10]:
        reasons = []
        if r["volume_days"] < cfg.min_volume_days:
            reasons.append(f"veri gun: {r['volume_days']}")
        if r["median_volume_tl"] < cfg.min_daily_volume_tl:
            reasons.append(f"hacim: {r['median_volume_tl']:,.0f}")
        if r["market"] in cfg.excluded_markets:
            reasons.append(f"pazar: {r['market']}")
        print(f"  {r['symbol']:8} - {', '.join(reasons)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="DB'ye yazmadan konsola ozetle",
    )
    args = parser.parse_args()

    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)
    builder = UniverseBuilder(repo, config)

    start = time.perf_counter()
    try:
        if args.preview:
            _preview(builder)
        else:
            total, kept = builder.build()
            print(f"\nBuild tamam: {total} aday → {kept} likit")
    finally:
        builder.close()

    dur = time.perf_counter() - start
    logger.info(f"Sure: {dur:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
