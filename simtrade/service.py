"""Couche applicative : relie moteur, stockage et source de prix.

C'est le seul point d'entrée que les interfaces (CLI, API FastAPI) doivent appeler.
Les méthodes qui touchent au réseau sont asynchrones. `compte` désigne un compte ouvert
par son nom ou son numéro ; sans `compte`, c'est le compte courant.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from simtrade import fmt
from simtrade import orders as rules_orders
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
from simtrade.sessions import SessionRules, close_due, entry_allowed, is_late, session_date, validate_rules
from simtrade.storage import Storage

log = logging.getLogger("simtrade.service")


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
    def __init__(self, storage: Storage, feed: PriceFeed, clock: Callable[[], datetime] = utcnow):
        self.storage = storage
        self.feed = feed
        self.clock = clock
        self.listeners: list[Callable[[], None]] = []  # prévenus après tout changement d'ordre en attente

    def _notify(self) -> None:
        for listener in self.listeners:
            listener()

    # --- Prix ---

    async def prices(self, pairs: Sequence[str] = PAIRS) -> dict[str, Quote]:
        return await self.feed.snapshot(list(pairs))

    def stream_prices(self, pairs: Sequence[str] = PAIRS) -> AsyncIterator[Quote]:
        return self.feed.stream(list(pairs))

    # --- Ordres au marché ---

    async def buy(self, pair: str, amount_usdt: object, *, compte: object = None) -> TradeResult:
        pair = normalize_pair(pair)
        account = self.storage.account(compte)
        self._check_entry(account)
        self.storage.load_portfolio(account.id).check_buy(amount_usdt)  # refus immédiat, sans réseau
        quote = (await self.feed.snapshot([pair]))[pair]
        with self.storage.transaction():
            portfolio = self._open_portfolio(account.id)
            order = portfolio.buy(quote, amount_usdt, self.clock())
            return TradeResult(self.storage.save_trade(portfolio, order), portfolio.account.cash)

    async def sell(self, pair: str, quantity: object, *, compte: object = None) -> TradeResult:
        pair = normalize_pair(pair)
        account = self.storage.account(compte)
        self.storage.load_portfolio(account.id).check_sell(pair, quantity)
        quote = (await self.feed.snapshot([pair]))[pair]
        with self.storage.transaction():
            portfolio = self._open_portfolio(account.id)
            order = portfolio.sell(quote, quantity, self.clock())
            return TradeResult(self.storage.save_trade(portfolio, order), portfolio.account.cash)

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

    # --- Ordres en attente ---

    async def place_order(
        self, pair: str, kind: str, side: object, price: object, size: object, *, compte: object = None
    ) -> PendingOrder:
        """Achat limite (size = montant USDT), vente limite, stop ou objectif (size = quantité ou "tout")."""
        pair = normalize_pair(pair)
        side = _side(side)
        rules_orders.check_kind(kind, side)
        trigger = rules_orders.parse_price(price)
        account = self.storage.account(compte)
        self._check_size(account, pair, side, trigger, size)
        quote = (await self.feed.snapshot([pair]))[pair]
        rules_orders.check_new(kind, side, pair, trigger, quote)
        with self.storage.transaction():
            new = self._new_pending(account, pair, kind, side, trigger, size)
            (saved,) = self.storage.add_pending([new])
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
        account = self.storage.account(compte)
        self._check_size(account, pair, Side.SELL, stop_price, quantity)
        quote = (await self.feed.snapshot([pair]))[pair]
        rules_orders.check_new(STOP, Side.SELL, pair, stop_price, quote)
        rules_orders.check_new(TARGET, Side.SELL, pair, target_price, quote)
        with self.storage.transaction():
            legs = [
                self._new_pending(account, pair, STOP, Side.SELL, stop_price, quantity),
                self._new_pending(account, pair, TARGET, Side.SELL, target_price, quantity),
            ]
            saved = self.storage.add_pending(legs)
        self._notify()
        return saved

    def _check_size(self, account: Account, pair: str, side: Side, trigger: Decimal, size: object) -> None:
        """Refus sans réseau : solde ou quantité libre insuffisants, session terminée."""
        self._new_pending(account, pair, LIMIT if side is Side.BUY else STOP, side, trigger, size)

    def _new_pending(
        self, account: Account, pair: str, kind: str, side: Side, trigger: Decimal, size: object
    ) -> PendingOrder:
        portfolio = self._open_portfolio(account.id)
        now = self.clock()
        if side is Side.BUY:
            self._check_entry(account)
            amount = portfolio.check_buy(size)
            rules_orders.check_min_buy(pair, amount, trigger, portfolio.fee_rate)
            return PendingOrder(account.id, pair, kind, side, trigger, amount, None, now)
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
        """Annule un ordre en attente et, s'il est lié, l'autre ordre de la paire."""
        try:
            number = int(str(pending_id).strip())
        except ValueError:
            raise InvalidInput(f"Numéro d'ordre invalide : '{pending_id}'.") from None
        with self.storage.transaction():
            order = self.storage.get_pending(number)
            if order is None or (compte is not None and order.account_id != self.storage.account(compte).id):
                raise InvalidInput(f"Ordre en attente n°{number} inconnu.")
            if order.status != PENDING:
                raise OrderRejected(f"L'ordre n°{number} n'est plus en attente (statut : {order.status}).")
            now = self.clock()
            group = [o for o in self.storage.group(order) if o.status == PENDING]
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
        log.info("Ordre en attente n°%s exécuté : %s %s %s à %s.", pending.id, pending.kind, order.quantity, order.pair, order.price)
        return TradeResult(saved, portfolio.account.cash)

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
        """Annule les ordres en attente du compte auto et vend toutes ses positions au bid."""
        pairs = list(self.storage.load_portfolio(account.id).positions)
        quotes = await self.feed.snapshot(pairs) if pairs else {}  # FeedUnavailable si cotation périmée
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
        for account in self.storage.open_accounts():
            opened = self.session_due(account)
            if opened is None:
                continue
            try:
                closed.append(await self.close_session(account, opened))
            except FeedUnavailable as exc:
                log.warning("Clôture de session du compte %s reportée : %s", account.name, exc)
        return closed

    # --- Consultation ---

    def load_portfolio(self, *, compte: object = None) -> Portfolio:
        return self.storage.load_portfolio(self.storage.account(compte, open_only=False).id)

    async def valuation(self, *, compte: object = None) -> Valuation:
        portfolio = self.load_portfolio(compte=compte)
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


def _side(raw: object) -> Side:
    if isinstance(raw, Side):
        return raw
    text = str(raw).strip().lower()
    for side in Side:
        if text == side.value:
            return side
    raise InvalidInput(f"Sens invalide : '{raw}' (achat ou vente).")
