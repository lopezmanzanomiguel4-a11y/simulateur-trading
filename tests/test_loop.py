"""Boucle asyncio : retard mesuré, logs horodatés en UTC, aucun SQLite dans l'écouteur de cotations."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from etape4 import NOON, Clock
from simtrade.dbworker import DbWorker
from simtrade.feeds import StaticFeed
from simtrade.hub import CachedFeed, PriceHub
from simtrade.loopmon import DATE_FORMAT, LOG_FORMAT, LoopMonitor, UtcFormatter, log_config
from simtrade.models import PAIRS, STOP, Quote
from simtrade.service import TradingService
from simtrade.storage import Storage
from simtrade.watcher import OrderWatcher

D = Decimal


def test_retard_de_boucle_detecte_et_journalise(caplog):
    monitor = LoopMonitor(interval=0.05, threshold=0.2)

    async def main():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.12)
        time.sleep(0.3)  # appel bloquant sur la boucle
        await asyncio.sleep(0.12)
        task.cancel()

    with caplog.at_level(logging.WARNING, logger="simtrade.boucle"):
        asyncio.run(main())
    assert monitor.alerts >= 1 and monitor.max_lag >= 0.25
    assert any("Boucle asyncio en retard" in r.getMessage() for r in caplog.records)


def test_pas_d_alerte_sans_blocage():
    monitor = LoopMonitor(interval=0.02, threshold=0.2)

    async def main():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        task.cancel()

    asyncio.run(main())
    assert monitor.alerts == 0


def test_logs_horodates_en_utc_a_la_milliseconde():
    formatter = UtcFormatter(LOG_FORMAT, DATE_FORMAT)
    record = logging.LogRecord("simtrade.hub", logging.WARNING, __file__, 1, "Flux de prix coupé", None, None)
    when = datetime(2026, 10, 28, 18, 1, 36, tzinfo=timezone.utc).timestamp()
    record.created, record.msecs = when + 0.506, 506  # en heure locale (UTC+1), on lirait 19:01
    assert formatter.format(record) == "2026-10-28 18:01:36.506 UTC WARNING simtrade.hub : Flux de prix coupé"


def test_configuration_des_logs_couvre_simtrade():
    import logging.config

    config = log_config()
    assert config["loggers"]["simtrade"] == {"handlers": ["default"], "level": "INFO", "propagate": False}
    assert config["formatters"]["default"]["()"] == "simtrade.loopmon.UtcFormatter"
    logging.config.dictConfig(config)  # la configuration est valide (uvicorn l'appliquera)
    logging.getLogger("simtrade").handlers.clear()
    logging.getLogger("simtrade").propagate = True


@pytest.fixture
def api_like(tmp_path):
    """Service comme dans l'API : base créée dans le thread SQLite, watcher branché sur le hub."""
    clock = Clock()
    hub = PriceHub(StaticFeed(), clock=clock)
    feed = CachedFeed(hub, clock=clock)
    db = DbWorker()
    storage = db.submit(Storage, tmp_path / "loop.db").result()
    service = TradingService(storage, feed, clock=clock, db=db)
    watcher = OrderWatcher(service)
    db.submit(watcher.load).result()
    hub.listeners.append(watcher.on_quote)

    def quote(pair, bid, age=0):
        hub.update(Quote(pair, D(bid), D(bid), "Test", clock() - timedelta(seconds=age)))

    yield service, hub, db, quote
    db.submit(storage.close).result()
    db.close()


def test_l_ecouteur_ne_touche_jamais_sqlite(api_like, caplog):
    service, hub, db, quote = api_like
    quote("BTCUSDT", "100")
    asyncio.run(service.buy("btc", "1001"))
    asyncio.run(service.place_order("btc", STOP, "vente", "95", "tout"))
    # La connexion appartient au thread SQLite : un accès depuis ce thread-ci lèverait ProgrammingError.
    with caplog.at_level(logging.ERROR):
        quote("BTCUSDT", "94")
    db.submit(lambda: None).result()  # vide la file du thread SQLite
    orders = db.submit(service.storage.orders).result()
    assert orders[-1].kind == STOP
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_aucun_ordre_perdu_ni_execute_deux_fois_sur_1000_cotations(api_like):
    service, hub, db, quote = api_like
    for pair, price in zip(PAIRS, ("100", "2000", "50")):
        quote(pair, price)
        asyncio.run(service.buy(pair, "1001"))
        asyncio.run(service.place_order(pair, STOP, "vente", str(D(price) * D("0.95")), "tout"))
    for i in range(1000):
        pair, price = PAIRS[i % 3], ("100", "2000", "50")[i % 3]
        factor = D("0.9") if i >= 500 else D("1")  # la moitié des cotations traverse les stops
        quote(pair, str(D(price) * factor))
    db.submit(lambda: None).result()
    orders = db.submit(service.storage.orders).result()
    stops = [o for o in orders if o.kind == STOP]
    assert sorted(o.pair for o in stops) == sorted(PAIRS)  # un stop exécuté par paire, pas plus
    assert db.submit(service.storage.pending_orders).result() == []
