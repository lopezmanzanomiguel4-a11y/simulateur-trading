"""Interface commune des sources de prix et bascule automatique entre sources."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing

from simtrade.errors import FeedUnavailable
from simtrade.models import Quote


class PriceFeed(ABC):
    name: str = "?"

    @abstractmethod
    def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        """Flux continu de cotations pour les paires demandées."""

    async def snapshot(self, pairs: Sequence[str], timeout: float = 10.0) -> dict[str, Quote]:
        """Première cotation disponible pour chaque paire."""
        wanted = set(pairs)
        quotes: dict[str, Quote] = {}

        async def collect() -> None:
            async with aclosing(self.stream(list(pairs))) as stream:
                async for quote in stream:
                    if quote.pair in wanted:
                        quotes[quote.pair] = quote
                    if len(quotes) == len(wanted):
                        return

        try:
            await asyncio.wait_for(collect(), timeout)
        except TimeoutError:
            pass
        missing = wanted - quotes.keys()
        if missing:
            raise FeedUnavailable(f"Pas de prix reçu pour {', '.join(sorted(missing))} ({self.name}).")
        return quotes


class FallbackFeed(PriceFeed):
    """Essaie chaque source dans l'ordre ; garde la première qui envoie une cotation."""

    def __init__(self, feeds: Sequence[PriceFeed], first_quote_timeout: float = 5.0):
        self.feeds = list(feeds)
        self.first_quote_timeout = first_quote_timeout
        self.errors: list[str] = []  # sources écartées lors du dernier appel, avec la raison
        self.name = " / ".join(f.name for f in self.feeds)

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        self.errors = []
        for feed in self.feeds:
            source = feed.stream(pairs)
            try:
                first = await asyncio.wait_for(anext(source), self.first_quote_timeout)
            except Exception as exc:  # réseau, blocage géographique, délai dépassé, flux vide...
                reason = str(exc) or type(exc).__name__
                self.errors.append(f"{feed.name} : {reason}")
                await source.aclose()
                continue
            try:
                yield first
                async for quote in source:
                    yield quote
            finally:
                await source.aclose()
            return
        raise FeedUnavailable("Aucune source de prix joignable. " + " | ".join(self.errors))
