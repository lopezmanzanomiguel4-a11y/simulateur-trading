"""Structures de données partagées par le moteur, le stockage et les interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
QUOTE_ASSET = "USDT"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


@dataclass(frozen=True)
class Trade:
    """Transaction exécutée sur la place de marché (sert aux bougies, au volume et au VWAP)."""

    pair: str
    price: Decimal
    quantity: Decimal
    source: str
    received_at: datetime  # heure locale de réception
    traded_at: datetime  # heure de l'exchange : c'est elle qui date les bougies
    count: int = 1  # transactions individuelles regroupées (aggTrade Binance : l - f + 1)


@dataclass
class Position:
    pair: str
    quantity: Decimal
    cost_basis: Decimal  # total payé pour la quantité détenue, frais d'achat inclus

    @property
    def avg_price(self) -> Decimal:
        """Prix de revient moyen par unité, frais d'achat inclus."""
        return self.cost_basis / self.quantity


MANUAL = "manuel"
AUTO = "auto"
MODES: tuple[str, ...] = (MANUAL, AUTO)


@dataclass
class Account:
    id: int | None
    initial_capital: Decimal
    cash: Decimal
    created_at: datetime  # date de création ou du dernier reset
    name: str = "principal"
    mode: str = MANUAL  # un compte auto est soumis aux règles de session, un compte manuel jamais
    archived_at: datetime | None = None  # None : compte ouvert
    current: bool = True  # compte utilisé quand aucun n'est précisé

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


# Types d'ordre exécuté (Order.kind)
MARKET = "marche"
LIMIT = "limite"
STOP = "stop"
TARGET = "objectif"
SESSION_CLOSE = "cloture_session"
LATE_SESSION_CLOSE = "cloture_tardive"


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
    kind: str = MARKET
    trigger_price: Decimal | None = None  # ordres en attente : limite, stop ou objectif
    slippage: Decimal = Decimal(0)  # en USDT ; positif = coût (stop sauté), négatif = amélioration
    pending_id: int | None = None


# Statuts d'un ordre en attente
PENDING = "en_attente"
FILLED = "execute"
CANCELLED = "annule"
REJECTED = "rejete"


@dataclass(frozen=True)
class PendingOrder:
    """Ordre limite, stop ou objectif, exécuté quand le flux live touche son prix."""

    account_id: int
    pair: str
    kind: str  # LIMIT, STOP ou TARGET
    side: Side
    trigger_price: Decimal
    amount: Decimal | None  # achat : montant USDT total, frais inclus
    quantity: Decimal | None  # vente : quantité ; None = toute la position au déclenchement
    created_at: datetime
    group_id: int | None = None  # paire stop + objectif : l'un annule l'autre
    status: str = PENDING
    reason: str | None = None
    closed_at: datetime | None = None
    order_id: int | None = None
    id: int | None = None
