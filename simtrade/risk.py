"""Garde-fou de risque : module pur (aucun I/O, aucune horloge), appelé avant chaque ENTRÉE
d'un compte automatique. Les sorties (stop, objectif, ventes, clôture de session) n'y passent jamais.

Le service mesure l'état du compte (RiskState) et décrit l'entrée demandée (EntryRequest) ;
check_entry renvoie toutes les règles violées, ou une liste vide si l'entrée est acceptée.

Perte de session = P&L réalisé des ordres de la session + P&L latent des positions au bid.
Valeur en début de session : E0 = E - perte de session (E = valeur actuelle au bid).

Comptes swing (limites `max_week_loss_pct` et `max_exposure_pct` renseignées) : la perte de session
est remplacée par la perte sur 7 x 24 h glissantes (réalisé des sorties des 7 derniers jours + latent,
base = E - ce P&L), et l'exposition totale (somme des positions + achats réservés) est plafonnée.
Les limites intraday laissent ces deux champs à None : leurs règles ne changent pas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from decimal import ROUND_DOWN

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
MAX_WEEK_LOSS = "perte_max_semaine"
MAX_EXPOSURE = "exposition_max"
STALE_PRICE = "prix_perime"


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: Decimal = Decimal("20")  # exposition max sur une paire, en % de la valeur du compte
    max_session_loss_pct: Decimal = Decimal("2")  # en % de la valeur en début de session
    max_trades: int = 6  # entrées par session
    max_positions: int = 2  # paires exposées en même temps
    min_entry_gap: timedelta = timedelta(minutes=15)  # entre deux entrées sur la même paire
    max_week_loss_pct: Decimal | None = None  # swing : perte max sur 7 jours glissants, en % de la valeur d'alors
    max_exposure_pct: Decimal | None = None  # swing : somme des positions, en % de la valeur du compte

    @property
    def swing(self) -> bool:
        return self.max_week_loss_pct is not None


@dataclass(frozen=True)
class RiskState:
    equity: Decimal  # valeur actuelle : solde + positions au bid
    session_pnl: Decimal  # réalisé de la session + latent
    entries: int  # achats exécutés dans la session + achats limites en attente
    exposure: dict[str, Decimal] = field(default_factory=dict)  # valeur au bid + achats limites réservés
    last_entry: dict[str, datetime] = field(default_factory=dict)
    halted: bool = False  # perte max déjà atteinte dans la session
    stale: tuple[str, ...] = ()  # paires sans cotation fraîche
    week_pnl: Decimal = Decimal(0)  # swing : réalisé des sorties sur 7 jours glissants + latent

    @property
    def start_equity(self) -> Decimal:
        return self.equity - self.session_pnl

    @property
    def week_start_equity(self) -> Decimal:
        return self.equity - self.week_pnl


# Défauts d'un compte swing : moins d'entrées, plus de positions, perte hebdo et exposition totale.
SWING_LIMITS = RiskLimits(
    max_position_pct=Decimal("20"),
    max_session_loss_pct=Decimal("2"),  # non appliquée en swing (remplacée par la perte hebdo)
    max_trades=3,  # entrées par jour UTC
    max_positions=4,
    min_entry_gap=timedelta(hours=1),
    max_week_loss_pct=Decimal("4"),
    max_exposure_pct=Decimal("60"),
)


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


def week_loss_breached(state: RiskState, limits: RiskLimits) -> bool:
    if limits.max_week_loss_pct is None:
        return False
    return state.week_pnl <= -(limits.max_week_loss_pct / 100) * state.week_start_equity


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
    if limits.swing:
        if week_loss_breached(state, limits):
            refusals.append(
                Refusal(
                    MAX_WEEK_LOSS,
                    f"perte max sur 7 jours glissants atteinte ({fmt.usdt(state.week_pnl, sign=True)}, "
                    f"limite {fmt.num(limits.max_week_loss_pct)} % de {fmt.usdt(state.week_start_equity)}) : "
                    "plus d'entrée tant qu'elle n'est pas sortie de la fenêtre",
                )
            )
    elif state.halted or loss_breached(state, limits):
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
    if limits.max_exposure_pct is not None:
        total = sum(state.exposure.values(), Decimal(0)) + request.amount
        cap_total = limits.max_exposure_pct / 100 * state.equity
        if total > cap_total:
            refusals.append(
                Refusal(
                    MAX_EXPOSURE,
                    f"exposition totale de {fmt.usdt(total)} après l'entrée, max {fmt.num(limits.max_exposure_pct)} % "
                    f"de la valeur du compte = {fmt.usdt(cap_total)}",
                )
            )
    return refusals


CENT = Decimal("0.01")


def risk_size(
    equity: Decimal, cash: Decimal, ask: Decimal, stop: Decimal, risk_pct: Decimal,
    max_position_pct: Decimal, fee_rate: Decimal,
) -> Decimal:
    """Montant d'achat (USDT, frais inclus) qui risque `risk_pct` % de la valeur du compte jusqu'au stop.

    q = risque % x valeur / (entrée - stop), entrée = ask ; montant = q x ask x (1 + frais),
    plafonné par la taille max du garde-fou (montant frais inclus, comme l'exposition qu'il vérifie)
    puis par le cash libre ; arrondi au centime inférieur.
    """
    if ask - stop <= 0:
        raise OrderRejected(f"Taille au risque impossible : le stop ({fmt.price(stop)}) doit être sous l'entrée "
                            f"({fmt.price(ask)}).")
    quantity = risk_pct / 100 * equity / (ask - stop)
    amount = quantity * ask * (1 + fee_rate)
    amount = min(amount, max_position_pct / 100 * equity, cash)
    return max(amount, Decimal(0)).quantize(CENT, rounding=ROUND_DOWN)


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


# --- Lecture et validation des limites (saisies en texte) ---

KEYS = {"taille": "max_position_pct", "perte": "max_session_loss_pct", "trades": "max_trades",
        "positions": "max_positions", "delai": "min_entry_gap", "semaine": "max_week_loss_pct",
        "exposition": "max_exposure_pct"}


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
        elif key in ("semaine", "exposition"):
            if not limits.swing:
                raise InvalidInput(f"Limite '{key}' réservée aux comptes swing.")
            label = "Perte max sur 7 jours" if key == "semaine" else "Exposition totale max"
            values[KEYS[key]] = _pct(raw, label)
        else:
            values["min_entry_gap"] = parse_gap(raw)
    return RiskLimits(**{**limits.__dict__, **values})
