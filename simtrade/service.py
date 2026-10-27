"""Couche applicative : relie moteur, stockage et source de prix.

C'est le seul point d'entrée que les interfaces (CLI aujourd'hui, API FastAPI demain)
doivent appeler. Les méthodes qui touchent au réseau sont asynchrones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

from simtrade.engine import DEFAULT_CAPITAL, Portfolio, Valuation, normalize_pair
from simtrade.feeds.base import PriceFeed
from simtrade.models import PAIRS, Account, Order, Quote
from simtrade.storage import Storage


@dataclass(frozen=True)
class TradeResult:
    order: Order
    cash: Decimal  # solde USDT après l'ordre


class TradingService:
    def __init__(self, storage: Storage, feed: PriceFeed):
        self.storage = storage
        self.feed = feed

    # --- Prix ---

    async def prices(self, pairs: Sequence[str] = PAIRS) -> dict[str, Quote]:
        return await self.feed.snapshot(list(pairs))

    def stream_prices(self, pairs: Sequence[str] = PAIRS) -> AsyncIterator[Quote]:
        return self.feed.stream(list(pairs))

    # --- Ordres ---

    async def buy(self, pair: str, amount_usdt: object) -> TradeResult:
        pair = normalize_pair(pair)
        self.storage.load_portfolio().check_buy(amount_usdt)  # refus immédiat, sans réseau
        quote = (await self.feed.snapshot([pair]))[pair]
        with self.storage.transaction():
            portfolio = self.storage.load_portfolio()
            order = portfolio.buy(quote, amount_usdt)
            return TradeResult(self.storage.save_trade(portfolio, order), portfolio.account.cash)

    async def sell(self, pair: str, quantity: object) -> TradeResult:
        pair = normalize_pair(pair)
        self.storage.load_portfolio().check_sell(pair, quantity)
        quote = (await self.feed.snapshot([pair]))[pair]
        with self.storage.transaction():
            portfolio = self.storage.load_portfolio()
            order = portfolio.sell(quote, quantity)
            return TradeResult(self.storage.save_trade(portfolio, order), portfolio.account.cash)

    # --- Consultation ---

    def load_portfolio(self) -> Portfolio:
        return self.storage.load_portfolio()

    async def valuation(self) -> Valuation:
        portfolio = self.storage.load_portfolio()
        quotes = await self.feed.snapshot(list(portfolio.positions)) if portfolio.positions else {}
        return portfolio.valuation(quotes)

    def history(self) -> tuple[Account, list[Order]]:
        account = self.storage.active_account()
        return account, self.storage.orders(account.id)

    def account(self) -> Account:
        return self.storage.active_account()

    def accounts(self) -> list[Account]:
        return self.storage.accounts()

    def reset(self, capital: object = DEFAULT_CAPITAL) -> Account:
        return self.storage.reset(capital)
