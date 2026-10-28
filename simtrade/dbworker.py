"""Thread SQLite dédié de l'API.

Toute la base est créée et utilisée dans UN thread : les accès sont sérialisés (file FIFO du
ThreadPoolExecutor) sans verrou applicatif, et une attente de verrou SQLite (busy timeout,
CLI qui écrit) ne bloque plus la boucle asyncio, donc ni le flux de prix ni ses pings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

log = logging.getLogger("simtrade.db")

T = TypeVar("T")


class DbWorker:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="simtrade-db")

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Exécute `fn` dans le thread SQLite et attend son résultat sans bloquer la boucle."""
        return await asyncio.get_running_loop().run_in_executor(self.executor, partial(fn, *args, **kwargs))

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        """Met `fn` dans la file du thread SQLite sans attendre (appel synchrone possible depuis la boucle)."""
        return self.executor.submit(fn, *args, **kwargs)

    def close(self) -> None:
        self.executor.shutdown(wait=True)
