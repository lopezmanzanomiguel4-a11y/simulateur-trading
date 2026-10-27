"""Structures de données partagées par le moteur, le stockage et les interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
QUOTE_ASSET = "USDT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean_decimal(raw: str) -> Decimal:
    """Convertit une chaîne de prix en Decimal sans zéros inutiles ("101.06000000" -> 101.06)."""
    value = Decimal(raw)
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def base_asset(pair: str) -> str:
    return pair.removesuffix(QUOTE_ASSET)


class Side(str, Enum):
    BUY = "achat"
    SELL = "vente"


@dataclass(frozen=True)
class Quote:
    """Meilleur prix acheteur (bid) et vendeur (ask) à un instant donné."""

    pair: str
    bid: Decimal
    ask: Decimal
    source: str
    received_at: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        return self.spread / self.mid * 100


@dataclass
class Position:
    pair: str
    quantity: Decimal
    cost_basis: Decimal  # total payé pour la quantité détenue, frais d'achat inclus

    @property
    def avg_price(self) -> Decimal:
        """Prix de revient moyen par unité, frais d'achat inclus."""
        return self.cost_basis / self.quantity


@dataclass
class Account:
    id: int | None
    initial_capital: Decimal
    cash: Decimal
    created_at: datetime  # date de création ou du dernier reset


@dataclass(frozen=True)
class Order:
    account_id: int | None
    pair: str
    side: Side
    quantity: Decimal
    price: Decimal  # ask pour un achat, bid pour une vente
    gross: Decimal  # quantité x prix, hors frais
    fee: Decimal
    cash_flow: Decimal  # variation du solde USDT : négative à l'achat, positive à la vente
    realized_pnl: Decimal  # nul pour un achat
    source: str
    created_at: datetime
    id: int | None = None
