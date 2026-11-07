"""Simulateur de backtest : règles d'exécution prudentes, frais et spread à la main, absence de regard vers le futur."""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

import pytest

from simtrade import backtest, strategies
from simtrade.backtest import BacktestConfig, Market, run
from simtrade.market import Candle
from simtrade.models import SESSION_CLOSE, STOP, TARGET, Side
from simtrade.risk import RiskLimits

D = Decimal
UTC = timezone.utc
DAY = datetime(2026, 5, 4, tzinfo=UTC)
MIN = timedelta(minutes=1)
LOOSE = BacktestConfig(full_window=False)

# Entre sur un RSI(2) bas (prix en baisse), sans sortie sur signal : stop 0,5 %, objectif 1 %.
RSI = {
    "nom": "test-bt",
    "paire": "BTCUSDT",
    "intervalle": "5m",
    "entree": [{"brique": "rsi_extreme", "zone": "bas", "n": 2, "seuil": "45"}],
    "sortie": [],
    "stop": {"type": "pct", "valeur": "0.5"},
    "objectif": {"type": "pct", "valeur": "1"},
    "taille": {"pct_capital": "10"},
    "heures_actives": {"debut": "00:00", "fin": "23:00"},
}


def strat(**changes) -> strategies.Strategy:
    return strategies.validate({**RSI, **changes})


def ones(start: datetime, prices, overrides: dict | None = None, volume: str = "1") -> list[Candle]:
    """Une bougie 1m par prix (ouverture = haut = bas = clôture) ; overrides[i] = {open, high, low, close}."""
    out = []
    for i, p in enumerate(prices):
        p = D(p)
        o = {"open": p, "high": p, "low": p, "close": p, **{k: D(v) for k, v in (overrides or {}).get(i, {}).items()}}
        v = D(volume)
        out.append(Candle("BTCUSDT", "1m", start + i * MIN, o["open"], o["high"], o["low"], o["close"], v,
                          o["close"] * v, 1, "binance"))
    return out


def flat_then_fall(start: datetime, flat: int = 60, fall: int = 30, step: str = "0.05") -> list[D]:
    return [D(100)] * flat + [D(100) - (i + 1) * D(step) for i in range(fall)]


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def one_trade(prices, overrides=None, **kw):
    start = at(9)
    data = ones(start, prices, overrides)
    cfg = BacktestConfig(full_window=False, limits=RiskLimits(max_trades=1))
    return run(strat(**kw), data, start, start + len(prices) * MIN, cfg)


# --- Entrée à l'ouverture suivante, frais et spread ---


def test_entree_a_l_ouverture_suivante_au_ask():
    prices = flat_then_fall(at(9))
    r = one_trade(prices)
    buy = r.orders[0]
    # première clôture 5m en baisse : 10:05 (bougie 10:00–10:05) ; exécution à l'ouverture de la minute 10:05
    assert buy.side is Side.BUY and buy.created_at == at(10, 5) + timedelta(seconds=1)
    open_ = prices[65]
    assert buy.price == open_ * (1 + D("0.0001"))  # ask = ouverture × (1 + s/2), s = 0,02 %
    sig = [s for s in r.signals if s.decision == "ordre"][0]
    assert sig.candle_open_time == at(10) and sig.kind == "entree"


def test_frais_et_spread_d_un_aller_retour_a_la_main():
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 30
    hit = 75  # objectif touché pendant la minute 10:15
    r = one_trade(prices, {hit: {"high": "200"}})
    buy, sell = r.orders
    ask = prices[65] * D("1.0001")
    amount = D("1000.00")  # 10 % de 10 000
    qty = (amount / (ask * D("1.001"))).quantize(D("0.00000001"), rounding=ROUND_DOWN)
    gross_in = (qty * ask).quantize(D("0.00000001"))
    fee_in = (gross_in * D("0.001")).quantize(D("0.00000001"))
    target = (ask * D("1.01")).quantize(D("0.01"), rounding=strategies.ROUND_CEILING)
    gross_out = (qty * target).quantize(D("0.00000001"))
    fee_out = (gross_out * D("0.001")).quantize(D("0.00000001"))
    assert buy.quantity == qty and buy.fee == fee_in and buy.cash_flow == -(gross_in + fee_in)
    assert sell.kind == TARGET and sell.price == target and sell.fee == fee_out
    assert sell.created_at == at(9) + hit * MIN + timedelta(seconds=30)
    trade = r.trades[0]
    assert trade.pnl == (gross_out - fee_out) - (gross_in + fee_in)
    assert trade.fees == fee_in + fee_out and trade.exit_reason == "objectif"
    assert r.stats.trades == 1 and r.stats.fees == fee_in + fee_out


def test_stop_et_objectif_dans_la_meme_minute_stop():
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 30
    r = one_trade(prices, {75: {"high": "200", "low": "50"}})
    sell = r.orders[1]
    stop = strategies.levels(strategies.Plan(D("0.5"), None, D("1"), None), r.orders[0].price,
                             D(0)).stop
    assert sell.kind == STOP and sell.price == stop and sell.slippage == 0
    assert r.trades[0].exit_reason == "stop"


def test_stop_touche_au_bid_estime():
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 30
    ask = prices[65] * D("1.0001")
    stop = (ask * D("0.995")).quantize(D("0.01"), rounding=strategies.ROUND_FLOOR)
    # bas juste au-dessus du stop, mais bas × (1 − s/2) en dessous : touché
    low = stop + D("0.005")
    assert low > stop and low * D("0.9999") <= stop
    r = one_trade(prices, {80: {"low": str(low)}})
    assert r.orders[1].kind == STOP and r.orders[1].created_at == at(10, 20) + timedelta(seconds=30)


def test_ouverture_sous_le_stop_execution_a_l_ouverture():
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 30
    r = one_trade(prices, {80: {"open": "90", "low": "89", "high": "91", "close": "90"}})
    sell = r.orders[1]
    stop = sell.trigger_price
    assert sell.kind == STOP and sell.price == D(90) * D("0.9999") and sell.price < stop
    assert sell.created_at == at(10, 20)
    assert sell.slippage == ((stop - sell.price) * sell.quantity).quantize(D("0.00000001")) > 0


def test_cloture_forcee_a_23h55():
    start = at(22, 0)
    prices = [D(100)] * 20 + [D(100) - (i + 1) * D("0.05") for i in range(10)] + [D("99.6")] * 90
    data = ones(start, prices)
    cfg = BacktestConfig(full_window=False, limits=RiskLimits(max_trades=1))
    r = run(strat(), data, start, start + len(prices) * MIN, cfg)
    sell = r.orders[-1]
    assert sell.kind == SESSION_CLOSE and sell.created_at == at(23, 55)
    assert sell.price == D("99.6") * D("0.9999")
    assert r.trades[0].exit_reason == "cloture_session"


def test_pas_d_entree_apres_23h():
    start = at(22, 0)
    prices = [D(100)] * 62 + [D(100) - (i + 1) * D("0.05") for i in range(30)]
    r = run(strat(), ones(start, prices), start, start + len(prices) * MIN, LOOSE)
    assert r.orders == []
    ignored = [s for s in r.signals if s.kind == "entree"]
    assert ignored and all("apres_derniere_entree" in s.reasons for s in ignored)


def test_vwap_remis_a_zero_a_minuit():
    start = DAY - timedelta(hours=1)
    prices = [D(200)] * 60 + [D(100)] * 10  # la veille à 200, la session du jour à 100
    m = Market(ones(start, prices), "5m")
    view, _ = m.window(DAY + timedelta(minutes=5), full=False)
    s = strategies.validate({**RSI, "entree": [{"brique": "ecart_vwap", "cote": "sous", "seuil_pct": "0.1"}]})
    ev = strategies.evaluate(s, view)
    assert ev.values["ecart_vwap.ecart_vwap_pct"] == 0  # VWAP du jour = 100, pas la moyenne avec la veille


def test_trou_dans_la_fenetre_signal_ignore():
    prices = flat_then_fall(at(9))
    data = [c for i, c in enumerate(ones(at(9), prices)) if i != 30]  # minute 09:30 absente
    r = run(strat(), data, at(9), at(10, 30), BacktestConfig(full_window=False, limits=RiskLimits(max_trades=1)))
    assert r.orders == []
    assert r.ignored[backtest.PARTIAL_HISTORY] > 0


def test_fenetre_du_live_exigee_par_defaut():
    prices = flat_then_fall(at(9))
    r = run(strat(), ones(at(9), prices), at(9), at(10, 30))  # 90 minutes d'historique < 1439
    assert r.orders == [] and r.ignored[backtest.PARTIAL_HISTORY] > 0


def test_minute_d_execution_absente_signal_ignore():
    prices = flat_then_fall(at(9))
    data = [c for i, c in enumerate(ones(at(9), prices)) if i != 65]  # pas d'ouverture à 10:05
    r = run(strat(), data, at(9), at(10, 6), LOOSE)
    assert r.orders == [] and r.ignored[strategies.NO_FRESH_QUOTE] == 1


def test_garde_fou_delai_entre_entrees():
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 2 + [D("99.4") - D("0.05") * i for i in range(40)]
    r = run(strat(), ones(at(9), prices, {75: {"high": "200"}}), at(9), at(9) + len(prices) * MIN, LOOSE)
    buys = [o for o in r.orders if o.side is Side.BUY]
    assert len(buys) >= 2 and buys[1].created_at - buys[0].created_at >= timedelta(minutes=15)
    assert any(s.decision == "refuse" and "delai_entrees" in s.reasons for s in r.signals)


def test_sortie_sur_signal_a_l_ouverture_au_bid():
    s = strat(sortie=[{"brique": "rsi_extreme", "zone": "haut", "n": 2, "seuil": "60"}],
              stop={"type": "pct", "valeur": "2"}, objectif={"type": "pct", "valeur": "5"})
    prices = flat_then_fall(at(9), fall=10) + [D("99.5") + D("0.05") * (i + 1) for i in range(20)]
    data = ones(at(9), prices)
    r = run(s, data, at(9), at(9) + len(prices) * MIN, BacktestConfig(full_window=False, limits=RiskLimits(max_trades=1)))
    sell = r.orders[1]
    assert sell.kind == "marche" and sell.created_at.second == 1
    assert sell.price == data[int((sell.created_at - at(9)) // MIN)].open * D("0.9999")
    assert r.trades[0].exit_reason == "signal"


# --- Pas de regard vers le futur ---


def poisoned(data: list[Candle], after: datetime) -> list[Candle]:
    return [c if c.open_time < after else Candle(c.pair, "1m", c.open_time, D(1), D(10**6), D(1), D(1), D(10**6),
                                                  D(10**6), 1, "binance") for c in data]


def test_futur_empoisonne_memes_decisions():
    prices = flat_then_fall(at(9), fall=10) + [D("99.5")] * 10 + [D("99.4") - D("0.05") * i for i in range(40)]
    data = ones(at(9), prices, {75: {"high": "200"}})
    end = at(10, 40)
    clean = run(strat(), data, at(9), end, LOOSE)
    dirty = run(strat(), poisoned(data, end), at(9), end, LOOSE)
    assert clean.evaluations == dirty.evaluations
    assert clean.orders == dirty.orders and clean.signals == dirty.signals
    assert clean.orders  # le scénario trade


def test_decision_ne_lit_que_le_passe():
    """Chaque décision en T : la fenêtre construite sur toutes les données = celle construite sans rien après T."""
    data = ones(at(9), flat_then_fall(at(9), fall=60))
    full = Market(data, "5m")
    s = strat()
    for k in range(13, 24):
        t = at(9) + k * timedelta(minutes=5)
        cut = Market(poisoned(data, t), "5m")
        a, b = full.window(t, False), cut.window(t, False)
        assert a[1] == b[1]
        assert strategies.evaluate(s, a[0]) == strategies.evaluate(s, b[0])


def test_donnees_d_une_autre_paire_refusees():
    with pytest.raises(Exception, match="Backtest"):
        run(strat(paire="ETHUSDT"), ones(at(9), [D(100)] * 10), at(9), at(9, 10))


def test_perte_max_verifiee_au_plus_bas_de_la_minute():
    """Plus bas de la minute sous la limite de perte, clôture remontée au-dessus : la session est arrêtée."""
    prices = flat_then_fall(at(9), fall=10) + [D("99.50")] * 40
    limits = RiskLimits(max_session_loss_pct=D("0.1"))  # 10 USDT sur 10 000 : position de 1 000 qui perd ~1 %
    cfg = BacktestConfig(full_window=False, limits=limits)
    s = strat(stop={"type": "pct", "valeur": "2"}, objectif={"type": "pct", "valeur": "5"})
    dip = {80: {"low": "98.3", "close": "99.5"}}  # ~ −1,4 % au plus bas, clôture inchangée
    r = run(s, ones(at(9), prices, dip), at(9), at(9) + len(prices) * MIN, cfg)
    assert r.halts == [DAY.date()]
    assert len(r.orders) == 1  # pas de stop touché : la position reste ouverte
    calm = run(s, ones(at(9), prices), at(9), at(9) + len(prices) * MIN, cfg)
    assert calm.halts == []  # sans le creux, pas d'arrêt
