"""Ordres en attente : règles de déclenchement et de glissement, sans réseau ni base de données.

Conventions (positions longues uniquement, pas de stop d'achat) :
- achat limite : déclenché quand ask <= limite, exécuté à l'ask ;
- vente limite et objectif : déclenchés quand bid >= prix, exécutés au bid ;
- stop : déclenché quand bid <= stop, exécuté au bid disponible, même s'il a sauté sous le stop ;
- glissement (USDT) = écart défavorable entre le prix de déclenchement et le prix d'exécution, x quantité :
  positif = coût, négatif = amélioration.
"""

from __future__ import annotations

from decimal import Decimal

from simtrade import fmt
from simtrade.engine import QTY_STEP, _q, parse_decimal
from simtrade.errors import OrderRejected
from simtrade.models import LIMIT, STOP, TARGET, PendingOrder, Quote, Side, base_asset

KINDS: tuple[str, ...] = (LIMIT, STOP, TARGET)


def parse_price(raw: object, label: str = "Prix") -> Decimal:
    value = parse_decimal(raw, label)
    if value <= 0:
        raise OrderRejected(f"{label} doit être strictement positif (reçu : {raw}).")
    return value


def triggered(order: PendingOrder, quote: Quote) -> bool:
    if order.side is Side.BUY:
        return quote.ask <= order.trigger_price
    if order.kind == STOP:
        return quote.bid <= order.trigger_price
    return quote.bid >= order.trigger_price


def slippage(side: Side, trigger_price: Decimal, fill_price: Decimal, quantity: Decimal) -> Decimal:
    gap = trigger_price - fill_price if side is Side.SELL else fill_price - trigger_price
    return _q(gap * quantity)


def check_kind(kind: str, side: Side) -> None:
    if kind not in KINDS:
        raise OrderRejected(f"Type d'ordre inconnu : '{kind}' ({', '.join(KINDS)}).")
    if side is Side.BUY and kind != LIMIT:
        raise OrderRejected("Pas de stop ni d'objectif à l'achat : seuls les achats limites sont possibles.")


def check_new(kind: str, side: Side, pair: str, trigger_price: Decimal, quote: Quote) -> None:
    """Refuse les combinaisons interdites et les ordres qui partiraient immédiatement."""
    check_kind(kind, side)
    probe = PendingOrder(0, pair, kind, side, trigger_price, None, None, quote.received_at)
    if not triggered(probe, quote):
        return
    current = f"ask {fmt.price(quote.ask)}" if side is Side.BUY else f"bid {fmt.price(quote.bid)}"
    raise OrderRejected(
        f"Cet ordre serait exécuté immédiatement ({kind} à {fmt.price(trigger_price)}, {current}) : "
        "passe un ordre au marché."
    )


def check_min_buy(pair: str, amount: Decimal, limit: Decimal, fee_rate: Decimal) -> None:
    """Le montant doit permettre d'acheter au moins un pas de quantité au prix limite."""
    if amount / (limit * (1 + fee_rate)) < QTY_STEP:
        raise OrderRejected(
            f"Montant trop faible : {fmt.usdt(amount)} ne permet pas d'acheter {QTY_STEP} {base_asset(pair)} "
            f"à {fmt.price(limit)}."
        )
