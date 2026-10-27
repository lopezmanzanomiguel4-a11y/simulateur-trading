"""Moteur de paper trading : calculs purs, sans réseau ni base de données.

Conventions :
- achat au marché au prix ask, vente au prix bid ;
- frais = FEE_RATE x (quantité x prix), à chaque ordre ;
- le montant d'un achat est le total débité, frais inclus ;
- le prix de revient (cost_basis) inclut les frais d'achat, donc le P&L réalisé est net de tous les frais ;
- le P&L latent est calculé au bid, hors frais de sortie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation

from simtrade import fmt
from simtrade.errors import InvalidCapital, InvalidInput, OrderRejected, SimTradeError
from simtrade.models import PAIRS, QUOTE_ASSET, Account, Order, Position, Quote, Side, base_asset, utcnow

FEE_RATE = Decimal("0.001")
DEFAULT_CAPITAL = Decimal("10000")
MAX_CAPITAL = Decimal("10000000")
QTY_STEP = Decimal("0.00000001")  # précision des quantités (8 décimales)
USDT_STEP = Decimal("0.00000001")  # précision interne des montants
CENT = Decimal("0.01")


def normalize_pair(raw: str) -> str:
    """Accepte BTCUSDT, btc, BTC/USDT, btc-usdt."""
    pair = str(raw).strip().upper().replace("/", "").replace("-", "")
    if pair and not pair.endswith(QUOTE_ASSET):
        pair += QUOTE_ASSET
    if pair not in PAIRS:
        raise InvalidInput(f"Paire inconnue : '{raw}'. Paires disponibles : {', '.join(PAIRS)}.")
    return pair


def parse_decimal(raw: object, label: str, error: type[SimTradeError] = InvalidInput) -> Decimal:
    """Lit un nombre ; accepte la virgule décimale."""
    if isinstance(raw, Decimal):
        value = raw
    else:
        text = str(raw).strip().replace(",", ".")
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise error(f"{label} invalide : '{raw}' n'est pas un nombre.") from None
    if not value.is_finite():
        raise error(f"{label} invalide : '{raw}' n'est pas un nombre fini.")
    return value


def validate_capital(raw: object = DEFAULT_CAPITAL) -> Decimal:
    value = parse_decimal(raw, "Capital", InvalidCapital)
    if value <= 0:
        raise InvalidCapital(f"Le capital doit être strictement positif (reçu : {raw}).")
    if value > MAX_CAPITAL:
        raise InvalidCapital(f"Capital trop élevé : maximum {fmt.usdt(MAX_CAPITAL)} (reçu : {raw}).")
    if value != value.quantize(CENT):
        raise InvalidCapital(f"Le capital accepte au plus 2 décimales (reçu : {raw}).")
    return value.quantize(CENT)


def _q(value: Decimal) -> Decimal:
    return value.quantize(USDT_STEP, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class PositionValue:
    position: Position
    bid: Decimal
    value: Decimal  # quantité x bid
    unrealized_pnl: Decimal
    unrealized_pct: Decimal


@dataclass(frozen=True)
class Valuation:
    account: Account
    positions: list[PositionValue]
    cash: Decimal
    positions_value: Decimal
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    performance_pct: Decimal  # (valeur totale - capital initial) / capital initial x 100


@dataclass
class Portfolio:
    account: Account
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    fee_rate: Decimal = FEE_RATE

    # --- Vérifications sans prix (permettent de refuser avant d'interroger le réseau) ---

    def check_buy(self, amount_usdt: object) -> Decimal:
        amount = parse_decimal(amount_usdt, "Montant")
        if amount <= 0:
            raise OrderRejected(f"Le montant d'achat doit être strictement positif (reçu : {amount_usdt}).")
        if amount > self.account.cash:
            raise OrderRejected(
                f"Solde insuffisant : {fmt.usdt(amount)} demandés, {fmt.usdt(self.account.cash)} disponibles."
            )
        return amount

    def check_sell(self, pair: str, quantity: object) -> tuple[Position, Decimal]:
        position = self.positions.get(pair)
        if position is None or position.quantity <= 0:
            raise OrderRejected(f"Aucune position sur {pair} : rien à vendre.")
        if str(quantity).strip().lower() == "tout":
            return position, position.quantity
        qty = parse_decimal(quantity, "Quantité")
        if qty <= 0:
            raise OrderRejected(f"La quantité à vendre doit être strictement positive (reçu : {quantity}).")
        qty = qty.quantize(QTY_STEP, rounding=ROUND_DOWN)
        if qty <= 0:
            raise OrderRejected(f"Quantité trop faible : minimum {QTY_STEP} {base_asset(pair)}.")
        if qty > position.quantity:
            raise OrderRejected(
                f"Quantité insuffisante : tu veux vendre {fmt.qty(qty)} {base_asset(pair)}, "
                f"tu en détiens {fmt.qty(position.quantity)}."
            )
        return position, qty

    # --- Ordres au marché ---

    def buy(self, quote: Quote, amount_usdt: object, now: datetime | None = None) -> Order:
        """Achète pour `amount_usdt` USDT au total (frais inclus), au prix ask."""
        amount = self.check_buy(amount_usdt)
        ask = quote.ask
        qty = (amount / (ask * (1 + self.fee_rate))).quantize(QTY_STEP, rounding=ROUND_DOWN)

        def costs(q: Decimal) -> tuple[Decimal, Decimal]:
            gross = _q(q * ask)
            return gross, _q(gross * self.fee_rate)

        gross, fee = costs(qty)
        while qty > 0 and gross + fee > amount:  # garde-fou contre les arrondis
            qty -= QTY_STEP
            gross, fee = costs(qty)
        if qty <= 0:
            raise OrderRejected(
                f"Montant trop faible : {fmt.usdt(amount)} ne permet pas d'acheter {QTY_STEP} {base_asset(quote.pair)}."
            )
        total = gross + fee

        self.account.cash -= total
        position = self.positions.get(quote.pair)
        if position is None:
            self.positions[quote.pair] = Position(quote.pair, qty, total)
        else:
            position.quantity += qty
            position.cost_basis += total
        self.fees_paid += fee

        return Order(
            account_id=self.account.id,
            pair=quote.pair,
            side=Side.BUY,
            quantity=qty,
            price=ask,
            gross=gross,
            fee=fee,
            cash_flow=-total,
            realized_pnl=Decimal(0),
            source=quote.source,
            created_at=now or utcnow(),
        )

    def sell(self, quote: Quote, quantity: object, now: datetime | None = None) -> Order:
        """Vend `quantity` unités (ou "tout") au prix bid."""
        position, qty = self.check_sell(quote.pair, quantity)
        bid = quote.bid
        gross = _q(qty * bid)
        fee = _q(gross * self.fee_rate)
        proceeds = gross - fee

        if qty == position.quantity:
            cost = position.cost_basis
        else:
            cost = _q(position.cost_basis * qty / position.quantity)
        realized = proceeds - cost

        self.account.cash += proceeds
        position.quantity -= qty
        position.cost_basis -= cost
        if position.quantity == 0:
            del self.positions[quote.pair]
        self.realized_pnl += realized
        self.fees_paid += fee

        return Order(
            account_id=self.account.id,
            pair=quote.pair,
            side=Side.SELL,
            quantity=qty,
            price=bid,
            gross=gross,
            fee=fee,
            cash_flow=proceeds,
            realized_pnl=realized,
            source=quote.source,
            created_at=now or utcnow(),
        )

    # --- Valorisation ---

    def valuation(self, quotes: dict[str, Quote]) -> Valuation:
        lines: list[PositionValue] = []
        for pair, position in sorted(self.positions.items()):
            quote = quotes.get(pair)
            if quote is None:
                raise SimTradeError(f"Pas de prix disponible pour {pair}, impossible de valoriser la position.")
            value = position.quantity * quote.bid
            pnl = value - position.cost_basis
            lines.append(PositionValue(position, quote.bid, value, pnl, pnl / position.cost_basis * 100))

        positions_value = sum((line.value for line in lines), Decimal(0))
        total = self.account.cash + positions_value
        initial = self.account.initial_capital
        return Valuation(
            account=self.account,
            positions=lines,
            cash=self.account.cash,
            positions_value=positions_value,
            total_value=total,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=sum((line.unrealized_pnl for line in lines), Decimal(0)),
            fees_paid=self.fees_paid,
            performance_pct=(total - initial) / initial * 100,
        )
