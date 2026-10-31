"""Briques pures : valeurs calculées à la main, bougies synthétiques, jamais la bougie en cours."""

from datetime import time, timedelta

from etape6 import DAY, D, candle, minutes, series
from simtrade import bricks
from simtrade.bricks import MarketView

NOON = DAY.replace(hour=12)


def view(candles, candles_1m=(), now=None):
    return MarketView(now or candles[-1].close_time, candles, list(candles_1m))


# --- ATR ---


def test_atr_de_wilder_calcule_a_la_main():
    # TR : max(3, |102-100|, |99-100|) = 3 ; max(3, 2, 1) = 3 ; max(7, |108-102|, |101-102|) = 7
    cs = [
        candle(NOON, "100", high="100", low="100"),
        candle(NOON + timedelta(minutes=5), "101", high="102", low="99"),
        candle(NOON + timedelta(minutes=10), "102", high="103", low="100"),
        candle(NOON + timedelta(minutes=15), "107", high="108", low="101"),
    ]
    # n = 2 : moyenne des 2 premiers TR = 3, puis (3 x 1 + 7) / 2 = 5
    assert bricks.atr(cs, 2) == D("5")
    assert bricks.atr_pct(cs, 2) == (D(5) / D(107) * 100).quantize(D("0.00000001"))
    assert bricks.atr(cs, 3) == D("4.33333333")  # (3 + 3 + 7) / 3
    assert bricks.atr(cs, 4) is None  # il faut n + 1 bougies


def test_volatilite_dans_les_bornes():
    cs = series(NOON, ["100"] * 15, spread="0.25")  # TR = 0,5 partout -> ATR 0,5 % de 100
    p = {"n": 14, "min_pct": D("0.4"), "max_pct": D("0.6")}
    r = bricks.volatilite(view(cs), p)
    assert r.ok is True and r.values["atr_pct"] == D("0.5")
    assert bricks.volatilite(view(cs), {**p, "max_pct": D("0.49")}).ok is False
    assert bricks.volatilite(view(cs[:14]), p).ok is None


# --- RSI ---


def test_rsi_extreme_bas_et_haut():
    down = series(NOON, [D(100) - i for i in range(15)])  # que des baisses : RSI = 0
    up = series(NOON, [D(100) + i for i in range(15)])  # que des hausses : RSI = 100
    assert bricks.rsi_extreme(view(down), {"zone": "bas", "n": 14, "seuil": D(30)}).ok is True
    assert bricks.rsi_extreme(view(up), {"zone": "bas", "n": 14, "seuil": D(30)}).ok is False
    assert bricks.rsi_extreme(view(up), {"zone": "haut", "n": 14, "seuil": D(70)}).values["rsi"] == D(100)
    assert bricks.rsi_extreme(view(down[:14]), {"zone": "bas", "n": 14, "seuil": D(30)}).ok is None


# --- EMA ---


def test_croisement_ema_haussier_sur_la_derniere_bougie():
    # rapide 3 (alpha 1/2), lente 5 (alpha 1/3), amorcées à 10.
    # Avant : rapide 9,5 <= lente 9,6667 ; après 12 : rapide 10,75 > lente 10,4444.
    cs = series(NOON, ["10"] * 6 + ["9", "12"])
    r = bricks.croisement_ema(view(cs), {"sens": "haussier", "rapide": 3, "lente": 5})
    assert r.ok is True and r.values["ema_rapide"] == D("10.75") and r.values["ema_lente"] == D("10.44444444")
    assert bricks.croisement_ema(view(cs), {"sens": "baissier", "rapide": 3, "lente": 5}).ok is False
    # Une bougie de plus sans nouveau croisement : faux
    more = cs + series(cs[-1].close_time, ["12.5"])
    assert bricks.croisement_ema(view(more), {"sens": "haussier", "rapide": 3, "lente": 5}).ok is False
    assert bricks.ema_cross(cs[:5], 3, 5) is None  # pas assez de bougies pour la lente à T-1


# --- VWAP ---


def test_ecart_au_vwap_de_session():
    start = DAY.replace(hour=11, minute=55)
    one = minutes(DAY, 12 * 60, "100")  # VWAP de session = 100
    cs = series(start, ["99"])  # clôture 99 : écart -1 %
    v = view(cs, one)
    assert bricks.ecart_vwap(v, {"cote": "sous", "seuil_pct": D("0.6")}).ok is True
    assert bricks.ecart_vwap(v, {"cote": "sous", "seuil_pct": D("1.01")}).ok is False
    assert bricks.ecart_vwap(v, {"cote": "dessus", "seuil_pct": D("0.1")}).ok is False
    assert bricks.ecart_vwap(v, {"cote": "sous", "seuil_pct": D("0.6")}).values["ecart_vwap_pct"] == D("-1")
    assert bricks.au_dessus_vwap(v, {}).ok is False
    assert bricks.retour_vwap(view(series(start, ["100"]), one), {}).ok is True


def test_vwap_pondere_par_les_volumes_et_remis_a_zero_a_minuit():
    yesterday = minutes(DAY - timedelta(minutes=10), 10, "200", volume="50")  # session d'avant : ignorée
    today = minutes(DAY, 1, "100", volume="3") + minutes(DAY + timedelta(minutes=1), 1, "104", volume="1")
    cs = series(DAY, ["101"], interval="5m")
    # (100 x 3 + 104 x 1) / 4 = 101 ; seules les 1m de la session ET clôturées à la fin de la bougie
    assert bricks.session_vwap(yesterday + today, cs[0]) == D("101")


def test_vwap_sans_volume_donne_none():
    cs = series(NOON, ["100"])
    assert bricks.ecart_vwap(view(cs, minutes(DAY, 5, "100", volume="0")), {"cote": "sous", "seuil_pct": D("1")}).ok is None


# --- Range d'ouverture ---


def opening(high="101", low="99"):
    return [candle(DAY + timedelta(minutes=i), "100", "1m", high=high, low=low) for i in range(30)]


def test_cassure_du_range_d_ouverture():
    rng = opening()
    cs = series(DAY + timedelta(minutes=30), ["100", "100.5", "102"])
    p = {"debut": time(0, 0), "minutes": 30}
    r = bricks.cassure_range(view(cs, rng), p)
    assert r.ok is True and (r.values["range_haut"], r.values["range_bas"]) == (D("101"), D("99"))
    assert bricks.cassure_range(view(cs[:2], rng), p).ok is False  # pas encore cassé
    # Seconde cassure (déjà une clôture au-dessus depuis la fin du range) : pas de signal
    again = series(DAY + timedelta(minutes=30), ["102", "100.5", "102"])
    assert bricks.cassure_range(view(again, rng), p).ok is False


def test_range_incomplet_ou_pas_encore_fini():
    p = {"debut": time(0, 0), "minutes": 30}
    cs = series(DAY + timedelta(minutes=30), ["100", "102"])
    assert bricks.cassure_range(view(cs, opening()[:29]), p).ok is None  # une minute manque
    during = series(DAY + timedelta(minutes=15), ["100", "102"])  # bougie dans le range
    assert bricks.cassure_range(view(during, opening()), p).ok is False


def test_retour_dans_le_range():
    p = {"debut": time(0, 0), "minutes": 30}
    assert bricks.retour_dans_range(view(series(DAY + timedelta(minutes=40), ["100.9"]), opening()), p).ok is True
    assert bricks.retour_dans_range(view(series(DAY + timedelta(minutes=40), ["101.1"]), opening()), p).ok is False


# --- Durée ---


def test_duree_max_en_bougies_depuis_l_entree():
    cs = series(NOON, ["100"] * 4)  # 12:00 -> 12:20
    entered = NOON + timedelta(minutes=2)
    assert bricks.duree_max(view(cs), {"bougies": 3}, entered).ok is True  # 18 min = 3 bougies complètes
    assert bricks.duree_max(view(cs), {"bougies": 4}, entered).ok is False
    assert bricks.duree_max(view(cs), {"bougies": 3}, None).ok is None


# --- Jamais la bougie en cours ---


def test_la_bougie_en_cours_ne_change_aucune_brique():
    closed = series(NOON, [D(100) - D("0.1") * i for i in range(30)], spread="0.1")
    one = minutes(DAY, 12 * 60 + 150, "100")
    now = closed[-1].close_time + timedelta(minutes=2)
    current = candle(closed[-1].close_time, "150", high="160", low="90")  # bougie en cours, extrême
    assert not current.is_closed(now)
    params = {
        "rsi_extreme": {"zone": "bas", "n": 14, "seuil": D(30)},
        "croisement_ema": {"sens": "haussier", "rapide": 9, "lente": 21},
        "ecart_vwap": {"cote": "sous", "seuil_pct": D("0.6")},
        "au_dessus_vwap": {},
        "volatilite": {"n": 14, "min_pct": D("0.05"), "max_pct": D("5")},
        "retour_vwap": {},
    }
    for name, p in params.items():
        fn = getattr(bricks, name)
        without = fn(MarketView(now, closed, one), p)
        with_current = fn(MarketView(now, closed + [current], one), p)
        assert without == with_current, name
