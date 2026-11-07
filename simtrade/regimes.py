"""Régime de marché à une date, lu sur les bougies 5m clôturées seulement (pur, aucun regard vers le futur).

Deux mesures sur les `WINDOW` dernières bougies 5m closes :
- volatilité : ATR de Wilder (n = 14) en % de la dernière clôture ;
- ratio d'efficacité sur 24 bougies : ER = |c_T − c_{T−24}| / Σ |c_t − c_{t−1}| (1 : mouvement droit, 0 : aller-retour).

Règle (D16) : `forte_volatilite` si ATR % ≥ seuil de volatilité ; sinon `tendance` si ER ≥ seuil
d'efficacité ; sinon `range`. Seuils fixés sur l'échantillon, par paire (quantiles 80 % et 70 %), puis
appliqués tels quels au hors échantillon.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from simtrade import bricks
from simtrade.market import Candle

ATR_N = 14
ER_N = 24
WINDOW = 100  # bougies lues : l'ATR lissé ne dépend pas de l'historique plus ancien
ATR_QUANTILE = Decimal("0.80")
ER_QUANTILE = Decimal("0.70")
RANGE = "range"
TREND = "tendance"
HIGH_VOL = "forte_volatilite"
REGIMES = (RANGE, TREND, HIGH_VOL)


@dataclass(frozen=True)
class Thresholds:
    atr_pct: Decimal
    efficiency: Decimal

    def json(self) -> dict:
        return {"atr_pct": format(self.atr_pct, "f"), "efficacite": format(self.efficiency, "f")}


def efficiency(closes: Sequence[Decimal], n: int = ER_N) -> Decimal | None:
    if len(closes) < n + 1:
        return None
    last = closes[-(n + 1):]
    path = sum((abs(b - a) for a, b in zip(last, last[1:])), Decimal(0))
    if path == 0:
        return Decimal(0)
    return abs(last[-1] - last[0]) / path


def features(candles: Sequence[Candle]) -> tuple[Decimal, Decimal] | None:
    """(ATR %, ER) sur les WINDOW dernières bougies données (déjà closes)."""
    window = list(candles[-WINDOW:])
    atr = bricks.atr_pct(window, ATR_N)
    er = efficiency([c.close for c in window])
    if atr is None or er is None:
        return None
    return atr, er


def classify(value: tuple[Decimal, Decimal] | None, thresholds: Thresholds) -> str | None:
    if value is None:
        return None
    atr, er = value
    if atr >= thresholds.atr_pct:
        return HIGH_VOL
    if er >= thresholds.efficiency:
        return TREND
    return RANGE


def regime(candles_5m: Sequence[Candle], now: datetime, thresholds: Thresholds) -> str | None:
    """Régime à `now` : seules les bougies closes à `now` sont lues."""
    return classify(features(bricks.closed(candles_5m, now)), thresholds)


def quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    """Quantile au rang le plus proche : la plus petite valeur v telle qu'au moins q des valeurs soient ≤ v."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def series(candles_5m: Sequence[Candle]) -> list[tuple[datetime, tuple[Decimal, Decimal] | None]]:
    """Mesures à la clôture de chaque bougie (bougies triées), chacune sur les bougies closes à ce moment."""
    out = []
    for k, c in enumerate(candles_5m):
        out.append((c.close_time, features(candles_5m[max(0, k - WINDOW + 1) : k + 1])))
    return out


def fit_thresholds(candles_5m: Sequence[Candle]) -> Thresholds:
    """Seuils de l'échantillon : quantile 80 % de l'ATR % et 70 % de l'ER, sur chaque clôture mesurable."""
    values = [v for _, v in series(candles_5m) if v is not None]
    if not values:
        raise ValueError("Pas assez de bougies 5m pour fixer les seuils de régime.")
    return Thresholds(quantile([a for a, _ in values], ATR_QUANTILE), quantile([e for _, e in values], ER_QUANTILE))


class Timeline:
    """Régime à toute date, à partir des mesures précalculées (dernière clôture ≤ date)."""

    def __init__(self, candles_5m: Sequence[Candle], thresholds: Thresholds):
        points = series(candles_5m)
        self.times = [t for t, _ in points]
        self.regimes = [classify(v, thresholds) for _, v in points]
        self.thresholds = thresholds

    def at(self, when: datetime) -> str | None:
        k = bisect.bisect_right(self.times, when) - 1
        return self.regimes[k] if k >= 0 else None
