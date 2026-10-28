"""Flux de prix partagé pour un processus permanent (API).

PriceHub garde UNE connexion ouverte et met à jour un cache {paire: Quote} ;
CachedFeed expose ce cache comme une PriceFeed ordinaire pour TradingService.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from datetime import datetime

from simtrade.errors import FeedUnavailable
from simtrade.feeds.base import PriceFeed
from simtrade.models import PAIRS, Quote, utcnow

log = logging.getLogger("simtrade.hub")

MAX_QUOTE_AGE = 5.0  # secondes
SUBSCRIBER_QUEUE = 100


class PriceHub:
    def __init__(
        self,
        feed: PriceFeed | None = None,
        pairs: Sequence[str] = PAIRS,
        *,
        max_backoff: float = 30,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        if feed is None:
            from simtrade.feeds import default_feed

            feed = default_feed()
        self.feed = feed
        self.pairs = list(pairs)
        self.max_backoff = max_backoff
        self._sleep = sleep
        self.quotes: dict[str, Quote] = {}
        self.connected = False
        self.source: str | None = None
        self.last_quote_at: datetime | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self._subscribers: set[asyncio.Queue[Quote]] = set()

    def update(self, quote: Quote) -> None:
        """Met à jour le cache et prévient les abonnés (les plus lents perdent leurs anciennes cotations)."""
        self.quotes[quote.pair] = quote
        self.source = quote.source
        self.last_quote_at = quote.received_at
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(quote)

    async def run(self) -> None:
        """Tourne jusqu'à annulation ; se reconnecte avec un délai de 1, 2, 4... max_backoff secondes."""
        delay = 1.0
        while True:
            received = False
            try:
                async with aclosing(self.feed.stream(self.pairs)) as stream:
                    async for quote in stream:
                        received = True
                        self.connected = True
                        self.update(quote)
                self.last_error = "flux terminé"
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:  # réseau, toutes les sources injoignables...
                self.last_error = str(exc) or type(exc).__name__
            self.connected = False
            if received:
                delay = 1.0
            log.warning("Flux de prix coupé (%s), reconnexion dans %g s.", self.last_error, delay)
            await self._sleep(delay)
            self.reconnects += 1
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
