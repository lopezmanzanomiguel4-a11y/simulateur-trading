"""Allers-retours (trades) reconstitués depuis les ordres, et journal chronologique d'un compte (pur).

Un trade commence quand la position d'une paire passe de 0 à > 0 et se termine quand elle revient
à 0. Son P&L est net des deux frais : produit des ventes (frais déduits) − coût des achats (frais
inclus). Motif de sortie : celui du dernier ordre de vente. Stratégie : celle du signal d'entrée lié
au premier achat. Le même code sert au live (ordres de la base) et au backtest (ordres simulés).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from simtrade.models import LATE_SESSION_CLOSE, SESSION_CLOSE, STOP, TARGET, Order, Side

EXIT_STOP = "stop"
EXIT_TARGET = "objectif"
EXIT_SIGNAL = "signal"
EXIT_SESSION = "cloture_session"
EXIT_MANUAL = "manuel"
EXIT_REASONS = (EXIT_STOP, EXIT_TARGET, EXIT_SIGNAL, EXIT_SESSION, EXIT_MANUAL)


@dataclass(frozen=True)
class Trade:
    pair: str
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal  # quantité totale achetée
    cost: Decimal  # USDT payés, frais inclus
    proceeds: Decimal  # USDT reçus, frais déduits
    fees: Decimal
    pnl: Decimal  # proceeds - cost
    exit_reason: str
    strategy: str | None
    order_ids: tuple[int, ...] = ()
    regime: str | None = None  # connu à l'entrée (backtest)
    slippage: Decimal = Decimal(0)

    @property
    def pnl_pct(self) -> Decimal:
        return self.pnl / self.cost * 100 if self.cost else Decimal(0)


@dataclass(frozen=True)
class Links:
    """Liens signaux → ordres : stratégie de chaque achat d'entrée, ventes sur signal."""

    strategy_by_order: Mapping[int, str] = field(default_factory=dict)
    signal_exits: frozenset[int] = frozenset()


def links_from_signals(signals: Iterable[Any]) -> Links:
    """`signals` : SignalRecord (ou équivalent : kind, decision, order_id, details)."""
    strategy_by_order: dict[int, str] = {}
    exits: set[int] = set()
    for s in signals:
        if s.decision != "ordre" or s.order_id is None:
            continue
        if s.kind == "entree":
            strategy_by_order[s.order_id] = s.details.get("strategie", "")
        elif s.kind == "sortie":
            exits.add(s.order_id)
    return Links(strategy_by_order, frozenset(exits))


def exit_reason(order: Order, links: Links) -> str:
    if order.kind == STOP:
        return EXIT_STOP
    if order.kind == TARGET:
        return EXIT_TARGET
    if order.kind in (SESSION_CLOSE, LATE_SESSION_CLOSE):
        return EXIT_SESSION
    if order.id is not None and order.id in links.signal_exits:
        return EXIT_SIGNAL
    return EXIT_MANUAL


def trades_from_orders(orders: Sequence[Order], links: Links = Links()) -> list[Trade]:
    """Trades fermés, dans l'ordre de leur clôture. Une position encore ouverte n'en donne pas."""
    open_: dict[str, dict] = {}
    out: list[Trade] = []
    for o in sorted(orders, key=lambda o: (o.created_at, o.id or 0)):
        t = open_.get(o.pair)
        if o.side is Side.BUY:
            if t is None:
                t = open_[o.pair] = {
                    "opened_at": o.created_at, "qty": Decimal(0), "held": Decimal(0), "cost": Decimal(0),
                    "proceeds": Decimal(0), "fees": Decimal(0), "ids": [], "slippage": Decimal(0),
                    "strategy": links.strategy_by_order.get(o.id) if o.id is not None else None,
                }
            t["qty"] += o.quantity
            t["held"] += o.quantity
            t["cost"] += -o.cash_flow
        else:
            if t is None:
                continue  # vente sans achat connu (ne devrait pas arriver)
            t["held"] -= o.quantity
            t["proceeds"] += o.cash_flow
        t["fees"] += o.fee
        t["slippage"] += o.slippage
        if o.id is not None:
            t["ids"].append(o.id)
        if o.side is Side.SELL and t["held"] <= 0:
            out.append(
                Trade(
                    pair=o.pair,
                    opened_at=t["opened_at"],
                    closed_at=o.created_at,
                    quantity=t["qty"],
                    cost=t["cost"],
                    proceeds=t["proceeds"],
                    fees=t["fees"],
                    pnl=t["proceeds"] - t["cost"],
                    exit_reason=exit_reason(o, links),
                    strategy=t["strategy"],
                    order_ids=tuple(t["ids"]),
                    slippage=t["slippage"],
                )
            )
            del open_[o.pair]
    return out


def with_regimes(trades: Sequence[Trade], regimes: Mapping[datetime, str | None]) -> list[Trade]:
    return [replace(t, regime=regimes.get(t.opened_at)) for t in trades]


# --- Journal ---

RANK = {"signal": 0, "refus": 1, "ordre": 2, "trade": 3}  # à heure égale : la décision avant son effet


@dataclass(frozen=True)
class JournalEntry:
    at: datetime
    kind: str  # signal, refus, ordre, trade
    ref: int | None
    item: Any


def journal(
    signals: Iterable[Any], refusals: Iterable[Any], orders: Iterable[Order], trades: Iterable[Trade]
) -> list[JournalEntry]:
    """Tout ce qui s'est passé sur un compte, du plus ancien au plus récent."""
    entries = [JournalEntry(s.created_at, "signal", s.id, s) for s in signals]
    entries += [JournalEntry(r.created_at, "refus", r.id, r) for r in refusals]
    entries += [JournalEntry(o.created_at, "ordre", o.id, o) for o in orders]
    entries += [JournalEntry(t.closed_at, "trade", t.order_ids[-1] if t.order_ids else None, t) for t in trades]
    return sorted(entries, key=lambda e: (e.at, RANK[e.kind], e.ref or 0))
