"""Catalogue : sélection par voisinage, rectangle de bornes, admission, échantillon sans lecture du futur,
entrées valides, catalogue vide valide, calcul en processus (spawn). Données synthétiques, sans réseau."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from test_backtest import DAY, ones
from test_references import walk_prices
from simtrade import catalogue, regimes, strategies
from simtrade.backtest import BacktestConfig
from simtrade.catalogue import Cell, Couple, Periods
from simtrade.marche import MarketStore
from simtrade.stats import Stats

D = Decimal
SHAPE = (3, 3)
LOOSE = BacktestConfig(full_window=False)
GOOD, BAD = Cell(D(100), 40), Cell(D(-5), 40)


def grid(rows):
    """rows : 3 chaînes de 3 caractères, '+' candidate, '-' non candidate, 'f' peu de trades."""
    out = {}
    for i, row in enumerate(rows):
        for j, ch in enumerate(row):
            out[(i, j)] = {"+": GOOD, "-": BAD, "f": Cell(D(500), 10)}[ch]
    return out


# --- Sélection ---


def test_score_de_voisinage():
    cells = {(i, j): Cell(D(10 * i + j), 40) for i in range(3) for j in range(3)}
    s = catalogue.scores(cells, SHAPE)
    assert s[(1, 1)] == D(11 + 1 + 21 + 10 + 12) / 5  # centre : 4 voisines
    assert s[(0, 0)] == D(0 + 10 + 1) / 3  # coin : 2 voisines
    assert s[(0, 1)] == D(1 + 0 + 2 + 11) / 4  # bord : 3 voisines


def test_selection_prefere_un_voisinage_solide_a_un_pic_isole():
    cells = grid(["---", "---", "---"])
    cells[(0, 0)] = Cell(D(300), 40)  # pic isolé : score (300 − 5 − 5) / 3 ≈ 97
    for c in [(2, 0), (2, 1), (2, 2), (1, 2)]:
        cells[c] = Cell(D(120), 40)  # plateau : score de (2, 1) = (120 · 3 − 5) / 4 ≈ 89 ; de (2, 2) = 120
    assert catalogue.select(cells, SHAPE, (1, 1)) == (2, 2)


def test_selection_ignore_les_non_candidates():
    cells = grid(["f--", "---", "--+"])  # (0, 0) : gros P&L mais 10 trades
    assert catalogue.select(cells, SHAPE, (1, 1)) == (2, 2)
    assert catalogue.select(grid(["f--", "---", "---"]), SHAPE, (1, 1)) is None


def test_egalite_plus_proche_des_defauts():
    cells = grid(["+++", "+++", "+++"])  # scores tous égaux
    assert catalogue.select(cells, SHAPE, (1, 0)) == (1, 0)
    cells = grid(["---", "+-+", "---"])  # (1, 0) et (1, 2) : même score, à 1 cran du défaut (1, 1)
    assert catalogue.select(cells, SHAPE, (1, 1)) == (1, 0)  # puis l'ordre de la grille


# --- Bornes : plus grand rectangle ---


def test_rectangle_zone_en_l_n_inclut_pas_le_coin_negatif():
    cells = grid(["++-", "++-", "+++"])  # zone en L : le min/max inclurait le coin (0, 2) négatif
    rect = catalogue.rectangle(cells, (2, 0), SHAPE)
    assert rect in (((0, 2), (0, 1)), ((2, 2), (0, 2)))  # 6 et 3 combinaisons
    assert rect == ((0, 2), (0, 1))  # le plus grand : 3 × 2
    assert all(cells[(a, b)].candidate for a in range(rect[0][0], rect[0][1] + 1) for b in range(rect[1][0], rect[1][1] + 1))
    found = catalogue.bounds("retour-vwap", rect)
    assert found == {"ecart_vwap.seuil_pct": {"min": "0.4", "max": "0.9"}, "rsi_extreme.seuil": {"min": "25", "max": "30"}}


def test_rectangle_reduit_a_la_combinaison():
    cells = grid(["-+-", "+-+", "-+-"])
    assert catalogue.rectangle(cells, (0, 1), SHAPE) == ((0, 0), (1, 1))


def test_bornes_dans_le_schema():
    for model in catalogue.GRID:
        catalogue.check_bounds(model, catalogue.bounds(model, ((0, 2), (0, 2))))
    with pytest.raises(ValueError, match="hors du schéma"):
        catalogue.check_bounds("retour-vwap", {"rsi_extreme.seuil": {"min": "25", "max": "60"}})


def test_toutes_les_combinaisons_valides_et_defauts_dans_la_grille():
    for model in catalogue.GRID:
        for _, ov in catalogue.combos(model):
            for pair in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                catalogue.strategy_for(model, pair, ov)
    assert [catalogue.default_cell(m) for m in catalogue.GRID] == [(1, 1), (1, 0), (1, 1)]


# --- Admission et régimes ---


def stats(pnl, trades):
    return Stats(D(pnl), D(pnl) / 100, D(0), None, None, trades, D(0), None, 0, 0)


def test_admission():
    assert catalogue.admission(stats(50, 40), D(10), 0.95) == (catalogue.ADMITTED, [])
    status, reasons = catalogue.admission(stats(50, 40), D(80), 0.89)
    assert status == catalogue.REJECTED_OOS and len(reasons) == 2
    assert catalogue.admission(stats(-1, 40), D(-5), 0.95)[0] == catalogue.REJECTED_OOS
    assert catalogue.admission(stats(500, 29), D(0), 1.0)[0] == catalogue.INSUFFICIENT
    assert catalogue.admission(stats(50, 40), D(10), 0.90)[0] == catalogue.ADMITTED  # 90 % : admis


def test_regimes_retenus():
    t = DAY
    trades = [(t, D(2), regimes.RANGE)] * 20 + [(t, D(-1), regimes.TREND)] * 30 + [(t, D(5), regimes.HIGH_VOL)] * 19
    found = catalogue.by_regime(trades)
    assert found[regimes.RANGE]["trades"] == 20 and found[regimes.RANGE]["pnl_par_trade"] == 2
    assert catalogue.retained_regimes(found) == [regimes.RANGE]  # tendance : P&L < 0 ; volatilité : 19 trades


# --- Données synthétiques ---

PERIODS = Periods(DAY, DAY + timedelta(hours=2), DAY + timedelta(hours=10), DAY + timedelta(hours=16))


class Guarded:
    """Chargeur qui refuse toute lecture au-delà de `limit` (et note les lectures)."""

    def __init__(self, data, limit=None):
        self.data, self.limit, self.calls = data, limit, []

    def __call__(self, pair, start, end):
        self.calls.append((start, end))
        if self.limit is not None and end > self.limit:
            raise AssertionError(f"lecture après la coupure : {end}")
        return [c for c in self.data if start <= c.open_time < end]


def walk_data():
    return ones(DAY, walk_prices(1440, seed=3))


def test_echantillon_ne_lit_jamais_le_hors_echantillon():
    loader = Guarded(walk_data(), limit=PERIODS.cut)
    couples = catalogue.in_sample_phase(loader, PERIODS, ["retour-vwap"], ["BTCUSDT"], LOOSE, progress=lambda m: None)
    c = couples[("retour-vwap", "BTCUSDT")]
    assert len(c.cells) == 9 and loader.calls and all(end <= PERIODS.cut for _, end in loader.calls)
    assert c.choice is None and c.status == catalogue.REJECTED_IS  # moins de 30 trades en 8 h


def selected_couple():
    c = Couple("retour-vwap", "BTCUSDT", cells={k: GOOD for k in [(i, j) for i in range(3) for j in range(3)]})
    c.is_stats = {k: stats(100, 40) for k in c.cells}
    c.is_trades = {k: [] for k in c.cells}
    c.choice = (1, 1)
    c.rect = catalogue.rectangle(c.cells, c.choice, SHAPE)
    return c


def test_hors_echantillon_entree_valide_et_percentile(tmp_path):
    c = selected_couple()
    couples = {("retour-vwap", "BTCUSDT"): c}
    catalogue.out_of_sample_phase(couples, Guarded(walk_data()), PERIODS, LOOSE, (1, 2, 3, 4), progress=lambda m: None)
    assert c.oos is not None and len(c.draws) == 4 and c.percentile in (0, 25, 50, 75, 100)
    assert c.status in (catalogue.ADMITTED, catalogue.REJECTED_OOS, catalogue.INSUFFICIENT)
    # Entrée de catalogue construite même si le couple n'est pas admis : valide et dans le schéma
    entry = catalogue.entry_json(c)
    strategies.validate(entry["definition"])
    catalogue.check_bounds("retour-vwap", entry["bornes"])
    assert entry["references"]["tirages"]["percentile"] == c.percentile and entry["references"]["tirages"]["n"] == 4
    assert entry["fragile_spread"] == (c.oos["sensitivity"].pnl <= 0)
    b = catalogue.Build(PERIODS, LOOSE, [c], (1, 2, 3, 4), "abc1234", datetime.now(catalogue.UTC), {"x": 1.0})
    data = catalogue.write(b, tmp_path / "catalogue.json", tmp_path / "backtests")
    assert data["resultats"][0]["percentile_tirages"] == c.percentile
    report = (tmp_path / "backtests" / "retour-vwap-btcusdt.md").read_text(encoding="utf-8")
    assert "percentile" in report and "Régimes" in report
    assert "faux positif" in (tmp_path / "backtests" / "synthese.md").read_text(encoding="utf-8")


def test_entree_admise_fragile_au_spread():
    c = selected_couple()
    c.draws = [D(-10)] * 10
    c.oos = {"stats": stats(50, 40), "sensitivity": stats(-3, 40), "open_close": stats(1, 4), "nothing": stats(0, 0),
             "p": 0.01, "entries": 40, "eligible": 4000, "ignored": {}, "thresholds": regimes.Thresholds(D(1), D("0.3")),
             "is_regimes": {}, "oos_regimes": {}}
    c.status = catalogue.ADMITTED
    entry = catalogue.entry_json(c)
    assert entry["fragile_spread"] is True and entry["references"]["tirages"]["percentile"] == 100


def test_catalogue_vide_valide(tmp_path):
    flat = ones(DAY, [D(100)] * 1440)
    b = catalogue.build(Guarded(flat), periods=PERIODS, models=["cassure-range"], pairs=["BTCUSDT"], config=LOOSE,
                        seeds=(1, 2), progress=lambda m: None)
    data = catalogue.write(b, tmp_path / "catalogue.json", tmp_path / "backtests")
    assert data["entrees"] == [] and data["resultats"][0]["statut"] == catalogue.REJECTED_IS
    again = json.loads((tmp_path / "catalogue.json").read_text(encoding="utf-8"))
    catalogue.validate_catalogue(again)
    assert "catalogue est vide" in (tmp_path / "backtests" / "synthese.md").read_text(encoding="utf-8")


def test_entree_hors_bornes_refusee():
    data = {"version": 1, "entrees": [{"modele": "retour-vwap", "paire": "BTCUSDT",
                                       "definition": strategies.model("retour-vwap"),
                                       "parametres": {"rsi_extreme.seuil": "40"},
                                       "bornes": {"rsi_extreme.seuil": {"min": "25", "max": "30"}}}]}
    with pytest.raises(ValueError, match="hors de ses bornes"):
        catalogue.validate_catalogue(data)


def test_calcul_en_processus_spawn(tmp_path):
    db = tmp_path / "marche.db"
    store = MarketStore(db)
    store.save(walk_data())
    store.close()
    loader = catalogue.StoreLoader(str(db))
    pool = catalogue._mapper(2)
    try:
        couples = catalogue.in_sample_phase(loader, PERIODS, ["retour-vwap"], ["BTCUSDT"], LOOSE, pool, lambda m: None)
        assert len(couples[("retour-vwap", "BTCUSDT")].cells) == 9
        c = selected_couple()
        catalogue.out_of_sample_phase({("retour-vwap", "BTCUSDT"): c}, loader, PERIODS, LOOSE, tuple(range(1, 5)),
                                      pool, lambda m: None)
    finally:
        pool.close()
        pool.join()
    serial = selected_couple()
    catalogue.out_of_sample_phase({("retour-vwap", "BTCUSDT"): serial}, loader, PERIODS, LOOSE, tuple(range(1, 5)),
                                  None, lambda m: None)
    assert c.draws == serial.draws and c.oos["stats"] == serial.oos["stats"]  # mêmes résultats qu'en série


# --- Présentation des rapports ---


def fake_build(couples, seconds):
    return catalogue.Build(PERIODS, BacktestConfig(), couples, tuple(range(1, 11)), "abc1234",
                           datetime(2026, 11, 7, 17, 59, tzinfo=catalogue.UTC), seconds)


def _couple_teste():
    c = selected_couple()
    c.draws = [D(-10)] * 10
    c.oos = {"stats": stats(50, 40), "sensitivity": stats(-3, 40), "open_close": stats(1, 4), "nothing": stats(0, 0),
             "p": 0.01, "entries": 40, "eligible": 4000, "ignored": {}, "thresholds": regimes.Thresholds(D("0.19"), D("0.26")),
             "is_regimes": {}, "oos_regimes": {}}
    c.status = catalogue.ADMITTED
    return c


def test_rapports_nombres_a_la_francaise():
    b = fake_build([_couple_teste()], {"echantillon": 3072.0, "hors_echantillon": 600.0})
    report = catalogue.report_md(b.couples[0], b)
    summary = catalogue.summary_md(b)
    for text in (report, summary):
        assert "spread 0,02 %" in text.lower() and "frais 0,1 % par ordre" in text and "10 000,00 USDT" in text
        assert "0.02" not in text and "0.100" not in text
    assert "| **0,4** |" in report and "+100,00 (40)*" in report and "ATR 14 ≥ 0,1900 %" in report
    assert "+50,00" in report and "percentile 100" in report and "s = 0,05 %" in report


def test_durees_lisibles():
    none_tested = fake_build([Couple("retour-vwap", "BTCUSDT")], {"echantillon": 3072.0, "hors_echantillon": 0.004})
    assert catalogue.durations(none_tested) == "échantillon 51,2 min, hors échantillon non lancé (aucune candidate)"
    tested = fake_build([_couple_teste()], {"echantillon": 45.0, "hors_echantillon": 600.0})
    assert catalogue.durations(tested) == "échantillon 45 s, hors échantillon 10,0 min"
    assert "0,0 min" not in catalogue.summary_md(none_tested) and "0.0 min" not in catalogue.summary_md(none_tested)


def test_prudence_sans_couple_teste():
    b = fake_build([Couple("retour-vwap", "BTCUSDT")], {"echantillon": 10.0})
    text = catalogue.prudence(b)
    assert "aucun couple n'a été testé hors échantillon" in text and "0,0 couple" not in text and "0.0" not in text
    assert "Si les 9 couples l'avaient été" in text
    tested = catalogue.prudence(fake_build([_couple_teste()], {"echantillon": 10.0}))
    assert "1 couple(s) testé(s)" in tested and "environ 0,1 couple(s)" in tested


def test_grille_echantillon_dans_le_catalogue(tmp_path):
    c = Couple("retour-vwap", "BTCUSDT", cells=grid(["+-f", "---", "---"]))
    data = catalogue.catalogue_json(fake_build([c], {"echantillon": 1.0}))
    rows = data["resultats"][0]["grille_echantillon"]
    assert len(rows) == 9 and rows[0] == {"parametres": {"ecart_vwap.seuil_pct": "0.4", "rsi_extreme.seuil": "25"},
                                           "pnl": "100.00", "trades": 40, "candidate": True}
    assert rows[2]["candidate"] is False and rows[2]["trades"] == 10
