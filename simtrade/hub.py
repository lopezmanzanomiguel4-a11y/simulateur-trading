"""Flux de prix partagé pour un processus permanent (API).

PriceHub garde UNE connexion de cotations ouverte et met à jour un cache {paire: Quote},
plus une connexion de transactions (volume) si la source en fournit. Les transactions
construisent les bougies (MarketData), raccordées à l'historique Binance (klines) :
- à chaque nouvelle connexion de transactions (démarrage, reconnexion, changement de source),
  les transactions sont mises en tampon le temps de recharger l'historique depuis la dernière
  bougie connue, puis appliquées si elles sont postérieures à la kline en cours ;
- toutes les `reconcile_every` secondes, chaque bougie live clôturée est remplacée par sa kline.
CachedFeed expose le cache comme une PriceFeed ordinaire pour TradingService.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from simtrade.errors import FeedUnavailable
from simtrade.feeds.base import FallbackFeed, PriceFeed
from simtrade.history import HistorySource
from simtrade.market import MAX_CLOCK_OFFSET, Candle, MarketData, Quality, assess_quality, bucket_start
from simtrade.models import PAIRS, Quote, Trade, utcnow

log = logging.getLogger("simtrade.hub")

MAX_QUOTE_AGE = 5.0  # secondes
SUBSCRIBER_QUEUE = 100
RECONCILE_EVERY = 5.0  # secondes entre deux comparaisons avec l'historique
RECONCILE_GRACE = timedelta(seconds=2)  # délai après la clôture avant de demander la kline définitive
RECONCILIATIONS_KEPT = 500


@dataclass
class LinkStatus:
    """État d'une connexion du hub (cotations ou transactions)."""

    connected: bool = False
    last_error: str | None = None
    reconnects: int = 0


@dataclass(frozen=True)
class ClockOffset:
    offset: float  # secondes : heure Binance - heure locale (milieu de l'aller-retour)
    round_trip: float  # secondes
    measured_at: datetime


class PriceHub:
    def __init__(
        self,
        feed: PriceFeed | None = None,
        pairs: Sequence[str] = PAIRS,
        *,
        max_backoff: float = 30,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        market: MarketData | None = None,
        history: HistorySource | None = None,
        clock: Callable[[], datetime] = utcnow,
        reconcile_every: float = RECONCILE_EVERY,
    ):
        """Sans `feed` : Binance puis Coinbase, avec l'historique Binance. Avec un `feed` fourni,
        l'historique n'est utilisé que si `history` est fourni aussi."""
        if feed is None:
            from simtrade.feeds import default_feed
            from simtrade.history import BinanceHistory

            feed = default_feed()
            history = history or BinanceHistory()
        self.feed = feed
        self.pairs = list(pairs)
        self.max_backoff = max_backoff
        self._sleep = sleep
        self.clock = clock
        self.history = history
        self.reconcile_every = reconcile_every
        self.market = market if market is not None else MarketData(self.pairs)
        self.quotes: dict[str, Quote] = {}
        self.source: str | None = None
        self.last_quote_at: datetime | None = None
        self.status = LinkStatus()  # connexion des cotations
        self.trade_status = LinkStatus()  # connexion des transactions
        self.last_trade_at: datetime | None = None
        self.last_trade_by_pair: dict[str, datetime] = {}  # heure locale de réception
        self.trade_source: str | None = None
        self.primary_source = feed.feeds[0].name if isinstance(feed, FallbackFeed) and feed.feeds else feed.name
        self.history_state: dict[tuple[str, str], str] = {
            key: "partiel" if history is not None else "desactive" for key in self.market.series
        }
        self.last_sync: datetime | None = None
        self.history_error: str | None = None
        self.clock_offset: ClockOffset | None = None
        self.clock_error: str | None = None
        # (copie de la bougie live juste avant son remplacement, kline qui la remplace)
        self.reconciliations: deque[tuple[Candle, Candle]] = deque(maxlen=RECONCILIATIONS_KEPT)
        self._buffer: list[Trade] | None = None  # transactions retenues pendant une resynchro
        self._resync = asyncio.Event()
        self._subscribers: set[asyncio.Queue[Quote]] = set()

    @property
    def connected(self) -> bool:
        return self.status.connected

    @property
    def last_error(self) -> str | None:
        return self.status.last_error

    @property
    def reconnects(self) -> int:
        return self.status.reconnects

    @property
    def trade_fallback(self) -> bool:
        """Transactions reçues d'une autre source que la source principale (Coinbase au lieu de Binance)."""
        return self.trade_source is not None and self.trade_source != self.primary_source

    def update(self, quote: Quote) -> None:
        """Met à jour le cache et prévient les abonnés (les plus lents perdent leurs anciennes cotations)."""
        self.quotes[quote.pair] = quote
        self.source = quote.source
        self.last_quote_at = quote.received_at
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(quote)

    def update_trade(self, trade: Trade) -> None:
        self.last_trade_at = trade.received_at
        self.last_trade_by_pair[trade.pair] = trade.received_at
        self.trade_source = trade.source
        if self._buffer is not None:
            self._buffer.append(trade)
        else:
            self.market.add_trade(trade)

    def begin_trade_session(self) -> None:
        """Nouvelle connexion de transactions : la suite a pu être interrompue, il faut resynchroniser."""
        self.market.mark_gap()
        if self.history is not None:
            if self._buffer is None:
                self._buffer = []  # dès la connexion : aucune transaction n'échappe au raccord
            for key in self.history_state:
                self.history_state[key] = "partiel"
            self._resync.set()

    # --- Boucles ---

    async def run(self) -> None:
        """Tourne jusqu'à annulation : cotations, et transactions + historique si disponibles."""
        if not self.feed.has_trades:
            await self.run_quotes()
            return
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.run_quotes())
            tg.create_task(self.run_trades())
            if self.history is not None:
                tg.create_task(self.run_history())

    async def run_quotes(self) -> None:
        await self._run_loop(self.feed.stream, self.update, self.status, "de prix")

    async def run_trades(self) -> None:
        await self._run_loop(
            self.feed.trades, self.update_trade, self.trade_status, "de transactions", self.begin_trade_session
        )

    async def run_history(self) -> None:
        """Resynchronise quand une connexion de transactions commence, sinon compare périodiquement."""
        delay = 1.0
        while True:
            try:
                await asyncio.wait_for(self._resync.wait(), self.reconcile_every)
            except TimeoutError:
                await self.reconcile()
                continue
            if await self.resync():
                delay = 1.0
                continue
            log.warning("Historique indisponible (%s), nouvel essai dans %g s.", self.history_error, delay)
            await self._sleep(delay)
            delay = min(delay * 2, self.max_backoff)
            self._resync.set()

    async def _run_loop(
        self,
        open_stream: Callable[[Sequence[str]], AsyncIterator[Any]],
        on_item: Callable[[Any], None],
        status: LinkStatus,
        label: str,
        on_session: Callable[[], None] | None = None,
    ) -> None:
        """Se reconnecte avec un délai de 1, 2, 4... max_backoff secondes."""
        delay = 1.0
        while True:
            received = False
            if on_session is not None:
                on_session()
            try:
                async with aclosing(open_stream(self.pairs)) as stream:
                    async for item in stream:
                        received = True
                        status.connected = True
                        on_item(item)
                status.last_error = "flux terminé"
            except asyncio.CancelledError:
                status.connected = False
                raise
            except Exception as exc:  # réseau, toutes les sources injoignables...
                status.last_error = str(exc) or type(exc).__name__
            status.connected = False
            if received:
                delay = 1.0
            log.warning("Flux %s coupé (%s), reconnexion dans %g s.", label, status.last_error, delay)
            await self._sleep(delay)
            status.reconnects += 1
            delay = min(delay * 2, self.max_backoff)

    # --- Historique ---

    async def measure_clock(self) -> None:
        """Écart entre l'heure de Binance et l'heure locale, au milieu de l'aller-retour."""
        if self.history is None:
            return
        try:
            sent = self.clock()
            server = await self.history.server_time()
            received = self.clock()
        except Exception as exc:
            self.clock_error = str(exc) or type(exc).__name__
            log.warning("Écart d'horloge non mesuré : %s", self.clock_error)
            return
        offset = (server - (sent + (received - sent) / 2)).total_seconds()
        self.clock_offset = ClockOffset(offset, (received - sent).total_seconds(), received)
        self.clock_error = None
        level = logging.WARNING if abs(offset) > MAX_CLOCK_OFFSET else logging.INFO
        log.log(level, "Écart d'horloge avec Binance : %+.3f s (aller-retour %.3f s).", offset, self.clock_offset.round_trip)

    def _last_buffered(self, pair: str) -> datetime | None:
        times = [t.traded_at for t in self._buffer or () if t.pair == pair]
        return max(times) if times else None

    async def resync(self) -> bool:
        """Recharge l'historique depuis la dernière bougie de chaque série ; False si une requête échoue."""
        self._resync.clear()
        if self.history is None:
            return True
        if self._buffer is None:
            self._buffer = []
        await self.measure_clock()
        # par série : (heure exchange de la dernière transaction reçue avant la réponse, début de la dernière kline)
        cutoffs: dict[tuple[str, str], tuple[datetime | None, datetime | None]] = {}
        ok = True
        try:
            for key, series in self.market.series.items():
                pair, interval = key
                start = series.resume_from(self.clock())
                candles = await self.history.klines(pair, interval, series.maxlen, start)
                # Transactions déjà reçues à la réponse : supposées comptées par les klines renvoyées.
                cutoffs[key] = (self._last_buffered(pair), max((c.open_time for c in candles), default=None))
                self.reconciliations.extend(series.merge(candles, self.clock()))
                self.history_state[key] = "complet"
            self.last_sync = self.clock()
            self.history_error = None
        except Exception as exc:
            ok = False
            self.history_error = str(exc) or type(exc).__name__
            for key in self.market.series:
                if key not in cutoffs:
                    self.history_state[key] = "indisponible"
        self._flush(cutoffs)
        return ok

    def _flush(self, cutoffs: dict[tuple[str, str], tuple[datetime | None, datetime | None]]) -> None:
        buffered, self._buffer = self._buffer or [], None
        for trade in buffered:
            for series in self.market.of(trade.pair):
                cutoff, newest = cutoffs.get((series.pair, series.interval), (None, None))
                covered = newest is not None and bucket_start(trade.traded_at, series.seconds) <= newest
                if covered and cutoff is not None and trade.traded_at <= cutoff:
                    series.note_live(trade.traded_at)  # reçue (donc flux actif), mais déjà dans une kline
                    continue
                series.add_trade(trade.traded_at, trade.price, trade.quantity, trade.count)

    async def reconcile(self) -> None:
        """Remplace chaque bougie live clôturée par sa kline définitive."""
        if self.history is None:
            return
        now = self.clock()
        for (pair, interval), series in self.market.series.items():
            start = series.pending_check(now, RECONCILE_GRACE)
            if start is None:
                continue
            try:
                candles = await self.history.klines(pair, interval, series.maxlen, start)
            except Exception as exc:
                self.history_error = str(exc) or type(exc).__name__
                log.warning("Réconciliation impossible (%s), nouvel essai plus tard.", self.history_error)
                return
            self.reconciliations.extend(series.merge(candles, self.clock(), closed_only=True))
            series.mark_checked(now, RECONCILE_GRACE)

    def history_overall(self) -> str:
        states = set(self.history_state.values())
        for state in ("desactive", "indisponible", "partiel"):
            if state in states:
                return state
        return "complet"

    def quality(self, pair: str, interval: str, vwap_volume: Decimal) -> Quality:
        now = self.clock()
        quote = self.quotes.get(pair)
        return assess_quality(
            now=now,
            quote_time=quote.received_at if quote else None,
            max_quote_age=MAX_QUOTE_AGE,
            trades_connected=self.trade_status.connected,
            last_trade_time=self.last_trade_by_pair.get(pair),
            trade_source=self.trade_source,
            fallback=self.trade_fallback,
            history=self.history_state.get((pair, interval), "desactive"),
            last_sync=self.last_sync,
            current=self.market.series[(pair, interval)].current(now),
            clock_offset=self.clock_offset.offset if self.clock_offset else None,
            vwap_volume=vwap_volume,
        )

    async def aclose(self) -> None:
        if self.history is not None:
            await self.history.aclose()

    async def subscribe(self, pairs: Sequence[str] = PAIRS) -> AsyncIterator[Quote]:
        """Contenu actuel du cache, puis chaque nouvelle cotation."""
        wanted = set(pairs)
        queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        try:
            for quote in list(self.quotes.values()):
                if quote.pair in wanted:
                    yield quote
            while True:
                quote = await queue.get()
                if quote.pair in wanted:
                    yield quote
        finally:
            self._subscribers.discard(queue)


class CachedFeed(PriceFeed):
    """Lit le cache du hub instantanément ; refuse une cotation de plus de max_age secondes."""

    def __init__(self, hub: PriceHub, max_age: float = MAX_QUOTE_AGE, clock: Callable[[], datetime] = utcnow):
        self.hub = hub
        self.max_age = max_age
        self.clock = clock
        self.name = hub.feed.name

    def age(self, quote: Quote) -> float:
        return (self.clock() - quote.received_at).total_seconds()

    async def snapshot(self, pairs: Sequence[str], timeout: float = 0) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for pair in pairs:
            quote = self.hub.quotes.get(pair)
            if quote is None:
                raise FeedUnavailable(f"Pas encore de prix pour {pair} (flux en connexion).")
            age = self.age(quote)
            if age > self.max_age:
                raise FeedUnavailable(f"Cotation périmée pour {pair} (reçue il y a {age:.0f} s).")
            quotes[pair] = quote
        return quotes

    def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        return self.hub.subscribe(pairs)
