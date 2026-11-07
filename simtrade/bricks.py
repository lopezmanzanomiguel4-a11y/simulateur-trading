"""Briques des stratégies intraday : fonctions pures (aucun I/O, aucune horloge interne).

Règle commune : une brique ne lit QUE les bougies clôturées à `now` (close_time <= now). La bougie
en cours est écartée même si l'appelant la fournit, pour qu'un signal ne change jamais après coup.
Chaque brique renvoie None quand il n'y a pas assez de bougies pour conclure.

Le même code sert en live (StrategyRunner) et au backtest (étape 7) : seule la provenance des
bougies change (mémoire du PriceHub ou klines en base).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from simtrade import market
from simtrade.market import Candle, _q, session_start

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class MarketView:
    """Ce qu'une stratégie peut lire à l'instant `now`."""

    now: datetime
    candles: Sequence[Candle]  # intervalle de la stratégie
    candles_1m: Sequence[Candle]  # pour le VWAP de session et le range d'ouverture


def closed(candles: Sequence[Candle], now: datetime) -> list[Candle]:
    """Bougies clôturées à `now`, dans l'ordre."""
    return [c for c in candles if c.close_time <= now]


def last_closed(view: MarketView) -> Candle | None:
    candles = closed(view.candles, view.now)
    return candles[-1] if candles else None


def closes(candles: Sequence[Candle]) -> list[Decimal]:
    return [c.close for c in candles]


# --- Valeurs ---


def atr(candles: Sequence[Candle], n: int) -> Decimal | None:
    """ATR de Wilder : vrai range TR_t = max(h - l, |h - c_{t-1}|, |l - c_{t-1}|), moyenne des n premiers
    puis lissage ATR_t = ((n - 1) ATR_{t-1} + TR_t) / n. Il faut n + 1 bougies."""
    if n < 1 or len(candles) < n + 1:
        return None
    ranges = [
        max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        for prev, c in zip(candles, candles[1:])
    ]
    value = sum(ranges[:n], Decimal(0)) / n
    for tr in ranges[n:]:
        value = (value * (n - 1) + tr) / n
    return _q(value)


def atr_pct(candles: Sequence[Candle], n: int) -> Decimal | None:
    """ATR en % de la dernière clôture."""
    value = atr(candles, n)
    if value is None or candles[-1].close == 0:
        return None
    return _q(value / candles[-1].close * HUNDRED)


def session_vwap(candles_1m: Sequence[Candle], candle: Candle) -> Decimal | None:
    """VWAP de la session de `candle` (00:00 UTC), sur les bougies 1m clôturées jusqu'à sa clôture."""
    start = session_start(candle.open_time)
    used = [c for c in candles_1m if start <= c.open_time and c.close_time <= candle.close_time]
    return market.vwap(used)


def vwap_gap_pct(candles_1m: Sequence[Candle], candle: Candle) -> Decimal | None:
    """Écart de la clôture au VWAP de session, en % du VWAP (négatif sous le VWAP)."""
    value = session_vwap(candles_1m, candle)
    if value is None or value == 0:
        return None
    return _q((candle.close - value) / value * HUNDRED)


def ema_pair(candles: Sequence[Candle], fast: int, slow: int) -> tuple[Decimal, Decimal] | None:
    values = closes(candles)
    f, s = market.ema(values, fast), market.ema(values, slow)
    return None if f is None or s is None else (f, s)


def ema_cross(candles: Sequence[Candle], fast: int, slow: int) -> str | None:
    """'haussier' si la rapide passe au-dessus de la lente sur la dernière bougie, 'baissier' pour
    l'inverse, '' sans croisement, None s'il manque des bougies."""
    now, before = ema_pair(candles, fast, slow), ema_pair(candles[:-1], fast, slow)
    if now is None or before is None:
        return None
    if before[0] <= before[1] and now[0] > now[1]:
        return "haussier"
    if before[0] >= before[1] and now[0] < now[1]:
        return "baissier"
    return ""


def opening_range(candles_1m: Sequence[Candle], day_start: datetime, start: time, minutes: int) -> tuple[Decimal, Decimal] | None:
    """(plus haut, plus bas) des bougies 1m de [début, début + minutes) ; None si une minute manque."""
    begin = day_start + timedelta(hours=start.hour, minutes=start.minute)
    end = begin + timedelta(minutes=minutes)
    inside = [c for c in candles_1m if begin <= c.open_time < end and c.interval == "1m"]
    if len(inside) != minutes:
        return None
    return max(c.high for c in inside), min(c.low for c in inside)


def range_end(candle: Candle, start: time, minutes: int) -> datetime:
    return session_start(candle.open_time) + timedelta(hours=start.hour, minutes=start.minute + minutes)


# --- Briques d'entrée et de sortie : (vue, paramètres) -> (vrai / faux / None, valeurs) ---


@dataclass(frozen=True)
class BrickResult:
    ok: bool | None  # None : pas assez de données
    values: dict[str, Decimal | str | None]


def rsi_extreme(view: MarketView, p: dict) -> BrickResult:
    candles = closed(view.candles, view.now)
    value = market.rsi(closes(candles), p["n"])
    if value is None:
        return BrickResult(None, {"rsi": None})
    ok = value <= p["seuil"] if p["zone"] == "bas" else value >= p["seuil"]
    return BrickResult(ok, {"rsi": value})


def croisement_ema(view: MarketView, p: dict) -> BrickResult:
    candles = closed(view.candles, view.now)
    cross = ema_cross(candles, p["rapide"], p["lente"])
    if cross is None:
        return BrickResult(None, {"croisement": None})
    pair = ema_pair(candles, p["rapide"], p["lente"])
    return BrickResult(cross == p["sens"], {"croisement": cross, "ema_rapide": pair[0], "ema_lente": pair[1]})


def ecart_vwap(view: MarketView, p: dict) -> BrickResult:
    candle = last_closed(view)
    gap = None if candle is None else vwap_gap_pct(closed(view.candles_1m, view.now), candle)
    if gap is None:
        return BrickResult(None, {"ecart_vwap_pct": None})
    ok = gap <= -p["seuil_pct"] if p["cote"] == "sous" else gap >= p["seuil_pct"]
    return BrickResult(ok, {"ecart_vwap_pct": gap})


def au_dessus_vwap(view: MarketView, p: dict) -> BrickResult:
    candle = last_closed(view)
    gap = None if candle is None else vwap_gap_pct(closed(view.candles_1m, view.now), candle)
    if gap is None:
        return BrickResult(None, {"ecart_vwap_pct": None})
    return BrickResult(gap > 0, {"ecart_vwap_pct": gap})


def cassure_range(view: MarketView, p: dict) -> BrickResult:
    """Première clôture au-dessus du plus haut du range d'ouverture, depuis la fin du range."""
    candles = closed(view.candles, view.now)
    if len(candles) < 2:
        return BrickResult(None, {"range_haut": None, "range_bas": None})
    last = candles[-1]
    end = range_end(last, p["debut"], p["minutes"])
    if last.open_time < end:
        return BrickResult(False, {"range_haut": None, "range_bas": None})  # range pas encore terminé
    rng = opening_range(closed(view.candles_1m, view.now), session_start(last.open_time), p["debut"], p["minutes"])
    if rng is None:
        return BrickResult(None, {"range_haut": None, "range_bas": None})
    high, low = rng
    earlier = [c for c in candles[:-1] if c.open_time >= end]
    first = all(c.close <= high for c in earlier)
    ok = first and candles[-2].close <= high < last.close
    return BrickResult(ok, {"range_haut": high, "range_bas": low})


def volatilite(view: MarketView, p: dict) -> BrickResult:
    value = atr_pct(closed(view.candles, view.now), p["n"])
    if value is None:
        return BrickResult(None, {"atr_pct": None})
    return BrickResult(p["min_pct"] <= value <= p["max_pct"], {"atr_pct": value})


def retour_vwap(view: MarketView, p: dict) -> BrickResult:
    candle = last_closed(view)
    gap = None if candle is None else vwap_gap_pct(closed(view.candles_1m, view.now), candle)
    if gap is None:
        return BrickResult(None, {"ecart_vwap_pct": None})
    return BrickResult(gap >= 0, {"ecart_vwap_pct": gap})


def retour_dans_range(view: MarketView, p: dict) -> BrickResult:
    candle = last_closed(view)
    if candle is None:
        return BrickResult(None, {"range_haut": None})
    rng = opening_range(closed(view.candles_1m, view.now), session_start(candle.open_time), p["debut"], p["minutes"])
    if rng is None:
        return BrickResult(None, {"range_haut": None})
    return BrickResult(candle.close < rng[0], {"range_haut": rng[0]})


def au_dessus_ema(view: MarketView, p: dict) -> BrickResult:
    """Vrai si la dernière clôture est au-dessus de l'EMA `n` des bougies de l'intervalle."""
    candles = closed(view.candles, view.now)
    value = market.ema(closes(candles), p["n"])
    if value is None:
        return BrickResult(None, {"ema": None})
    return BrickResult(candles[-1].close > value, {"ema": value})


def hauteur_range(view: MarketView, p: dict) -> BrickResult:
    """Vrai si la hauteur du range d'ouverture ((haut − bas) / haut) atteint `min_pct` %.
    Les paramètres du range (debut, minutes) viennent de la brique cassure_range."""
    candle = last_closed(view)
    if candle is None:
        return BrickResult(None, {"hauteur_range_pct": None})
    rng = opening_range(closed(view.candles_1m, view.now), session_start(candle.open_time), p["debut"], p["minutes"])
    if rng is None:
        return BrickResult(None, {"hauteur_range_pct": None})
    high, low = rng
    height = _q((high - low) / high * 100)
    return BrickResult(height >= p["min_pct"], {"hauteur_range_pct": height})


def duree_max(view: MarketView, p: dict, entered_at: datetime | None) -> BrickResult:
    """Vrai quand `bougies` bougies complètes de l'intervalle se sont écoulées depuis l'entrée."""
    candle = last_closed(view)
    if candle is None or entered_at is None:
        return BrickResult(None, {"bougies_depuis_entree": None})
    step = candle.close_time - candle.open_time
    elapsed = (candle.close_time - entered_at) // step
    return BrickResult(elapsed >= p["bougies"], {"bougies_depuis_entree": Decimal(elapsed)})
