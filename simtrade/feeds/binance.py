"""Websocket public Binance, sans compte : bookTicker (meilleur bid/ask) et aggTrade (transactions)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta

from websockets.asyncio.client import connect

from simtrade.feeds.base import PriceFeed
from simtrade.models import EPOCH, Quote, Trade, clean_decimal, utcnow

BINANCE_URL = "wss://stream.binance.com:443/stream?streams="


def parse_message(raw: str | bytes) -> Quote | None:
    msg = json.loads(raw)
    data = msg.get("data", msg)
    if not all(k in data for k in ("s", "b", "a")):
        return None
    return Quote(
        pair=data["s"],
        bid=clean_decimal(data["b"]),
        ask=clean_decimal(data["a"]),
        source=BinanceFeed.name,
        received_at=utcnow(),
    )


def parse_trade(raw: str | bytes) -> Trade | None:
    """aggTrade : T = heure de la transaction (ms), f..l = transactions individuelles regroupées."""
    msg = json.loads(raw)
    data = msg.get("data", msg)
    if data.get("e") != "aggTrade" or not all(k in data for k in ("s", "p", "q", "T")):
        return None
    count = int(data["l"]) - int(data["f"]) + 1 if "f" in data and "l" in data else 1
    return Trade(
        pair=data["s"],
        price=clean_decimal(data["p"]),
        quantity=clean_decimal(data["q"]),
        source=BinanceFeed.name,
        received_at=utcnow(),
        traded_at=EPOCH + timedelta(milliseconds=int(data["T"])),
        count=count,
    )


class BinanceFeed(PriceFeed):
    name = "Binance"
    has_trades = True

    def __init__(self, base_url: str = BINANCE_URL, open_timeout: float = 5.0):
        self.base_url = base_url
        self.open_timeout = open_timeout

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        streams = "/".join(f"{p.lower()}@bookTicker" for p in pairs)
        # Binance ne répond pas à la trame de fermeture : sans close_timeout court, chaque
        # déconnexion attend 10 s.
        async with connect(self.base_url + streams, open_timeout=self.open_timeout, close_timeout=0.2) as ws:
            async for raw in ws:
                quote = parse_message(raw)
                if quote is not None:
                    yield quote

    async def trades(self, pairs: Sequence[str]) -> AsyncIterator[Trade]:
        # Connexion distincte de celle des cotations : une coupure de l'une ne touche pas l'autre.
        streams = "/".join(f"{p.lower()}@aggTrade" for p in pairs)
        async with connect(self.base_url + streams, open_timeout=self.open_timeout, close_timeout=0.2) as ws:
            async for raw in ws:
                trade = parse_trade(raw)
                if trade is not None:
                    yield trade
