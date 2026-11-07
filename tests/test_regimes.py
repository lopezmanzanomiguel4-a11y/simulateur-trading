"""Régimes de marché : passé seulement, seuils par quantiles, règle volatilité puis efficacité."""

from datetime import timedelta
from decimal import Decimal

from test_backtest import DAY, ones
from simtrade import regimes
from simtrade.market import Candle, aggregate
from simtrade.regimes import Thresholds

D = Decimal


def five(prices):
    return aggregate(ones(DAY, prices), "5m")


def test_efficacite():
    assert regimes.efficiency([D(i) for i in range(25)]) == 1
    assert regimes.efficiency([D(100), D(101)] * 13) == 0  # aller-retour
    assert regimes.efficiency([D(100), D(101)] * 12 + [D(102)]) == D(2) / 24
    assert regimes.efficiency([D(100)] * 25) == 0
    assert regimes.efficiency([D(1)] * 10) is None


def test_quantile_rang_le_plus_proche():
    values = [D(i) for i in range(1, 11)]
    assert regimes.quantile(values, D("0.8")) == 8 and regimes.quantile(values, D("0.7")) == 7


def test_classement():
    th = Thresholds(D("0.5"), D("0.4"))
    assert regimes.classify((D("0.6"), D("0.9")), th) == regimes.HIGH_VOL  # la volatilité prime
    assert regimes.classify((D("0.1"), D("0.4")), th) == regimes.TREND
    assert regimes.classify((D("0.1"), D("0.39")), th) == regimes.RANGE
    assert regimes.classify(None, th) is None


def test_tendance_et_range_sur_des_bougies():
    up = five([D(100) + D("0.02") * i for i in range(700)])
    zigzag = five([D(100) + (D("0.3") if (i // 5) % 2 else 0) for i in range(700)])
    th = Thresholds(D("5"), D("0.7"))
    assert regimes.regime(up, up[-1].close_time, th) == regimes.TREND
    assert regimes.regime(zigzag, zigzag[-1].close_time, th) == regimes.RANGE


def test_passe_seulement():
    prices = [D(100) + D("0.05") * ((i * 7) % 13) for i in range(1500)]
    bars = five(prices)
    th = regimes.fit_thresholds(bars[:150])
    timeline = regimes.Timeline(bars, th)
    poisoned = [c if c.open_time < bars[200].open_time else
                Candle(c.pair, "5m", c.open_time, D(1), D(10**6), D(1), D(10**6), D(1), D(1), 1, "binance")
                for c in bars]
    poisoned_line = regimes.Timeline(poisoned, th)
    for k in range(100, 200):
        when = bars[k].close_time
        assert regimes.regime(bars, when, th) == regimes.regime(poisoned, when, th) == timeline.at(when)
        assert poisoned_line.at(when) == timeline.at(when)
        assert timeline.at(when + timedelta(minutes=4)) == timeline.at(when)  # bougie suivante pas encore close


def test_seuils_sur_l_echantillon():
    bars = five([D(100) + D("0.05") * ((i * 7) % 13) + D("0.001") * i for i in range(1500)])
    th = regimes.fit_thresholds(bars)
    values = [v for _, v in regimes.series(bars) if v is not None]
    assert th.atr_pct == regimes.quantile([a for a, _ in values], D("0.8"))
    assert th.efficiency == regimes.quantile([e for _, e in values], D("0.7"))
    assert len(values) == len(bars) - regimes.ER_N  # mesurable dès 25 bougies (ER sur 24 variations)
