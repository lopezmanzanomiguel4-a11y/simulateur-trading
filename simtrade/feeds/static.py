"""Source de prix fixes, pour les tests et le développement hors ligne."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal

from simtrade.feeds.base import PriceFeed
from simtrade.models import Quote, Trade, utcnow


class StaticFeed(PriceFeed):
    name = "Simulé"
    has_trades = True

    def __init__(self, prices: dict[str, tuple[str | Decimal, str | Decimal]] | None = None):
        self.quotes: dict[str, Quote] = {}
        self.pending_trades: list[Trade] = []
        for pair, (bid, ask) in (prices or {}).items():
            self.set(pair, bid, ask)

    def set(self, pair: str, bid: str | Decimal, ask: str | Decimal) -> None:
        self.quotes[pair] = Quote(pair, Decimal(bid), Decimal(ask), self.name, utcnow())

    def add_trade(self, pair: str, price: str | Decimal, quantity: str | Decimal) -> None:
        self.pending_trades.append(Trade(pair, Decimal(price), Decimal(quantity), self.name, utcnow()))

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        for pair in pairs:
            if pair in self.quotes:
                yield self.quotes[pair]

    async def trades(self, pairs: Sequence[str]) -> AsyncIterator[Trade]:
        # Chaque transaction n'est envoyée qu'une fois, même après une reconnexion (pas de volume compté deux fois).
        while self.pending_trades:
            trade = self.pending_trades.pop(0)
            if trade.pair in pairs:
                yield trade
