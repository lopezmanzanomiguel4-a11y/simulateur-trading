"""Diagnostic de l'API : logs horodatés en UTC et mesure du retard de la boucle asyncio.

Une boucle bloquée (calcul long, attente de verrou SQLite...) n'envoie plus les pings des
websockets : Binance coupe alors la connexion (« keepalive ping timeout »). LoopMonitor dort
`interval` secondes en boucle et mesure de combien il se réveille en retard.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Awaitable, Callable

from uvicorn.logging import AccessFormatter

log = logging.getLogger("simtrade.boucle")

LAG_INTERVAL = 0.1  # secondes
LAG_THRESHOLD = 0.2  # au-delà, un avertissement est journalisé
LOG_FORMAT = "%(asctime)s.%(msecs)03d UTC %(levelname)s %(name)s : %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


class UtcAccessFormatter(AccessFormatter):
    """Lignes d'accès de uvicorn (client, requête, statut), en heure UTC."""

    converter = time.gmtime


class LoopMonitor:
    def __init__(
        self,
        interval: float = LAG_INTERVAL,
        threshold: float = LAG_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.interval = interval
        self.threshold = threshold
        self.clock = clock
        self._sleep = sleep
        self.last_lag = 0.0
        self.max_lag = 0.0
        self.alerts = 0

    def record(self, lag: float) -> None:
        self.last_lag = lag
        self.max_lag = max(self.max_lag, lag)
        if lag > self.threshold:
            self.alerts += 1
            log.warning("Boucle asyncio en retard de %.0f ms (seuil %.0f ms).", lag * 1000, self.threshold * 1000)

    async def run(self) -> None:
        while True:
            start = self.clock()
            await self._sleep(self.interval)
            self.record(max(self.clock() - start - self.interval, 0.0))


def log_config() -> dict:
    """Configuration de uvicorn étendue à simtrade.*, avec heure UTC et millisecondes partout."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["formatters"]["default"] = {"()": "simtrade.loopmon.UtcFormatter", "fmt": LOG_FORMAT, "datefmt": DATE_FORMAT}
    config["formatters"]["access"] = {
        "()": "simtrade.loopmon.UtcAccessFormatter",
        "fmt": '%(asctime)s.%(msecs)03d UTC %(levelname)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        "datefmt": DATE_FORMAT,
        "use_colors": False,
    }
    config["loggers"]["simtrade"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config
