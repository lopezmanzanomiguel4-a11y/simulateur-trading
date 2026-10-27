"""Websocket public Coinbase Exchange : canal ticker (best_bid/best_ask), sans compte.

Le canal ticker n'envoie une mise à jour qu'à chaque transaction (plus un message
initial à l'abonnement) : le flux est moins fréquent que celui de Binance.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from websockets.asyncio.client import connect

from simtrade.errors import FeedUnavailable
from simtrade.feeds.base import PriceFeed
from simtrade.models import QUOTE_ASSET, Quote, base_asset, clean_decimal, utcnow

COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"


def to_product(pair: str) -> str:
    """BTCUSDT -> BTC-USDT."""
    return f"{base_asset(pair)}-{QUOTE_ASSET}"


def parse_message(raw: str | bytes, products: dict[str, str]) -> Quote | None:
    msg = json.loads(raw)
    kind = msg.get("type")
    if kind == "error":
        raise FeedUnavailable(f"Coinbase : {msg.get('message', '')} {msg.get('reason', '')}".strip())
    if kind != "ticker":
        return None
    pair = products.get(msg.get("product_id", ""))
    if pair is None or not msg.get("best_bid") or not msg.get("best_ask"):
        return None
    return Quote(
        pair=pair,
        bid=clean_decimal(msg["best_bid"]),
        ask=clean_decimal(msg["best_ask"]),
        source=CoinbaseFeed.name,
        received_at=utcnow(),
    )


class CoinbaseFeed(PriceFeed):
    name = "Coinbase"

    def __init__(self, url: str = COINBASE_URL, open_timeout: float = 5.0):
        self.url = url
        self.open_timeout = open_timeout

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        products = {to_product(p): p for p in pairs}
        async with connect(self.url, open_timeout=self.open_timeout, close_timeout=0.2) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": list(products), "channels": ["ticker"]}))
            async for raw in ws:
                quote = parse_message(raw, products)
                if quote is not None:
                    yield quote
