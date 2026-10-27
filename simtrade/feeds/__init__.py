"""Sources de prix (bid/ask)."""

from simtrade.feeds.base import FallbackFeed, PriceFeed
from simtrade.feeds.binance import BinanceFeed
from simtrade.feeds.coinbase import CoinbaseFeed
from simtrade.feeds.static import StaticFeed


def default_feed() -> FallbackFeed:
    """Binance en priorité, Coinbase si Binance ne répond pas."""
    return FallbackFeed([BinanceFeed(), CoinbaseFeed()])


__all__ = ["BinanceFeed", "CoinbaseFeed", "FallbackFeed", "PriceFeed", "StaticFeed", "default_feed"]
