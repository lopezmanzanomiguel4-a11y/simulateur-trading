"""Outils communs aux tests de l'étape 4 : horloge réglable, hub sans réseau, watcher branché."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from simtrade.feeds import StaticFeed
from simtrade.hub import CachedFeed, PriceHub
from simtrade.models import Quote
from simtrade.service import TradingService
from simtrade.storage import Storage
from simtrade.watcher import OrderWatcher, SessionScheduler

D = Decimal
UTC = timezone.utc
NOON = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOON):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, hh: int, mm: int, ss: int = 0, days: int = 0) -> None:
        self.now = datetime.combine(NOON.date() + timedelta(days=days), datetime.min.time(), tzinfo=UTC).replace(
            hour=hh, minute=mm, second=ss
        )


class Env:
    """Service + hub + watcher sur une base temporaire, comme dans le lifespan de l'API."""

    def __init__(self, path, clock: Clock | None = None):
        self.path = path
        self.clock = clock or Clock()
        self.hub = PriceHub(StaticFeed(), clock=self.clock)
        self.feed = CachedFeed(self.hub, clock=self.clock)
        self.storage = Storage(path)
        self.service = TradingService(self.storage, self.feed, clock=self.clock)
        self.watcher = OrderWatcher(self.service)
        self.watcher.load()
        self.hub.listeners.append(self.watcher.on_quote)
        self.scheduler = SessionScheduler(self.service, self.watcher)

    def quote(self, bid: str, ask: str | None = None, pair: str = "BTCUSDT", age: float = 0) -> Quote:
        quote = Quote(pair, D(bid), D(ask or bid), "Test", self.clock() - timedelta(seconds=age))
        self.hub.update(quote)
        return quote

    def run(self, coro):
        return asyncio.run(coro)

    def close(self) -> None:
        self.storage.close()
