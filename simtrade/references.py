"""Références d'un backtest, sur les mêmes données, la même période et la même taille de position (D11) :
- ne rien faire (P&L nul) ;
- achat à 00:00 et vente à la clôture de session (23:55), chaque jour, sans stop ni garde-fou ;
- entrées au hasard : sur chaque clôture éligible, entrée avec une probabilité p (entrées de la stratégie /
  clôtures éligibles), même calcul de stop et d'objectif, mêmes sorties, garde-fou compris ; graines fixes.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal

from simtrade import stats as stats_mod
from simtrade import trades as trades_mod
from simtrade.backtest import CENT, HUNDRED, BacktestConfig, BacktestResult, EvalCache, Market, run
from simtrade.engine import Portfolio
from simtrade.models import AUTO, SESSION_CLOSE, Account, Order
from simtrade.sessions import at, close_time, session_date
from simtrade.strategies import Strategy

SEEDS = tuple(range(1, 101))  # 100 tirages
OPEN_CLOSE = "achat-0h-cloture"
MIDNIGHT = time(0, 0)


def do_nothing(config: BacktestConfig, start: datetime, end: datetime) -> stats_mod.Stats:
    curve = stats_mod.equity_curve(config.capital, [], {}, start, end, config.spread_pct)
    return stats_mod.compute([], curve, config.capital, Decimal(0))


def _first(market: Market, start: datetime, end: datetime) -> datetime | None:
    found = market.candles(start, end)
    return found[0].open_time if found else None


def open_close(market: Market, start: datetime, end: datetime, config: BacktestConfig,
               size_pct: Decimal = Decimal(10)) -> BacktestResult:
    """Chaque session commencée dans [start, end) : achat à l'ouverture de 00:00 (ou de la première minute
    disponible avant la dernière entrée), vente à l'ouverture de la clôture de session (ou de la première
    minute disponible ensuite), au bid. Ni stop ni garde-fou."""
    pf = Portfolio(Account(None, config.capital, config.capital, start, "reference", AUTO), fee_rate=config.fee_rate)
    orders: list[Order] = []
    day = session_date(start)
    if at(day, MIDNIGHT) < start:
        day += timedelta(days=1)
    while at(day, MIDNIGHT) < end:
        buy_at = _first(market, at(day, MIDNIGHT), min(at(day, config.rules.last_entry), end))
        if buy_at is not None:
            quote = config.quote(market.pair, market.by_minute[buy_at].open, buy_at)
            amount = (pf.account.cash * size_pct / HUNDRED).quantize(CENT, rounding=ROUND_DOWN)
            orders.append(replace(pf.buy(quote, amount, buy_at), id=len(orders) + 1))
            sell_at = _first(market, close_time(day, config.rules), end)
            if sell_at is not None:
                quote = config.quote(market.pair, market.by_minute[sell_at].open, sell_at)
                orders.append(replace(pf.sell(quote, "tout", sell_at), id=len(orders) + 1, kind=SESSION_CLOSE))
        day += timedelta(days=1)
    trades = trades_mod.trades_from_orders(orders)
    marks = {market.pair: market.candles(start - timedelta(days=1), end)}
    curve = stats_mod.equity_curve(config.capital, orders, marks, start, end, config.spread_pct)
    stats = stats_mod.compute(trades, curve, config.capital, pf.fees_paid)
    return BacktestResult(OPEN_CLOSE, market.pair, market.interval, start, end, config, orders, [], [], trades,
                          curve, stats)


def entry_rate(result: BacktestResult) -> float:
    """p = entrées de la stratégie / clôtures éligibles (0 sans clôture éligible)."""
    return result.entries / result.eligible if result.eligible else 0.0


def random_entries(strategy: Strategy, market: Market, start: datetime, end: datetime, config: BacktestConfig,
                   p: float, seed: int, cache: EvalCache | None = None) -> BacktestResult:
    rng = random.Random(seed)
    return run(strategy, market, start, end, config, entries=lambda: rng.random() < p, cache=cache)


def random_draws(strategy: Strategy, market: Market, start: datetime, end: datetime, config: BacktestConfig,
                 p: float, seeds=SEEDS, cache: EvalCache | None = None) -> list[stats_mod.Stats]:
    cache = cache if cache is not None else EvalCache()
    return [random_entries(strategy, market, start, end, config, p, s, cache).stats for s in seeds]


def share_beaten(pnl: Decimal, draws: list[stats_mod.Stats]) -> float:
    """Part des tirages dont le P&L est strictement inférieur à `pnl`."""
    return sum(1 for d in draws if d.pnl < pnl) / len(draws) if draws else 0.0

