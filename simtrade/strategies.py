"""Stratégies intraday décrites en JSON : schéma, validation, évaluation. Module pur (aucun I/O).

Une stratégie = {nom, paire, intervalle, entree, sortie, stop, objectif, taille, heures_actives}.
Pas de code libre : `entree` et `sortie` sont des listes de briques (simtrade.bricks) avec leurs
paramètres, chacun borné. Le schéma (types, bornes, défauts) est exporté tel quel par l'API pour
les agents de l'étape 8.

Le même code décide en live (StrategyRunner) et au backtest (étape 7) :
- evaluate(strategie, vue, entree_le) : signaux d'entrée et de sortie sur la dernière bougie clôturée ;
- entry_blockers / quality_blockers : raisons d'ignorer un signal ;
- levels(plan, ask, bid) : stop et objectif à l'exécution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from simtrade import bricks
from simtrade.bricks import BrickResult, MarketView
from simtrade.engine import FEE_RATE, normalize_pair, parse_decimal
from simtrade.errors import InvalidInput
from simtrade.market import Quality
from simtrade.models import PAIRS
from simtrade.sessions import SessionRules, entry_allowed, parse_hhmm

MODELS_DIR = Path(__file__).parent / "modeles"
INTERVALS = ("5m", "15m")
TICK = Decimal("0.01")  # pas de prix des trois paires
HUNDRED = Decimal(100)
# Objectif minimum : frais aller-retour (2 x 0,1 %) + marge de 0,10 % (spread, glissement).
MIN_TARGET_MARGIN_PCT = Decimal("0.10")
MIN_TARGET_PCT = 2 * FEE_RATE * HUNDRED + MIN_TARGET_MARGIN_PCT  # 0,30 %
STOP_MIN_PCT = Decimal("0.2")  # bornes d'un stop calculé (ATR, range) : au-delà, signal ignoré
STOP_MAX_PCT = Decimal("3")
MAX_ENTRY_BRICKS = 4
MAX_EXIT_BRICKS = 3
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Raisons d'ignorer un signal
OUT_OF_HOURS = "hors_heures"
AFTER_LAST_ENTRY = "apres_derniere_entree"
ALREADY_IN = "deja_en_position"
NO_POSITION = "pas_de_position"
HALTED = "arret_session"
STOP_OUT_OF_BOUNDS = "stop_hors_bornes"
TARGET_TOO_CLOSE = "objectif_trop_proche"
NOT_DEFINITIVE = "bougie_non_definitive"
NO_FRESH_QUOTE = "pas_de_cotation_fraiche"
CLOCK_UNKNOWN = "horloge_non_mesuree"
VWAP_UNRELIABLE = "vwap_non_fiable"
# Raisons de `qualite` qui bloquent un signal (la bougie en cours et le volume nul ne comptent pas)
BLOCKING_QUALITY = ("prix_perime", "flux_transactions_muet", "repli_coinbase", "horloge_decalee")


# --- Schéma ---


@dataclass(frozen=True)
class Param:
    kind: str  # "entier", "decimal", "choix", "heure"
    default: Any = None
    min: Any = None
    max: Any = None
    choices: tuple[str, ...] = ()

    def json(self) -> dict:
        out: dict[str, Any] = {"type": self.kind, "defaut": _out(self.default)}
        if self.choices:
            out["choix"] = list(self.choices)
        if self.min is not None:
            out["min"], out["max"] = _out(self.min), _out(self.max)
        return out


def _int(lo: int, hi: int, default: int) -> Param:
    return Param("entier", default, lo, hi)


def _dec(lo: str, hi: str, default: str) -> Param:
    return Param("decimal", Decimal(default), Decimal(lo), Decimal(hi))


def _hour(lo: str, hi: str, default: str) -> Param:
    return Param("heure", parse_hhmm(default, default), parse_hhmm(lo, lo), parse_hhmm(hi, hi))


def _choice(*choices: str) -> Param:
    return Param("choix", choices[0], choices=choices)


# Seuil du RSI : bornes et défaut selon la zone.
RSI_THRESHOLD = {"bas": _dec("5", "45", "30"), "haut": _dec("60", "95", "70")}

BRICKS: dict[str, dict[str, Param]] = {
    "rsi_extreme": {"zone": _choice("bas", "haut"), "n": _int(2, 50, 14), "seuil": RSI_THRESHOLD["bas"]},
    "croisement_ema": {"sens": _choice("haussier", "baissier"), "rapide": _int(3, 50, 9), "lente": _int(10, 200, 21)},
    "ecart_vwap": {"cote": _choice("sous", "dessus"), "seuil_pct": _dec("0.1", "3", "0.6")},
    "au_dessus_vwap": {},
    "cassure_range": {"debut": _hour("00:00", "20:00", "00:00"), "minutes": _int(15, 120, 30)},
    "volatilite": {"n": _int(5, 50, 14), "min_pct": _dec("0.05", "2", "0.10"), "max_pct": _dec("0.2", "5", "1.50")},
    "retour_vwap": {},
    "retour_dans_range": {},
    "duree_max": {"bougies": _int(1, 96, 12)},
}
ENTRY_BRICKS = ("rsi_extreme", "croisement_ema", "ecart_vwap", "au_dessus_vwap", "cassure_range", "volatilite")
EXIT_BRICKS = ("rsi_extreme", "croisement_ema", "retour_vwap", "retour_dans_range", "duree_max")
# Défaut propre au côté : on entre sur un RSI bas / un croisement haussier, on sort sur l'inverse.
SIDE_DEFAULTS = {"sortie": {"rsi_extreme": {"zone": "haut"}, "croisement_ema": {"sens": "baissier"}}}
VWAP_BRICKS = {"ecart_vwap", "au_dessus_vwap", "retour_vwap"}

STOPS: dict[str, dict[str, Param]] = {
    "pct": {"valeur": _dec("0.2", "5", "0.6")},
    "atr": {"multiple": _dec("0.5", "5", "1.5"), "n": _int(5, 50, 14)},
    "range": {"marge_pct": _dec("0", "1", "0.05")},
}
TARGETS: dict[str, dict[str, Param]] = {
    "pct": {"valeur": _dec(str(MIN_TARGET_PCT), "10", "1.0")},
    "atr": {"multiple": _dec("0.5", "10", "3"), "n": _int(5, 50, 14)},
    "r": {"multiple": _dec("1", "5", "2")},
}
GROUPS: dict[str, dict[str, Param]] = {
    "taille": {"pct_capital": _dec("1", "20", "10")},
    "heures_actives": {"debut": _hour("00:00", "23:00", "06:00"), "fin": _hour("00:00", "23:00", "22:00")},
}
TOP: dict[str, Param] = {"paire": _choice(*PAIRS), "intervalle": _choice(*INTERVALS)}
KEYS = ("nom", "paire", "intervalle", "entree", "sortie", "stop", "objectif", "taille", "heures_actives")


def schema() -> dict:
    """Types, bornes et défauts de chaque paramètre (pour l'API et les agents)."""
    bricks_json = {name: {k: p.json() for k, p in params.items()} for name, params in BRICKS.items()}
    bricks_json["rsi_extreme"]["seuil"] = {"type": "decimal", "selon_zone": {z: p.json() for z, p in RSI_THRESHOLD.items()}}
    return {
        "paire": TOP["paire"].json(),
        "intervalle": TOP["intervalle"].json(),
        "briques": bricks_json,
        "briques_entree": list(ENTRY_BRICKS),
        "briques_sortie": list(EXIT_BRICKS),
        "max_briques_entree": MAX_ENTRY_BRICKS,
        "max_briques_sortie": MAX_EXIT_BRICKS,
        "stop": {t: {k: p.json() for k, p in ps.items()} for t, ps in STOPS.items()},
        "objectif": {t: {k: p.json() for k, p in ps.items()} for t, ps in TARGETS.items()},
        "objectif_minimum_pct": _out(MIN_TARGET_PCT),
        "stop_calcule_pct": {"min": _out(STOP_MIN_PCT), "max": _out(STOP_MAX_PCT)},
        **{g: {k: p.json() for k, p in ps.items()} for g, ps in GROUPS.items()},
    }


# --- Stratégie validée ---


@dataclass(frozen=True)
class Brick:
    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Strategy:
    nom: str
    paire: str
    intervalle: str
    entree: tuple[Brick, ...]
    sortie: tuple[Brick, ...]
    stop: Brick  # name = type de stop
    objectif: Brick  # name = type d'objectif
    taille_pct: Decimal
    debut: time
    fin: time

    def brick(self, name: str) -> Brick | None:
        return next((b for b in self.entree + self.sortie if b.name == name), None)

    @property
    def uses_vwap(self) -> bool:
        return any(b.name in VWAP_BRICKS for b in self.entree + self.sortie)

    def to_json(self) -> dict:
        """Forme canonique (décimaux en chaîne, heures HH:MM), relue à l'identique par validate."""
        def brick(b: Brick, key: str = "brique") -> dict:
            return {key: b.name, **{k: _out(v) for k, v in b.params.items()}}

        return {
            "nom": self.nom,
            "paire": self.paire,
            "intervalle": self.intervalle,
            "entree": [brick(b) for b in self.entree],
            "sortie": [brick(b) for b in self.sortie],
            "stop": brick(self.stop, "type"),
            "objectif": brick(self.objectif, "type"),
            "taille": {"pct_capital": _out(self.taille_pct)},
            "heures_actives": {"debut": _out(self.debut), "fin": _out(self.fin)},
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=False)


def _out(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, time):
        return f"{value:%H:%M}"
    return value


def _err(path: str, message: str) -> InvalidInput:
    return InvalidInput(f"Stratégie : '{path}' {message}")


def _value(param: Param, raw: Any, path: str) -> Any:
    if param.kind == "choix":
        if raw not in param.choices:
            raise _err(path, f"invalide : {raw!r} (choix : {', '.join(param.choices)}).")
        return raw
    if param.kind == "entier":
        if isinstance(raw, bool) or not (isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit())):
            raise _err(path, f"invalide : {raw!r} (entier attendu).")
        value: Any = int(raw)
    elif param.kind == "decimal":
        if not isinstance(raw, (str, int, Decimal)) or isinstance(raw, bool):
            raise _err(path, f"invalide : {raw!r} (nombre décimal en chaîne attendu, par exemple \"0.5\").")
        value = parse_decimal(raw, f"Stratégie : '{path}'")
    else:  # heure
        if not isinstance(raw, str):
            raise _err(path, f"invalide : {raw!r} (heure HH:MM attendue).")
        value = parse_hhmm(raw, f"Stratégie : '{path}'")
    if not param.min <= value <= param.max:
        raise _err(path, f"hors bornes : {_out(value)} (de {_out(param.min)} à {_out(param.max)}).")
    return value


def _params(spec: Mapping[str, Param], raw: Mapping[str, Any], path: str, ignore: tuple[str, ...] = ()) -> dict:
    unknown = sorted(set(raw) - set(spec) - set(ignore))
    if unknown:
        raise _err(f"{path}.{unknown[0]}", f"inconnu (permis : {', '.join(spec) or 'aucun paramètre'}).")
    return {k: _value(p, raw[k], f"{path}.{k}") if k in raw else p.default for k, p in spec.items()}


def _mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise _err(path, "doit être un objet JSON.")
    return raw


def _bricks(raw: Any, side: str, allowed: tuple[str, ...], most: int, least: int) -> tuple[Brick, ...]:
    if not isinstance(raw, list):
        raise _err(side, "doit être une liste de briques.")
    if not least <= len(raw) <= most:
        raise _err(side, f"doit contenir de {least} à {most} briques ({len(raw)} données).")
    out: list[Brick] = []
    for item in raw:
        item = _mapping(item, side)
        name = item.get("brique")
        if name not in allowed:
            raise _err(f"{side}.brique", f"inconnue ou non permise ici : {name!r} (permises : {', '.join(allowed)}).")
        if any(b.name == name for b in out):
            raise _err(f"{side}.{name}", "présente deux fois.")
        path = f"{side}.{name}"
        given = {k: v for k, v in item.items() if k != "brique"}
        merged = {**SIDE_DEFAULTS.get(side, {}).get(name, {}), **given}
        spec = dict(BRICKS[name])
        if name == "rsi_extreme":
            zone = _value(spec["zone"], merged.get("zone", "bas"), f"{path}.zone")
            spec["seuil"] = RSI_THRESHOLD[zone]
        params = _params(spec, merged, path)
        if name == "croisement_ema" and params["rapide"] >= params["lente"]:
            raise _err(f"{path}.rapide", f"doit être inférieure à la lente ({params['rapide']} >= {params['lente']}).")
        if name == "volatilite" and params["min_pct"] >= params["max_pct"]:
            raise _err(f"{path}.min_pct", f"doit être inférieur à max_pct ({_out(params['min_pct'])} >= {_out(params['max_pct'])}).")
        out.append(Brick(name, params))
    return tuple(out)


def _typed(raw: Any, path: str, types: dict[str, dict[str, Param]]) -> Brick:
    raw = _mapping(raw, path)
    kind = raw.get("type")
    if kind not in types:
        raise _err(f"{path}.type", f"invalide : {kind!r} (choix : {', '.join(types)}).")
    return Brick(kind, _params(types[kind], raw, f"{path}", ignore=("type",)))


def validate(raw: Any) -> Strategy:
    """Valide une stratégie JSON (dict) ; lève InvalidInput avec un message qui nomme le paramètre."""
    raw = _mapping(raw, "stratégie")
    unknown = sorted(set(raw) - set(KEYS))
    if unknown:
        raise _err(unknown[0], f"inconnu (clés permises : {', '.join(KEYS)}).")
    name = raw.get("nom")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _err("nom", f"invalide : {name!r} (1 à 40 caractères : minuscules, chiffres, tirets).")
    pair_raw = raw.get("paire", TOP["paire"].default)
    try:
        pair = normalize_pair(pair_raw) if isinstance(pair_raw, str) else pair_raw
    except InvalidInput:
        pair = pair_raw
    pair = _value(TOP["paire"], pair, "paire")
    interval = _value(TOP["intervalle"], raw.get("intervalle", TOP["intervalle"].default), "intervalle")
    if "entree" not in raw:
        raise _err("entree", "manquante (au moins une brique).")
    entry = _bricks(raw["entree"], "entree", ENTRY_BRICKS, MAX_ENTRY_BRICKS, 1)
    exits = _bricks(raw.get("sortie", []), "sortie", EXIT_BRICKS, MAX_EXIT_BRICKS, 0)
    for key in ("stop", "objectif"):
        if key not in raw:
            raise _err(key, "manquant.")
    stop = _typed(raw["stop"], "stop", STOPS)
    target = _typed(raw["objectif"], "objectif", TARGETS)
    has_range = any(b.name == "cassure_range" for b in entry)
    if stop.name == "range" and not has_range:
        raise _err("stop.type", "'range' demande la brique cassure_range dans l'entrée.")
    if any(b.name == "retour_dans_range" for b in exits) and not has_range:
        raise _err("sortie.retour_dans_range", "demande la brique cassure_range dans l'entrée.")
    size = _params(GROUPS["taille"], _mapping(raw.get("taille", {}), "taille"), "taille")
    hours = _params(GROUPS["heures_actives"], _mapping(raw.get("heures_actives", {}), "heures_actives"), "heures_actives")
    if hours["debut"] >= hours["fin"]:
        raise _err("heures_actives.debut", f"doit être avant la fin ({_out(hours['debut'])} >= {_out(hours['fin'])}).")
    return Strategy(name, pair, interval, entry, exits, stop, target, size["pct_capital"], hours["debut"], hours["fin"])


def loads(text: str) -> Strategy:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidInput(f"Stratégie : JSON illisible ({exc.msg}, ligne {exc.lineno}).") from None
    return validate(raw)


# --- Modèles et surcharges ---


def models() -> dict[str, dict]:
    """Modèles de départ (fichiers JSON de simtrade/modeles), par nom."""
    out = {}
    for path in sorted(MODELS_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        out[raw["nom"]] = raw
    return out


def model(name: str) -> dict:
    found = models()
    if name not in found:
        raise InvalidInput(f"Modèle de stratégie inconnu : '{name}' (modèles : {', '.join(found)}).")
    return found[name]


def with_overrides(raw: Mapping[str, Any], changes: Mapping[str, Any]) -> dict:
    """Copie de `raw` avec des surcharges 'cle=valeur' :
    - 'paire', 'intervalle', 'nom' ;
    - 'stop.<param>', 'objectif.<param>' (et '.type'), 'taille.pct_capital', 'heures_actives.debut' ;
    - '<brique>.<param>' (toutes les occurrences), ou 'entree.<brique>.<param>' / 'sortie.<brique>.<param>'."""
    out = json.loads(json.dumps(raw))
    # Un changement de type de stop ou d'objectif repart d'un objet neuf (les paramètres de l'ancien type n'ont plus de sens).
    for group in ("stop", "objectif"):
        if f"{group}.type" in changes and changes[f"{group}.type"] != out.get(group, {}).get("type"):
            out[group] = {}
    for key, value in changes.items():
        parts = key.split(".")
        if len(parts) == 1 and parts[0] in ("nom", "paire", "intervalle"):
            out[parts[0]] = value
        elif len(parts) == 2 and parts[0] in ("stop", "objectif", "taille", "heures_actives"):
            out.setdefault(parts[0], {})[parts[1]] = value
        elif len(parts) in (2, 3):
            sides = [parts[0]] if len(parts) == 3 else ["entree", "sortie"]
            if len(parts) == 3 and parts[0] not in ("entree", "sortie"):
                raise InvalidInput(f"Paramètre inconnu : '{key}'.")
            name, param = parts[-2], parts[-1]
            hits = [b for side in sides for b in out.get(side, []) if isinstance(b, dict) and b.get("brique") == name]
            if not hits:
                raise InvalidInput(f"Paramètre inconnu : '{key}' (la brique '{name}' n'est pas dans la stratégie).")
            for b in hits:
                b[param] = value
        else:
            raise InvalidInput(f"Paramètre inconnu : '{key}'.")
    return out


# --- Évaluation ---


@dataclass(frozen=True)
class Plan:
    """Stop et objectif d'une entrée, calculés sur les bougies clôturées, appliqués à l'exécution."""

    stop_pct: Decimal | None  # distance du stop en % sous le prix d'entrée (pct, atr)
    stop_level: Decimal | None  # niveau absolu (range)
    target_pct: Decimal | None  # distance de l'objectif en % (pct, atr)
    target_r: Decimal | None  # multiple de la distance du stop (r)


@dataclass(frozen=True)
class Evaluation:
    candle_open: datetime | None  # début de la dernière bougie clôturée (clé du signal)
    entry: bool | None  # None : pas assez de données
    exit: bool | None
    values: dict[str, Any] = field(default_factory=dict)
    plan: Plan | None = None
    exit_reasons: tuple[str, ...] = ()  # briques de sortie déclenchées


def _run(brick: Brick, view: MarketView, entered_at: datetime | None, strategy: Strategy) -> BrickResult:
    params = dict(brick.params)
    if brick.name == "retour_dans_range":
        params.update(strategy.brick("cassure_range").params)  # type: ignore[union-attr]
    if brick.name == "duree_max":
        return bricks.duree_max(view, params, entered_at)
    fn: Callable[[MarketView, dict], BrickResult] = getattr(bricks, brick.name)
    return fn(view, params)


def _plan(strategy: Strategy, view: MarketView) -> Plan | None:
    candles = bricks.closed(view.candles, view.now)
    stop, target = strategy.stop, strategy.objectif
    stop_pct = stop_level = target_pct = target_r = None
    if stop.name == "pct":
        stop_pct = stop.params["valeur"]
    elif stop.name == "atr":
        value = bricks.atr_pct(candles, stop.params["n"])
        if value is None:
            return None
        stop_pct = value * stop.params["multiple"]
    else:  # range
        rp = strategy.brick("cassure_range").params  # type: ignore[union-attr]
        last = candles[-1] if candles else None
        rng = None if last is None else bricks.opening_range(
            bricks.closed(view.candles_1m, view.now), bricks.session_start(last.open_time), rp["debut"], rp["minutes"]
        )
        if rng is None:
            return None
        stop_level = rng[1] * (1 - stop.params["marge_pct"] / HUNDRED)
    if target.name == "pct":
        target_pct = target.params["valeur"]
    elif target.name == "atr":
        value = bricks.atr_pct(candles, target.params["n"])
        if value is None:
            return None
        target_pct = value * target.params["multiple"]
    else:
        target_r = target.params["multiple"]
    return Plan(stop_pct, stop_level, target_pct, target_r)


def _combine(results: Sequence[tuple[str, BrickResult]], all_: bool) -> bool | None:
    oks = [r.ok for _, r in results]
    if all_:
        if any(ok is False for ok in oks):
            return False
        return None if any(ok is None for ok in oks) else True
    if any(ok is True for ok in oks):
        return True
    return None if oks and all(ok is None for ok in oks) else False


def evaluate(strategy: Strategy, view: MarketView, entered_at: datetime | None = None) -> Evaluation:
    """Signaux sur la dernière bougie clôturée à view.now. Entrée : TOUTES les briques d'entrée ;
    sortie : AU MOINS UNE brique de sortie. `entered_at` : heure de l'entrée de la position en cours."""
    last = bricks.last_closed(view)
    if last is None:
        return Evaluation(None, None, None)
    values: dict[str, Any] = {"cloture": last.close}
    entry_results = [(b.name, _run(b, view, entered_at, strategy)) for b in strategy.entree]
    exit_results = [(b.name, _run(b, view, entered_at, strategy)) for b in strategy.sortie]
    for name, result in entry_results + exit_results:
        for k, v in result.values.items():
            values[f"{name}.{k}"] = v
    entry = _combine(entry_results, all_=True)
    exit_ = _combine(exit_results, all_=False) if exit_results else False
    plan = _plan(strategy, view) if entry else None
    if entry and plan is None:
        entry = None
    fired = tuple(name for name, r in exit_results if r.ok)
    return Evaluation(last.open_time, entry, exit_, values, plan, fired)


# --- Exécution : niveaux et raisons d'ignorer ---


@dataclass(frozen=True)
class Levels:
    stop: Decimal
    target: Decimal
    stop_pct: Decimal
    target_pct: Decimal
    reasons: tuple[str, ...]  # vide : niveaux acceptés


def min_target_pct(spread_pct: Decimal) -> Decimal:
    """Distance minimale de l'objectif : frais aller-retour + spread + marge."""
    return 2 * FEE_RATE * HUNDRED + spread_pct + MIN_TARGET_MARGIN_PCT


def levels(plan: Plan, ask: Decimal, bid: Decimal) -> Levels:
    """Stop et objectif pour une entrée à `ask` ; raisons d'ignorer si hors bornes."""
    if plan.stop_level is not None:
        stop = plan.stop_level.quantize(TICK, rounding=ROUND_FLOOR)
    else:
        stop = (ask * (1 - plan.stop_pct / HUNDRED)).quantize(TICK, rounding=ROUND_FLOOR)  # type: ignore[operator]
    stop_pct = (ask - stop) / ask * HUNDRED
    target_pct = plan.target_pct if plan.target_pct is not None else plan.target_r * stop_pct  # type: ignore[operator]
    target = (ask * (1 + target_pct / HUNDRED)).quantize(TICK, rounding=ROUND_CEILING)
    reasons = []
    if not STOP_MIN_PCT <= stop_pct <= STOP_MAX_PCT:
        reasons.append(STOP_OUT_OF_BOUNDS)
    spread_pct = (ask - bid) / ((ask + bid) / 2) * HUNDRED
    if (target - ask) / ask * HUNDRED < min_target_pct(spread_pct):
        reasons.append(TARGET_TOO_CLOSE)
    return Levels(stop, target, _round(stop_pct), _round(target_pct), tuple(reasons))


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def in_hours(strategy: Strategy, now: datetime) -> bool:
    return strategy.debut <= now.time() < strategy.fin


def entry_blockers(
    strategy: Strategy, now: datetime, rules: SessionRules, has_position: bool, halted: bool
) -> tuple[str, ...]:
    """Raisons d'ignorer un signal d'entrée, hors qualité des données."""
    reasons = []
    if not in_hours(strategy, now):
        reasons.append(OUT_OF_HOURS)
    if not entry_allowed(now, rules):
        reasons.append(AFTER_LAST_ENTRY)
    if has_position:
        reasons.append(ALREADY_IN)
    if halted:
        reasons.append(HALTED)
    return tuple(reasons)


def quality_blockers(strategy: Strategy, qualities: Sequence[Quality]) -> tuple[str, ...]:
    """Raisons tirées de `qualite` (série de l'intervalle et série 1m) qui interdisent tout signal."""
    reasons: list[str] = []

    def add(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    for q in qualities:
        for code in q.raisons:
            if code in BLOCKING_QUALITY or code.startswith("historique_"):
                add(code)
        if q.ecart_horloge is None:
            add(CLOCK_UNKNOWN)
        if strategy.uses_vwap and not q.vwap_fiable:
            add(VWAP_UNRELIABLE)
    return tuple(reasons)
