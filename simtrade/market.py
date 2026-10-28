"""Calculs de marché : bougies OHLC et indicateurs, sans réseau ni base de données.

Conventions :
- les bougies sont construites sur le mid (bid + ask) / 2, horodaté à la réception ;
- le volume et le notional (somme prix x quantité) viennent des transactions ;
- une période sans cotation donne des bougies plates (clôture précédente, volume nul) ;
- une cotation arrivée en retard (bougie déjà dépassée) est ignorée ;
- les indicateurs portent sur les bougies fournies, bougie en cours comprise.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from simtrade.models import PAIRS, Quote, Trade

INTERVALS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}  # secondes
MAX_CANDLES = 500  # par paire et par intervalle
STEP = Decimal("0.00000001")  # précision des indicateurs (8 décimales)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _q(value: Decimal) -> Decimal:
    return value.quantize(STEP, rounding=ROUND_HALF_EVEN)


def bucket_start(ts: datetime, seconds: int) -> datetime:
    """Début de la bougie qui contient `ts` (alignée sur l'époque Unix, en UTC)."""
    step = timedelta(seconds=seconds)
    return EPOCH + (ts - EPOCH) // step * step


@dataclass
class Candle:
    pair: str
    interval: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)  # quantité échangée (actif de base)
    notional: Decimal = Decimal(0)  # somme prix x quantité des transactions (USDT)
    ticks: int = 0  # nombre de cotations reçues

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=INTERVALS[self.interval])

    def is_closed(self, now: datetime) -> bool:
        return now >= self.close_time

    def add_price(self, price: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.ticks += 1


class CandleSeries:
    """Bougies d'une paire pour un intervalle, les plus anciennes perdues au-delà de `maxlen`."""

    def __init__(self, pair: str, interval: str, maxlen: int = MAX_CANDLES):
        if interval not in INTERVALS:
            raise ValueError(f"Intervalle inconnu : {interval}")
        self.pair = pair
        self.interval = interval
        self.seconds = INTERVALS[interval]
        self.candles: deque[Candle] = deque(maxlen=maxlen)

    def _flat(self, open_time: datetime, price: Decimal) -> Candle:
        return Candle(self.pair, self.interval, open_time, price, price, price, price)

    def _advance(self, start: datetime) -> Candle | None:
        """Bougie qui commence à `start` : la courante, ou une nouvelle (trous comblés). None si `start` est passé."""
        if not self.candles:
            return None
        last = self.candles[-1]
        if start == last.open_time:
            return last
        if start < last.open_time:
            return None
        step = timedelta(seconds=self.seconds)
        missing = (start - last.open_time) // step - 1
        fill = min(missing, self.candles.maxlen or missing)
        first = start - fill * step
        for i in range(fill):
            self.candles.append(self._flat(first + i * step, last.close))
        candle = self._flat(start, last.close)
        self.candles.append(candle)
        return candle

    def add_price(self, ts: datetime, price: Decimal) -> None:
        start = bucket_start(ts, self.seconds)
        if not self.candles:
            candle = self._flat(start, price)
            candle.ticks = 1
            self.candles.append(candle)
            return
        candle = self._advance(start)
        if candle is None:
            return  # cotation en retard
        if candle.ticks == 0:  # bougie neuve : elle s'ouvre à ce prix
            candle.open = candle.high = candle.low = price
        candle.add_price(price)

    def add_trade(self, ts: datetime, price: Decimal, quantity: Decimal) -> None:
        start = bucket_start(ts, self.seconds)
        candle = self._advance(start)
        if candle is None:  # aucune bougie encore, ou transaction en retard
            candle = next((c for c in self.candles if c.open_time == start), None)
            if candle is None:
                return
        candle.volume += quantity
        candle.notional += price * quantity

    def last(self, limit: int | None = None) -> list[Candle]:
        candles = list(self.candles)
        return candles if limit is None else candles[-limit:] if limit > 0 else []


class MarketData:
    """Toutes les séries : une par paire et par intervalle."""

    def __init__(self, pairs: Sequence[str] = PAIRS, intervals: Sequence[str] = tuple(INTERVALS), maxlen: int = MAX_CANDLES):
        self.series: dict[tuple[str, str], CandleSeries] = {
            (pair, interval): CandleSeries(pair, interval, maxlen) for pair in pairs for interval in intervals
        }

    def _of(self, pair: str) -> list[CandleSeries]:
        return [s for (p, _), s in self.series.items() if p == pair]

    def add_quote(self, quote: Quote) -> None:
        mid = quote.mid
        for series in self._of(quote.pair):
            series.add_price(quote.received_at, mid)

    def add_trade(self, trade: Trade) -> None:
        for series in self._of(trade.pair):
            series.add_trade(trade.received_at, trade.price, trade.quantity)

    def candles(self, pair: str, interval: str, limit: int | None = None) -> list[Candle]:
        return self.series[(pair, interval)].last(limit)


# --- Indicateurs : dernière valeur, ou None s'il n'y a pas assez de données ---


def sma(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Moyenne des n dernières clôtures."""
    if n < 1 or len(closes) < n:
        return None
    return _q(sum(closes[-n:], Decimal(0)) / n)


def ema(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Moyenne exponentielle, alpha = 2 / (n + 1), amorcée par la SMA des n premières clôtures."""
    if n < 1 or len(closes) < n:
        return None
    alpha = Decimal(2) / (n + 1)
    value = sum(closes[:n], Decimal(0)) / n
    for close in closes[n:]:
        value = alpha * close + (1 - alpha) * value
    return _q(value)


def rsi(closes: Sequence[Decimal], n: int = 14) -> Decimal | None:
    """RSI de Wilder : moyennes des hausses et des baisses lissées sur n variations.

    100 si aucune baisse, 50 si les prix n'ont pas bougé du tout.
    """
    if n < 1 or len(closes) < n + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum((max(c, Decimal(0)) for c in changes[:n]), Decimal(0)) / n
    loss = sum((max(-c, Decimal(0)) for c in changes[:n]), Decimal(0)) / n
    for c in changes[n:]:
        gain = (gain * (n - 1) + max(c, Decimal(0))) / n
        loss = (loss * (n - 1) + max(-c, Decimal(0))) / n
    if loss == 0:
        return Decimal(50) if gain == 0 else Decimal(100)
    return _q(100 - 100 / (1 + gain / loss))


def vwap(candles: Sequence[Candle]) -> Decimal | None:
    """Prix moyen pondéré par les volumes : somme des notionals / somme des volumes."""
    volume = sum((c.volume for c in candles), Decimal(0))
    if volume == 0:
        return None
    return _q(sum((c.notional for c in candles), Decimal(0)) / volume)


def variation_pct(candles: Sequence[Candle]) -> Decimal | None:
    """(clôture de la dernière - ouverture de la première) / ouverture de la première x 100."""
    if not candles or candles[0].open == 0:
        return None
    return _q((candles[-1].close - candles[0].open) / candles[0].open * 100)
