"""Flux de cotations (étape 6, point 0) : métriques de la file de réception, débit, coupures keepalive.
Faux websocket et sources simulées, sans réseau."""

import asyncio
from decimal import Decimal

from websockets.asyncio.messages import Assembler

from simtrade.feeds.base import FlowStats, PriceFeed
from simtrade.hub import PriceHub
from simtrade.models import Quote, utcnow

D = Decimal


class FakeWs:
    def __init__(self, high=16):
        self.recv_messages = Assembler(high=high)


def test_file_de_reception_et_pauses_mesurees():
    async def scenario():
        ws, stats = FakeWs(high=4), FlowStats()
        stats.sample(ws)
        assert (stats.queue_max, stats.pauses) == (0, 0)
        for _ in range(6):  # 6 trames > 4 : la bibliothèque met la lecture du socket en pause
            ws.recv_messages.put(object())
        stats.sample(ws)
        assert ws.recv_messages.paused and (stats.queue_max, stats.pauses) == (6, 1)
        stats.sample(ws)
        assert stats.pauses == 1  # toujours la même pause

    asyncio.run(scenario())


def test_sans_file_websockets_rien_n_est_mesure():
    stats = FlowStats()
    stats.sample(object())
    assert (stats.queue_max, stats.pauses) == (0, 0)


def quote(pair="BTCUSDT"):
    return Quote(pair, D("100"), D("100.01"), "Test", utcnow())


def test_debit_et_duree_de_traitement():
    hub = PriceHub(_NoFeed())
    for _ in range(50):
        hub.update(quote())
    assert hub.update_max > 0 and hub._rate_count + hub.rate_last >= 50


class _NoFeed(PriceFeed):
    name = "Rien"

    async def stream(self, pairs):
        if False:
            yield


class _Keepalive(PriceFeed):
    """Une cotation puis une coupure keepalive, deux fois, puis une erreur réseau ordinaire."""

    name = "Test"

    def __init__(self):
        self.calls = 0

    async def stream(self, pairs):
        self.calls += 1
        yield quote()
        if self.calls <= 2:
            raise ConnectionError("sent 1011 (internal error) keepalive ping timeout; no close frame received")
        raise ConnectionError("réseau")


def test_coupures_keepalive_comptees():
    feed = _Keepalive()
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    hub = PriceHub(feed, sleep=sleep)
    try:
        asyncio.run(hub.run_quotes())
    except asyncio.CancelledError:
        pass
    assert hub.keepalive_timeouts == 2 and hub.reconnects == 2


class _Hangs(PriceFeed):
    """Une cotation, puis plus rien (connexion bloquée sans être fermée)."""

    name = "Test"

    def __init__(self):
        self.closed = 0

    async def stream(self, pairs):
        try:
            yield quote()
            await asyncio.Event().wait()
        finally:
            self.closed += 1


def test_chien_de_garde_coupe_une_connexion_muette():
    feed = _Hangs()

    async def sleep(delay):
        raise asyncio.CancelledError

    hub = PriceHub(feed, sleep=sleep, quote_silence=0.05)
    try:
        asyncio.run(hub.run_quotes())
    except asyncio.CancelledError:
        pass
    assert "chien de garde" in hub.last_error and feed.closed == 1 and "BTCUSDT" in hub.quotes


class _Slow(PriceFeed):
    """Trois cotations espacées de 20 ms, puis fin du flux."""

    name = "Test"

    async def stream(self, pairs):
        for _ in range(3):
            yield quote()
            await asyncio.sleep(0.02)


def test_chien_de_garde_ne_coupe_pas_un_flux_vivant():
    async def sleep(delay):
        raise asyncio.CancelledError

    hub = PriceHub(_Slow(), sleep=sleep, quote_silence=0.5)
    try:
        asyncio.run(hub.run_quotes())
    except asyncio.CancelledError:
        pass
    assert hub.last_error == "flux terminé" and hub._rate_count + hub.rate_last == 3
