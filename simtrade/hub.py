"""Flux de prix partagé pour un processus permanent (API).

PriceHub garde UNE connexion de cotations ouverte et met à jour un cache {paire: Quote},
plus une connexion de transactions (volume) si la source en fournit ; les deux alimentent
les bougies (MarketData). CachedFeed expose le cache comme une PriceFeed ordinaire pour TradingService.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from simtrade.errors import FeedUnavailable
from simtrade.feeds.base import PriceFeed
from simtrade.market import MarketData
from simtrade.models import PAIRS, Quote, Trade, utcnow

log = logging.getLogger("simtrade.hub")

MAX_QUOTE_AGE = 5.0  # secondes
SUBSCRIBER_QUEUE = 100


@dataclass
class LinkStatus:
    """État d'une connexion du hub (cotations ou transactions)."""

    connected: bool = False
    last_error: str | None = None
    reconnects: int = 0


class PriceHub:
    def __init__(
        self,
        feed: PriceFeed | None = None,
        pairs: Sequence[str] = PAIRS,
        *,
        max_backoff: float = 30,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        market: MarketData | None = None,
    ):
        if feed is None:
            from simtrade.feeds import default_feed

            feed = default_feed()
        self.feed = feed
        self.pairs = list(pairs)
        self.max_backoff = max_backoff
        self._sleep = sleep
        self.market = market if market is not None else MarketData(self.pairs)
        self.quotes: dict[str, Quote] = {}
        self.source: str | None = None
        self.last_quote_at: datetime | None = None
        self.status = LinkStatus()  # connexion des cotations
        self.trade_status = LinkStatus()  # connexion des transactions
        self.last_trade_at: datetime | None = None
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

    def update(self, quote: Quote) -> None:
        """Met à jour le cache et les bougies, prévient les abonnés (les plus lents perdent leurs anciennes cotations)."""
        self.quotes[quote.pair] = quote
        self.source = quote.source
        self.last_quote_at = quote.received_at
        self.market.add_quote(quote)
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(quote)

    def update_trade(self, trade: Trade) -> None:
        self.last_trade_at = trade.received_at
        self.market.add_trade(trade)

    async def run(self) -> None:
        """Tourne jusqu'à annulation : cotations, et transactions si la source en fournit."""
        if not self.feed.has_trades:
            await self.run_quotes()
            return
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.run_quotes())
            tg.create_task(self.run_trades())

    async def run_quotes(self) -> None:
        await self._run_loop(self.feed.stream, self.update, self.status, "de prix")

    async def run_trades(self) -> None:
        await self._run_loop(self.feed.trades, self.update_trade, self.trade_status, "de transactions")

    async def _run_loop(
        self,
        open_stream: Callable[[Sequence[str]], AsyncIterator[Any]],
        on_item: Callable[[Any], None],
        status: LinkStatus,
        label: str,
    ) -> None:
        """Se reconnecte avec un délai de 1, 2, 4... max_backoff secondes."""
        delay = 1.0
        while True:
            received = False
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
