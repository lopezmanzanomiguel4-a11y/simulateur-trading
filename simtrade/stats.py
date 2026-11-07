"""Statistiques d'un compte ou d'une stratégie, partagées par le live et le backtest (pur).

Courbe de valeur : chaque minute, solde + positions marquées au bid. Le bid historique n'étant pas
enregistré, il est estimé par la clôture 1m × (1 − s/2) (même convention que le backtest) ; une
minute sans bougie reprend le dernier prix connu et est comptée comme manquante.
Formules : voir README, « Journal et statistiques ».
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from simtrade.market import Candle, bucket_start
from simtrade.models import Order, Side
from simtrade.trades import Trade

MINUTE = timedelta(minutes=1)
DEFAULT_SPREAD_PCT = Decimal("0.02")
HUNDRED = Decimal(100)
ANNUAL_DAYS = 365


@dataclass(frozen=True)
class Curve:
    points: list[tuple[datetime, Decimal]]  # (fin de la minute, valeur)
    missing: int  # minutes avec une position ouverte et sans bougie (prix reporté)


@dataclass(frozen=True)
class Stats:
    pnl: Decimal  # valeur finale - capital (ou somme des trades sans courbe)
    pnl_pct: Decimal
    max_drawdown_pct: Decimal
    win_rate: Decimal | None  # en %, None sans trade
    avg_pnl: Decimal | None  # par trade
    trades: int
    fees: Decimal
    sharpe: float | None  # None sous 2 sessions ou si l'écart type est nul
    sessions: int  # jours UTC de la période (effectif du Sharpe)
    wins: int


def bid_mark(close: Decimal, spread_pct: Decimal) -> Decimal:
    return close * (1 - spread_pct / HUNDRED / 2)


def equity_curve(
    capital: Decimal,
    orders: Sequence[Order],
    marks: Mapping[str, Sequence[Candle]],
    start: datetime,
    end: datetime,
    spread_pct: Decimal = DEFAULT_SPREAD_PCT,
    step: timedelta = MINUTE,
) -> Curve:
    """Valeur à la fin de chaque minute (ou de chaque `step`, 1 h pour le swing) de [start, end).
    `marks` : bougies triées par paire, de la même durée que `step`. Les ordres antérieurs à `start`
    sont appliqués d'emblée."""
    sec = int(step.total_seconds())
    start = bucket_start(start, sec)
    end = end if bucket_start(end, sec) == end else bucket_start(end, sec) + step  # pas entamé inclus
    orders = sorted(orders, key=lambda o: (o.created_at, o.id or 0))
    by_pair = {p: {c.open_time: c.close for c in cs} for p, cs in marks.items()}
    last_price: dict[str, Decimal] = {}
    for pair, cs in marks.items():  # dernier prix connu avant le début
        before = [c.close for c in cs if c.open_time < start]
        if before:
            last_price[pair] = before[-1]
    cash = capital
    qty: dict[str, Decimal] = {}
    i = 0
    points: list[tuple[datetime, Decimal]] = []
    missing = 0
    t = start
    while t < end:
        stop = t + step
        while i < len(orders) and orders[i].created_at < stop:
            o = orders[i]
            cash += o.cash_flow
            qty[o.pair] = qty.get(o.pair, Decimal(0)) + (o.quantity if o.side is Side.BUY else -o.quantity)
            if o.price:
                last_price.setdefault(o.pair, o.price)
            i += 1
        value = cash
        for pair, q in qty.items():
            close = by_pair.get(pair, {}).get(t)
            if close is not None:
                last_price[pair] = close
            if q:
                if close is None:
                    missing += 1
                price = last_price.get(pair)
                if price is not None:
                    value += q * bid_mark(price, spread_pct)
        points.append((stop, value))
        t = stop
    return Curve(points, missing)


def max_drawdown_pct(values: Sequence[Decimal], start_value: Decimal) -> Decimal:
    peak, worst = start_value, Decimal(0)
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * HUNDRED)
    return worst


def daily_returns(points: Sequence[tuple[datetime, Decimal]], start_value: Decimal) -> list[Decimal]:
    """Rendement de chaque jour UTC : valeur de fin du jour / valeur de fin du jour précédent − 1.
    Un jour sans trade (et sans position) a un rendement nul."""
    ends: dict[date, Decimal] = {}
    for at, v in points:
        ends[(at - MINUTE).date()] = v  # point daté de la fin de sa minute
    out, prev = [], start_value
    for day in sorted(ends):
        out.append(ends[day] / prev - 1 if prev else Decimal(0))
        prev = ends[day]
    return out


def sharpe(returns: Sequence[Decimal]) -> float | None:
    """Moyenne / écart type (n − 1) des rendements quotidiens × √365, taux sans risque nul."""
    n = len(returns)
    if n < 2:
        return None
    xs = [float(r) for r in returns]
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(ANNUAL_DAYS)


def compute(trades: Sequence[Trade], curve: Curve | None, capital: Decimal, fees: Decimal | None = None) -> Stats:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    total = sum((t.pnl for t in trades), Decimal(0))
    if curve is not None and curve.points:
        values = [v for _, v in curve.points]
        pnl = values[-1] - capital
        dd = max_drawdown_pct(values, capital)
        returns = daily_returns(curve.points, capital)
    else:
        pnl, dd, returns = total, Decimal(0), []
    return Stats(
        pnl=pnl,
        pnl_pct=pnl / capital * HUNDRED if capital else Decimal(0),
        max_drawdown_pct=dd,
        win_rate=Decimal(wins) / n * HUNDRED if n else None,
        avg_pnl=total / n if n else None,
        trades=n,
        fees=fees if fees is not None else sum((t.fees for t in trades), Decimal(0)),
        sharpe=sharpe(returns),
        sessions=len(returns),
        wins=wins,
    )


def orders_of(trades: Sequence[Trade], orders: Sequence[Order]) -> list[Order]:
    """Ordres des trades donnés (pour la courbe d'une stratégie seule)."""
    ids = {i for t in trades for i in t.order_ids}
    return [o for o in orders if o.id in ids]


def parse_utc(raw: object, label: str) -> datetime:
    """'AAAA-MM-JJ' (00:00 UTC) ou date-heure ISO (UTC si sans fuseau)."""
    from datetime import timezone

    from simtrade.errors import InvalidInput

    text = str(raw).strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInput(f"Paramètre '{label}' invalide : '{text}' (attendu AAAA-MM-JJ ou AAAA-MM-JJTHH:MM).") from None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
