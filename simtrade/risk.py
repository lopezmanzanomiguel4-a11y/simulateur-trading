"""Garde-fou de risque : module pur (aucun I/O, aucune horloge), appelé avant chaque ENTRÉE
d'un compte automatique. Les sorties (stop, objectif, ventes, clôture de session) n'y passent jamais.

Le service mesure l'état du compte (RiskState) et décrit l'entrée demandée (EntryRequest) ;
check_entry renvoie toutes les règles violées, ou une liste vide si l'entrée est acceptée.

Perte de session = P&L réalisé des ordres de la session + P&L latent des positions au bid.
Valeur en début de session : E0 = E - perte de session (E = valeur actuelle au bid).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from simtrade import fmt
from simtrade.errors import InvalidInput, OrderRejected

# Moments d'une entrée
MARKET_ENTRY = "marche"
LIMIT_PLACED = "pose_limite"
LIMIT_TRIGGERED = "declenchement_limite"

# Codes de règle
STOP_REQUIRED = "stop_obligatoire"
STOP_ALREADY_HIT = "stop_deja_touche"
MAX_SIZE = "taille_max"
MAX_LOSS = "perte_max_session"
MAX_TRADES = "trades_max_session"
MAX_POSITIONS = "positions_max"
ENTRY_GAP = "delai_entrees"
STALE_PRICE = "prix_perime"


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: Decimal = Decimal("20")  # exposition max sur une paire, en % de la valeur du compte
    max_session_loss_pct: Decimal = Decimal("2")  # en % de la valeur en début de session
    max_trades: int = 6  # entrées par session
    max_positions: int = 2  # paires exposées en même temps
    min_entry_gap: timedelta = timedelta(minutes=15)  # entre deux entrées sur la même paire


@dataclass(frozen=True)
class RiskState:
    equity: Decimal  # valeur actuelle : solde + positions au bid
    session_pnl: Decimal  # réalisé de la session + latent
    entries: int  # achats exécutés dans la session + achats limites en attente
    exposure: dict[str, Decimal] = field(default_factory=dict)  # valeur au bid + achats limites réservés
    last_entry: dict[str, datetime] = field(default_factory=dict)
    halted: bool = False  # perte max déjà atteinte dans la session
    stale: tuple[str, ...] = ()  # paires sans cotation fraîche

    @property
    def start_equity(self) -> Decimal:
        return self.equity - self.session_pnl


@dataclass(frozen=True)
class EntryRequest:
    pair: str
    amount: Decimal  # USDT, frais inclus
    price: Decimal  # prix d'entrée de référence : ask (marché) ou limite
    stop: Decimal | None
    moment: str
    now: datetime
    bid: Decimal | None = None  # entrée au marché : le stop doit aussi être sous le bid (sinon il partirait aussitôt)


@dataclass(frozen=True)
class Refusal:
    rule: str
    message: str


class RiskRefused(OrderRejected):
    """Entrée refusée par le garde-fou ; porte les règles violées pour le journal."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("Refusé par le garde-fou : " + " ; ".join(r.message for r in refusals))

    @property
    def rules(self) -> list[str]:
        return [r.rule for r in self.refusals]


def loss_breached(state: RiskState, limits: RiskLimits) -> bool:
    return state.session_pnl <= -(limits.max_session_loss_pct / 100) * state.start_equity


def check_entry(request: EntryRequest, state: RiskState, limits: RiskLimits) -> list[Refusal]:
    refusals: list[Refusal] = []
    pair = request.pair
    if request.stop is None:
        refusals.append(Refusal(STOP_REQUIRED, "stop-loss obligatoire pour toute entrée automatique"))
    elif request.stop >= request.price:
        refusals.append(
            Refusal(
                STOP_REQUIRED,
                f"le stop ({fmt.price(request.stop)}) doit être sous le prix d'entrée ({fmt.price(request.price)})",
            )
        )
    elif request.bid is not None and request.stop >= request.bid:
        refusals.append(
            Refusal(
                STOP_REQUIRED,
                f"le stop ({fmt.price(request.stop)}) doit être sous le bid ({fmt.price(request.bid)}), "
                "sinon il partirait aussitôt",
            )
        )
    if state.stale:
        refusals.append(Refusal(STALE_PRICE, f"pas de cotation fraîche pour {', '.join(state.stale)}"))
    if state.halted or loss_breached(state, limits):
        refusals.append(
            Refusal(
                MAX_LOSS,
                f"perte max de session atteinte ({fmt.usdt(state.session_pnl, sign=True)}, "
                f"limite {fmt.num(limits.max_session_loss_pct)} % de {fmt.usdt(state.start_equity)}) : "
                "plus d'entrée avant la session suivante",
            )
        )
    if state.entries >= limits.max_trades:
        refusals.append(Refusal(MAX_TRADES, f"{state.entries} entrées dans la session (max {limits.max_trades})"))
    exposed = {p for p, value in state.exposure.items() if value > 0}
    if pair not in exposed and len(exposed) >= limits.max_positions:
        refusals.append(
            Refusal(MAX_POSITIONS, f"{len(exposed)} paires déjà exposées (max {limits.max_positions})")
        )
    last = state.last_entry.get(pair)
    if last is not None and request.now - last < limits.min_entry_gap:
        wait = limits.min_entry_gap - (request.now - last)
        refusals.append(
            Refusal(ENTRY_GAP, f"dernière entrée sur {pair} il y a moins de {_duration(limits.min_entry_gap)} "
                    f"(encore {_duration(wait)})")
        )
    after = state.exposure.get(pair, Decimal(0)) + request.amount
    cap = limits.max_position_pct / 100 * state.equity
    if after > cap:
        refusals.append(
            Refusal(
                MAX_SIZE,
                f"exposition sur {pair} de {fmt.usdt(after)} après l'entrée, max {fmt.num(limits.max_position_pct)} % "
                f"de la valeur du compte = {fmt.usdt(cap)}",
            )
        )
    return refusals


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


# --- Lecture et validation des limites (saisies en texte) ---

KEYS = {"taille": "max_position_pct", "perte": "max_session_loss_pct", "trades": "max_trades",
        "positions": "max_positions", "delai": "min_entry_gap"}


def parse_gap(raw: object) -> timedelta:
    """'15m', '900s', '1h' ou un nombre de minutes."""
    text = str(raw).strip().lower().replace(",", ".")
    units = {"s": 1, "m": 60, "h": 3600}
    factor = 60
    if text and text[-1] in units:
        factor, text = units[text[-1]], text[:-1]
    try:
        seconds = Decimal(text) * factor
    except Exception:
        raise InvalidInput(f"Délai invalide : '{raw}' (ex. 15m, 900s, 1h).") from None
    if not seconds.is_finite() or seconds < 0:
        raise InvalidInput(f"Délai invalide : '{raw}' (positif ou nul).")
    return timedelta(seconds=int(seconds))


def _pct(raw: object, label: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except Exception:
        raise InvalidInput(f"{label} invalide : '{raw}' n'est pas un nombre.") from None
    if not value.is_finite() or not 0 < value <= 100:
        raise InvalidInput(f"{label} invalide : '{raw}' (entre 0 exclu et 100 %).")
    return value


def _count(raw: object, label: str) -> int:
    text = str(raw).strip()
    if not text.isdigit() or int(text) < 1:
        raise InvalidInput(f"{label} invalide : '{raw}' (entier >= 1).")
    return int(text)


def update_limits(limits: RiskLimits, changes: dict[str, object]) -> RiskLimits:
    """Applique des changements {taille, perte, trades, positions, delai} validés."""
    values = {}
    for key, raw in changes.items():
        if key not in KEYS:
            raise InvalidInput(f"Limite inconnue : '{key}' ({', '.join(KEYS)}).")
        if key == "taille":
            values["max_position_pct"] = _pct(raw, "Taille max")
        elif key == "perte":
            values["max_session_loss_pct"] = _pct(raw, "Perte max de session")
        elif key == "trades":
            values["max_trades"] = _count(raw, "Entrées max par session")
        elif key == "positions":
            values["max_positions"] = _count(raw, "Positions max")
        else:
            values["min_entry_gap"] = parse_gap(raw)
    return RiskLimits(**{**limits.__dict__, **values})
