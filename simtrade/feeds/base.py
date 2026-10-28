"""Interface commune des sources de prix et bascule automatique entre sources."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from typing import TypeVar

from simtrade.errors import FeedUnavailable
from simtrade.models import Quote, Trade

T = TypeVar("T")


class PriceFeed(ABC):
    name: str = "?"
    has_trades: bool = False  # True si la source sait aussi fournir les transactions (volume)

    @abstractmethod
    def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        """Flux continu de cotations pour les paires demandées."""

    def trades(self, pairs: Sequence[str]) -> AsyncIterator[Trade]:
        """Flux continu des transactions exécutées (si has_trades)."""
        raise NotImplementedError(f"{self.name} ne fournit pas les transactions.")

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

    def __init__(self, feeds: Sequence[PriceFeed], first_quote_timeout: float = 5.0, first_trade_timeout: float = 15.0):
        self.feeds = list(feeds)
        self.first_quote_timeout = first_quote_timeout
        self.first_trade_timeout = first_trade_timeout
        self.errors: list[str] = []  # sources écartées lors du dernier appel, avec la raison
        self.trade_errors: list[str] = []  # idem pour le flux des transactions
        self.name = " / ".join(f.name for f in self.feeds)
        self.has_trades = any(f.has_trades for f in self.feeds)

    def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        self.errors = []
        return self._first_alive(
            self.feeds, lambda f: f.stream(pairs), self.first_quote_timeout, self.errors, "de prix"
        )

    def trades(self, pairs: Sequence[str]) -> AsyncIterator[Trade]:
        self.trade_errors = []
        feeds = [f for f in self.feeds if f.has_trades]
        return self._first_alive(
            feeds, lambda f: f.trades(pairs), self.first_trade_timeout, self.trade_errors, "de transactions"
        )

    @staticmethod
    async def _first_alive(
        feeds: Sequence[PriceFeed],
        open_stream: Callable[[PriceFeed], AsyncIterator[T]],
        timeout: float,
        errors: list[str],
        label: str,
    ) -> AsyncIterator[T]:
        for feed in feeds:
            source = open_stream(feed)
            try:
                first = await asyncio.wait_for(anext(source), timeout)
            except Exception as exc:  # réseau, blocage géographique, délai dépassé, flux vide...
                reason = str(exc) or type(exc).__name__
                errors.append(f"{feed.name} : {reason}")
                await source.aclose()
                continue
            try:
                yield first
                async for item in source:
                    yield item
            finally:
                await source.aclose()
            return
        raise FeedUnavailable(f"Aucune source {label} joignable. " + " | ".join(errors))
