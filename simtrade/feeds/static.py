"""Source de prix fixes, pour les tests et le développement hors ligne."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal

from simtrade.feeds.base import PriceFeed
from simtrade.models import Quote, utcnow


class StaticFeed(PriceFeed):
    name = "Simulé"

    def __init__(self, prices: dict[str, tuple[str | Decimal, str | Decimal]] | None = None):
        self.quotes: dict[str, Quote] = {}
        for pair, (bid, ask) in (prices or {}).items():
            self.set(pair, bid, ask)

    def set(self, pair: str, bid: str | Decimal, ask: str | Decimal) -> None:
        self.quotes[pair] = Quote(pair, Decimal(bid), Decimal(ask), self.name, utcnow())

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        for pair in pairs:
            if pair in self.quotes:
                yield self.quotes[pair]
