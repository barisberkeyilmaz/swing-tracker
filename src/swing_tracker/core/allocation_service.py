"""Allocation okuma orkestrasyonu — web router ve scheduler ayni yolu kullanir."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from swing_tracker.config import AllocationConfig
from swing_tracker.core import etf_prices
from swing_tracker.core.allocation import (
    AllocationReport,
    DcaPlan,
    RebalanceAlert,
    RebalancePlan,
    TargetEta,
    check_rebalance,
    compute_weights,
    estimate_months_to_core_target,
    plan_dca,
    plan_rebalance,
)
from swing_tracker.db.repository import Repository


@dataclass
class AllocationView:
    report: AllocationReport
    alert: RebalanceAlert
    dca: DcaPlan
    rebalance: RebalancePlan
    eta: TargetEta
    contribution_usd: float


def _resolve_contribution(
    repo: Repository, config: AllocationConfig, override: float | None
) -> float:
    if override is not None:
        return float(override)
    saved = repo.get_allocation_setting("last_contribution_usd")
    if saved is not None:
        try:
            return float(saved)
        except ValueError:
            pass
    return float(config.monthly_contribution_usd)


def build_report(
    repo: Repository,
    config: AllocationConfig,
    now: datetime | None = None,
    contribution_override: float | None = None,
    price_cache=etf_prices.etf_price_cache,
) -> AllocationView:
    now = now or datetime.now()
    holdings = repo.get_allocation_holdings()
    symbol_exchange = {t.symbol: t.exchange for t in config.targets.values()}
    prices = price_cache.fetch_many(symbol_exchange)
    usdtry = price_cache.fetch_usdtry()

    report = compute_weights(holdings, prices, config.targets, usdtry=usdtry)

    last_row = repo.get_last_allocation_review()
    last_review = None
    if last_row and last_row.get("reviewed_at"):
        try:
            last_review = datetime.fromisoformat(last_row["reviewed_at"])
        except ValueError:
            last_review = None

    alert = check_rebalance(
        report,
        config.drift_threshold_pct,
        last_review,
        config.review_interval_days,
        now,
    )
    contribution = _resolve_contribution(repo, config, contribution_override)
    dca = plan_dca(report, contribution, config.fractional)
    rebalance = plan_rebalance(report, contribution, config.fractional)
    eta = estimate_months_to_core_target(report, contribution, config.targets, now)

    return AllocationView(report, alert, dca, rebalance, eta, contribution)
