"""Stratégies JSON : bornes de chaque paramètre, validation, niveaux de stop et d'objectif,
évaluation des 3 modèles de départ, raisons d'ignorer un signal. Module pur, sans réseau."""

from datetime import timedelta

import pytest

from etape6 import DAY, D, TEST_STRATEGY, candle, falling, minutes, series
from simtrade import strategies
from simtrade.bricks import MarketView
from simtrade.errors import InvalidInput
from simtrade.market import Quality
from simtrade.sessions import SessionRules
from simtrade.strategies import Plan

NOON = DAY.replace(hour=12)


def model(name, **changes):
    return strategies.with_overrides(strategies.model(name), changes)


# --- Modèles ---


def test_les_trois_modeles_sont_valides_et_relus_a_l_identique():
    found = strategies.models()
    assert sorted(found) == ["cassure-matin", "cassure-range", "repli-tendance", "retour-vwap", "tendance-1h",
                             "tendance-ema"]
    for raw in found.values():
        s = strategies.validate(raw)
        assert strategies.validate(s.to_json()) == s
        assert strategies.loads(s.dumps()) == s


def test_valeurs_par_defaut():
    s = strategies.validate({"nom": "x", "entree": [{"brique": "rsi_extreme"}], "stop": {"type": "pct"}, "objectif": {"type": "r"}})
    assert (s.paire, s.intervalle, s.taille_pct, f"{s.debut:%H:%M}", f"{s.fin:%H:%M}") == ("BTCUSDT", "5m", D(10), "06:00", "22:00")
    assert s.entree[0].params == {"zone": "bas", "n": 14, "seuil": D(30)}
    assert s.stop.params == {"valeur": D("0.6")} and s.objectif.params == {"multiple": D(2)}


# --- Bornes : acceptées à la limite, refusées juste au-delà, message qui nomme le paramètre ---

BOUNDS = [
    ("retour-vwap", "taille.pct_capital", "1", "0.99"),
    ("retour-vwap", "taille.pct_capital", "20", "20.01"),
    ("retour-vwap", "rsi_extreme.n", "2", "1"),
    ("retour-vwap", "rsi_extreme.n", "50", "51"),
    ("retour-vwap", "rsi_extreme.seuil", "5", "4.9"),
    ("retour-vwap", "rsi_extreme.seuil", "45", "45.1"),
    ("retour-vwap", "ecart_vwap.seuil_pct", "0.1", "0.09"),
    ("retour-vwap", "ecart_vwap.seuil_pct", "3", "3.01"),
    ("retour-vwap", "volatilite.n", "5", "4"),
    ("retour-vwap", "volatilite.n", "50", "51"),
    ("retour-vwap", "volatilite.min_pct", "0.05", "0.04"),
    ("retour-vwap", "volatilite.max_pct", "5", "5.01"),
    ("retour-vwap", "duree_max.bougies", "1", "0"),
    ("retour-vwap", "duree_max.bougies", "96", "97"),
    ("retour-vwap", "stop.multiple", "0.5", "0.49"),
    ("retour-vwap", "stop.multiple", "5", "5.1"),
    ("retour-vwap", "stop.n", "5", "4"),
    ("retour-vwap", "stop.n", "50", "51"),
    ("retour-vwap", "objectif.multiple", "1", "0.99"),
    ("retour-vwap", "objectif.multiple", "5", "5.01"),
    ("retour-vwap", "heures_actives.fin", "23:00", "23:01"),
    ("tendance-ema", "croisement_ema.rapide", "3", "2"),
    ("tendance-ema", "croisement_ema.rapide", "20", "51"),
    ("tendance-ema", "croisement_ema.lente", "10", "9"),
    ("tendance-ema", "croisement_ema.lente", "200", "201"),
    ("cassure-range", "cassure_range.minutes", "15", "14"),
    ("cassure-range", "cassure_range.minutes", "480", "481"),
    ("cassure-range", "cassure_range.debut", "20:00", "20:01"),
    ("cassure-range", "stop.marge_pct", "0", "-0.01"),
    ("cassure-range", "stop.marge_pct", "1", "1.01"),
]


@pytest.mark.parametrize("name,key,ok,ko", BOUNDS)
def test_borne_acceptee_a_la_limite_et_refusee_au_dela(name, key, ok, ko):
    strategies.validate(model(name, **{key: ok}))
    with pytest.raises(InvalidInput) as err:
        strategies.validate(model(name, **{key: ko}))
    assert f"'{key.split('.')[-1]}'" in str(err.value) or key.split(".")[-1] in str(err.value)
    assert "hors bornes" in str(err.value) or "entier" in str(err.value) or "invalide" in str(err.value)


def test_le_message_nomme_le_chemin_complet():
    with pytest.raises(InvalidInput, match=r"'entree\.rsi_extreme\.seuil' hors bornes : 50 \(de 5 à 45\)"):
        strategies.validate(model("retour-vwap", **{"rsi_extreme.seuil": "50"}))


def test_seuil_du_rsi_selon_la_zone():
    raw = {**TEST_STRATEGY, "sortie": [{"brique": "rsi_extreme", "zone": "haut", "seuil": "60"}]}
    assert strategies.validate(raw).sortie[0].params["seuil"] == D(60)
    for bad in ("59", "96"):
        with pytest.raises(InvalidInput, match="sortie.rsi_extreme.seuil"):
            strategies.validate({**raw, "sortie": [{"brique": "rsi_extreme", "zone": "haut", "seuil": bad}]})
    assert strategies.validate({**raw, "sortie": [{"brique": "rsi_extreme"}]}).sortie[0].params["zone"] == "haut"


def test_objectif_sous_le_minimum_refuse():
    assert strategies.MIN_TARGET_PCT == D("0.30")
    ok = model("retour-vwap", **{"objectif.type": "pct", "objectif.valeur": "0.3"})
    assert strategies.validate(ok).objectif.params == {"valeur": D("0.3")}
    with pytest.raises(InvalidInput, match=r"'objectif.valeur' hors bornes : 0.29 \(de 0.3 à 10\)"):
        strategies.validate(model("retour-vwap", **{"objectif.type": "pct", "objectif.valeur": "0.29"}))


def test_paire_et_intervalle():
    assert strategies.validate(model("retour-vwap", paire="eth", intervalle="15m")).paire == "ETHUSDT"
    with pytest.raises(InvalidInput, match="'intervalle' invalide : '1m'"):
        strategies.validate(model("retour-vwap", intervalle="1m"))
    with pytest.raises(InvalidInput, match="'paire' invalide"):
        strategies.validate(model("retour-vwap", paire="doge"))


@pytest.mark.parametrize(
    "change,message",
    [
        ({"code": "print(1)"}, "'code' inconnu"),
        ({"entree": [{"brique": "magie"}]}, "inconnue ou non permise"),
        ({"entree": [{"brique": "retour_vwap"}]}, "inconnue ou non permise"),  # brique de sortie seulement
        ({"entree": [{"brique": "rsi_extreme"}, {"brique": "rsi_extreme"}]}, "présente deux fois"),
        ({"entree": []}, "de 1 à 4 briques"),
        ({"entree": [{"brique": "rsi_extreme", "periode": 3}]}, "'entree.rsi_extreme.periode' inconnu"),
        ({"entree": [{"brique": "croisement_ema", "rapide": 21, "lente": 21}]}, "inférieure à la lente"),
        ({"entree": [{"brique": "volatilite", "min_pct": "1", "max_pct": "1"}]}, "inférieur à max_pct"),
        ({"entree": [{"brique": "ecart_vwap", "seuil_pct": 0.6}]}, "nombre décimal en chaîne"),
        ({"entree": [{"brique": "rsi_extreme", "n": "14.5"}]}, "entier attendu"),
        ({"heures_actives": {"debut": "12:00", "fin": "12:00"}}, "doit être avant la fin"),
        ({"stop": {"type": "range"}}, "demande la brique cassure_range"),
        ({"sortie": [{"brique": "retour_dans_range"}]}, "demande la brique cassure_range"),
        ({"stop": {"type": "trailing"}}, "'stop.type' invalide"),
        ({"nom": "Mon Robot"}, "'nom' invalide"),
    ],
)
def test_definitions_invalides_refusees(change, message):
    with pytest.raises(InvalidInput, match=message):
        strategies.validate({**TEST_STRATEGY, **change})


def test_surcharges():
    raw = strategies.with_overrides(strategies.model("tendance-ema"), {"croisement_ema.rapide": "5", "sortie.croisement_ema.lente": "30"})
    s = strategies.validate(raw)
    assert s.entree[0].params["rapide"] == 5 and s.sortie[0].params["rapide"] == 5
    assert s.entree[0].params["lente"] == 21 and s.sortie[0].params["lente"] == 30
    with pytest.raises(InvalidInput, match="n'est pas dans la stratégie"):
        strategies.with_overrides(strategies.model("tendance-ema"), {"rsi_extreme.n": "3"})


def test_schema_exporte_les_bornes():
    schema = strategies.schema()
    assert schema["objectif"]["pct"]["valeur"] == {"type": "decimal", "defaut": "1", "min": "0.3", "max": "10"}
    assert schema["briques"]["croisement_ema"]["lente"] == {"type": "entier", "defaut": 21, "min": 10, "max": 200}
    assert schema["briques"]["rsi_extreme"]["seuil"]["selon_zone"]["haut"]["min"] == "60"
    assert schema["intervalle"]["choix"] == ["5m", "15m", "1h"]


# --- Niveaux ---


def test_niveaux_pct_et_r():
    lv = strategies.levels(Plan(D("0.5"), None, None, D("2")), D("100"), D("99.99"))
    assert (lv.stop, lv.target, lv.stop_pct, lv.target_pct, lv.reasons) == (D("99.50"), D("101.00"), D("0.5"), D("1.0"), ())


def test_stop_calcule_hors_bornes_ignore():
    assert strategies.levels(Plan(D("0.1"), None, None, D("3")), D("100"), D("100")).reasons == ("stop_hors_bornes",)
    assert strategies.levels(Plan(D("3.5"), None, None, D("1")), D("100"), D("100")).reasons == ("stop_hors_bornes",)
    assert strategies.levels(Plan(None, D("97"), None, D("1")), D("100"), D("100")).reasons == ()  # 3 % : à la limite


def test_objectif_trop_proche_avec_le_spread():
    # 0,30 % d'objectif, spread de 0,05 % : minimum 0,2 + 0,05 + 0,1 = 0,35 %
    assert strategies.levels(Plan(D("0.5"), None, D("0.3"), None), D("100.025"), D("99.975")).reasons == ("objectif_trop_proche",)
    assert strategies.levels(Plan(D("0.5"), None, D("0.36"), None), D("100.025"), D("99.975")).reasons == ()


# --- Évaluation des 3 modèles ---


def test_retour_au_vwap_entree_et_sortie():
    s = strategies.validate(strategies.model("retour-vwap"))
    end = NOON
    cs = series(end - timedelta(minutes=100), falling(20, "100", "0.05"), spread="0.1")  # 100 -> 99,05
    one = minutes(DAY, 12 * 60, "100")  # VWAP 100 : écart -0,95 %
    ev = strategies.evaluate(s, MarketView(end, cs, one))
    assert ev.entry is True and ev.candle_open == cs[-1].open_time
    assert ev.values["rsi_extreme.rsi"] == D(0) and ev.values["ecart_vwap.ecart_vwap_pct"] == D("-0.95")
    assert ev.plan is not None and ev.plan.target_r == D(2) and ev.plan.stop_pct > 0
    # Retour au VWAP : sortie
    back = cs + series(end, ["100.1"])
    ev2 = strategies.evaluate(s, MarketView(end + timedelta(minutes=5), back, one + minutes(end, 5, "100")), entered_at=end)
    assert ev2.exit is True and ev2.exit_reasons == ("retour_vwap",)


def test_tendance_ema_croisement_haussier():
    s = strategies.validate(strategies.model("tendance-ema"))
    closes = ["100"] * 30 + ["99", "101.5"]
    cs = series(NOON - timedelta(minutes=5 * len(closes)), closes, spread="0.2")
    one = minutes(DAY, 12 * 60, "100")
    ev = strategies.evaluate(s, MarketView(NOON, cs, one))
    assert ev.entry is True, ev.values
    assert ev.values["croisement_ema.croisement"] == "haussier"
    # Même chose sous le VWAP : pas d'entrée
    ev_low = strategies.evaluate(s, MarketView(NOON, cs, minutes(DAY, 12 * 60, "102")))
    assert ev_low.entry is False
    # Croisement baissier ensuite : sortie
    down = cs + series(NOON, ["98"])
    ev_exit = strategies.evaluate(s, MarketView(NOON + timedelta(minutes=5), down, one), entered_at=NOON)
    assert ev_exit.exit is True


def test_cassure_du_range_stop_au_bas_du_range():
    s = strategies.validate(strategies.model("cassure-range"))
    rng = [candle(DAY + timedelta(minutes=i), "100", "1m", high="101", low="99.5") for i in range(30)]
    before = series(DAY - timedelta(minutes=80), ["100"] * 16, spread="0.3")  # historique pour l'ATR
    after = series(DAY + timedelta(minutes=30), ["100.2", "100.6", "101.4"], spread="0.3")
    one = rng + minutes(DAY + timedelta(minutes=30), 15, "100.5")
    now = DAY + timedelta(minutes=45)
    ev = strategies.evaluate(s, MarketView(now, before + after, one))
    assert ev.entry is True, ev.values
    assert ev.plan.stop_level == D("99.5") * (1 - D("0.0005"))
    lv = strategies.levels(ev.plan, D("101.4"), D("101.39"))
    assert lv.stop == D("99.45") and lv.reasons == ()


def test_aucun_signal_sur_une_bougie_non_cloturee():
    s = strategies.validate(TEST_STRATEGY)
    cs = series(NOON - timedelta(minutes=100), ["100"] * 20, spread="0.1")  # RSI 50 : pas d'entrée
    now = NOON + timedelta(minutes=2)
    current = candle(NOON, "90", spread="0.1")  # la bougie en cours ferait tomber le RSI
    assert strategies.evaluate(s, MarketView(now, cs, minutes(DAY, 12 * 60))).entry is False
    ev = strategies.evaluate(s, MarketView(now, cs + [current], minutes(DAY, 12 * 60)))
    assert ev.entry is False and ev.candle_open == cs[-1].open_time
    # Une fois clôturée, elle compte
    assert strategies.evaluate(s, MarketView(NOON + timedelta(minutes=5), cs + [current], minutes(DAY, 12 * 60))).entry is True


def test_pas_assez_de_bougies():
    s = strategies.validate(TEST_STRATEGY)
    ev = strategies.evaluate(s, MarketView(NOON, series(NOON - timedelta(minutes=25), ["100"] * 5), []))
    assert ev.entry is None


# --- Raisons d'ignorer ---


def quality(**kw) -> Quality:
    base = dict(
        prix_perime=False, flux_transactions_muet=False, source_transactions="Binance", repli_coinbase=False,
        historique="complet", derniere_synchro=NOON, bougie_courante_complete=True, ecart_horloge=0.01,
        vwap_fiable=True, raisons=(),
    )
    return Quality(**{**base, **kw})


def test_raisons_de_qualite():
    s = strategies.validate(strategies.model("retour-vwap"))
    assert strategies.quality_blockers(s, [quality(), quality()]) == ()
    assert strategies.quality_blockers(s, [quality(raisons=("prix_perime",))]) == ("prix_perime",)
    assert strategies.quality_blockers(s, [quality(raisons=("flux_transactions_muet",))]) == ("flux_transactions_muet",)
    assert strategies.quality_blockers(s, [quality(raisons=("horloge_decalee",))]) == ("horloge_decalee",)
    assert strategies.quality_blockers(s, [quality(ecart_horloge=None)]) == ("horloge_non_mesuree",)
    assert strategies.quality_blockers(s, [quality(), quality(raisons=("historique_partiel",))]) == ("historique_partiel",)
    assert strategies.quality_blockers(s, [quality(vwap_fiable=False)]) == ("vwap_non_fiable",)
    # La bougie en cours incomplète ne bloque pas (elle n'est jamais lue)
    assert strategies.quality_blockers(s, [quality(raisons=("bougie_courante_incomplete",))]) == ()
    # VWAP non fiable sans brique VWAP : ne bloque pas
    plain = strategies.validate(strategies.model("cassure-range"))
    assert strategies.quality_blockers(plain, [quality(vwap_fiable=False)]) == ()


def test_raisons_d_entree():
    s = strategies.validate(strategies.model("retour-vwap"))  # 06:00 - 22:00
    rules = SessionRules()
    assert strategies.entry_blockers(s, NOON, rules, False, False) == ()
    assert strategies.entry_blockers(s, DAY.replace(hour=5, minute=59), rules, False, False) == ("hors_heures",)
    assert strategies.entry_blockers(s, DAY.replace(hour=22), rules, False, False) == ("hors_heures",)
    late = SessionRules(last_entry=DAY.replace(hour=11).time())
    assert strategies.entry_blockers(s, NOON, late, True, True) == ("apres_derniere_entree", "deja_en_position", "arret_session")
