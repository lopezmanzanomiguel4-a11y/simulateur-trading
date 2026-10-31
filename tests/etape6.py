"""Outils communs aux tests de l'étape 6 : bougies synthétiques, marché « sain », stratégie de test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from etape4 import Clock, Env
from simtrade.hub import ClockOffset
from simtrade.market import INTERVALS, ORIGIN_LIVE, ORIGIN_REST, Candle
from simtrade.models import AUTO
from simtrade.runner import StrategyRunner

D = Decimal
UTC = timezone.utc
DAY = datetime(2026, 10, 28, tzinfo=UTC)


def candle(
    open_time: datetime,
    close: str | Decimal,
    interval: str = "5m",
    *,
    open_: str | Decimal | None = None,
    high: str | Decimal | None = None,
    low: str | Decimal | None = None,
    volume: str | Decimal = "1",
    spread: str | Decimal = "0",
    origin: str = ORIGIN_REST,
    pair: str = "BTCUSDT",
) -> Candle:
    """Bougie de clôture `close` ; haut/bas = max/min(ouverture, clôture) -/+ `spread` sauf s'ils sont donnés."""
    c = D(close)
    o = D(open_) if open_ is not None else c
    h = D(high) if high is not None else max(o, c) + D(spread)
    lo = D(low) if low is not None else min(o, c) - D(spread)
    v = D(volume)
    return Candle(pair, interval, open_time, o, h, lo, c, v, c * v, 1, origin, True)


def series(start: datetime, closes, interval: str = "5m", **kw) -> list[Candle]:
    step = timedelta(seconds=INTERVALS[interval])
    return [candle(start + i * step, x, interval, **kw) for i, x in enumerate(closes)]


def minutes(start: datetime, count: int, close: str = "100", **kw) -> list[Candle]:
    """`count` bougies 1m identiques à partir de `start`."""
    return series(start, [close] * count, "1m", **kw)


def falling(n: int, top: str = "100", step: str = "0.05") -> list[Decimal]:
    return [D(top) - i * D(step) for i in range(n)]


# Stratégie de test : entre sur tout RSI <= 45 (une série en baisse donne RSI = 0), sort au retour au VWAP.
TEST_STRATEGY = {
    "nom": "test-rsi",
    "paire": "BTCUSDT",
    "intervalle": "5m",
    "entree": [
        {"brique": "rsi_extreme", "zone": "bas", "n": 14, "seuil": "45"},
        {"brique": "volatilite", "n": 14, "min_pct": "0.05", "max_pct": "5"},
    ],
    "sortie": [{"brique": "retour_vwap"}],
    "stop": {"type": "pct", "valeur": "0.5"},
    "objectif": {"type": "r", "multiple": "2"},
    "taille": {"pct_capital": "10"},
    "heures_actives": {"debut": "00:00", "fin": "23:00"},
}


class Market6:
    """Env de l'étape 4 + bougies injectées comme des klines, et un marché sain (qualité sans reproche)."""

    def __init__(self, path, clock: Clock | None = None):
        self.env = Env(path, clock or Clock(DAY.replace(hour=12)))
        self.hub, self.service, self.clock = self.env.hub, self.env.service, self.env.clock
        self.runner = StrategyRunner(self.service, self.hub)
        self.healthy()

    def healthy(self) -> None:
        now = self.clock()
        for key in self.hub.history_state:
            self.hub.history_state[key] = "complet"
        self.hub.last_sync = now
        self.hub.clock_offset = ClockOffset(0.0, 0.05, now)
        self.hub.trade_status.connected = True
        self.hub.trade_source = self.hub.primary_source
        for pair in self.hub.pairs:
            self.hub.last_trade_by_pair[pair] = now

    def load(self, candles: list[Candle]) -> None:
        """Ajoute des bougies clôturées (klines définitives) aux séries du hub, quelle que soit l'horloge."""
        by_series: dict[tuple[str, str], list[Candle]] = {}
        for c in candles:
            by_series.setdefault((c.pair, c.interval), []).append(c)
        for key, cs in by_series.items():
            self.hub.market.series[key].merge(cs, max(self.clock(), max(c.close_time for c in cs)))

    def scenario(self, end: datetime, closes, minute_close: str = "100", spread: str = "0.2") -> None:
        """Bougies 5m `closes` finissant à `end` (exclu), et bougies 1m de la session jusqu'à `end` à `minute_close`
        (VWAP de session = minute_close). L'horloge est mise juste après la clôture de la dernière 5m."""
        start = end - len(closes) * timedelta(minutes=5)
        session = (end - timedelta(seconds=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.load(series(start, closes, "5m", spread=spread))
        self.load(minutes(session, int((end - session).total_seconds() // 60), minute_close))
        self.clock.now = end + timedelta(seconds=3)
        self.healthy()

    def quote(self, bid: str, ask: str | None = None, age: float = 0):
        return self.env.quote(bid, ask, age=age)

    def auto(self, name: str = "robot", capital: str = "10000", definition: dict | None = None):
        self.service.create_account(name, capital, AUTO)
        return self.service.activate_strategy(compte=name, definition=definition or TEST_STRATEGY)

    def tick(self):
        return self.env.run(self.runner.tick())

    def close(self) -> None:
        self.env.close()


__all__ = ["D", "DAY", "UTC", "ORIGIN_LIVE", "ORIGIN_REST", "Market6", "TEST_STRATEGY", "candle", "falling", "minutes", "series"]
