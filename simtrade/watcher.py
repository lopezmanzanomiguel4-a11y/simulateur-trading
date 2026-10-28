"""Surveillance permanente (dans l'API) : ordres en attente et fin de session.

OrderWatcher est appelé par PriceHub pour chaque cotation, de façon synchrone : aucune
cotation n'est sautée. Il garde les ordres en attente en mémoire, par paire. Un ordre touché
est mis dans la file du thread SQLite (service.db) : la boucle asyncio ne touche jamais la base
et ne peut donc pas être bloquée par un verrou SQLite. Sans thread SQLite (CLI, tests),
l'ordre est exécuté directement. L'index est rechargé après chaque changement fait par le
service, et quand une autre connexion (la CLI) a écrit dans la base.

SessionScheduler vérifie chaque seconde si un compte auto doit être fermé (fin de session,
ou rattrapage après un arrêt) ; si la cotation est périmée, il réessaie à la seconde suivante.
Il enregistre aussi l'arrêt de session d'un compte auto qui a atteint sa perte max.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from simtrade.hub import MAX_QUOTE_AGE
from simtrade.models import PendingOrder, Quote
from simtrade.orders import triggered
from simtrade.service import SessionClose, TradeResult, TradingService

log = logging.getLogger("simtrade.watcher")

TICK = 1.0  # secondes


class OrderWatcher:
    def __init__(
        self,
        service: TradingService,
        clock: Callable[[], datetime] | None = None,
        max_age: float = MAX_QUOTE_AGE,
    ):
        self.service = service
        self.clock = clock or service.clock
        self.max_age = max_age
        self.index: dict[str, list[PendingOrder]] = {}
        self._version: int | None = None
        self._inflight: set[int] = set()  # ordres déjà dans la file du thread SQLite
        service.listeners.append(self.load)

    def load(self) -> None:
        index: dict[str, list[PendingOrder]] = {}
        for order in self.service.watched_orders():
            index.setdefault(order.pair, []).append(order)
        self.index = index
        self._version = self.service.storage.data_version()

    def refresh(self) -> None:
        """Recharge si une autre connexion a modifié la base depuis le dernier chargement."""
        if self.service.storage.data_version() != self._version:
            self.load()

    def on_quote(self, quote: Quote) -> list[TradeResult]:
        """Exécute les ordres touchés par `quote` ; ignore une cotation périmée.

        Avec un thread SQLite, l'exécution est mise en file (FIFO) et la liste renvoyée est vide."""
        if (self.clock() - quote.received_at).total_seconds() > self.max_age:
            return []
        done: list[TradeResult] = []
        db = self.service.db
        for order in list(self.index.get(quote.pair, ())):
            if not triggered(order, quote):
                continue
            if db is None:
                result = self.service.execute_pending(order.id, quote)
                if result is not None:
                    done.append(result)
            elif order.id not in self._inflight:
                self._inflight.add(order.id)
                db.submit(self._execute, order.id, quote)
        return done

    def _execute(self, pending_id: int, quote: Quote) -> TradeResult | None:
        """Dans le thread SQLite : exécute l'ordre mis en file."""
        try:
            return self.service.execute_pending(pending_id, quote)
        except Exception:
            log.exception("Exécution de l'ordre en attente n°%s en erreur.", pending_id)
            return None
        finally:
            self._inflight.discard(pending_id)


class SessionScheduler:
    def __init__(
        self,
        service: TradingService,
        watcher: OrderWatcher | None = None,
        every: float = TICK,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.service = service
        self.watcher = watcher
        self.every = every
        self._sleep = sleep

    async def tick(self) -> list[SessionClose]:
        if self.watcher is not None:
            await self.service.run(self.watcher.refresh)
        await self.service.record_halts()
        return await self.service.close_due_sessions()

    async def run(self) -> None:
        """Tourne jusqu'à annulation ; le premier passage fait le rattrapage au démarrage."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Vérification de fin de session en erreur, nouvel essai dans %g s.", self.every)
            await self._sleep(self.every)
