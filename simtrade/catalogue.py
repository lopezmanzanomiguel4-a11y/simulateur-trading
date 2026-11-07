"""Construction du catalogue de stratégies backtestées (CLI : python -m simtrade catalogue construire).

Règles figées avant tout calcul (RAPPORT-ETAPE7, B3), rappelées au README :
1. échantillon : grille 3 × 3 par modèle et par paire ; sélection par voisinage ; bornes = plus grand
   rectangle de candidates contenant la combinaison choisie. Cette phase ne lit QUE les données avant la
   coupure (le chargeur est appelé avec une fin = coupure) ;
2. hors échantillon, une seule fois aux paramètres choisis : stratégie, sensibilité au spread, références
   (ne rien faire, achat 00:00 / vente clôture, 100 tirages au hasard) ; régimes fixés en échantillon ;
3. admission : P&L > 0, > achat-clôture, meilleur que ≥ 90 % des tirages, ≥ 30 trades.

Calcul en parallèle (multiprocessing, spawn) : chaque processus charge ses bougies depuis `marche.db`.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import statistics
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from simtrade import backtest, references, regimes, strategies
from simtrade.backtest import BacktestConfig, EvalCache, Market
from simtrade.market import Candle
from simtrade.models import PAIRS
from simtrade.stats import Stats

UTC = timezone.utc
VERSION = 1
INTERVAL = "5m"
MIN_TRADES = 30
REGIME_MIN_TRADES = 20
BEAT_SHARE = 0.90
SENSITIVITY_SPREAD = Decimal("0.05")
DRAW_CHUNK = 20  # tirages par tâche

ADMITTED = "admise"
REJECTED_IS = "rejetee_en_echantillon"
REJECTED_OOS = "rejetee_hors_echantillon"
INSUFFICIENT = "donnees_insuffisantes"

# Deux paramètres par modèle, trois valeurs chacun (défaut du modèle compris). Ordre : du plus coûteux au moins coûteux.
GRID: dict[str, tuple[tuple[str, tuple[Any, ...]], tuple[str, tuple[Any, ...]]]] = {
    "retour-vwap": (("ecart_vwap.seuil_pct", ("0.4", "0.6", "0.9")), ("rsi_extreme.seuil", ("25", "30", "35"))),
    "tendance-ema": (("croisement_ema.rapide", (6, 9, 12)), ("croisement_ema.lente", (21, 30, 42))),
    "cassure-range": (("cassure_range.minutes", (15, 30, 60)), ("objectif.multiple", ("1", "1.5", "2.5"))),
}


@dataclass(frozen=True)
class Periods:
    data_start: datetime  # début des données lues (fenêtres du live)
    is_start: datetime  # début de l'échantillon
    cut: datetime  # coupure : fin de l'échantillon, début du hors échantillon
    end: datetime  # fin du hors échantillon (exclue)

    def json(self) -> dict:
        return {"donnees": self.data_start.isoformat(), "echantillon": [self.is_start.isoformat(), self.cut.isoformat()],
                "hors_echantillon": [self.cut.isoformat(), self.end.isoformat()]}


DEFAULT_PERIODS = Periods(
    datetime(2026, 5, 7, tzinfo=UTC), datetime(2026, 5, 9, tzinfo=UTC),
    datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC),
)

Loader = Callable[[str, datetime, datetime], list[Candle]]


@dataclass(frozen=True)
class StoreLoader:
    """Lit les klines 1m de [start, end) dans `marche.db` (une connexion par appel : utilisable par les processus)."""

    path: str

    def __call__(self, pair: str, start: datetime, end: datetime) -> list[Candle]:
        from simtrade.marche import MarketStore

        store = MarketStore(self.path)
        try:
            return store.candles(pair, start, end)
        finally:
            store.close()


# --- Grille ---


def strategy_for(model: str, pair: str, overrides: dict[str, Any]) -> strategies.Strategy:
    raw = strategies.with_overrides(strategies.model(model), {"paire": pair, "intervalle": INTERVAL, **overrides})
    return strategies.validate(raw)


def combos(model: str, grid=GRID) -> list[tuple[tuple[int, int], dict[str, Any]]]:
    (k1, v1), (k2, v2) = grid[model]
    return [((i, j), {k1: a, k2: b}) for i, a in enumerate(v1) for j, b in enumerate(v2)]


def default_cell(model: str, grid=GRID) -> tuple[int, int]:
    """Position des valeurs par défaut du modèle dans la grille."""
    raw = strategies.model(model)
    defaults = strategies.validate({**raw, "paire": PAIRS[0]})
    out = []
    for key, values in grid[model]:
        current = _current(defaults, key)
        out.append(next((k for k, v in enumerate(values) if Decimal(str(v)) == Decimal(str(current))), 1))
    return out[0], out[1]


def _current(strategy: strategies.Strategy, key: str) -> Any:
    group, param = key.split(".")
    if group == "stop":
        return strategy.stop.params[param]
    if group == "objectif":
        return strategy.objectif.params[param]
    return strategy.brick(group).params[param]  # type: ignore[union-attr]


def param_spec(model_raw: dict, key: str) -> strategies.Param:
    group, param = key.split(".")
    if group in ("stop", "objectif"):
        table = strategies.STOPS if group == "stop" else strategies.TARGETS
        return table[model_raw[group]["type"]][param]
    if group == "rsi_extreme" and param == "seuil":
        brick = next(b for b in model_raw["entree"] if b["brique"] == "rsi_extreme")
        return strategies.RSI_THRESHOLD[brick.get("zone", "bas")]
    return strategies.BRICKS[group][param]


# --- Sélection (pure) ---


@dataclass(frozen=True)
class Cell:
    pnl: Decimal
    trades: int

    @property
    def candidate(self) -> bool:
        return self.trades >= MIN_TRADES and self.pnl > 0


def neighbours(cell: tuple[int, int], shape: tuple[int, int]) -> list[tuple[int, int]]:
    i, j = cell
    out = [(i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)]
    return [(a, b) for a, b in out if 0 <= a < shape[0] and 0 <= b < shape[1]]


def scores(cells: dict[tuple[int, int], Cell], shape: tuple[int, int]) -> dict[tuple[int, int], Decimal]:
    """Moyenne du P&L de la combinaison et de ses voisines directes."""
    out = {}
    for cell, value in cells.items():
        group = [value.pnl] + [cells[n].pnl for n in neighbours(cell, shape)]
        out[cell] = sum(group, Decimal(0)) / len(group)
    return out


def select(cells: dict[tuple[int, int], Cell], shape: tuple[int, int], default: tuple[int, int]) -> tuple[int, int] | None:
    """Candidate de meilleur score ; égalité : la plus proche des défauts (crans), puis l'ordre de la grille."""
    score = scores(cells, shape)
    candidates = [c for c, v in cells.items() if v.candidate]
    if not candidates:
        return None
    distance = lambda c: abs(c[0] - default[0]) + abs(c[1] - default[1])  # noqa: E731
    return sorted(candidates, key=lambda c: (-score[c], distance(c), c))[0]


def rectangle(cells: dict[tuple[int, int], Cell], choice: tuple[int, int], shape: tuple[int, int]
              ) -> tuple[tuple[int, int], tuple[int, int]]:
    """Plus grand rectangle (en nombre de combinaisons) contenant `choice` dont toutes les combinaisons sont
    candidates : ((i0, i1), (j0, j1)), bornes incluses. Égalité : le premier (rangées puis colonnes de départ)."""
    best = ((choice[0], choice[0]), (choice[1], choice[1]))
    best_size = 1
    for i0 in range(choice[0] + 1):
        for i1 in range(choice[0], shape[0]):
            for j0 in range(choice[1] + 1):
                for j1 in range(choice[1], shape[1]):
                    size = (i1 - i0 + 1) * (j1 - j0 + 1)
                    if size <= best_size:
                        continue
                    if all(cells[(a, b)].candidate for a in range(i0, i1 + 1) for b in range(j0, j1 + 1)):
                        best, best_size = ((i0, i1), (j0, j1)), size
    return best


def bounds(model: str, rect: tuple[tuple[int, int], tuple[int, int]], grid=GRID) -> dict[str, dict[str, Any]]:
    out = {}
    for (key, values), (lo, hi) in zip(grid[model], rect):
        chosen = sorted(values[lo : hi + 1], key=lambda v: Decimal(str(v)))
        out[key] = {"min": chosen[0], "max": chosen[-1]}
    return out


def check_bounds(model: str, found: dict[str, dict[str, Any]]) -> None:
    """Bornes ⊂ schéma (lève ValueError sinon)."""
    raw = strategies.model(model)
    for key, b in found.items():
        spec = param_spec(raw, key)
        lo, hi = Decimal(str(spec.min)), Decimal(str(spec.max))
        if not lo <= Decimal(str(b["min"])) <= Decimal(str(b["max"])) <= hi:
            raise ValueError(f"Bornes de '{key}' hors du schéma : {b} (schéma {spec.min} à {spec.max}).")


# --- Régimes et admission (pur) ---


def by_regime(trades: Iterable[tuple[datetime, Decimal, str | None]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _, pnl, regime in trades:
        key = regime or "inconnu"
        r = out.setdefault(key, {"trades": 0, "pnl": Decimal(0)})
        r["trades"] += 1
        r["pnl"] += pnl
    for r in out.values():
        r["pnl_par_trade"] = r["pnl"] / r["trades"]
    return out


def retained_regimes(in_sample: dict[str, dict[str, Any]]) -> list[str]:
    return [r for r in regimes.REGIMES if r in in_sample
            and in_sample[r]["trades"] >= REGIME_MIN_TRADES and in_sample[r]["pnl_par_trade"] > 0]


def admission(oos: Stats, open_close_pnl: Decimal, share: float) -> tuple[str, list[str]]:
    if oos.trades < MIN_TRADES:
        return INSUFFICIENT, [f"{oos.trades} trades hors échantillon (minimum {MIN_TRADES})"]
    reasons = []
    if oos.pnl <= 0:
        reasons.append("P&L hors échantillon ≤ 0")
    if oos.pnl <= open_close_pnl:
        reasons.append("pas mieux que l'achat 00:00 / vente clôture")
    if share < BEAT_SHARE:
        reasons.append(f"meilleure que {share:.0%} des tirages au hasard seulement (minimum {BEAT_SHARE:.0%})")
    return (REJECTED_OOS, reasons) if reasons else (ADMITTED, [])


# --- Tâches (exécutées dans les processus) ---

_MARKET: dict[tuple, Market] = {}


def _market(loader: Loader, pair: str, start: datetime, end: datetime) -> Market:
    key = (loader, pair, start, end)
    if key not in _MARKET:
        _MARKET.clear()  # une seule paire en mémoire par processus
        _MARKET[key] = Market(loader(pair, start, end), INTERVAL)
    return _MARKET[key]


def _compact(result: backtest.BacktestResult) -> list[tuple[datetime, Decimal]]:
    return [(t.opened_at, t.pnl) for t in result.trades]


def in_sample_task(task: tuple) -> dict:
    loader, model, pair, cell, overrides, periods, config = task
    started = time.perf_counter()
    market = _market(loader, pair, periods.data_start, periods.cut)  # rien après la coupure
    result = backtest.run(strategy_for(model, pair, overrides), market, periods.is_start, periods.cut, config)
    return {"model": model, "pair": pair, "cell": cell, "overrides": overrides, "stats": result.stats,
            "trades": _compact(result), "seconds": time.perf_counter() - started}


def out_of_sample_task(task: tuple) -> dict:
    loader, model, pair, overrides, periods, config, is_trades = task
    started = time.perf_counter()
    market = _market(loader, pair, periods.data_start, periods.end)
    strategy = strategy_for(model, pair, overrides)
    cache = EvalCache()
    result = backtest.run(strategy, market, periods.cut, periods.end, config, cache=cache)
    sensitivity = backtest.run(strategy, market, periods.cut, periods.end,
                               BacktestConfig(**{**config.__dict__, "spread_pct": SENSITIVITY_SPREAD}))
    open_close = references.open_close(market, periods.cut, periods.end, config, strategy.taille_pct)
    nothing = references.do_nothing(config, periods.cut, periods.end)
    in_sample_bars = [b for b in market.bars if b.close_time <= periods.cut]
    thresholds = regimes.fit_thresholds(in_sample_bars)
    timeline = regimes.Timeline(market.bars, thresholds)
    return {
        "model": model, "pair": pair, "stats": result.stats, "sensitivity": sensitivity.stats,
        "open_close": open_close.stats, "nothing": nothing, "p": references.entry_rate(result),
        "entries": result.entries, "eligible": result.eligible, "ignored": dict(result.ignored),
        "thresholds": thresholds,
        "is_regimes": by_regime((at, pnl, timeline.at(at)) for at, pnl in is_trades),
        "oos_regimes": by_regime((t.opened_at, t.pnl, timeline.at(t.opened_at)) for t in result.trades),
        "cache": cache, "seconds": time.perf_counter() - started,
    }


def draws_task(task: tuple) -> dict:
    loader, model, pair, overrides, periods, config, p, seeds, cache = task
    started = time.perf_counter()
    market = _market(loader, pair, periods.data_start, periods.end)
    strategy = strategy_for(model, pair, overrides)
    stats = references.random_draws(strategy, market, periods.cut, periods.end, config, p, seeds, cache)
    return {"model": model, "pair": pair, "seeds": list(seeds), "pnl": [s.pnl for s in stats],
            "seconds": time.perf_counter() - started}


# --- Orchestration ---


@dataclass
class Couple:
    model: str
    pair: str
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    is_trades: dict[tuple[int, int], list] = field(default_factory=dict)
    is_stats: dict[tuple[int, int], Stats] = field(default_factory=dict)
    choice: tuple[int, int] | None = None
    rect: tuple[tuple[int, int], tuple[int, int]] | None = None
    oos: dict | None = None
    draws: list[Decimal] = field(default_factory=list)
    status: str = REJECTED_IS
    reasons: list[str] = field(default_factory=list)

    @property
    def overrides(self) -> dict[str, Any] | None:
        if self.choice is None:
            return None
        return dict(dict(combos(self.model))[self.choice])

    @property
    def percentile(self) -> float | None:
        if self.oos is None or not self.draws:
            return None
        beaten = sum(1 for d in self.draws if d < self.oos["stats"].pnl)
        return 100 * beaten / len(self.draws)

    @property
    def report_name(self) -> str:
        return f"{self.model}-{self.pair.lower()}.md"


def _mapper(processes: int | None):
    """map ordonné ou non : en série (tests) sans processus, sinon un pool spawn."""
    if not processes or processes <= 1:
        return None
    return multiprocessing.get_context("spawn").Pool(processes)


def in_sample_phase(loader: Loader, periods: Periods, models: Sequence[str], pairs: Sequence[str],
                    config: BacktestConfig, pool=None, progress=print) -> dict[tuple[str, str], Couple]:
    """Grille en échantillon et sélection. Le chargeur n'est appelé qu'avec une fin égale à la coupure."""
    couples = {(m, p): Couple(m, p) for m in models for p in pairs}
    tasks = [(loader, m, p, cell, ov, periods, config) for m in models for p in pairs for cell, ov in combos(m)]
    results = pool.imap_unordered(in_sample_task, tasks) if pool else map(in_sample_task, tasks)
    for k, r in enumerate(results, 1):
        c = couples[(r["model"], r["pair"])]
        c.cells[r["cell"]] = Cell(r["stats"].pnl, r["stats"].trades)
        c.is_trades[r["cell"]] = r["trades"]
        c.is_stats[r["cell"]] = r["stats"]
        progress(f"échantillon {k}/{len(tasks)} : {r['model']} {r['pair']} {r['overrides']} → "
                 f"P&L {r['stats'].pnl:.2f}, {r['stats'].trades} trades ({r['seconds']:.0f} s)")
    for c in couples.values():
        c.choice = select(c.cells, (3, 3), default_cell(c.model))
        if c.choice is None:
            c.status, c.reasons = REJECTED_IS, ["aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon"]
        else:
            c.rect = rectangle(c.cells, c.choice, (3, 3))
    return couples


def out_of_sample_phase(couples: dict[tuple[str, str], Couple], loader: Loader, periods: Periods,
                        config: BacktestConfig, seeds: Sequence[int], pool=None, progress=print) -> None:
    chosen = [c for c in couples.values() if c.choice is not None]
    tasks = [(loader, c.model, c.pair, c.overrides, periods, config, c.is_trades[c.choice]) for c in chosen]
    results = pool.imap_unordered(out_of_sample_task, tasks) if pool else map(out_of_sample_task, tasks)
    for r in results:
        couples[(r["model"], r["pair"])].oos = r
        progress(f"hors échantillon : {r['model']} {r['pair']} → P&L {r['stats'].pnl:.2f}, "
                 f"{r['stats'].trades} trades, p = {r['p']:.4f} ({r['seconds']:.0f} s)")
    draw_tasks = []
    for c in chosen:
        o = c.oos
        for k in range(0, len(seeds), DRAW_CHUNK):
            draw_tasks.append((loader, c.model, c.pair, c.overrides, periods, config, o["p"],
                               tuple(seeds[k : k + DRAW_CHUNK]), o["cache"]))
    results = pool.imap_unordered(draws_task, draw_tasks) if pool else map(draws_task, draw_tasks)
    by_seed: dict[tuple[str, str], dict[int, Decimal]] = {}
    for k, r in enumerate(results, 1):
        by_seed.setdefault((r["model"], r["pair"]), {}).update(zip(r["seeds"], r["pnl"]))
        progress(f"tirages {k}/{len(draw_tasks)} : {r['model']} {r['pair']} ({r['seconds']:.0f} s)")
    for c in chosen:
        c.draws = [by_seed[(c.model, c.pair)][s] for s in seeds]
        c.oos.pop("cache", None)
        share = sum(1 for d in c.draws if d < c.oos["stats"].pnl) / len(c.draws)
        c.status, c.reasons = admission(c.oos["stats"], c.oos["open_close"].pnl, share)


@dataclass
class Build:
    periods: Periods
    config: BacktestConfig
    couples: list[Couple]
    seeds: tuple[int, ...]
    commit: str | None
    created_at: datetime
    seconds: dict[str, float]

    @property
    def admitted(self) -> list[Couple]:
        return [c for c in self.couples if c.status == ADMITTED]


def build(loader: Loader, *, periods: Periods = DEFAULT_PERIODS, models: Sequence[str] = tuple(GRID),
          pairs: Sequence[str] = PAIRS, config: BacktestConfig = BacktestConfig(), seeds: Sequence[int] = references.SEEDS,
          processes: int | None = None, commit: str | None = None, progress: Callable[[str], None] = print) -> Build:
    seconds: dict[str, float] = {}
    pool = _mapper(processes)
    try:
        t = time.perf_counter()
        couples = in_sample_phase(loader, periods, models, pairs, config, pool, progress)
        seconds["echantillon"] = time.perf_counter() - t
        t = time.perf_counter()
        out_of_sample_phase(couples, loader, periods, config, tuple(seeds), pool, progress)
        seconds["hors_echantillon"] = time.perf_counter() - t
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    return Build(periods, config, list(couples.values()), tuple(seeds), commit, datetime.now(UTC), seconds)


def default_processes() -> int:
    return max(1, min(11, (os.cpu_count() or 2) - 1))


# --- Sorties : catalogue.json et rapports ---


def _d(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else format(value.quantize(Decimal(places)), "f")


def stats_json(s: Stats) -> dict:
    return {
        "pnl": _d(s.pnl), "pnl_pct": _d(s.pnl_pct, "0.0001"), "drawdown_max_pct": _d(s.max_drawdown_pct, "0.0001"),
        "taux_reussite_pct": _d(s.win_rate), "gain_moyen": _d(s.avg_pnl, "0.0001"), "trades": s.trades,
        "gagnants": s.wins, "frais": _d(s.fees), "sharpe": None if s.sharpe is None else round(s.sharpe, 3),
        "sessions": s.sessions,
    }


def _regimes_json(found: dict[str, dict[str, Any]]) -> dict:
    return {k: {"trades": v["trades"], "pnl": _d(v["pnl"]), "pnl_par_trade": _d(v["pnl_par_trade"], "0.0001")}
            for k, v in sorted(found.items())}


def criteria_json() -> dict:
    return {
        "candidate_echantillon": {"trades_min": MIN_TRADES, "pnl": "> 0"},
        "selection": "voisinage : moyenne du P&L de la combinaison et de ses voisines directes",
        "bornes": "plus grand rectangle de candidates contenant la combinaison choisie",
        "admission": {"pnl": "> 0", "achat_0h_cloture": "P&L supérieur", "tirages_battus_min_pct": BEAT_SHARE * 100,
                      "trades_min": MIN_TRADES},
        "regime_retenu": {"trades_min": REGIME_MIN_TRADES, "pnl_par_trade": "> 0 en échantillon"},
        "sensibilite_spread_pct": format(SENSITIVITY_SPREAD, "f"),
    }


def entry_json(c: Couple, reports_prefix: str = "backtests/") -> dict:
    strategy = strategy_for(c.model, c.pair, c.overrides or {})
    found = bounds(c.model, c.rect)  # type: ignore[arg-type]
    check_bounds(c.model, found)
    o = c.oos or {}
    retained = retained_regimes(o["is_regimes"])
    return {
        "modele": c.model,
        "paire": c.pair,
        "intervalle": INTERVAL,
        "parametres": c.overrides,
        "bornes": found,
        "definition": strategy.to_json(),
        "regimes_retenus": retained,
        "seuils_regime": o["thresholds"].json(),
        "echantillon": stats_json(c.is_stats[c.choice]),  # type: ignore[index]
        "hors_echantillon": stats_json(o["stats"]),
        "hors_echantillon_spread_0_05": stats_json(o["sensitivity"]),
        "references": {
            "ne_rien_faire_pnl": _d(o["nothing"].pnl),
            "achat_0h_cloture_pnl": _d(o["open_close"].pnl),
            "tirages": {"n": len(c.draws), "p_entree": round(o["p"], 6), "percentile": c.percentile,
                        "mediane_pnl": _d(statistics.median(c.draws))},
        },
        "regimes_hors_echantillon": _regimes_json(o["oos_regimes"]),
        "fragile_spread": o["sensitivity"].pnl <= 0,
        "rapport": reports_prefix + c.report_name,
    }


def catalogue_json(b: Build) -> dict:
    results = []
    for c in b.couples:
        results.append({
            "modele": c.model, "paire": c.pair, "statut": c.status, "raisons": c.reasons,
            "parametres": c.overrides,
            "pnl_hors_echantillon": _d(c.oos["stats"].pnl) if c.oos else None,
            "trades_hors_echantillon": c.oos["stats"].trades if c.oos else None,
            "percentile_tirages": c.percentile,
            "rapport": "backtests/" + c.report_name,
        })
    return {
        "version": VERSION,
        "cree_le": b.created_at.isoformat(),
        "commit": b.commit,
        "periodes": b.periods.json(),
        "intervalle": INTERVAL,
        "spread_pct": format(b.config.spread_pct, "f"),
        "frais_pct": format(b.config.fee_rate * 100, "f"),
        "capital": format(b.config.capital, "f"),
        "grille": {m: {k: list(v) for k, v in g} for m, g in GRID.items()},
        "criteres": criteria_json(),
        "tirages": {"n": len(b.seeds), "graines": [b.seeds[0], b.seeds[-1]] if b.seeds else []},
        "entrees": [entry_json(c) for c in b.admitted],
        "resultats": results,
    }


def validate_catalogue(data: dict) -> None:
    """Chaque entrée passe strategies.validate et ses bornes sont dans le schéma ; lève sinon."""
    if data.get("version") != VERSION or not isinstance(data.get("entrees"), list):
        raise ValueError("Catalogue illisible (version ou entrées).")
    for e in data["entrees"]:
        strategies.validate(e["definition"])
        check_bounds(e["modele"], e["bornes"])
        for key, b in e["bornes"].items():
            v = Decimal(str(e["parametres"][key]))
            if not Decimal(str(b["min"])) <= v <= Decimal(str(b["max"])):
                raise ValueError(f"Paramètre '{key}' hors de ses bornes dans l'entrée {e['modele']} {e['paire']}.")


# --- Rapports Markdown ---


def _money(value: Decimal | None) -> str:
    return "–" if value is None else f"{value:+.2f}"


def report_md(c: Couple, b: Build) -> str:
    (k1, v1), (k2, v2) = GRID[c.model]
    lines = [f"# {c.model} sur {c.pair} ({INTERVAL})", "",
             f"Statut : **{c.status}**" + (f" ({' ; '.join(c.reasons)})" if c.reasons else ""), "",
             f"Échantillon : {b.periods.is_start:%d/%m/%Y} → {b.periods.cut:%d/%m/%Y} ; hors échantillon : "
             f"{b.periods.cut:%d/%m/%Y} → {b.periods.end:%d/%m/%Y} (fin exclue). Spread {b.config.spread_pct} %, "
             f"frais {b.config.fee_rate * 100} % par ordre, capital {b.config.capital} USDT.", "",
             "## Grille en échantillon", "",
             f"P&L en USDT (trades) ; lignes : `{k1}`, colonnes : `{k2}`. * : candidate (≥ {MIN_TRADES} trades et P&L > 0) ; "
             "[ ] : combinaison choisie.", "",
             "| | " + " | ".join(str(v) for v in v2) + " |", "|---|" + "---|" * len(v2)]
    score = scores(c.cells, (3, 3)) if len(c.cells) == 9 else {}
    for i, a in enumerate(v1):
        row = []
        for j in range(len(v2)):
            cell = c.cells.get((i, j))
            text = "–" if cell is None else f"{_money(cell.pnl)} ({cell.trades}){'*' if cell.candidate else ''}"
            row.append(f"[{text}]" if c.choice == (i, j) else text)
        lines.append(f"| **{a}** | " + " | ".join(row) + " |")
    if score:
        lines += ["", "Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :", "",
                  "| | " + " | ".join(str(v) for v in v2) + " |", "|---|" + "---|" * len(v2)]
        for i, a in enumerate(v1):
            lines.append(f"| **{a}** | " + " | ".join(_money(score[(i, j)]) for j in range(len(v2))) + " |")
    if c.choice is None:
        lines += ["", "Aucune candidate : hors échantillon non lancé."]
        return "\n".join(lines) + "\n"
    found = bounds(c.model, c.rect)  # type: ignore[arg-type]
    lines += ["", f"Paramètres choisis : {', '.join(f'`{k}` = {v}' for k, v in c.overrides.items())}.",  # type: ignore[union-attr]
              "Bornes (plus grand rectangle de candidates) : "
              + ", ".join(f"`{k}` de {v['min']} à {v['max']}" for k, v in found.items()) + "."]
    o = c.oos
    if o is None:
        return "\n".join(lines) + "\n"
    s, sens = o["stats"], o["sensitivity"]
    lines += ["", "## Hors échantillon (lancé une fois)", "",
              "| | P&L | P&L % | Trades | Réussite | Drawdown max | Sharpe | Frais |", "|---|---|---|---|---|---|---|---|"]
    for label, st in ((f"Stratégie (s = {b.config.spread_pct} %)", s), (f"Stratégie (s = {SENSITIVITY_SPREAD} %)", sens),
                      ("Achat 00:00 / vente clôture", o["open_close"]), ("Ne rien faire", o["nothing"])):
        lines.append(f"| {label} | {_money(st.pnl)} | {st.pnl_pct:+.3f} | {st.trades} | "
                     f"{'–' if st.win_rate is None else f'{st.win_rate:.1f} %'} | {st.max_drawdown_pct:.3f} % | "
                     f"{'–' if st.sharpe is None else f'{st.sharpe:.2f}'} | {st.fees:.2f} |")
    beaten = sum(1 for d in c.draws if d < s.pnl)
    lines += ["", f"Tirages au hasard : {len(c.draws)} (graines {b.seeds[0]} à {b.seeds[-1]}), probabilité d'entrée "
              f"p = {o['entries']} / {o['eligible']} = {o['p']:.5f} par clôture éligible. Stratégie meilleure que "
              f"{beaten} tirages sur {len(c.draws)} : **percentile {c.percentile:.0f}** (critère : ≥ {BEAT_SHARE:.0%}). "
              f"P&L des tirages : min {_money(min(c.draws))}, médiane {_money(statistics.median(c.draws))}, "
              f"max {_money(max(c.draws))}."]
    if s.trades >= MIN_TRADES and c.status == ADMITTED and sens.pnl <= 0:
        lines.append(f"\n**Fragile au spread** : P&L ≤ 0 à s = {SENSITIVITY_SPREAD} % (`fragile_spread`).")
    th = o["thresholds"]
    lines += ["", "## Régimes", "",
              f"Seuils fixés en échantillon : ATR 14 ≥ {th.atr_pct:.4f} % → forte volatilité ; sinon efficacité (24 bougies) "
              f"≥ {th.efficiency:.4f} → tendance ; sinon range.", "",
              "| Régime | Trades (éch.) | P&L / trade (éch.) | Trades (hors éch.) | P&L / trade (hors éch.) |",
              "|---|---|---|---|---|"]
    for r in regimes.REGIMES + ("inconnu",):
        a, z = o["is_regimes"].get(r), o["oos_regimes"].get(r)
        if a is None and z is None:
            continue
        lines.append(f"| {r} | {a['trades'] if a else 0} | {_money(a['pnl_par_trade']) if a else '–'} | "
                     f"{z['trades'] if z else 0} | {_money(z['pnl_par_trade']) if z else '–'} |")
    retained = retained_regimes(o["is_regimes"])
    lines += ["", f"Régimes retenus (≥ {REGIME_MIN_TRADES} trades et P&L par trade > 0 en échantillon) : "
              + (", ".join(retained) if retained else "aucun") + "."]
    if o["ignored"]:
        lines += ["", "Signaux ignorés ou refusés hors échantillon : "
                  + ", ".join(f"{k} {v}" for k, v in sorted(o["ignored"].items())) + "."]
    return "\n".join(lines) + "\n"


def summary_md(b: Build) -> str:
    tested = [c for c in b.couples if c.oos is not None]
    lines = ["# Catalogue : synthèse", "",
             f"Construit le {b.created_at:%d/%m/%Y %H:%M} UTC (commit {b.commit or 'inconnu'}) en "
             + ", ".join(f"{k} {v / 60:.1f} min" for k, v in b.seconds.items()) + ".", "",
             f"Échantillon {b.periods.is_start:%d/%m/%Y} → {b.periods.cut:%d/%m/%Y}, hors échantillon "
             f"{b.periods.cut:%d/%m/%Y} → {b.periods.end:%d/%m/%Y}, intervalle {INTERVAL}, spread {b.config.spread_pct} %, "
             f"frais {b.config.fee_rate * 100} %. Critères figés avant le calcul (README, « Backtest et catalogue »).", "",
             "| Modèle | Paire | Statut | P&L hors éch. | Trades | Percentile tirages | Achat-clôture |",
             "|---|---|---|---|---|---|---|"]
    for c in b.couples:
        o = c.oos
        lines.append(f"| {c.model} | {c.pair} | {c.status} | {_money(o['stats'].pnl) if o else '–'} | "
                     f"{o['stats'].trades if o else '–'} | {'–' if c.percentile is None else f'{c.percentile:.0f}'} | "
                     f"{_money(o['open_close'].pnl) if o else '–'} |")
    lines += ["", f"Stratégies admises : {len(b.admitted)}." + (" Le catalogue est vide : c'est un résultat valide."
                                                               if not b.admitted else ""), "",
              f"**Prudence statistique** : {len(tested)} couple(s) testé(s) hors échantillon contre les tirages au hasard, "
              f"au seuil de {100 - BEAT_SHARE * 100:.0f} %. Même sans aucun avantage réel, environ "
              f"{len(tested) * (1 - BEAT_SHARE):.1f} couple(s) passeraient ce critère par hasard "
              f"({len(GRID) * len(PAIRS)} tests au seuil de 10 % : environ un faux positif). Le critère reste à 90 % ; "
              "le percentile exact de chaque couple est donné ci-dessus et dans son rapport."]
    return "\n".join(lines) + "\n"


def write(b: Build, catalogue_path: Path, reports_dir: Path) -> dict:
    data = catalogue_json(b)
    validate_catalogue(data)
    reports_dir.mkdir(parents=True, exist_ok=True)
    for c in b.couples:
        (reports_dir / c.report_name).write_text(report_md(c, b), encoding="utf-8", newline="\n")
    (reports_dir / "synthese.md").write_text(summary_md(b), encoding="utf-8", newline="\n")
    catalogue_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return data
