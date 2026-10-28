"""Historique public Binance (REST, sans compte) : klines et heure du serveur.

GET /api/v3/klines renvoie au plus 1000 bougies par requête, triées de la plus ancienne à
la plus récente ; la dernière est la bougie en cours. GET /api/v3/time donne l'heure du serveur.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import httpx

from simtrade.errors import FeedUnavailable
from simtrade.market import INTERVALS, ORIGIN_REST, Candle
from simtrade.models import EPOCH, clean_decimal

BINANCE_REST = "https://api.binance.com"
PAGE_LIMIT = 1000  # maximum autorisé par Binance pour /api/v3/klines
MS = timedelta(milliseconds=1)


def to_ms(dt: datetime) -> int:
    return (dt - EPOCH) // MS


def from_ms(ms: int | str) -> datetime:
    return EPOCH + int(ms) * MS


def parse_kline(row: list, pair: str, interval: str) -> Candle:
    """[openTime, open, high, low, close, volume, closeTime, quoteVolume, nbTrades, ...] -> Candle."""
    return Candle(
        pair=pair,
        interval=interval,
        open_time=from_ms(row[0]),
        open=clean_decimal(row[1]),
        high=clean_decimal(row[2]),
        low=clean_decimal(row[3]),
        close=clean_decimal(row[4]),
        volume=clean_decimal(row[5]),
        notional=clean_decimal(row[7]),
        transactions=int(row[8]),
        origin=ORIGIN_REST,
    )


class HistorySource(ABC):
    name: str = "?"

    @abstractmethod
    async def klines(self, pair: str, interval: str, limit: int, start: datetime | None = None) -> list[Candle]:
        """Sans `start` : les `limit` dernières bougies. Avec `start` : depuis `start` jusqu'à maintenant (au plus `limit`)."""

    @abstractmethod
    async def server_time(self) -> datetime:
        """Heure du serveur de l'exchange."""

    async def aclose(self) -> None:
        pass


class BinanceHistory(HistorySource):
    name = "Binance"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str = BINANCE_REST,
        page_size: int = PAGE_LIMIT,
        timeout: float = 10.0,
    ):
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.page_size = page_size

    async def _get(self, path: str, params: dict | None = None):
        try:
            response = await self.client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FeedUnavailable(f"Historique Binance indisponible ({path}) : {str(exc) or type(exc).__name__}") from exc

    async def _page(self, pair: str, interval: str, limit: int, **bounds: int) -> list[Candle]:
        params = {"symbol": pair, "interval": interval, "limit": limit, **bounds}
        rows = await self._get("/api/v3/klines", params)
        return [parse_kline(row, pair, interval) for row in rows]

    async def klines(self, pair: str, interval: str, limit: int, start: datetime | None = None) -> list[Candle]:
        candles: list[Candle] = []
        if start is None:
            # De la plus récente vers le passé : endTime = début de la plus ancienne reçue - 1 ms.
            end: int | None = None
            while len(candles) < limit:
                size = min(self.page_size, limit - len(candles))
                page = await self._page(pair, interval, size, **({} if end is None else {"endTime": end}))
                candles = page + candles
                if len(page) < size:
                    break  # plus d'historique disponible
                end = to_ms(page[0].open_time) - 1
            return candles
        step = timedelta(seconds=INTERVALS[interval])
        begin = to_ms(start)
        while True:
            page = await self._page(pair, interval, self.page_size, startTime=begin)
            candles += page
            if len(page) < self.page_size:
                break  # présent rattrapé
            begin = to_ms(page[-1].open_time + step)
        return candles[-limit:]

    async def server_time(self) -> datetime:
        data = await self._get("/api/v3/time")
        return from_ms(data["serverTime"])

    async def aclose(self) -> None:
        await self.client.aclose()
