"""Allocation & rebalance — saf hesap fonksiyonlari (I/O yok, network yok)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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
        stale = price is None or price <= 0
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


@dataclass
class RebalanceAlert:
    drifted_legs: list[AllocationLeg]
    review_due: bool
    next_review_date: date | None
    last_review_date: date | None


@dataclass
class DcaItem:
    symbol: str
    buy_usd: float
    buy_shares: float


@dataclass
class DcaPlan:
    items: list[DcaItem]
    deployed_usd: float
    leftover_usd: float


def check_rebalance(
    report: AllocationReport,
    threshold_pct: float,
    last_review: datetime | None,
    interval_days: int,
    now: datetime,
) -> RebalanceAlert:
    drifted = [
        leg
        for leg in report.legs
        if not leg.price_stale and abs(leg.drift_pct) >= threshold_pct
    ]
    if last_review is None:
        return RebalanceAlert(drifted, True, None, None)
    next_date = (last_review + timedelta(days=interval_days)).date()
    review_due = now.date() >= next_date
    return RebalanceAlert(drifted, review_due, next_date, last_review.date())


_EPS = 1e-9


def _waterfill(
    values: dict[str, float], target_frac: dict[str, float], budget: float
) -> dict[str, float]:
    """Alim-only water-filling: en dusuk value/target oranli bacaklara para doker.
    Dondurur: {symbol: eklenecek_usd}. Toplam ~= budget (butce > 0 ise)."""
    add = {s: 0.0 for s in target_frac}
    syms = [s for s in target_frac if target_frac[s] > 0]
    if budget <= _EPS or not syms:
        return add
    remaining = budget
    while remaining > _EPS:
        ratios = {s: (values[s] + add[s]) / target_frac[s] for s in syms}
        min_r = min(ratios.values())
        group = [s for s in syms if ratios[s] <= min_r + _EPS]
        higher = [ratios[s] for s in syms if ratios[s] > min_r + _EPS]
        tsum = sum(target_frac[s] for s in group)
        if higher:
            target_r = min(higher)
            cost = sum(target_frac[s] * (target_r - ratios[s]) for s in group)
            if cost <= remaining + _EPS:
                for s in group:
                    add[s] += target_frac[s] * (target_r - ratios[s])
                remaining -= cost
                continue
        # ya hepsi esit (higher yok) ya da butce bir sonraki seviyeye yetmiyor:
        # kalan butceyi grup icinde target agirligina gore dagit
        for s in group:
            add[s] += remaining * (target_frac[s] / tsum)
        remaining = 0.0
    return add


def plan_dca(
    report: AllocationReport, contribution_usd: float, fractional: bool
) -> DcaPlan:
    legs = [leg for leg in report.legs if not leg.price_stale and leg.target_pct > 0]
    if contribution_usd <= 0 or not legs:
        return DcaPlan(items=[], deployed_usd=0.0,
                       leftover_usd=max(contribution_usd, 0.0))
    values = {leg.symbol: leg.value_usd for leg in legs}
    target_frac = {leg.symbol: leg.target_pct / 100.0 for leg in legs}
    prices = {leg.symbol: leg.price_usd for leg in legs}
    add = _waterfill(values, target_frac, contribution_usd)

    items: list[DcaItem] = []
    deployed = 0.0
    leftover = 0.0
    for sym, amt in add.items():
        if amt <= _EPS:
            continue
        price = prices[sym]
        if fractional:
            shares = amt / price
            spend = amt
        else:
            shares = float(math.floor(amt / price))
            spend = shares * price
        if shares <= 0:
            leftover += amt
            continue
        deployed += spend
        leftover += amt - spend
        items.append(DcaItem(symbol=sym, buy_usd=round(spend, 2),
                             buy_shares=round(shares, 4)))
    return DcaPlan(items=items, deployed_usd=round(deployed, 2),
                   leftover_usd=round(leftover, 2))


@dataclass
class RebalanceItem:
    symbol: str
    action: str  # "BUY" | "SELL" | "HOLD"
    amount_usd: float
    shares: float


@dataclass
class RebalancePlan:
    items: list[RebalanceItem]
    net_cash_usd: float


def plan_rebalance(
    report: AllocationReport,
    contribution_usd: float,
    fractional: bool,
    min_trade_usd: float = 1.0,
) -> RebalancePlan:
    legs = [leg for leg in report.legs if not leg.price_stale and leg.target_pct > 0]
    if not legs:
        return RebalancePlan(items=[], net_cash_usd=0.0)
    total = sum(leg.value_usd for leg in legs)
    t_prime = total + max(contribution_usd, 0.0)

    items: list[RebalanceItem] = []
    net = 0.0
    for leg in legs:
        target_val = (leg.target_pct / 100.0) * t_prime
        delta = target_val - leg.value_usd
        if abs(delta) < min_trade_usd:
            items.append(RebalanceItem(leg.symbol, "HOLD", 0.0, 0.0))
            continue
        price = leg.price_usd
        if fractional:
            shares = abs(delta) / price
            amount = abs(delta)
        else:
            shares = float(math.floor(abs(delta) / price))
            amount = shares * price
            if shares <= 0:
                items.append(RebalanceItem(leg.symbol, "HOLD", 0.0, 0.0))
                continue
        if delta > 0:
            items.append(RebalanceItem(leg.symbol, "BUY", round(amount, 2),
                                       round(shares, 4)))
            net += amount
        else:
            items.append(RebalanceItem(leg.symbol, "SELL", round(amount, 2),
                                       round(shares, 4)))
            net -= amount
    return RebalancePlan(items=items, net_cash_usd=round(net, 2))
