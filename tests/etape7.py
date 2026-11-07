"""Outils communs aux tests de l'étape 7 : faux Binance (MockTransport), lignes de klines, ordres."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from simtrade.history import to_ms
from simtrade.models import MARKET, Order, Side

D = Decimal
UTC = timezone.utc
T0 = datetime(2026, 5, 1, tzinfo=UTC)
MIN = timedelta(minutes=1)


def kline_row(t: datetime, o="100", h="101", l="99", c="100.5", v="2", n="201", trades=10, seconds=60) -> list:
    return [to_ms(t), o, h, l, c, v, to_ms(t + timedelta(seconds=seconds)) - 1, n, trades, "0", "0", "0"]


def minute_rows(start: datetime, count: int, skip: set[int] = frozenset()) -> list[list]:
    """`count` minutes à partir de `start`, prix différents par minute ; indices de `skip` absents (trous)."""
    rows = []
    for i in range(count):
        if i in skip:
            continue
        base = D(100) + D(i) / 10
        rows.append(kline_row(start + i * MIN, str(base), str(base + 1), str(base - 1), str(base + D("0.5")),
                              str(1 + i % 3), str((base + D("0.2")) * (1 + i % 3)), 5 + i))
    return rows


class FakeBinance:
    """Sert /api/v3/klines (startTime, endTime, limit) depuis des lignes par paire ; peut répondre 429 d'abord."""

    def __init__(self, rows_by_pair: dict[str, list[list]], fail_first: int = 0, status: int = 429,
                 weight: int | None = None, retry_after: str = "7"):
        self.rows = rows_by_pair
        self.requests: list[dict] = []
        self.fail_first = fail_first
        self.status = status
        self.weight = weight
        self.retry_after = retry_after

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        headers = {} if self.weight is None else {"X-MBX-USED-WEIGHT-1M": str(self.weight)}
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(self.status, headers={**headers, "Retry-After": self.retry_after}, json={})
        rows = self.rows.get(params["symbol"], [])
        lo = int(params.get("startTime", 0))
        hi = int(params.get("endTime", 10**15))
        selected = [r for r in rows if lo <= r[0] <= hi][: int(params["limit"])]
        return httpx.Response(200, headers=headers, json=selected)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="https://api.binance.com")


def order(oid: int, at: datetime, side: Side, qty: str, price: str, kind: str = MARKET, pair: str = "BTCUSDT",
          fee_rate: str = "0.001") -> Order:
    q, p = D(qty), D(price)
    gross = q * p
    fee = gross * D(fee_rate)
    flow = -(gross + fee) if side is Side.BUY else gross - fee
    return Order(1, pair, side, q, p, gross, fee, flow, D(0), "Test", at, id=oid, kind=kind)
