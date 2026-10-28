"""Couche applicative : relie moteur, stockage, garde-fou et source de prix.

C'est le seul point d'entrée que les interfaces (CLI, API FastAPI) doivent appeler.
Les méthodes qui touchent au réseau sont asynchrones. `compte` désigne un compte ouvert
par son nom ou son numéro ; sans `compte`, c'est le compte courant.

Dans l'API, toute la base passe par un thread dédié (`db`, voir dbworker.py) : les méthodes
asynchrones y envoient leur partie SQLite avec `run()`, et les routes appellent les méthodes
synchrones avec `run()` aussi. Sans `db` (CLI, tests), `run()` appelle directement.

Garde-fou (risk.py) : appelé avant chaque ENTRÉE d'un compte automatique (achat au marché,
pose et déclenchement d'un achat limite). Les sorties n'y passent jamais.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from simtrade import fmt
from simtrade import orders as rules_orders
from simtrade.dbworker import DbWorker
from simtrade.engine import DEFAULT_CAPITAL, Portfolio, Valuation, normalize_pair
from simtrade.errors import FeedUnavailable, InvalidInput, OrderRejected
from simtrade.feeds.base import PriceFeed
from simtrade.models import (
    AUTO,
    CANCELLED,
    FILLED,
    LATE_SESSION_CLOSE,
    LIMIT,
    MANUAL,
    PAIRS,
    PENDING,
    REJECTED,
    SESSION_CLOSE,
    STOP,
    TARGET,
    Account,
    Order,
    PendingOrder,
    Quote,
    Side,
    utcnow,
)
from simtrade.risk import (
    LIMIT_PLACED,
    LIMIT_TRIGGERED,
    MARKET_ENTRY,
    MAX_LOSS,
    STOP_ALREADY_HIT,
    EntryRequest,
    RiskLimits,
    RiskRefused,
    RiskState,
    check_entry,
    loss_breached,
    update_limits,
)
from simtrade.sessions import SessionRules, close_due, entry_allowed, is_late, session_date, validate_rules
from simtrade.storage import RefusalRecord, Storage

log = logging.getLogger("simtrade.service")

T = TypeVar("T")


@dataclass(frozen=True)
class TradeResult:
    order: Order
    cash: Decimal  # solde USDT après l'ordre


@dataclass(frozen=True)
class SessionClose:
    account: Account
    day: date  # session fermée
    closed_at: datetime
    late: bool
    orders: list[Order]
    cancelled: int  # ordres en attente annulés


class TradingService:
    def __init__(
        self,
        storage: Storage,
        feed: PriceFeed,
        clock: Callable[[], datetime] = utcnow,
        db: DbWorker | None = None,
    ):
        self.storage = storage
        self.feed = feed
        self.clock = clock
        self.db = db
        self.listeners: list[Callable[[], None]] = []  # prévenus après tout changement d'ordre en attente

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Exécute une partie SQLite dans le thread de la base (directement s'il n'y en a pas)."""
        if self.db is None:
            return fn(*args, **kwargs)
        return await self.db.run(fn, *args, **kwargs)

    def _notify(self) -> None:
        for listener in self.listeners:
            listener()

    # --- Prix ---

    async def prices(self, pairs: Sequence[str] = PAIRS) -> dict[str, Quote]:
        return await self.feed.snapshot(list(pairs))

    def stream_prices(self, pairs: Sequence[str] = PAIRS) -> AsyncIterator[Quote]:
        return self.feed.stream(list(pairs))

    async def _fresh_quotes(self, pairs: Iterable[str]) -> dict[str, Quote]:
        """Cotations fraîches des paires ; une paire sans cotation fraîche est absente du résultat."""
        wanted = sorted(set(pairs))
        if not wanted:
            return {}
        try:
            return await self.feed.snapshot(wanted)
        except FeedUnavailable:
            quotes: dict[str, Quote] = {}
            for pair in wanted:
                try:
                    quotes.update(await self.feed.snapshot([pair]))
                except FeedUnavailable:
                    pass
            return quotes

    def _peek(self, pairs: Iterable[str]) -> dict[str, Quote]:
        """Lecture synchrone du cache de prix (API) ; vide pour une source sans cache."""
        peek = getattr(self.feed, "peek", None)
        return peek(sorted(set(pairs))) if peek is not None else {}

    # --- Ordres au marché ---

    async def buy(
        self, pair: str, amount_usdt: object, *, compte: object = None, stop: object = None, target: object = None
    ) -> TradeResult:
        """Achat au marché ; avec `stop` (et `target`), pose en plus le stop de protection de la paire,
        dans la même transaction. Obligatoire sur un compte automatique."""
        pair = normalize_pair(pair)
        stop_price = None if stop is None else rules_orders.parse_price(stop, "Stop")
        target_price = None if target is None else rules_orders.parse_price(target, "Objectif")
        held = await self.run(self._precheck_buy, compte, amount_usdt)  # refus immédiat, sans réseau
        quote = (await self.feed.snapshot([pair]))[pair]
        quotes = await self._fresh_quotes(held)
        quotes[pair] = quote
        return await self.run(self._buy, compte, pair, amount_usdt, stop_price, target_price, quotes)

    buy_with_stop = buy

    def _precheck_buy(self, compte: object, amount_usdt: object) -> list[str]:
        account = self.storage.account(compte)
        self._check_entry(account)
        portfolio = self.storage.load_portfolio(account.id)
        portfolio.check_buy(amount_usdt)
        return list(portfolio.positions)

    def _buy(
        self,
        compte: object,
        pair: str,
        amount_usdt: object,
        stop: Decimal | None,
        target: Decimal | None,
        quotes: dict[str, Quote],
    ) -> TradeResult:
        quote = quotes[pair]
        now = self.clock()
        try:
            with self.storage.transaction():
                account = self.storage.account(compte)
                self._check_entry(account)
                portfolio = self._open_portfolio(account.id)
                amount = portfolio.check_buy(amount_usdt)
                if account.mode == AUTO:
                    self._guard(account, EntryRequest(pair, amount, quote.ask, stop, MARKET_ENTRY, now), quotes)
                if stop is not None:
                    self._check_protection(pair, stop, target, quote)
                order = portfolio.buy(quote, amount, now)
                saved = self.storage.save_trade(portfolio, order)
                if stop is not None:
                    self._set_protection(account.id, pair, stop, target, now)
        except RiskRefused as exc:
            self._log_refusal(exc.record)
            raise
        if stop is not None:
            self._notify()
        return TradeResult(saved, portfolio.account.cash)

    async def sell(self, pair: str, quantity: object, *, compte: object = None) -> TradeResult:
        pair = normalize_pair(pair)
        await self.run(self._precheck_sell, compte, pair, quantity)
        quote = (await self.feed.snapshot([pair]))[pair]
        return await self.run(self._sell, compte, pair, quantity, quote)

    def _precheck_sell(self, compte: object, pair: str, quantity: object) -> None:
        account = self.storage.account(compte)
        self.storage.load_portfolio(account.id).check_sell(pair, quantity)

    def _sell(self, compte: object, pair: str, quantity: object, quote: Quote) -> TradeResult:
        now = self.clock()
        with self.storage.transaction():
            account = self.storage.account(compte)
            portfolio = self._open_portfolio(account.id)
            order = portfolio.sell(quote, quantity, now)
            saved = self.storage.save_trade(portfolio, order)
            closed = self._after_exit(account.id, portfolio, pair, now)
        if closed:
            self._notify()
        return TradeResult(saved, portfolio.account.cash)

    def _open_portfolio(self, account_id: int) -> Portfolio:
        portfolio = self.storage.load_portfolio(account_id)
        if portfolio.account.archived:
            raise OrderRejected(f"Le compte n°{account_id} vient d'être archivé.")
        return portfolio

    def _check_entry(self, account: Account) -> None:
        """Compte auto : aucune entrée après l'heure limite de la session."""
        if account.mode != AUTO:
            return
        rules = self.storage.session_rules()
        if not entry_allowed(self.clock(), rules):
            raise OrderRejected(
                f"Hors session : plus d'entrée sur le compte automatique '{account.name}' "
                f"après {rules.last_entry:%H:%M} UTC."
            )

    # --- Garde-fou ---

    def _risk_state(
        self, account: Account, quotes: dict[str, Quote], now: datetime, exclude_pending: int | None = None
    ) -> RiskState:
        """Mesure le compte pour le garde-fou ; `quotes` ne contient que des cotations fraîches."""
        day = session_date(now)
        portfolio = self.storage.load_portfolio(account.id)
        pending_buys = [
            p for p in self.storage.pending_orders(account.id) if p.side is Side.BUY and p.id != exclude_pending
        ]
        orders = self.storage.orders(account.id)
        exposure: dict[str, Decimal] = {}
        stale: list[str] = []
        positions_value = Decimal(0)
        unrealized = Decimal(0)
        for pair, position in portfolio.positions.items():
            quote = quotes.get(pair)
            if quote is None:
                stale.append(pair)
                value = position.cost_basis  # valorisation impossible : l'entrée sera refusée (prix_perime)
            else:
                value = position.quantity * quote.bid
                unrealized += value - position.cost_basis
            positions_value += value
            exposure[pair] = value
        for p in pending_buys:
            exposure[p.pair] = exposure.get(p.pair, Decimal(0)) + (p.amount or Decimal(0))
        today = [o for o in orders if session_date(o.created_at) == day]
        last_entry: dict[str, datetime] = {}
        for pair, at in [(o.pair, o.created_at) for o in orders if o.side is Side.BUY] + [
            (p.pair, p.created_at) for p in pending_buys
        ]:
            last_entry[pair] = max(last_entry.get(pair, at), at)
        return RiskState(
            equity=portfolio.account.cash + positions_value,
            session_pnl=sum((o.realized_pnl for o in today), Decimal(0)) + unrealized,
            entries=sum(1 for o in today if o.side is Side.BUY) + len(pending_buys),
            exposure=exposure,
            last_entry=last_entry,
            halted=self.storage.session_halted(account.id, day),
            stale=tuple(sorted(stale)),
        )

    def _guard(
        self, account: Account, request: EntryRequest, quotes: dict[str, Quote], exclude_pending: int | None = None
    ) -> None:
        """Lève RiskRefused (avec l'entrée de journal prête) si une règle est violée."""
        state = self._risk_state(account, quotes, request.now, exclude_pending)
        refusals = check_entry(request, state, self.storage.risk_limits(account.id))
        if not refusals:
            return
        exc = RiskRefused(refusals)
        exc.record = RefusalRecord(
            account_id=account.id,
            created_at=request.now,
            moment=request.moment,
            pair=request.pair,
            amount=request.amount,
            stop_price=request.stop,
            pending_id=exclude_pending,
            rules=tuple(exc.rules),
            message=str(exc),
            state=_state_json(state),
        )
        raise exc

    def _log_refusal(self, record: RefusalRecord) -> None:
        """Journal des refus (et arrêt de session si perte max) : transaction à part, validée même si l'ordre ne l'est pas."""
        with self.storage.transaction():
            self.storage.add_refusal(record)
            if MAX_LOSS in record.rules:
                self.storage.halt_session(record.account_id, session_date(record.created_at), record.created_at, MAX_LOSS)
        log.warning("Garde-fou, compte n°%s : %s", record.account_id, record.message)

    def risk_limits(self, *, compte: object = None) -> tuple[Account, RiskLimits]:
        account = self.storage.account(compte, open_only=False)
        return account, self.storage.risk_limits(account.id)

    def set_risk_limits(self, changes: dict[str, object], *, compte: object = None) -> tuple[Account, RiskLimits]:
        with self.storage.transaction():
            account = self.storage.account(compte)
            limits = update_limits(self.storage.risk_limits(account.id), changes)
            self.storage.set_risk_limits(account.id, limits)
        return account, limits

    def refusals(self, *, compte: object = None, limit: int = 100) -> list[RefusalRecord]:
        """Journal des refus d'un compte, ou de tous les comptes si `compte` est None."""
        account_id = None if compte is None else self.storage.account(compte, open_only=False).id
        return self.storage.refusals(account_id, limit)

    def session_refusals(self) -> int:
        day = session_date(self.clock())
        return sum(1 for r in self.storage.refusals(None, 10_000) if session_date(r.created_at) == day)

    async def record_halts(self) -> list[Account]:
        """Enregistre l'arrêt de session des comptes auto qui ont atteint la perte max (même sans tentative
        d'entrée), pour que les entrées restent bloquées si le latent remonte ensuite."""
        halted: list[Account] = []
        for account, pairs in await self.run(self._halt_candidates):
            quotes = await self._fresh_quotes(pairs)
            if len(quotes) < len(pairs):
                continue  # valorisation impossible : on réessaiera
            if await self.run(self._maybe_halt, account, quotes):
                halted.append(account)
        return halted

    def _halt_candidates(self) -> list[tuple[Account, list[str]]]:
        day = session_date(self.clock())
        return [
            (a, list(self.storage.load_portfolio(a.id).positions))
            for a in self.storage.open_accounts()
            if a.mode == AUTO and not self.storage.session_halted(a.id, day)
        ]

    def _maybe_halt(self, account: Account, quotes: dict[str, Quote]) -> bool:
        now = self.clock()
        with self.storage.transaction():
            state = self._risk_state(account, quotes, now)
            if state.stale or not loss_breached(state, self.storage.risk_limits(account.id)):
                return False
            self.storage.halt_session(account.id, session_date(now), now, MAX_LOSS)
        log.warning("Compte %s : perte max de session atteinte (%s), plus d'entrée jusqu'à la session suivante.",
                    account.name, state.session_pnl)
        return True

    # --- Stop de protection ---

    def _check_protection(self, pair: str, stop: Decimal, target: Decimal | None, quote: Quote) -> None:
        rules_orders.check_new(STOP, Side.SELL, pair, stop, quote)
        if target is not None:
            if target <= stop:
                raise OrderRejected(f"L'objectif ({fmt.price(target)}) doit être au-dessus du stop ({fmt.price(stop)}).")
            rules_orders.check_new(TARGET, Side.SELL, pair, target, quote)

    def _set_protection(
        self, account_id: int, pair: str, stop: Decimal, target: Decimal | None, now: datetime
    ) -> list[PendingOrder]:
        """Pose (ou remplace) LE stop de protection de la paire, avec son objectif éventuel."""
        for old in self.storage.protection_orders(account_id, pair):
            self.storage.close_pending(old.id, CANCELLED, "remplace", now)
        legs = [PendingOrder(account_id, pair, STOP, Side.SELL, stop, None, None, now, protection=True)]
        if target is not None:
            legs.append(PendingOrder(account_id, pair, TARGET, Side.SELL, target, None, None, now, protection=True))
        return self.storage.add_pending(legs)

    def _after_exit(self, account_id: int, portfolio: Portfolio, pair: str, now: datetime) -> bool:
        """Position fermée : son stop de protection et son objectif n'ont plus d'objet."""
        if pair in portfolio.positions:
            return False
        orders = self.storage.protection_orders(account_id, pair)
        for o in orders:
            self.storage.close_pending(o.id, CANCELLED, "position_fermee", now)
        return bool(orders)

    async def move_stop(self, pending_id: object, price: object, *, compte: object = None) -> PendingOrder:
        """Déplace un stop en attente ; sur un compte automatique, seulement vers le haut."""
        number = _number(pending_id)
        new_price = rules_orders.parse_price(price, "Stop")
        order = await self.run(self._stop_order, number, compte)
        quote = (await self.feed.snapshot([order.pair]))[order.pair]
        moved = await self.run(self._move_stop, number, new_price, quote)
        return moved

    def _stop_order(self, number: int, compte: object) -> PendingOrder:
        order = self.storage.get_pending(number)
        if order is None or (compte is not None and order.account_id != self.storage.account(compte).id):
            raise InvalidInput(f"Ordre en attente n°{number} inconnu.")
        if order.status != PENDING:
            raise OrderRejected(f"L'ordre n°{number} n'est plus en attente (statut : {order.status}).")
        if order.kind != STOP:
            raise OrderRejected(f"L'ordre n°{number} n'est pas un stop ({order.kind}).")
        return order

    def _move_stop(self, number: int, price: Decimal, quote: Quote) -> PendingOrder:
        with self.storage.transaction():
            order = self._stop_order(number, None)
            account = self.storage.account(order.account_id, open_only=False)
            if account.mode == AUTO and price <= order.trigger_price:
                raise OrderRejected(
                    f"Sur un compte automatique, le stop ne se déplace que vers le haut "
                    f"(actuel {fmt.price(order.trigger_price)}, demandé {fmt.price(price)})."
                )
            rules_orders.check_new(STOP, Side.SELL, order.pair, price, quote)
            self.storage.set_trigger_price(order.id, price)
        self._notify()
        return replace(order, trigger_price=price)

    # --- Ordres en attente ---

    async def place_order(
        self,
        pair: str,
        kind: str,
        side: object,
        price: object,
        size: object,
        *,
        compte: object = None,
        stop: object = None,
        target: object = None,
    ) -> PendingOrder:
        """Achat limite (size = montant USDT, avec `stop` et `target` posés à l'exécution), vente limite,
        stop ou objectif (size = quantité ou "tout")."""
        pair = normalize_pair(pair)
        side = _side(side)
        rules_orders.check_kind(kind, side)
        trigger = rules_orders.parse_price(price)
        stop_price = None if stop is None else rules_orders.parse_price(stop, "Stop")
        target_price = None if target is None else rules_orders.parse_price(target, "Objectif")
        if side is Side.SELL and (stop_price is not None or target_price is not None):
            raise OrderRejected("Un stop et un objectif ne s'attachent qu'à un achat limite.")
        held = await self.run(self._check_size, compte, pair, side, trigger, size, stop_price, target_price)
        quote = (await self.feed.snapshot([pair]))[pair]
        rules_orders.check_new(kind, side, pair, trigger, quote)
        quotes = await self._fresh_quotes(held)
        quotes[pair] = quote
        return await self.run(self._place, compte, pair, kind, side, trigger, size, stop_price, target_price, quotes)

    def _place(
        self,
        compte: object,
        pair: str,
        kind: str,
        side: Side,
        trigger: Decimal,
        size: object,
        stop: Decimal | None,
        target: Decimal | None,
        quotes: dict[str, Quote],
    ) -> PendingOrder:
        now = self.clock()
        try:
            with self.storage.transaction():
                account = self.storage.account(compte)
                new = self._new_pending(account, pair, kind, side, trigger, size, stop, target)
                if side is Side.BUY and account.mode == AUTO:
                    self._guard(account, EntryRequest(pair, new.amount, trigger, stop, LIMIT_PLACED, now), quotes)
                (saved,) = self.storage.add_pending([new])
        except RiskRefused as exc:
            self._log_refusal(exc.record)
            raise
        self._notify()
        return saved

    async def place_bracket(
        self, pair: str, stop: object, target: object, quantity: object = "tout", *, compte: object = None
    ) -> list[PendingOrder]:
        """Stop + objectif liés sur la même quantité : le premier exécuté annule l'autre."""
        pair = normalize_pair(pair)
        stop_price = rules_orders.parse_price(stop, "Stop")
        target_price = rules_orders.parse_price(target, "Objectif")
        if stop_price >= target_price:
            raise OrderRejected(f"Le stop ({stop}) doit être sous l'objectif ({target}).")
        await self.run(self._check_size, compte, pair, Side.SELL, stop_price, quantity)
        quote = (await self.feed.snapshot([pair]))[pair]
        rules_orders.check_new(STOP, Side.SELL, pair, stop_price, quote)
        rules_orders.check_new(TARGET, Side.SELL, pair, target_price, quote)
        return await self.run(self._bracket, compte, pair, stop_price, target_price, quantity)

    def _bracket(
        self, compte: object, pair: str, stop: Decimal, target: Decimal, quantity: object
    ) -> list[PendingOrder]:
        with self.storage.transaction():
            account = self.storage.account(compte)
            legs = [
                self._new_pending(account, pair, STOP, Side.SELL, stop, quantity),
                self._new_pending(account, pair, TARGET, Side.SELL, target, quantity),
            ]
            saved = self.storage.add_pending(legs)
        self._notify()
        return saved

    def _check_size(
        self,
        compte: object,
        pair: str,
        side: Side,
        trigger: Decimal,
        size: object,
        stop: Decimal | None = None,
        target: Decimal | None = None,
    ) -> list[str]:
        """Refus sans réseau : solde ou quantité libre insuffisants, session terminée, stop mal placé.
        Renvoie les paires détenues (à valoriser pour le garde-fou)."""
        account = self.storage.account(compte)
        self._new_pending(account, pair, LIMIT if side is Side.BUY else STOP, side, trigger, size, stop, target)
        return list(self.storage.load_portfolio(account.id).positions)

    def _new_pending(
        self,
        account: Account,
        pair: str,
        kind: str,
        side: Side,
        trigger: Decimal,
        size: object,
        stop: Decimal | None = None,
        target: Decimal | None = None,
    ) -> PendingOrder:
        portfolio = self._open_portfolio(account.id)
        now = self.clock()
        if side is Side.BUY:
            self._check_entry(account)
            amount = portfolio.check_buy(size)
            rules_orders.check_min_buy(pair, amount, trigger, portfolio.fee_rate)
            if stop is not None and stop >= trigger:
                raise OrderRejected(f"Le stop ({fmt.price(stop)}) doit être sous la limite d'achat ({fmt.price(trigger)}).")
            if target is not None and target <= trigger:
                raise OrderRejected(
                    f"L'objectif ({fmt.price(target)}) doit être au-dessus de la limite d'achat ({fmt.price(trigger)})."
                )
            return PendingOrder(
                account.id, pair, kind, side, trigger, amount, None, now, stop_price=stop, target_price=target
            )
        if str(size).strip().lower() == "tout":
            position = portfolio.positions.get(pair)
            if position is None:
                raise OrderRejected(f"Aucune position sur {pair} : rien à vendre.")
            free = portfolio.free_quantity(pair)
            if free <= 0:
                raise OrderRejected(
                    f"Toute la position {pair} est réservée par des ordres en attente : annule-les d'abord."
                )
            if free < position.quantity:
                raise OrderRejected(
                    f"Une partie de la position {pair} est déjà réservée par des ordres en attente : "
                    f"précise une quantité (au plus {fmt.qty(free)})."
                )
            return PendingOrder(account.id, pair, kind, side, trigger, None, None, now)
        _, qty = portfolio.check_sell(pair, size)
        return PendingOrder(account.id, pair, kind, side, trigger, None, qty, now)

    def cancel_order(self, pending_id: object, *, compte: object = None) -> list[PendingOrder]:
        """Annule un ordre en attente et, s'il est lié, l'autre ordre de la paire.

        Le stop de protection d'une position ouverte d'un compte auto ne s'annule pas (on le déplace
        vers le haut ou on vend) ; son objectif, lui, s'annule seul.
        """
        number = _number(pending_id)
        with self.storage.transaction():
            order = self.storage.get_pending(number)
            if order is None or (compte is not None and order.account_id != self.storage.account(compte).id):
                raise InvalidInput(f"Ordre en attente n°{number} inconnu.")
            if order.status != PENDING:
                raise OrderRejected(f"L'ordre n°{number} n'est plus en attente (statut : {order.status}).")
            now = self.clock()
            group = [o for o in self.storage.group(order) if o.status == PENDING]
            if order.protection:
                account = self.storage.account(order.account_id, open_only=False)
                held = order.pair in self.storage.load_portfolio(account.id).positions
                if order.kind == STOP and account.mode == AUTO and held:
                    raise OrderRejected(
                        f"Le stop n°{number} protège la position {order.pair} du compte automatique "
                        f"'{account.name}' : il ne peut pas être annulé tant qu'elle est ouverte. "
                        "Déplace-le vers le haut ou vends la position."
                    )
                if order.kind == TARGET:
                    group = [order]
            for o in group:
                self.storage.close_pending(o.id, CANCELLED, "annulation", now)
        self._notify()
        return [replace(o, status=CANCELLED, reason="annulation", closed_at=now) for o in group]

    def pending_orders(self, *, compte: object = None, all_statuses: bool = False) -> list[PendingOrder]:
        account = self.storage.account(compte, open_only=False)
        return self.storage.pending_orders(account.id, all_statuses=all_statuses)

    def watched_orders(self) -> list[PendingOrder]:
        """Tous les ordres en attente des comptes ouverts (ceux des comptes archivés sont annulés)."""
        return self.storage.pending_orders()

    def execute_pending(self, pending_id: int, quote: Quote) -> TradeResult | None:
        """Exécute un ordre en attente touché par `quote`, si toujours en attente (sinon None)."""
        changed = False
        try:
            with self.storage.transaction():
                order = self.storage.get_pending(pending_id)
                if order is None or order.status != PENDING or order.pair != quote.pair:
                    return None
                if not rules_orders.triggered(order, quote):
                    return None
                changed = True
                return self._fill(order, quote, self.clock())
        finally:
            if changed:
                self._notify()

    def _fill(self, pending: PendingOrder, quote: Quote, now: datetime) -> TradeResult | None:
        account = self.storage.account(pending.account_id, open_only=False)
        others = [o for o in self.storage.group(pending) if o.id != pending.id and o.status == PENDING]

        def close(status: str, reason: str, with_others: bool = False) -> None:
            self.storage.close_pending(pending.id, status, reason, now)
            for o in others if with_others else ():
                self.storage.close_pending(o.id, CANCELLED, reason, now)

        if account.archived:
            close(CANCELLED, "archive", with_others=True)
            return None
        portfolio = self.storage.load_portfolio(account.id, exclude_pending=pending.id)
        try:
            if pending.side is Side.BUY:
                if account.mode == AUTO and not entry_allowed(now, self.storage.session_rules()):
                    close(REJECTED, "hors_session")
                    return None
                if pending.stop_price is not None and quote.bid <= pending.stop_price:
                    self._reject_entry(pending, quote, now, STOP_ALREADY_HIT, account)
                    close(REJECTED, STOP_ALREADY_HIT)
                    return None
                if account.mode == AUTO:
                    quotes = self._peek(set(portfolio.positions) | {pending.pair})
                    quotes[pending.pair] = quote
                    request = EntryRequest(
                        pending.pair, pending.amount, quote.ask, pending.stop_price, LIMIT_TRIGGERED, now
                    )
                    try:
                        self._guard(account, request, quotes, exclude_pending=pending.id)
                    except RiskRefused as exc:
                        self.storage.add_refusal(exc.record)
                        if MAX_LOSS in exc.rules:
                            self.storage.halt_session(account.id, session_date(now), now, MAX_LOSS)
                        log.warning("Garde-fou, compte %s : %s", account.name, exc)
                        close(REJECTED, "garde_fou:" + ",".join(exc.rules))
                        return None
                order = portfolio.buy(quote, pending.amount, now)
            else:
                free = portfolio.free_quantity(pending.pair)
                if free <= 0:
                    held = pending.pair in portfolio.positions
                    close(CANCELLED, "quantite_reservee" if held else "plus_de_position", with_others=True)
                    return None
                qty = free if pending.quantity is None else min(pending.quantity, free)
                order = portfolio.sell(quote, qty, now)
        except OrderRejected as exc:
            close(REJECTED, str(exc))
            return None
        order = replace(
            order,
            kind=pending.kind,
            trigger_price=pending.trigger_price,
            slippage=rules_orders.slippage(pending.side, pending.trigger_price, order.price, order.quantity),
            pending_id=pending.id,
        )
        saved = self.storage.save_trade(portfolio, order)
        self.storage.close_pending(pending.id, FILLED, None, now, saved.id)
        for o in others:
            self.storage.close_pending(o.id, CANCELLED, "oco", now)
        if pending.side is Side.BUY and pending.stop_price is not None:
            self._set_protection(account.id, pending.pair, pending.stop_price, pending.target_price, now)
        if pending.side is Side.SELL:
            self._after_exit(account.id, portfolio, pending.pair, now)
        log.info("Ordre en attente n°%s exécuté : %s %s %s à %s.", pending.id, pending.kind, order.quantity, order.pair, order.price)
        return TradeResult(saved, portfolio.account.cash)

    def _reject_entry(self, pending: PendingOrder, quote: Quote, now: datetime, rule: str, account: Account) -> None:
        message = (
            f"Refusé par le garde-fou : le bid ({fmt.price(quote.bid)}) est déjà sous le stop "
            f"({fmt.price(pending.stop_price)}) au déclenchement de l'achat limite"
        )
        self.storage.add_refusal(
            RefusalRecord(
                account.id, now, LIMIT_TRIGGERED, pending.pair, pending.amount, pending.stop_price, pending.id,
                (rule,), message, {"bid": str(quote.bid), "ask": str(quote.ask)},
            )
        )
        log.warning("Compte %s : %s", account.name, message)

    # --- Sessions ---

    def session_rules(self) -> SessionRules:
        return self.storage.session_rules()

    def set_session_rules(self, last_entry: object, close: object) -> SessionRules:
        rules = validate_rules(last_entry, close)
        self.storage.set_session_rules(rules)
        return rules

    def session_due(self, account: Account, now: datetime | None = None) -> datetime | None:
        """Début de la session à fermer (activité la plus ancienne) si sa clôture est passée, sinon None."""
        if account.mode != AUTO or account.archived:
            return None
        now = now or self.clock()
        if not self.storage.load_portfolio(account.id).positions and not self.storage.pending_orders(account.id):
            return None
        since = self.storage.last_session_close(account.id)
        opened = self.storage.first_activity_since(account.id, since) or since or account.created_at
        return opened if close_due(opened, now, self.storage.session_rules()) else None

    async def close_session(self, account: Account, opened_at: datetime) -> SessionClose:
        """Annule les ordres en attente du compte auto et vend toutes ses positions au bid.
        Jamais filtré par le garde-fou."""
        pairs = await self.run(lambda: list(self.storage.load_portfolio(account.id).positions))
        quotes = await self.feed.snapshot(pairs) if pairs else {}  # FeedUnavailable si cotation périmée
        return await self.run(self._close_session, account, opened_at, quotes)

    def _close_session(self, account: Account, opened_at: datetime, quotes: dict[str, Quote]) -> SessionClose:
        now = self.clock()
        late = is_late(opened_at, now, self.storage.session_rules())
        kind = LATE_SESSION_CLOSE if late else SESSION_CLOSE
        day = session_date(opened_at)
        done: list[Order] = []
        with self.storage.transaction():
            cancelled = self.storage.cancel_account_pending(account.id, "fin_de_session", now)
            portfolio = self.storage.load_portfolio(account.id)
            for pair in sorted(portfolio.positions):
                if pair not in quotes:
                    raise FeedUnavailable(f"Pas de cotation fraîche pour {pair} : clôture reportée.")
                order = replace(portfolio.sell(quotes[pair], "tout", now), kind=kind)
                done.append(self.storage.save_trade(portfolio, order))
            self.storage.record_session(account.id, day, now, late)
        self._notify()
        log.info("Session du %s fermée pour le compte %s (%d ventes, tardive : %s).", day, account.name, len(done), late)
        return SessionClose(account, day, now, late, done, cancelled)

    async def close_due_sessions(self) -> list[SessionClose]:
        """Ferme chaque compte auto dont la session est terminée ; reporte si la cotation est périmée."""
        closed: list[SessionClose] = []
        for account, opened in await self.run(self._due_accounts):
            try:
                closed.append(await self.close_session(account, opened))
            except FeedUnavailable as exc:
                log.warning("Clôture de session du compte %s reportée : %s", account.name, exc)
        return closed

    def _due_accounts(self) -> list[tuple[Account, datetime]]:
        due = []
        for account in self.storage.open_accounts():
            opened = self.session_due(account)
            if opened is not None:
                due.append((account, opened))
        return due

    # --- Consultation ---

    def load_portfolio(self, *, compte: object = None) -> Portfolio:
        return self.storage.load_portfolio(self.storage.account(compte, open_only=False).id)

    async def valuation(self, *, compte: object = None) -> Valuation:
        portfolio = await self.run(self.load_portfolio, compte=compte)
        quotes = await self.feed.snapshot(list(portfolio.positions)) if portfolio.positions else {}
        return portfolio.valuation(quotes)

    def history(self, *, compte: object = None) -> tuple[Account, list[Order]]:
        account = self.storage.account(compte, open_only=False)
        return account, self.storage.orders(account.id)

    def account(self, compte: object = None) -> Account:
        return self.storage.account(compte, open_only=False)

    def accounts(self) -> list[Account]:
        return self.storage.accounts()

    def open_accounts(self) -> list[Account]:
        return self.storage.open_accounts()

    def create_account(self, name: object, capital: object = DEFAULT_CAPITAL, mode: str = MANUAL) -> Account:
        return self.storage.create_account(name, capital, mode, self.clock())

    def select_account(self, compte: object) -> Account:
        return self.storage.select_account(compte)

    def archive_account(self, compte: object) -> Account:
        account = self.storage.archive_account(compte, self.clock())
        self._notify()
        return account

    def reset(self, capital: object = DEFAULT_CAPITAL, *, compte: object = None) -> Account:
        account = self.storage.reset(capital, self.clock(), account=compte)
        self._notify()
        return account


def _number(raw: object) -> int:
    try:
        return int(str(raw).strip())
    except ValueError:
        raise InvalidInput(f"Numéro d'ordre invalide : '{raw}'.") from None


def _side(raw: object) -> Side:
    if isinstance(raw, Side):
        return raw
    text = str(raw).strip().lower()
    for side in Side:
        if text == side.value:
            return side
    raise InvalidInput(f"Sens invalide : '{raw}' (achat ou vente).")


def _state_json(state: RiskState) -> dict:
    return {
        "valeur_compte": str(state.equity),
        "pnl_session": str(state.session_pnl),
        "valeur_debut_session": str(state.start_equity),
        "entrees_session": state.entries,
        "exposition": {pair: str(value) for pair, value in sorted(state.exposure.items())},
        "arret_session": state.halted,
        "paires_perimees": list(state.stale),
    }
