import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from simtrade.engine import Portfolio
from simtrade.models import Account, Quote

D = Decimal


def quote(pair: str = "BTCUSDT", bid: str = "100", ask: str = "100") -> Quote:
    return Quote(pair, D(bid), D(ask), "Test", datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(Account(1, D("10000"), D("10000"), datetime(2026, 1, 1, tzinfo=timezone.utc)))


# --- Aucun accès réseau pendant les tests ---

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class NetworkAccess(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def pas_de_reseau(monkeypatch):
    """Toute résolution de nom ou connexion hors loopback échoue (la boucle asyncio de Windows
    a besoin du loopback pour son socketpair interne)."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in LOOPBACK:
            raise NetworkAccess(f"Accès réseau interdit pendant les tests : {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in LOOPBACK:
            raise NetworkAccess(f"Connexion interdite pendant les tests : {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
