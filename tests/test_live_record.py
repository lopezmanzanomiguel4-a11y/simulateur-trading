"""Enregistrement live des bougies 1m définitives : hub → thread SQLite, jamais depuis la boucle. Sans réseau."""

import asyncio
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from etape7 import MIN, T0, D, kline_row
from simtrade import marche
from simtrade.api import create_app
from simtrade.feeds import StaticFeed
from simtrade.history import HistorySource, parse_kline
from simtrade.hub import PriceHub
from simtrade.market import ORIGIN_LIVE, Candle
from simtrade.marche import MarketStore
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("100", "100.02"), "ETHUSDT": ("10", "10.01"), "SOLUSDT": ("1", "1.001")}
NOW = T0 + 10 * MIN + timedelta(seconds=30)


class FakeHistory(HistorySource):
    name = "Fake"

    def __init__(self):
        self.calls = []

    async def klines(self, pair, interval, limit, start=None):
        self.calls.append((pair, interval, start))
        seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}[interval]
        first = start or T0
        rows = []
        t = first
        while t <= NOW:
            rows.append(parse_kline(kline_row(t, seconds=seconds), pair, interval))
            t += timedelta(seconds=seconds)
        return rows[-limit:]

    async def server_time(self):
        return NOW


def hub_with(history):
    return PriceHub(StaticFeed(PRICES), history=history, clock=lambda: NOW)


def test_prechargement_et_reconciliation_envoient_les_1m_cloturees():
    hub = hub_with(FakeHistory())
    received = []
    hub.on_definitive = received.append
    asyncio.run(hub.resync())
    assert {c.interval for batch in received for c in batch} == {"1m"}
    btc = [c for batch in received for c in batch if c.pair == "BTCUSDT"]
    assert [c.open_time for c in btc] == [T0 + i * MIN for i in range(10)]  # la minute en cours (10) exclue

    # Une bougie live clôturée, remplacée par sa kline à la réconciliation
    received.clear()
    series = hub.market.series[("BTCUSDT", "1m")]
    series.candles.append(Candle("BTCUSDT", "1m", T0 + 11 * MIN, D(1), D(1), D(1), D(1), origin=ORIGIN_LIVE))
    hub.clock = lambda: T0 + 13 * MIN

    async def later_klines(pair, interval, limit, start=None):
        return [parse_kline(kline_row(T0 + 11 * MIN), pair, interval)]

    hub.history.klines = later_klines
    asyncio.run(hub.reconcile())
    got = [(c.interval, c.open_time) for batch in received for c in batch if c.pair == "BTCUSDT"]
    assert got == [("1m", T0 + 11 * MIN)]


def test_erreur_du_callback_n_arrete_pas_le_hub():
    hub = hub_with(FakeHistory())

    def boom(candles):
        raise RuntimeError("base pleine")

    hub.on_definitive = boom
    assert asyncio.run(hub.resync()) is True


def test_api_enregistre_dans_le_thread_sqlite_jamais_dans_la_boucle(tmp_path, monkeypatch):
    threads = {"connect": [], "save": []}
    real_connect = marche.sqlite3.connect
    real_save = MarketStore.save

    class SpySqlite:
        def __getattr__(self, name):
            return getattr(__import__("sqlite3"), name)

        def connect(self, *args, **kwargs):
            threads["connect"].append(threading.current_thread().name)
            return real_connect(*args, **kwargs)

    def spy_save(self, candles, source=marche.DOWNLOAD):
        threads["save"].append(threading.current_thread().name)
        return real_save(self, candles, source)

    monkeypatch.setattr(marche, "sqlite3", SpySqlite())
    monkeypatch.setattr(MarketStore, "save", spy_save)
    source = StaticFeed(PRICES)
    hub = PriceHub(source)
    db = tmp_path / "trading.db"
    app = create_app(service=lambda feed: TradingService(Storage(db), feed), hub=hub)
    with TestClient(app) as client:
        loop_thread = client.portal.call(lambda: threading.current_thread().name)
        batch = [parse_kline(kline_row(T0 + i * MIN), "BTCUSDT", "1m") for i in range(3)]

        def from_loop():
            started = time.perf_counter()
            hub.on_definitive(batch)  # appelé par le hub depuis la boucle
            return time.perf_counter() - started

        client.portal.call(from_loop)
        deadline = time.time() + 5
        while not threads["save"] and time.time() < deadline:
            time.sleep(0.01)
        svc = app.state.service
        stored = client.portal.call(lambda: svc.run(lambda: svc.market_store().count("BTCUSDT")))
    assert stored == 3
    assert threads["save"] and threads["connect"]
    assert all(name.startswith("simtrade-db") for name in threads["save"] + threads["connect"])
    assert loop_thread not in threads["save"] + threads["connect"]
    assert (tmp_path / "marche.db").exists()  # à côté de la base de trading
