"""Allocation & rebalance — saf hesap fonksiyonlari (I/O yok, network yok)."""

from __future__ import annotations

from dataclasses import dataclass

from swing_tracker.config import AllocationTarget


@dataclass
class AllocationLeg:
    symbol: str
    exchange: str
    group: str
    target_pct: float
    shares: float
    price_usd: float | None
    value_usd: float
    weight_pct: float
    drift_pct: float
    price_stale: bool


@dataclass
class AllocationReport:
    legs: list[AllocationLeg]
    total_value_usd: float
    core_weight_pct: float
    satellite_weight_pct: float
    usdtry: float | None = None


def compute_weights(
    holdings: list[dict],
    prices: dict[str, float],
    targets: dict[str, AllocationTarget],
    usdtry: float | None = None,
) -> AllocationReport:
    shares_by_sym = {h["symbol"]: (h.get("shares") or 0.0) for h in holdings}

    raw: list[tuple[AllocationTarget, float, float | None, float, bool]] = []
    total = 0.0
    for sym, tgt in targets.items():
        shares = shares_by_sym.get(sym, 0.0)
        price = prices.get(sym)
        stale = price is None
        value = 0.0 if stale else shares * price
        if not stale:
            total += value
        raw.append((tgt, shares, price, value, stale))

    legs: list[AllocationLeg] = []
    core_w = 0.0
    sat_w = 0.0
    for tgt, shares, price, value, stale in raw:
        weight = (value / total * 100.0) if (total > 0 and not stale) else 0.0
        drift = weight - tgt.weight
        legs.append(
            AllocationLeg(
                symbol=tgt.symbol,
                exchange=tgt.exchange,
                group=tgt.group,
                target_pct=tgt.weight,
                shares=shares,
                price_usd=price,
                value_usd=value,
                weight_pct=weight,
                drift_pct=drift,
                price_stale=stale,
            )
        )
        if not stale:
            if tgt.group == "core":
                core_w += weight
            elif tgt.group == "satellite":
                sat_w += weight

    return AllocationReport(
        legs=legs,
        total_value_usd=total,
        core_weight_pct=core_w,
        satellite_weight_pct=sat_w,
        usdtry=usdtry,
    )
