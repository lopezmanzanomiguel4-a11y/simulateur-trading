"""Calculs de marché : bougies OHLC et indicateurs, sans réseau ni base de données.

Conventions :
- les bougies sont construites sur le prix des transactions, datées à l'heure de l'exchange,
  bornes alignées sur l'époque Unix (UTC) ;
- une bougie clôturée venant de l'historique Binance (klines) remplace toujours la bougie live ;
- la bougie en cours est construite en live, éventuellement amorcée par la kline en cours ;
- une période sans transaction donne des bougies plates (clôture précédente, volume nul), comme les klines ;
- les indicateurs portent sur les bougies fournies : l'appelant choisit d'inclure ou non la bougie en cours.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from simtrade.models import EPOCH, PAIRS, Trade

IntervalName = Literal["1m", "5m", "15m", "1h"]
INTERVALS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}  # secondes
MAX_CANDLES: dict[str, int] = {"1m": 1440, "5m": 500, "15m": 500, "1h": 500}  # 1440 x 1m = une journée UTC
LIMIT_MAX = max(MAX_CANDLES.values())
VwapAnchor = Literal["jour_utc", "fenetre"]
VWAP_ANCHORS: tuple[str, ...] = ("jour_utc", "fenetre")
SESSION = 86400  # le VWAP de session repart de zéro à 00:00 UTC
ORIGIN_REST = "binance"  # kline clôturée de l'historique
ORIGIN_LIVE = "live"  # construite avec le flux de transactions (éventuellement amorcée par la kline en cours)
STEP = Decimal("0.00000001")  # précision des indicateurs (8 décimales)
TRADE_SILENCE = 30.0  # secondes sans transaction avant de considérer le flux muet
MAX_CLOCK_OFFSET = 1.0  # secondes d'écart toléré avec l'horloge de Binance


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
    notional: Decimal = Decimal(0)  # somme prix x quantité (USDT)
    transactions: int = 0
    origin: str = ORIGIN_LIVE
    complete: bool = True  # False : des transactions de la période peuvent manquer ou être comptées deux fois
    checked: bool = False  # live, clôturée, déjà comparée à l'historique sans kline correspondante

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=INTERVALS[self.interval])

    def is_closed(self, now: datetime) -> bool:
        return now >= self.close_time

    def add_trade(self, price: Decimal, quantity: Decimal, count: int = 1, *, late: bool = False) -> None:
        """`late` : transaction arrivée après des transactions plus récentes, elle ne change pas la clôture."""
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        if not late:
            self.close = price
        self.volume += quantity
        self.notional += price * quantity
        self.transactions += count


class CandleSeries:
    """Bougies d'une paire pour un intervalle, les plus anciennes perdues au-delà de `maxlen`."""

    def __init__(self, pair: str, interval: str, maxlen: int | None = None):
        if interval not in INTERVALS:
            raise ValueError(f"Intervalle inconnu : {interval}")
        self.pair = pair
        self.interval = interval
        self.seconds = INTERVALS[interval]
        self.maxlen = maxlen or MAX_CANDLES[interval]
        self.candles: deque[Candle] = deque(maxlen=self.maxlen)
        # Heure exchange de la première transaction reçue depuis la dernière (re)connexion.
        # Une bougie live n'est complète que si elle commence après : avant, des transactions
        # ont pu être exécutées sans être reçues (connexion pas encore établie, coupure).
        self.live_since: datetime | None = None

    def _candle(self, open_time: datetime, price: Decimal, complete: bool) -> Candle:
        return Candle(self.pair, self.interval, open_time, price, price, price, price, complete=complete)

    def _find(self, open_time: datetime) -> Candle | None:
        for candle in reversed(self.candles):
            if candle.open_time == open_time:
                return candle
            if candle.open_time < open_time:
                break
        return None

    def note_live(self, ts: datetime) -> None:
        if self.live_since is None:
            self.live_since = ts

    def _complete(self, open_time: datetime) -> bool:
        return self.live_since is not None and open_time > self.live_since

    def add_trade(self, ts: datetime, price: Decimal, quantity: Decimal, count: int = 1) -> None:
        self.note_live(ts)
        start = bucket_start(ts, self.seconds)
        last = self.candles[-1] if self.candles else None
        if last is not None and start == last.open_time:
            last.add_trade(price, quantity, count)
            return
        if last is not None and start < last.open_time:
            # En retard : seulement dans une bougie live incomplète (une kline l'a déjà comptée).
            candle = self._find(start)
            if candle is not None and candle.origin == ORIGIN_LIVE and not candle.complete:
                candle.add_trade(price, quantity, count, late=True)
            return
        if last is not None:
            step = timedelta(seconds=self.seconds)
            missing = min((start - last.open_time) // step - 1, self.maxlen)
            for i in range(missing, 0, -1):
                open_time = start - i * step
                self.candles.append(self._candle(open_time, last.close, self._complete(open_time)))
        candle = self._candle(start, price, self._complete(start))
        candle.add_trade(price, quantity, count)
        self.candles.append(candle)

    def mark_gap(self) -> None:
        """Connexion (re)commencée : on ne sait plus depuis quand les transactions sont reçues."""
        self.live_since = None

    def merge(self, rest: Sequence[Candle], now: datetime, closed_only: bool = False) -> list[tuple[Candle, Candle]]:
        """Fusionne des klines (clé : open_time, jamais de doublon).

        Kline clôturée : remplace la bougie de même début. Kline en cours (si not closed_only) :
        devient la base live de la bougie en cours, marquée incomplète.
        Renvoie les couples (copie de la bougie live remplacée, nouvelle bougie).
        """
        by_time = {c.open_time: c for c in self.candles}
        replaced: list[tuple[Candle, Candle]] = []
        for kline in sorted(rest, key=lambda c: c.open_time):
            if (kline.pair, kline.interval) != (self.pair, self.interval):
                continue
            closed = kline.is_closed(now)
            if not closed and closed_only:
                continue
            new = dataclasses.replace(kline, origin=ORIGIN_REST if closed else ORIGIN_LIVE, complete=closed)
            old = by_time.get(kline.open_time)
            if old is not None and old.origin == ORIGIN_LIVE:
                replaced.append((dataclasses.replace(old), new))
            by_time[kline.open_time] = new
        self.candles = deque(sorted(by_time.values(), key=lambda c: c.open_time)[-self.maxlen :], maxlen=self.maxlen)
        return replaced

    def resume_from(self, now: datetime) -> datetime | None:
        """Début de la resynchro : plus ancienne bougie live pas encore vérifiée (sinon la dernière),
        ou None s'il faut tout recharger (rien en mémoire, ou trou plus long que la série)."""
        if not self.candles:
            return None
        last = self.candles[-1].open_time
        if now - last >= timedelta(seconds=self.seconds * self.maxlen):
            return None
        live = (c.open_time for c in self.candles if c.origin == ORIGIN_LIVE and not c.checked)
        return next(live, last)

    def pending_check(self, now: datetime, grace: timedelta) -> datetime | None:
        """Début de la plus ancienne bougie live clôturée (depuis `grace`) pas encore comparée à l'historique."""
        for candle in self.candles:
            if candle.origin == ORIGIN_LIVE and not candle.checked and candle.close_time + grace <= now:
                return candle.open_time
        return None

    def mark_checked(self, now: datetime, grace: timedelta) -> None:
        for candle in self.candles:
            if candle.origin == ORIGIN_LIVE and candle.close_time + grace <= now:
                candle.checked = True

    def current(self, now: datetime) -> Candle | None:
        if self.candles and not self.candles[-1].is_closed(now):
            return self.candles[-1]
        return None

    def last(self, limit: int | None = None) -> list[Candle]:
        candles = list(self.candles)
        return candles if limit is None else candles[-limit:] if limit > 0 else []


class MarketData:
    """Toutes les séries : une par paire et par intervalle."""

    def __init__(
        self,
        pairs: Sequence[str] = PAIRS,
        intervals: Sequence[str] = tuple(INTERVALS),
        maxlen: dict[str, int] | None = None,
    ):
        sizes = maxlen or MAX_CANDLES
        self.series: dict[tuple[str, str], CandleSeries] = {
            (pair, interval): CandleSeries(pair, interval, sizes.get(interval))
            for pair in pairs
            for interval in intervals
        }

    def of(self, pair: str) -> list[CandleSeries]:
        return [s for (p, _), s in self.series.items() if p == pair]

    def add_trade(self, trade: Trade) -> None:
        for series in self.of(trade.pair):
            series.add_trade(trade.traded_at, trade.price, trade.quantity, trade.count)

    def mark_gap(self) -> None:
        for series in self.series.values():
            series.mark_gap()

    def candles(self, pair: str, interval: str, limit: int | None = None) -> list[Candle]:
        return self.series[(pair, interval)].last(limit)


def closed(candles: Sequence[Candle], now: datetime) -> list[Candle]:
    return [c for c in candles if c.is_closed(now)]


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


@dataclass(frozen=True)
class VwapResult:
    value: Decimal | None
    anchor: str  # "jour_utc" ou "fenetre"
    start: datetime | None
    end: datetime | None
    count: int
    volume: Decimal


def session_start(now: datetime) -> datetime:
    return bucket_start(now, SESSION)


def vwap_window(candles: Sequence[Candle]) -> VwapResult:
    """VWAP glissant sur les bougies fournies (aucune remise à zéro)."""
    return VwapResult(
        vwap(candles),
        "fenetre",
        candles[0].open_time if candles else None,
        candles[-1].close_time if candles else None,
        len(candles),
        sum((c.volume for c in candles), Decimal(0)),
    )


def vwap_session(candles_1m: Sequence[Candle], now: datetime) -> VwapResult:
    """VWAP remis à zéro à 00:00 UTC : bougies 1m du jour en cours parmi celles fournies."""
    start = session_start(now)
    today = [c for c in candles_1m if c.open_time >= start]
    end = min(today[-1].close_time, now) if today else None
    return VwapResult(vwap(today), "jour_utc", start, end, len(today), sum((c.volume for c in today), Decimal(0)))


# --- Qualité des données ---


@dataclass(frozen=True)
class Quality:
    prix_perime: bool
    flux_transactions_muet: bool
    source_transactions: str | None
    repli_coinbase: bool
    historique: str  # "complet", "partiel", "indisponible" ou "desactive"
    derniere_synchro: datetime | None
    bougie_courante_complete: bool | None  # None : pas de bougie en cours
    ecart_horloge: float | None  # secondes, heure Binance - heure locale
    vwap_fiable: bool
    raisons: tuple[str, ...]


def assess_quality(
    *,
    now: datetime,
    quote_time: datetime | None,
    max_quote_age: float,
    trades_connected: bool,
    last_trade_time: datetime | None,
    trade_source: str | None,
    fallback: bool,
    history: str,
    last_sync: datetime | None,
    current: Candle | None,
    clock_offset: float | None,
    vwap_volume: Decimal,
    trade_silence: float = TRADE_SILENCE,
    max_clock_offset: float = MAX_CLOCK_OFFSET,
) -> Quality:
    """Signale ce qui rend les bougies ou le VWAP douteux ; `raisons` liste chaque problème détecté."""
    stale = quote_time is None or (now - quote_time).total_seconds() > max_quote_age
    muted = (
        not trades_connected
        or last_trade_time is None
        or (now - last_trade_time).total_seconds() > trade_silence
    )
    clock_bad = clock_offset is not None and abs(clock_offset) > max_clock_offset
    current_complete = None if current is None else current.complete
    reasons = []
    if stale:
        reasons.append("prix_perime")
    if muted:
        reasons.append("flux_transactions_muet")
    if fallback:
        reasons.append("repli_coinbase")
    if history != "complet":
        reasons.append(f"historique_{history}")
    if vwap_volume == 0:
        reasons.append("volume_nul")
    if clock_bad:
        reasons.append("horloge_decalee")
    if current_complete is False:
        reasons.append("bougie_courante_incomplete")
    return Quality(
        prix_perime=stale,
        flux_transactions_muet=muted,
        source_transactions=trade_source,
        repli_coinbase=fallback,
        historique=history,
        derniere_synchro=last_sync,
        bougie_courante_complete=current_complete,
        ecart_horloge=clock_offset,
        vwap_fiable=not (muted or fallback or history != "complet" or vwap_volume == 0),
        raisons=tuple(reasons),
    )
