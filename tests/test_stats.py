"""Statistiques calculées à la main : drawdown, Sharpe avec jours à 0, réussite, courbe de valeur (pur)."""

import math
from datetime import timedelta

import pytest

from etape7 import MIN, T0, D, order
from simtrade.market import Candle
from simtrade.models import Side
from simtrade.stats import Curve, compute, daily_returns, equity_curve, max_drawdown_pct, sharpe
from simtrade.trades import Trade

DAY = timedelta(days=1)


def trade(pnl: str, fees: str = "0.2") -> Trade:
    return Trade("BTCUSDT", T0, T0 + MIN, D(1), D(100), D(100) + D(pnl), D(fees), D(pnl), "stop", None)


def test_drawdown_max():
    assert max_drawdown_pct([D(1100), D(990), D(1050)], D(1000)) == D(10)  # (1100 − 990) / 1100
    assert max_drawdown_pct([D(1010), D(1020)], D(1000)) == 0


def test_sharpe_avec_jour_sans_trade():
    # fins de jour : 1010 (+1 %), 1010 (jour sans trade : 0), 1030,2 (+2 %)
    points = [(T0 + DAY, D(1010)), (T0 + 2 * DAY, D(1010)), (T0 + 3 * DAY, D("1030.2"))]
    returns = daily_returns(points, D(1000))
    assert returns == [D("0.01"), D(0), D("0.02")]
    # moyenne 0,01 ; écart type (n − 1) = 0,01 → 1 × √365
    assert sharpe(returns) == pytest.approx(math.sqrt(365))


def test_sharpe_null_sous_deux_sessions():
    assert sharpe([D("0.01")]) is None
    assert sharpe([D(0), D(0)]) is None  # écart type nul


def test_taux_de_reussite_gain_moyen_frais():
    curve = Curve([(T0 + DAY, D(1003))], 0)
    s = compute([trade("5"), trade("-3"), trade("1")], curve, D(1000))
    assert s.trades == 3 and s.wins == 2
    assert s.win_rate == D(2) / 3 * 100
    assert s.avg_pnl == 1
    assert s.fees == D("0.6")
    assert s.pnl == 3 and s.pnl_pct == D("0.3")
    assert s.sessions == 1 and s.sharpe is None


def test_sans_trade():
    s = compute([], Curve([(T0 + DAY, D(1000)), (T0 + 2 * DAY, D(1000))], 0), D(1000))
    assert s.win_rate is None and s.avg_pnl is None and s.pnl == 0 and s.sessions == 2 and s.sharpe is None


def one(t, close):
    c = D(close)
    return Candle("BTCUSDT", "1m", t, c, c, c, c, D(1), c, 1)


def test_courbe_marquee_au_bid_chaque_minute():
    # achat de 1 BTC à 100 pendant la minute 1, vente à 101 pendant la minute 3 ; bougie de la minute 2 absente
    orders = [order(1, T0 + MIN + timedelta(seconds=10), Side.BUY, "1", "100"),
              order(2, T0 + 3 * MIN + timedelta(seconds=5), Side.SELL, "1", "101")]
    marks = {"BTCUSDT": [one(T0, "99"), one(T0 + MIN, "100"), one(T0 + 3 * MIN, "101")]}
    curve = equity_curve(D(1000), orders, marks, T0, T0 + 4 * MIN, spread_pct=D("0.02"))
    bid = lambda p: D(p) * (1 - D("0.0001"))  # clôture × (1 − s/2)
    cash = D(1000) - D("100.1")
    assert curve.points == [
        (T0 + MIN, D(1000)),
        (T0 + 2 * MIN, cash + bid("100")),
        (T0 + 3 * MIN, cash + bid("100")),  # minute sans bougie : prix reporté
        (T0 + 4 * MIN, cash + D(101) - D("0.101")),
    ]
    assert curve.missing == 1
