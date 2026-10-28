"""Parsing des messages websocket et bascule entre sources, sans réseau."""

import asyncio
import json
from datetime import datetime, timezone
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


# --- Transactions (volume) ---


def test_parse_binance_agg_trade():
    raw = json.dumps(
        {
            "stream": "btcusdt@aggTrade",
            "data": {
                "e": "aggTrade", "E": 1735689600200, "s": "BTCUSDT", "a": 1, "p": "80660.01000000",
                "q": "0.00125000", "f": 100, "l": 104, "T": 1735689600123, "m": False,
            },
        }
    )
    t = binance.parse_trade(raw)
    assert (t.pair, t.price, t.quantity, t.source) == ("BTCUSDT", D("80660.01"), D("0.00125"), "Binance")
    assert t.traded_at == datetime(2025, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc)  # champ T, pas l'heure locale
    assert t.count == 5  # transactions 100 à 104 regroupées
    assert binance.parse_message(raw) is None  # pas une cotation


def test_parse_binance_agg_trade_sans_heure_ignore():
    raw = json.dumps({"data": {"e": "aggTrade", "s": "BTCUSDT", "p": "1", "q": "1"}})
    assert binance.parse_trade(raw) is None


def test_parse_binance_trade_ignore_book_ticker():
    raw = json.dumps({"data": {"s": "BTCUSDT", "b": "1", "a": "2"}})
    assert binance.parse_trade(raw) is None


def test_parse_coinbase_match():
    products = {"ETH-USDT": "ETHUSDT"}
    raw = json.dumps(
        {
            "type": "match", "product_id": "ETH-USDT", "price": "2350.10", "size": "0.5000", "side": "buy",
            "time": "2025-01-01T00:00:59.987654Z",
        }
    )
    t = coinbase.parse_match(raw, products)
    assert (t.pair, t.price, t.quantity, t.source, t.count) == ("ETHUSDT", D("2350.1"), D("0.5"), "Coinbase", 1)
    assert t.traded_at == datetime(2025, 1, 1, 0, 0, 59, 987654, tzinfo=timezone.utc)


def test_parse_coinbase_last_match_ignore():
    raw = json.dumps({"type": "last_match", "product_id": "ETH-USDT", "price": "2350", "size": "1"})
    assert coinbase.parse_match(raw, {"ETH-USDT": "ETHUSDT"}) is None


def test_parse_coinbase_match_erreur():
    with pytest.raises(FeedUnavailable, match="invalid product"):
        coinbase.parse_match(json.dumps({"type": "error", "message": "x", "reason": "invalid product"}), {})


class BrokenTradesFeed(BrokenFeed):
    has_trades = True

    async def trades(self, pairs):
        raise OSError("connexion refusée")
        yield  # pragma: no cover


async def collect_trades(feed, pairs):
    return [t async for t in feed.trades(pairs)]


def test_bascule_des_transactions():
    static = StaticFeed()
    static.add_trade("BTCUSDT", "100", "2")
    feed = FallbackFeed([BrokenTradesFeed(), SilentFeed(), static])  # SilentFeed : pas de transactions, sautée
    assert feed.has_trades
    (trade,) = asyncio.run(collect_trades(feed, ["BTCUSDT"]))
    assert (trade.price, trade.quantity, trade.source) == (D("100"), D("2"), "Simulé")
    assert feed.trade_errors == ["Panne : connexion refusée"] and feed.errors == []


def test_aucune_source_de_transactions():
    feed = FallbackFeed([BrokenFeed(), SilentFeed()])
    assert not feed.has_trades
    with pytest.raises(FeedUnavailable, match="Aucune source de transactions"):
        asyncio.run(collect_trades(feed, ["BTCUSDT"]))


def test_static_feed_n_envoie_chaque_transaction_qu_une_fois():
    feed = StaticFeed()
    feed.add_trade("BTCUSDT", "100", "1")
    feed.add_trade("ETHUSDT", "10", "1")
    assert [t.pair for t in asyncio.run(collect_trades(feed, ["BTCUSDT", "ETHUSDT"]))] == ["BTCUSDT", "ETHUSDT"]
    assert asyncio.run(collect_trades(feed, ["BTCUSDT"])) == []
