"""Scanner performance profiling.

Amac: Mevcut scanner darbogazlarini olcmek.
- Tek sembol fetch maliyeti (daily + hourly)
- Indikator hesabi CPU maliyeti
- bp.scan() prefilter maliyeti
- Index components cekme maliyeti
- End-to-end quick_scan suresi

Cikti: stdout tablo + data/perf_profile.json
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import borsapy as bp
import pandas as pd

from swing_tracker.config import load_config
from swing_tracker.core.scanner import Scanner
from swing_tracker.core.signals import _add_all_indicators
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class Timing:
    label: str
    samples: list[float] = field(default_factory=list)

    def add(self, seconds: float) -> None:
        self.samples.append(seconds)

    def stats(self) -> dict:
        if not self.samples:
            return {"count": 0}
        return {
            "count": len(self.samples),
            "total_s": round(sum(self.samples), 3),
            "mean_ms": round(statistics.mean(self.samples) * 1000, 1),
            "median_ms": round(statistics.median(self.samples) * 1000, 1),
            "p95_ms": round(sorted(self.samples)[int(len(self.samples) * 0.95) - 1] * 1000, 1)
            if len(self.samples) >= 20
            else None,
            "min_ms": round(min(self.samples) * 1000, 1),
            "max_ms": round(max(self.samples) * 1000, 1),
        }


class Stopwatch:
    def __init__(self, timing: Timing):
        self._t = timing
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._t.add(time.perf_counter() - self._start)


def profile_index_components(universe: str, timings: dict[str, Timing]) -> list[str]:
    """bp.Index(universe).components cagrisi - evren kac sembol?"""
    t = Timing("index.components")
    timings["index.components"] = t
    with Stopwatch(t):
        idx = bp.Index(universe)
        components = idx.components
    symbols: list[str] = []
    if isinstance(components, list):
        symbols = [s["symbol"] if isinstance(s, dict) else str(s) for s in components]
    print(f"  Universe {universe}: {len(symbols)} sembol, {t.stats()['total_s']}s")
    return symbols


def profile_prefilters(universe: str, prefilters: list[str], timings: dict[str, Timing]) -> None:
    """bp.scan(universe, prefilter) cagrisi - her prefilter icin sure."""
    for pf in prefilters:
        key = f"prefilter[{pf[:30]}]"
        t = Timing(key)
        timings[key] = t
        try:
            with Stopwatch(t):
                result = bp.scan(universe, pf, interval="1d")
            n = len(result) if result is not None else 0
            print(f"  Prefilter '{pf[:40]}': {n} sonuc, {t.stats()['total_s']}s")
        except Exception as e:
            print(f"  Prefilter '{pf[:40]}': HATA {e}")


def profile_market_regime(scanner: Scanner, timings: dict[str, Timing]) -> None:
    t = Timing("check_market_regime")
    timings["check_market_regime"] = t
    with Stopwatch(t):
        scanner.check_market_regime()
    print(f"  Market regime check: {t.stats()['total_s']}s")


def profile_single_symbol_fetch(symbols: list[str], timings: dict[str, Timing]) -> None:
    """Per-sembol: daily fetch, hourly fetch, indikator hesabi."""
    t_daily = Timing("per_symbol.history_daily_6mo")
    t_hourly = Timing("per_symbol.history_hourly_5d")
    t_indicators = Timing("per_symbol.add_all_indicators_daily")
    t_indicators_h = Timing("per_symbol.add_all_indicators_hourly")
    timings["per_symbol.history_daily_6mo"] = t_daily
    timings["per_symbol.history_hourly_5d"] = t_hourly
    timings["per_symbol.add_all_indicators_daily"] = t_indicators
    timings["per_symbol.add_all_indicators_hourly"] = t_indicators_h

    fetched_daily: list[pd.DataFrame] = []
    fetched_hourly: list[pd.DataFrame | None] = []

    for sym in symbols:
        try:
            ticker = bp.Ticker(sym)
            with Stopwatch(t_daily):
                df_d = ticker.history(period="6mo", interval="1d")
            fetched_daily.append(df_d)
            with Stopwatch(t_hourly):
                df_h = ticker.history(period="5d", interval="1h")
            fetched_hourly.append(df_h)
        except Exception as e:
            print(f"    {sym}: fetch hata {e}")
            continue

    # CPU: indikator hesabi ayrik olc
    for df_d in fetched_daily:
        if df_d is None or len(df_d) < 50:
            continue
        with Stopwatch(t_indicators):
            _add_all_indicators(df_d.copy())

    for df_h in fetched_hourly:
        if df_h is None or len(df_h) < 3:
            continue
        with Stopwatch(t_indicators_h):
            _add_all_indicators(df_h.copy())


def profile_end_to_end_quick(scanner: Scanner, timings: dict[str, Timing]) -> None:
    t = Timing("end_to_end.run_quick_scan")
    timings["end_to_end.run_quick_scan"] = t
    with Stopwatch(t):
        result = scanner.run_quick_scan(available_cash=100_000)
    print(
        f"  Quick scan: scanned={result.scanned_count}, "
        f"signals={result.filtered_count}, {t.stats()['total_s']}s"
    )


def profile_end_to_end_deep(scanner: Scanner, timings: dict[str, Timing]) -> None:
    """Tum evreni tara - bu yavas, sample al."""
    t = Timing("end_to_end.run_deep_scan")
    timings["end_to_end.run_deep_scan"] = t
    print("  Deep scan basliyor (tum evren)...")
    with Stopwatch(t):
        result = scanner.run_deep_scan(available_cash=100_000)
    print(
        f"  Deep scan: scanned={result.scanned_count}, "
        f"signals={result.filtered_count}, {t.stats()['total_s']}s"
    )


def print_report(timings: dict[str, Timing]) -> dict:
    print("\n" + "=" * 80)
    print(f"{'Metric':<50} {'count':>6} {'total(s)':>9} {'median(ms)':>11} {'p95(ms)':>9}")
    print("-" * 80)
    report: dict[str, dict] = {}
    for key, t in timings.items():
        s = t.stats()
        report[key] = s
        if s["count"] == 0:
            continue
        p95 = s.get("p95_ms") or "-"
        print(f"{key:<50} {s['count']:>6} {s['total_s']:>9} {s['median_ms']:>11} {str(p95):>9}")
    print("=" * 80)
    return report


def extrapolate(timings: dict[str, Timing], n_universe_full: int) -> None:
    """BIST Tum icin tahmini tarama suresi - sequential."""
    per_sym_daily = timings["per_symbol.history_daily_6mo"].stats()
    per_sym_hourly = timings["per_symbol.history_hourly_5d"].stats()
    per_sym_ind = timings["per_symbol.add_all_indicators_daily"].stats()
    per_sym_ind_h = timings["per_symbol.add_all_indicators_hourly"].stats()

    if per_sym_daily.get("count", 0) == 0:
        return

    median_total_ms = (
        per_sym_daily["median_ms"]
        + per_sym_hourly["median_ms"]
        + per_sym_ind["median_ms"]
        + (per_sym_ind_h.get("median_ms", 0) if per_sym_ind_h.get("count", 0) > 0 else 0)
    )
    total_s = (median_total_ms * n_universe_full) / 1000

    print("\n--- Extrapolasyon ---")
    print(f"Sembol basi toplam (median): {median_total_ms:.0f}ms")
    print(f"Tum BIST ({n_universe_full} sembol) sequential tahmini: {total_s:.0f}s ({total_s/60:.1f}dk)")
    print(f"  - Daily fetch: {per_sym_daily['median_ms']*n_universe_full/1000:.0f}s")
    print(f"  - Hourly fetch: {per_sym_hourly['median_ms']*n_universe_full/1000:.0f}s")
    print(f"  - Indikator hesap: {(per_sym_ind['median_ms']+per_sym_ind_h.get('median_ms',0))*n_universe_full/1000:.0f}s")


def main() -> None:
    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)
    scanner = Scanner(repo, config)

    timings: dict[str, Timing] = {}

    print("\n### 1. Index components (universe yukleme)")
    symbols = profile_index_components(config.scanner.universe, timings)

    print("\n### 2. Prefilters (bp.scan)")
    profile_prefilters(config.scanner.universe, config.scanner.prefilters, timings)

    print("\n### 3. Market regime check")
    profile_market_regime(scanner, timings)

    print("\n### 4. Per-sembol fetch + indikator (ilk 15 sembol sample)")
    sample = symbols[:15]
    profile_single_symbol_fetch(sample, timings)

    print("\n### 5. End-to-end quick_scan")
    profile_end_to_end_quick(scanner, timings)

    # BIST Tum kac sembol? Tahmini olarak yaklasik 500
    # XTUMY endeksi varsa onu kullan, yoksa XU100 * 5 tahmini
    try:
        all_bist = bp.Index("XTUMY").components
        n_all = len(all_bist) if isinstance(all_bist, list) else 500
    except Exception:
        n_all = 500
    print(f"\n### 6. BIST Tum evren buyuklugu: {n_all}")

    report = print_report(timings)
    extrapolate(timings, n_all)

    # Save
    out = Path("data/perf_profile.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nDetayli rapor: {out}")


if __name__ == "__main__":
    main()
