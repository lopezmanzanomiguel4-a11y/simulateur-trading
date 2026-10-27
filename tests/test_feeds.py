"""Parsing des messages websocket et bascule entre sources, sans réseau."""

import asyncio
import json
from decimal import Decimal

import pytest

from simtrade.errors import FeedUnavailable
from simtrade.feeds import FallbackFeed, PriceFeed, StaticFeed
from simtrade.feeds import binance, coinbase

D = Decimal


def test_parse_binance_book_ticker():
    raw = json.dumps(
        {
            "stream": "btcusdt@bookTicker",
            "data": {"u": 1, "s": "BTCUSDT", "b": "80659.94000000", "B": "4.1", "a": "80659.95000000", "A": "1.2"},
        }
    )
    q = binance.parse_message(raw)
    assert (q.pair, q.bid, q.ask, q.source) == ("BTCUSDT", D("80659.94"), D("80659.95"), "Binance")
    assert str(q.bid) == "80659.94"


def test_parse_binance_ignore_les_autres_messages():
    assert binance.parse_message('{"result": null, "id": 1}') is None


def test_parse_coinbase_ticker():
    products = {"SOL-USDT": "SOLUSDT"}
    raw = json.dumps({"type": "ticker", "product_id": "SOL-USDT", "best_bid": "101.030", "best_ask": "101.07"})
    q = coinbase.parse_message(raw, products)
    assert (q.pair, q.bid, q.ask, q.source) == ("SOLUSDT", D("101.03"), D("101.07"), "Coinbase")


def test_parse_coinbase_ignore_subscriptions():
    assert coinbase.parse_message('{"type": "subscriptions", "channels": []}', {}) is None


def test_parse_coinbase_erreur():
    raw = json.dumps({"type": "error", "message": "Failed to subscribe", "reason": "invalid product"})
    with pytest.raises(FeedUnavailable, match="invalid product"):
        coinbase.parse_message(raw, {})


def test_produits_coinbase():
    assert coinbase.to_product("ETHUSDT") == "ETH-USDT"


class BrokenFeed(PriceFeed):
    name = "Panne"

    async def stream(self, pairs):
        raise OSError("connexion refusée")
        yield  # pragma: no cover


class SilentFeed(PriceFeed):
    name = "Muet"

    async def stream(self, pairs):
        await asyncio.sleep(3600)
        yield  # pragma: no cover


def test_bascule_sur_la_seconde_source():
    feed = FallbackFeed([BrokenFeed(), StaticFeed({"BTCUSDT": ("1", "2")})])
    quotes = asyncio.run(feed.snapshot(["BTCUSDT"]))
    assert quotes["BTCUSDT"].source == "Simulé"
    assert feed.errors == ["Panne : connexion refusée"]


def test_bascule_si_la_premiere_source_ne_repond_pas():
    feed = FallbackFeed([SilentFeed(), StaticFeed({"BTCUSDT": ("1", "2")})], first_quote_timeout=0.05)
    quotes = asyncio.run(feed.snapshot(["BTCUSDT"]))
    assert quotes["BTCUSDT"].bid == D("1")
    assert feed.errors[0].startswith("Muet")


def test_toutes_les_sources_en_panne():
    feed = FallbackFeed([BrokenFeed(), SilentFeed()], first_quote_timeout=0.05)
    with pytest.raises(FeedUnavailable, match="Aucune source"):
        asyncio.run(feed.snapshot(["BTCUSDT"]))


def test_snapshot_incomplet():
    with pytest.raises(FeedUnavailable, match="ETHUSDT"):
        asyncio.run(StaticFeed({"BTCUSDT": ("1", "2")}).snapshot(["BTCUSDT", "ETHUSDT"]))
