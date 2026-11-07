"""Exécuteur des stratégies, branché sur le service et le hub comme dans l'API. Horloge simulée,
bougies synthétiques (klines), sans réseau."""

from datetime import timedelta

import pytest

from etape4 import Clock
from etape6 import DAY, D, ORIGIN_LIVE, TEST_STRATEGY, Market6, candle, falling, minutes, series
from simtrade import strategies
from simtrade.bricks import MarketView
from simtrade.hub import ClockOffset
from simtrade.models import STOP, TARGET, Side
from simtrade.runner import StrategyRunner

NOON = DAY.replace(hour=12)


@pytest.fixture
def m(tmp_path):
    market = Market6(tmp_path / "s.db")
    yield market
    market.close()


def ready(m, end=NOON, closes=None):
    """20 bougies 5m en baisse (RSI 0 : signal d'entrée) finissant à `end`, VWAP de session 100."""
    m.scenario(end, closes or falling(20, "100", "0.05"))
    return m.quote("99.04", "99.05")


def orders(m, compte="robot"):
    return m.service.history(compte=compte)[1]


def signals(m):
    return m.service.signals()


def protection(m):
    robot = m.service.account("robot")
    return {o.kind: o for o in m.env.storage.protection_orders(robot.id, "BTCUSDT")}


# --- Signal -> un ordre ---


def test_signal_passe_un_ordre_avec_stop_et_objectif(m):
    m.auto()
    ready(m)
    m.tick()
    (sig,) = signals(m)
    (order,) = orders(m)
    assert (sig.kind, sig.decision, sig.order_id, sig.reasons) == ("entree", "ordre", order.id, ())
    assert sig.candle_open_time == NOON - timedelta(minutes=5)
    assert order.side is Side.BUY and order.price == D("99.05")
    # 10 % de la valeur du compte (10 000), frais inclus
    assert sig.details["montant"] == "1000.00" and order.gross + order.fee <= D("1000")
    prot = protection(m)
    # stop pct 0,5 % sous l'ask, arrondi au centime inférieur ; objectif r 2 = 2 x la distance réelle du stop
    assert prot[STOP].trigger_price == D("98.55") == D(sig.details["stop"])
    assert prot[TARGET].trigger_price == D(sig.details["objectif"]) == D("100.05")
    assert D(sig.details["valeurs"]["rsi_extreme.rsi"]) == 0
    state = m.runner.states[m.service.account("robot").id]
    assert (state.entry, state.decision) == (True, "ordre")


def test_un_seul_ordre_par_signal_meme_avec_plusieurs_ticks(m):
    m.auto()
    ready(m)
    for _ in range(3):
        m.tick()
    m.clock.now += timedelta(seconds=30)
    m.quote("99.04", "99.05")
    m.tick()
    assert len(orders(m)) == 1 and len(signals(m)) == 1


def test_un_seul_ordre_apres_redemarrage(tmp_path):
    path = tmp_path / "r.db"
    first = Market6(path)
    first.auto()
    ready(first)
    first.tick()
    assert len(orders(first)) == 1
    # Nouvel exécuteur (mémoire vide) sur le même service
    first.runner = StrategyRunner(first.service, first.hub)
    first.tick()
    assert len(orders(first)) == 1
    first.close()
    # Redémarrage complet : nouvelle connexion, nouveau hub, mêmes bougies, même heure
    second = Market6(path, Clock(first.clock.now))
    ready(second)
    second.tick()
    assert len(orders(second)) == 1 and len(signals(second)) == 1
    second.close()


def test_panne_entre_l_achat_et_le_commit_puis_un_seul_ordre(m, monkeypatch):
    m.auto()
    ready(m)
    real = m.env.storage.add_signal
    calls = []

    def broken(signal):
        calls.append(signal)
        if len(calls) == 1:
            raise RuntimeError("panne simulée après l'achat")
        return real(signal)

    monkeypatch.setattr(m.env.storage, "add_signal", broken)
    m.tick()
    # L'achat était déjà écrit dans la transaction : le ROLLBACK l'a effacé, avec son stop.
    assert orders(m) == [] and signals(m) == [] and protection(m) == {}
    assert m.service.load_portfolio(compte="robot").account.cash == D("10000")
    m.tick()  # même bougie, nouvel essai : un seul ordre
    assert len(orders(m)) == 1 and len(signals(m)) == 1 and len(calls) == 2


# --- Aucun signal si la qualité est mauvaise ---


QUALITY_CASES = {
    "prix_perime": lambda m: m.quote("99.04", "99.05", age=10),
    "flux_transactions_muet": lambda m: setattr(m.hub.trade_status, "connected", False),
    "horloge_decalee": lambda m: setattr(m.hub, "clock_offset", ClockOffset(2.0, 0.05, m.clock())),
    "horloge_non_mesuree": lambda m: setattr(m.hub, "clock_offset", None),
    "historique_partiel": lambda m: m.hub.history_state.__setitem__(("BTCUSDT", "5m"), "partiel"),
    "historique_indisponible": lambda m: m.hub.history_state.__setitem__(("BTCUSDT", "1m"), "indisponible"),
    "repli_coinbase": lambda m: setattr(m.hub, "trade_source", "Coinbase"),
}


@pytest.mark.parametrize("reason", list(QUALITY_CASES))
def test_aucun_ordre_si_la_qualite_est_mauvaise(m, reason):
    m.auto()
    ready(m)
    QUALITY_CASES[reason](m)
    m.tick()
    assert orders(m) == []
    (sig,) = signals(m)
    assert sig.decision == "ignore" and reason in sig.reasons


def test_aucun_ordre_hors_des_heures_actives(m):
    m.auto(definition={**TEST_STRATEGY, "heures_actives": {"debut": "13:00", "fin": "22:00"}})
    ready(m)
    m.tick()
    assert orders(m) == [] and signals(m)[0].reasons == ("hors_heures",)


def test_aucun_ordre_apres_la_derniere_entree(m):
    m.auto()
    m.service.set_session_rules("11:00", "23:55")
    ready(m)
    m.tick()
    assert orders(m) == [] and signals(m)[0].reasons == ("apres_derniere_entree",)


def test_pas_de_renfort_d_une_position(m):
    m.auto()
    ready(m)
    m.tick()
    end = NOON + timedelta(minutes=5)
    m.load(series(NOON, ["98.95"], spread="0.1") + minutes(NOON, 5, "100"))
    m.clock.now = end + timedelta(seconds=3)
    m.healthy()
    m.quote("98.94", "98.95")
    m.tick()
    assert len(orders(m)) == 1
    assert signals(m)[0].reasons == ("deja_en_position",)


# --- Garde-fou ---


def test_refus_du_garde_fou_journalise_sans_nouvel_essai(m):
    m.auto()
    m.service.set_risk_limits({"taille": "5"}, compte="robot")  # la stratégie demande 10 %
    ready(m)
    m.tick()
    assert orders(m) == []
    (sig,) = signals(m)
    (refusal,) = m.service.refusals(compte="robot")
    assert (sig.decision, sig.reasons, sig.refusal_id) == ("refuse", ("taille_max",), refusal.id)
    assert refusal.amount == D("1000.00") and refusal.moment == "marche"
    assert "garde-fou" in sig.details["message"]
    # Même bougie : ni nouvel essai ni nouveau refus, même 30 s plus tard
    m.tick()
    m.clock.now += timedelta(seconds=30)
    m.quote("99.04", "99.05")
    m.tick()
    assert len(m.service.refusals(compte="robot")) == 1 and len(signals(m)) == 1
    # Bougie suivante : une nouvelle décision (une par bougie au plus)
    m.load(series(NOON, ["98.95"], spread="0.1") + minutes(NOON, 5, "100"))
    m.clock.now = NOON + timedelta(minutes=5, seconds=3)
    m.healthy()
    m.quote("98.94", "98.95")
    m.tick()
    assert len(m.service.refusals(compte="robot")) == 2 and orders(m) == []


def test_sortie_sur_signal_meme_apres_la_perte_max(m):
    m.auto(definition={**TEST_STRATEGY, "objectif": {"type": "pct", "valeur": "5"}})  # objectif loin : pas touché
    ready(m)
    m.tick()
    robot = m.service.account("robot")
    m.env.storage.halt_session(robot.id, NOON.date(), m.clock(), "perte_max_session")
    # Retour au-dessus du VWAP (100) : signal de sortie
    m.load(series(NOON, ["100.1"], spread="0.1") + minutes(NOON, 5, "100"))
    m.clock.now = NOON + timedelta(minutes=5, seconds=3)
    m.healthy()
    m.quote("100.09", "100.1")
    m.tick()
    buy, sell = orders(m)
    assert sell.side is Side.SELL and sell.price == D("100.09")
    exit_sig = next(s for s in signals(m) if s.kind == "sortie")
    assert (exit_sig.decision, exit_sig.order_id) == ("ordre", sell.id)
    assert exit_sig.details["briques_sortie"] == ["retour_vwap"]
    assert m.service.load_portfolio(compte="robot").positions == {}
    assert protection(m) == {}  # stop et objectif annulés avec la position
    assert m.service.refusals(compte="robot") == []  # la sortie n'est jamais passée par le garde-fou


# --- Bougie définitive, bougie ancienne ---


def test_attend_la_kline_puis_ignore_une_bougie_non_definitive(m):
    m.auto()
    ready(m)
    m.hub.market.series[("BTCUSDT", "5m")].candles[-1].origin = ORIGIN_LIVE
    m.tick()
    assert signals(m) == [] and orders(m) == []  # on attend la kline
    m.clock.now += timedelta(seconds=10)
    m.healthy()
    m.quote("99.04", "99.05")
    m.tick()
    assert signals(m) == []
    m.clock.now = NOON + timedelta(seconds=21)
    m.healthy()
    m.quote("99.04", "99.05")
    m.tick()
    (sig,) = signals(m)
    assert sig.decision == "ignore" and sig.reasons == ("bougie_non_definitive",) and orders(m) == []


def test_la_kline_arrivee_pendant_l_attente_permet_l_ordre(m):
    m.auto()
    ready(m)
    last = m.hub.market.series[("BTCUSDT", "5m")].candles[-1]
    last.origin = ORIGIN_LIVE
    m.tick()
    last.origin = "binance"  # remplacée par la kline
    m.clock.now += timedelta(seconds=5)
    m.quote("99.04", "99.05")
    m.tick()
    assert len(orders(m)) == 1


def test_pas_de_rattrapage_d_une_bougie_ancienne(m):
    m.auto()
    ready(m)
    m.clock.now = NOON + timedelta(minutes=5, seconds=3)  # aucune bougie depuis 5 min (API arrêtée, flux coupé)
    m.healthy()
    m.quote("99.04", "99.05")
    m.tick()
    assert signals(m) == [] and orders(m) == []


def test_strategie_desactivee_plus_rien(m):
    m.auto()
    m.service.deactivate_strategy(compte="robot")
    ready(m)
    m.tick()
    assert signals(m) == [] and m.runner.states == {}


# --- Le même code en live et au rejeu (backtest) ---


def walk(n: int, seed: int = 7) -> list[D]:
    """Marche pseudo-aléatoire déterministe (générateur congruentiel), autour de 100."""
    x, price, out = seed, D("100"), []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2**31
        price += D((x % 41) - 20) / 100
        out.append(price)
    return out


def test_rejeu_identique_au_live(m):
    definition = {
        **strategies.model("retour-vwap"),
        "nom": "rejeu",
        "entree": [
            {"brique": "ecart_vwap", "cote": "sous", "seuil_pct": "0.1"},
            {"brique": "rsi_extreme", "zone": "bas", "n": 14, "seuil": "45"},
            {"brique": "volatilite", "n": 14, "min_pct": "0.05", "max_pct": "5"},
        ],
        "sortie": [{"brique": "retour_vwap"}],
        "heures_actives": {"debut": "00:00", "fin": "23:00"},
    }
    strategy = strategies.validate(definition)
    m.auto(definition=definition)
    closes = walk(80)
    start = DAY.replace(hour=6)
    five = series(start, closes, spread="0.15")
    ones = [candle(c.open_time + timedelta(minutes=i), c.close, "1m", volume=str(1 + i)) for c in five for i in range(5)]

    # Rejeu (comme le backtest) : evaluate bougie par bougie, vue datée de la clôture
    replay = []
    for k in range(len(five)):
        view = MarketView(five[k].close_time, five[: k + 1], ones[: 5 * (k + 1)])
        ev = strategies.evaluate(strategy, view)
        replay.append((ev.candle_open, ev.entry, ev.exit))

    # Live : les bougies arrivent une à une dans le hub, l'exécuteur tourne à chaque clôture
    account = m.service.account("robot")
    live = []
    for k, c in enumerate(five):
        m.load([c] + ones[5 * k : 5 * (k + 1)])
        m.clock.now = c.close_time + timedelta(seconds=3)
        m.healthy()
        m.quote(str(c.close - D("0.01")), str(c.close))
        m.tick()
        state = m.runner.states[account.id]
        live.append((state.candle, state.entry, state.exit))

    assert live == replay
    assert any(e for _, e, _ in replay) and not all(e for _, e, _ in replay)  # le scénario produit des signaux


def test_backtest_identique_au_live(m):
    """Étape 7 : backtest.run prend les mêmes décisions que le runner live, bougie par bougie, et passe les mêmes
    ordres quand le live est coté comme le backtest (ouverture de la minute suivante, ± s/2)."""
    from simtrade import backtest, trades
    from simtrade.market import aggregate
    from simtrade.models import MARKET

    definition = {
        **strategies.model("retour-vwap"),
        "nom": "parite",
        "entree": [
            {"brique": "ecart_vwap", "cote": "sous", "seuil_pct": "0.1"},
            {"brique": "rsi_extreme", "zone": "bas", "n": 14, "seuil": "45"},
        ],
        "sortie": [{"brique": "retour_vwap"}],
        "stop": {"type": "pct", "valeur": "2"},
        "objectif": {"type": "pct", "valeur": "5"},
        "heures_actives": {"debut": "00:00", "fin": "23:00"},
    }
    strategy = strategies.validate(definition)
    m.auto(definition=definition)
    start = DAY.replace(hour=6)
    prices = walk(401, seed=3)
    ones = []
    for i in range(400):
        o, c = prices[i], prices[i + 1]
        ones.append(candle(start + timedelta(minutes=i), c, "1m", open_=o, high=max(o, c) + D("0.03"),
                           low=min(o, c) - D("0.03"), volume=str(1 + i % 5)))
    five = aggregate(ones, "5m")
    config = backtest.BacktestConfig(full_window=False)
    h = config.half_spread

    # Live : à chaque clôture, cotation = ouverture de la minute suivante ± s/2
    account = m.service.account("robot")
    live = []
    for k, c in enumerate(five[:-1]):
        m.load([c] + ones[5 * k : 5 * (k + 1)])
        m.clock.now = c.close_time + timedelta(seconds=3)
        m.healthy()
        nxt = ones[5 * (k + 1)].open
        m.quote(str(backtest._price(nxt * (1 - h))), str(backtest._price(nxt * (1 + h))))
        m.tick()
        state = m.runner.states[account.id]
        live.append((state.candle, state.entry, state.exit))

    result = backtest.run(strategy, ones, start, five[-2].close_time + timedelta(minutes=1), config)
    assert result.evaluations == live

    live_orders = m.service.history(compte="robot")[1]
    key = lambda o: (o.side, o.kind, o.price, o.quantity, o.created_at.replace(second=0))  # live T+3 s, backtest T+1 s
    assert [key(o) for o in result.orders] == [key(o) for o in live_orders]
    assert len(live_orders) >= 2 and all(o.kind == MARKET for o in live_orders)  # entrées et sorties sur signal
    live_trades = trades.trades_from_orders(live_orders, trades.links_from_signals(m.service.signals()))
    assert [t.exit_reason for t in result.trades] == [t.exit_reason for t in live_trades]
    assert [t.pnl for t in result.trades] == [t.pnl for t in live_trades]
