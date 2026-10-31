"""Exécution des stratégies actives (dans l'API) : aucune règle ici, seulement de la plomberie.

Chaque seconde, pour chaque stratégie active :
1. dernière bougie CLÔTURÉE de son intervalle ; rien à faire si elle a déjà été traitée ;
2. attente qu'elle soit définitive (remplacée par la kline Binance, ainsi que les bougies 1m qui
   la composent), au plus `max_wait` : le signal est alors le même qu'au backtest ;
3. strategies.evaluate sur une vue datée de la clôture de la bougie (pur, même code qu'au backtest) ;
4. signal : raisons d'ignorer (qualité, heures, session, position), sinon ordre dans le thread SQLite
   (service.handle_entry_signal / handle_exit_signal : ordre et journal dans une seule transaction).

Après un redémarrage, seule une bougie clôturée depuis moins d'un intervalle est évaluée, et la base
refuse une seconde décision sur la même bougie : un signal ne produit jamais deux ordres.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from simtrade import bricks, strategies
from simtrade.hub import PriceHub
from simtrade.market import INTERVALS, ORIGIN_REST, Candle, session_start
from simtrade.models import PAIRS
from simtrade.service import ENTRY, EXIT, TradingService
from simtrade.storage import ActivationRecord
from simtrade.strategies import Evaluation, Strategy

log = logging.getLogger("simtrade.runner")

TICK = 1.0  # secondes
MAX_WAIT = timedelta(seconds=20)  # attente maximale de la kline définitive après la clôture
MAX_ATTEMPTS = 3  # essais d'une même bougie en cas d'erreur (panne SQLite...) ; un refus n'est jamais réessayé


@dataclass
class RunnerState:
    """Dernière évaluation d'une stratégie (exposée par l'API)."""

    strategy: str
    candle: datetime | None = None
    evaluated_at: datetime | None = None
    entry: bool | None = None
    exit: bool | None = None
    reasons: tuple[str, ...] = ()
    decision: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


def jsonable(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if isinstance(v, Decimal):
            out[k] = format(v, "f")
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def definitive(candle: Candle, candles_1m: list[Candle]) -> bool:
    """Bougie remplacée par sa kline, de même que les bougies 1m qui la composent."""
    if candle.origin != ORIGIN_REST:
        return False
    inside = [c for c in candles_1m if candle.open_time <= c.open_time < candle.close_time]
    expected = INTERVALS[candle.interval] // 60
    return len(inside) == expected and all(c.origin == ORIGIN_REST for c in inside)


class StrategyRunner:
    def __init__(
        self,
        service: TradingService,
        hub: PriceHub,
        clock: Callable[[], datetime] | None = None,
        every: float = TICK,
        max_wait: timedelta = MAX_WAIT,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.service = service
        self.hub = hub
        self.clock = clock or service.clock
        self.every = every
        self.max_wait = max_wait
        self._sleep = sleep
        self.states: dict[int, RunnerState] = {}  # par compte
        self._done: dict[int, datetime] = {}  # par activation : dernière bougie traitée (la base fait foi)
        self._attempts: dict[tuple[int, datetime], int] = {}
        self._parsed: dict[int, Strategy] = {}
        self.max_eval_ms = 0.0
        self.evaluations = 0

    async def tick(self) -> None:
        actives = await self.service.run(self.service.active_strategies)
        alive = {a.account_id for a in actives}
        for account_id in list(self.states):
            if account_id not in alive:
                del self.states[account_id]
        for activation in actives:
            try:
                await self.step(activation)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Stratégie %s (compte n°%s) en erreur.", activation.name, activation.account_id)

    def _strategy(self, activation: ActivationRecord) -> Strategy:
        if activation.id not in self._parsed:
            self._parsed[activation.id] = strategies.loads(activation.definition)
        return self._parsed[activation.id]

    async def step(self, activation: ActivationRecord) -> None:
        strategy = self._strategy(activation)
        state = self.states.get(activation.account_id)
        if state is None or state.strategy != strategy.nom:
            state = self.states[activation.account_id] = RunnerState(strategy.nom)
        now = self.clock()
        candles = bricks.closed(self.hub.market.candles(strategy.paire, strategy.intervalle), now)
        if not candles:
            state.reasons = ("pas_de_bougie",)
            return
        last = candles[-1]
        if self._done.get(activation.id) == last.open_time:
            return
        step = timedelta(seconds=INTERVALS[strategy.intervalle])
        if now - last.close_time >= step:
            # Bougie trop ancienne (redémarrage, flux coupé) : pas de rattrapage de signaux passés.
            self._done[activation.id] = last.open_time
            return
        candles_1m = bricks.closed(self.hub.market.candles(strategy.paire, "1m"), now)
        final = definitive(last, candles_1m)
        if not final and now - last.close_time < self.max_wait:
            return  # on attend la kline
        key = (activation.id, last.open_time)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        if self._attempts[key] >= MAX_ATTEMPTS:
            self._done[activation.id] = last.open_time  # dernier essai : la bougie ne sera plus reprise
        view = bricks.MarketView(last.close_time, candles, candles_1m)
        context = await self.service.run(self.service.strategy_context, activation.account_id, strategy.paire)
        started = time.perf_counter()
        evaluation = strategies.evaluate(strategy, view, context.entered_at if context.has_position else None)
        elapsed = (time.perf_counter() - started) * 1000
        self.max_eval_ms = max(self.max_eval_ms, elapsed)
        self.evaluations += 1
        state.candle, state.evaluated_at = last.open_time, now
        state.entry, state.exit, state.values = evaluation.entry, evaluation.exit, jsonable(evaluation.values)
        state.reasons, state.decision = (), None

        data_reasons = self._quality_reasons(strategy, now, candles_1m)
        if not final:
            data_reasons = (strategies.NOT_DEFINITIVE, *data_reasons)
        if context.has_position and evaluation.exit:
            await self._exit(activation, strategy, evaluation, data_reasons, state)
        if evaluation.entry:
            reasons = data_reasons + strategies.entry_blockers(
                strategy, now, context.rules, context.has_position, context.halted
            )
            await self._entry(activation, strategy, evaluation, reasons, state)
        # Traitée sans erreur : plus de nouvel essai sur cette bougie (une erreur avant ce point la fait reprendre
        # au tick suivant ; la base garantit qu'une bougie déjà décidée ne produit pas de second ordre).
        self._done[activation.id] = last.open_time
        self._attempts.pop(key, None)

    def _quality_reasons(self, strategy: Strategy, now: datetime, candles_1m: list[Candle]) -> tuple[str, ...]:
        today = [c for c in candles_1m if c.open_time >= session_start(now)]
        volume = sum((c.volume for c in today), Decimal(0))
        qualities = [
            self.hub.quality(strategy.paire, strategy.intervalle, volume),
            self.hub.quality(strategy.paire, "1m", volume),
        ]
        return strategies.quality_blockers(strategy, qualities)

    def _details(self, strategy: Strategy, evaluation: Evaluation, extra: dict | None = None) -> dict:
        details = {"strategie": strategy.nom, "valeurs": jsonable(evaluation.values)}
        if evaluation.exit_reasons:
            details["briques_sortie"] = list(evaluation.exit_reasons)
        return {**details, **(extra or {})}

    async def _entry(
        self,
        activation: ActivationRecord,
        strategy: Strategy,
        evaluation: Evaluation,
        reasons: tuple[str, ...],
        state: RunnerState,
    ) -> None:
        quotes = self.service._peek(PAIRS)
        if not reasons and strategy.paire not in quotes:
            reasons = (strategies.NO_FRESH_QUOTE,)
        details = self._details(strategy, evaluation)
        if reasons:
            await self.service.run(
                self.service.record_ignored, activation.id, evaluation.candle_open, ENTRY, reasons, details
            )
            state.reasons, state.decision = reasons, "ignore"
            return
        signal = await self.service.run(
            self.service.handle_entry_signal, activation.id, evaluation.candle_open, evaluation.plan, quotes, details
        )
        if signal is not None:
            state.reasons, state.decision = signal.reasons, signal.decision

    async def _exit(
        self,
        activation: ActivationRecord,
        strategy: Strategy,
        evaluation: Evaluation,
        reasons: tuple[str, ...],
        state: RunnerState,
    ) -> None:
        quote = self.service._peek([strategy.paire]).get(strategy.paire)
        if not reasons and quote is None:
            reasons = (strategies.NO_FRESH_QUOTE,)
        details = self._details(strategy, evaluation)
        if reasons:
            await self.service.run(
                self.service.record_ignored, activation.id, evaluation.candle_open, EXIT, reasons, details
            )
            state.reasons, state.decision = reasons, "ignore"
            return
        signal = await self.service.run(
            self.service.handle_exit_signal, activation.id, evaluation.candle_open, quote, details
        )
        if signal is not None:
            state.reasons, state.decision = signal.reasons, signal.decision

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Exécution des stratégies en erreur, nouvel essai dans %g s.", self.every)
            await self._sleep(self.every)
