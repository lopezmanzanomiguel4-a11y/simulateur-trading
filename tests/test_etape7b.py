"""Étape 7b : briques au_dessus_ema et hauteur_range, modèles 15m / 1h, catalogue au format 2 (fusion)."""

import json
from datetime import time, timedelta
from pathlib import Path

import pytest

from etape6 import DAY, D, candle, series
from test_catalogue import PERIODS, _couple_teste, fake_build, walk_data
from test_cli_etape7 import run  # noqa: F401 (fixture)
from simtrade import backtest, bricks, catalogue, market, strategies
from simtrade.backtest import BacktestConfig, Market
from simtrade.bricks import MarketView
from simtrade.catalogue import Couple
from simtrade.errors import InvalidInput

NEW = ("tendance-1h", "cassure-matin", "repli-tendance")
REPO_CATALOGUE = Path(catalogue.__file__).resolve().parent / "catalogue.json"


def view(candles, candles_1m=(), now=None):
    return MarketView(now or candles[-1].close_time, candles, list(candles_1m))


# --- Briques ---


def test_au_dessus_ema_vrai_faux_et_manque_de_donnees():
    rising = series(DAY, [str(100 + i) for i in range(25)], "15m")
    r = bricks.au_dessus_ema(view(rising), {"n": 20})
    assert r.ok is True and r.values["ema"] == market.ema([c.close for c in rising], 20)
    falling = series(DAY, [str(200 - i) for i in range(25)], "15m")
    assert bricks.au_dessus_ema(view(falling), {"n": 20}).ok is False
    assert bricks.au_dessus_ema(view(rising[:19]), {"n": 20}).ok is None


def test_au_dessus_ema_ignore_la_bougie_en_cours():
    cs = series(DAY, ["100"] * 20 + ["90"], "15m")
    now = cs[-1].open_time + timedelta(minutes=5)  # dernière bougie pas encore close
    assert bricks.au_dessus_ema(view(cs, now=now), {"n": 20}).ok is False  # 100 > 100 : faux, 90 ignoré
    assert bricks.au_dessus_ema(view(cs), {"n": 20}).ok is False


def matin(high="102", low="100"):
    """Range 00:00–08:00 : 480 minutes entre `low` et `high`."""
    return [candle(DAY + timedelta(minutes=i), "101", "1m", high=high, low=low) for i in range(480)]


def test_hauteur_range_calculee_a_la_main():
    p = {"debut": time(0, 0), "minutes": 480, "min_pct": D("1.5")}
    cs = series(DAY + timedelta(hours=8), ["101"], "15m")
    r = bricks.hauteur_range(view(cs, matin()), p)
    assert r.values["hauteur_range_pct"] == market._q(D(2) / D(102) * 100)  # 1,96 %
    assert r.ok is True
    assert bricks.hauteur_range(view(cs, matin("101", "100")), p).ok is False  # 0,99 %
    assert bricks.hauteur_range(view(cs, matin()[:479]), p).ok is None  # une minute manque


def test_modeles_7b_valides_et_relus_a_l_identique():
    for name in NEW:
        s = strategies.validate(strategies.model(name))
        assert strategies.validate(s.to_json()) == s
    assert strategies.validate(strategies.model("tendance-1h")).intervalle == "1h"


def test_hauteur_range_demande_cassure_range():
    raw = strategies.model("repli-tendance")
    raw["entree"] = raw["entree"] + [{"brique": "hauteur_range", "min_pct": "1"}]
    with pytest.raises(InvalidInput, match="hauteur_range"):
        strategies.validate(raw)


def test_hauteur_range_lit_le_range_de_cassure_range():
    s = strategies.validate(strategies.model("cassure-matin"))
    brick = next(b for b in s.entree if b.name == "hauteur_range")
    cs = series(DAY + timedelta(hours=8), ["101"], "15m")
    r = strategies._run(brick, view(cs, matin()), None, s)
    assert r.ok is True  # 1,96 % ≥ 1,5 % sur le range 00:00 + 480 min


# --- Catalogue : grille 7b ---


def test_grille_7b_valide_et_defauts():
    for model in catalogue.GRID_7B:
        assert catalogue.interval_of(model) in ("15m", "1h")
        catalogue.check_bounds(model, catalogue.bounds(model, ((0, 2), (0, 2))))
        for _, ov in catalogue.combos(model):
            for pair in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                assert catalogue.strategy_for(model, pair, ov).intervalle == catalogue.interval_of(model)
    assert [catalogue.default_cell(m) for m in catalogue.GRID_7B] == [(1, 0), (1, 1), (1, 1)]
    assert catalogue.interval_of("retour-vwap") == "5m"


def test_repli_tendance_surcharge_seulement_le_rsi_d_entree():
    s = catalogue.strategy_for("repli-tendance", "BTCUSDT", {"entree.rsi_extreme.seuil": "25", "objectif.valeur": "2"})
    assert next(b for b in s.entree if b.name == "rsi_extreme").params["seuil"] == D("25")
    assert next(b for b in s.sortie if b.name == "rsi_extreme").params["seuil"] == D("70")
    assert catalogue._current(s, "entree.rsi_extreme.seuil") == D("25")


def test_objectif_d_au_moins_un_pourcent():
    """Au plus petit cran de chaque grille, l'objectif visé reste ≥ 1 %."""
    t = strategies.validate(strategies.model("tendance-1h"))
    vol = next(b for b in t.entree if b.name == "volatilite").params["min_pct"]
    assert vol * D("2") >= 1  # ATR ≥ 0,5 % × multiple 2
    assert min(D(v) for v in catalogue.GRID_7B["cassure-matin"][0][1]) >= 1  # hauteur ≥ 1 %, r ≥ 1
    assert min(D(v) for v in catalogue.GRID_7B["repli-tendance"][1][1]) >= 1


def test_backtest_en_1h_sur_bougies_agregees():
    m = Market(walk_data(), "1h")
    assert m.interval == "1h" and all(b.interval == "1h" for b in m.bars) and len(m.bars) == 24
    s = catalogue.strategy_for("tendance-1h", "BTCUSDT", {})
    result = backtest.run(s, m, m.bars[0].close_time, m.bars[-1].close_time, BacktestConfig(full_window=False))
    assert result.eligible > 0


def test_tache_echantillon_utilise_l_intervalle_du_modele():
    r = catalogue.in_sample_task((lambda pair, a, b: walk_data(), "tendance-1h", "BTCUSDT", (1, 0), {}, PERIODS,
                                  BacktestConfig(full_window=False)))
    assert r["model"] == "tendance-1h"
    assert next(iter(catalogue._MARKET.values())).interval == "1h"


# --- Catalogue : format 2 et fusion ---


def build_7b(couples):
    b = fake_build(couples, {"echantillon": 1.0})
    b.periods = catalogue.PERIODS_7B
    return b


def test_fusion_garde_les_resultats_de_l_etape_7_tels_quels():
    old = json.loads(REPO_CATALOGUE.read_text(encoding="utf-8"))
    assert old["version"] == 1 and len(old["resultats"]) == 9
    new = catalogue.catalogue_json(build_7b([Couple("tendance-1h", "BTCUSDT")]))
    merged = catalogue.merge(old, new)
    catalogue.validate_catalogue(merged)
    assert merged["version"] == 2 and [c["etape"] for c in merged["constructions"]] == ["7", "7b"]
    assert len(merged["resultats"]) == 10
    for before, after in zip(old["resultats"], merged["resultats"][:9]):
        assert {k: after[k] for k in before} == before  # aucune valeur changée
        assert (after["etape"], after["commit"], after["intervalle"]) == ("7", old["commit"], "5m")
        assert after["periodes"] == old["periodes"]
    c7 = merged["constructions"][0]
    assert c7["grille"] == old["grille"] and c7["cree_le"] == old["cree_le"]
    last = merged["resultats"][-1]
    assert (last["etape"], last["intervalle"], last["commit"]) == ("7b", "1h", "abc1234")
    assert last["periodes"]["echantillon"][0].startswith("2025-11-09")
    # Une nouvelle construction 7b remplace l'ancienne 7b, jamais l'étape 7
    again = catalogue.merge(merged, catalogue.catalogue_json(build_7b([Couple("cassure-matin", "ETHUSDT")])))
    assert [r["modele"] for r in again["resultats"]][-1] == "cassure-matin" and len(again["resultats"]) == 10
    assert again["resultats"][:9] == merged["resultats"][:9]


def test_write_fusionne_et_ecrit_la_synthese_7b(tmp_path):
    (tmp_path / "catalogue.json").write_text(REPO_CATALOGUE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "backtests").mkdir()
    (tmp_path / "backtests" / "synthese.md").write_text("étape 7", encoding="utf-8")
    data = catalogue.write(build_7b([Couple("repli-tendance", "SOLUSDT")]), tmp_path / "catalogue.json",
                           tmp_path / "backtests")
    assert len(data["resultats"]) == 10
    assert (tmp_path / "backtests" / "synthese.md").read_text(encoding="utf-8") == "étape 7"
    summary = (tmp_path / "backtests" / "synthese-7b.md").read_text(encoding="utf-8")
    assert "intervalle 15m" in summary and "repli-tendance" in summary
    assert (tmp_path / "backtests" / "repli-tendance-solusdt.md").exists()


def test_prudence_7b_compte_les_couples_testes_et_rappelle_l_etape_7():
    tested = _couple_teste()
    tested.model = "tendance-1h"
    tested.choice, tested.rect = (1, 0), ((1, 1), (0, 0))
    text = catalogue.prudence(build_7b([tested, Couple("cassure-matin", "BTCUSDT")]))
    assert "1 couple(s) testé(s)" in text and "(9 tests" in text
    assert "81 combinaisons ont été explorées ici, après 81 à l'étape 7" in text
    assert "étape 7" not in catalogue.prudence(fake_build([_couple_teste()], {"echantillon": 1.0}))


def test_cli_affiche_le_format_2(run, capsys, tmp_path):
    new = catalogue.catalogue_json(build_7b([Couple("tendance-1h", "BTCUSDT")]))
    merged = catalogue.merge(json.loads(REPO_CATALOGUE.read_text(encoding="utf-8")), new)
    (tmp_path / "catalogue.json").write_text(json.dumps(merged), encoding="utf-8")
    assert run("catalogue", "--sortie", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "étape 7 :" in out and "étape 7b :" in out and "tendance-1h" in out and "1h" in out
