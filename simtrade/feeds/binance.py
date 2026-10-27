"""Websocket public Binance : flux bookTicker (meilleur bid/ask en temps réel), sans compte."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from websockets.asyncio.client import connect

from simtrade.feeds.base import PriceFeed
from simtrade.models import Quote, clean_decimal, utcnow

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


class BinanceFeed(PriceFeed):
    name = "Binance"

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
