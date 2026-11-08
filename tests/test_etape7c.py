"""Étape 7c, lot A : comptes swing, stop suiveur, taille au risque, garde-fou swing, frais par compte.
Horloge simulée, sans réseau. L'intraday ne doit pas bouger."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from etape4 import Env
from simtrade import migrations, strategies
from simtrade.errors import InvalidInput, OrderRejected
from simtrade.models import (
    AUTO, FEE_BNB, FEE_STANDARD, FILLED, INTRADAY, MANUAL, PENDING, STOP, SWING, TRAILING, PendingOrder, Side,
)
from simtrade.orders import raise_trail, triggered
from simtrade.risk import (
    MAX_EXPOSURE, MAX_LOSS, MAX_WEEK_LOSS, SWING_LIMITS, EntryRequest, RiskLimits, RiskState, check_entry, risk_size,
)
from simtrade.storage import Storage
from conftest import quote as make_quote

D = Decimal
UTC = timezone.utc
NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "s.db")
    e.service.create_account("swing", "10000", AUTO, SWING)
    e.service.create_account("robot", "10000", AUTO)
    e.quote("100")
    yield e
    e.close()


def protection(env, name="swing", pair="BTCUSDT"):
    return env.storage.protection_orders(env.service.account(name).id, pair)


SWING_DEF = {
    "nom": "essai-swing",
    "horizon": "swing",
    "paire": "BTCUSDT",
    "intervalle": "1d",
    "entree": [{"brique": "au_dessus_ema", "n": 20}],
    "stop": {"type": "suiveur_pct", "valeur": "5"},
    "objectif": {"type": "aucun"},
    "taille": {"risque_pct": "1"},
}


# --- Migration v4 ---


def test_migration_v4_garde_les_comptes_en_intraday_a_0_1_pct(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    with monkeypatch.context() as m:
        m.setattr(migrations, "VERSION", 3)
        s = Storage(path)
        s.conn.execute(
            "INSERT INTO accounts (initial_capital, cash, created_at, active, name, mode) "
            "VALUES ('500', '500', '2026-01-01T00:00:00+00:00', 0, 'robot', 'auto')"
        )
        s.close()
    before = sqlite3.connect(path).execute("SELECT id, name, cash FROM accounts ORDER BY id").fetchall()
    s = Storage(path)
    try:
        assert migrations.version(s.conn) == 4
        assert s.backup_path is not None and s.backup_path.name.startswith("v3.db.avant-v4-")
        assert [(a.id, a.name, str(a.cash)) for a in s.accounts()] == before
        assert {(a.horizon, a.fee_rate) for a in s.accounts()} == {(INTRADAY, FEE_STANDARD)}
        assert s.risk_limits(s.account("robot").id) == RiskLimits()
    finally:
        s.close()


def test_migration_v4_en_echec_laisse_la_v3(tmp_path, monkeypatch):
    path = tmp_path / "v3.db"
    with monkeypatch.context() as m:
        m.setattr(migrations, "VERSION", 3)
        Storage(path).close()
    monkeypatch.setattr(migrations, "V4_STEPS", migrations.V4_STEPS + ["CREATE TABLE accounts (x)"])
    with pytest.raises(migrations.MigrationError):
        Storage(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 3


# --- Comptes ---


def test_compte_swing_et_frais(env):
    a = env.service.account("swing")
    assert (a.mode, a.horizon, a.fee_rate) == (AUTO, SWING, FEE_STANDARD)
    assert env.storage.risk_limits(a.id) == SWING_LIMITS
    bnb = env.service.create_account("bnb", "1000", AUTO, SWING, "0.075")
    assert bnb.fee_rate == FEE_BNB and env.storage.load_portfolio(bnb.id).fee_rate == FEE_BNB
    with pytest.raises(InvalidInput):
        env.service.create_account("x", "1000", AUTO, SWING, "0.05")
    with pytest.raises(InvalidInput):
        env.service.create_account("y", "1000", MANUAL, SWING)
    reset = env.service.reset("2000", compte="bnb")
    assert (reset.horizon, reset.fee_rate) == (SWING, FEE_BNB)


def test_frais_bnb_appliques_a_l_achat(env):
    env.service.create_account("bnb", "10000", AUTO, SWING, "0.075")
    result = env.run(env.service.buy("btc", "1000", compte="bnb", stop="90"))
    assert result.order.fee == (result.order.gross * FEE_BNB).quantize(D("0.00000001"))


def test_compte_swing_garde_sa_position_apres_23h55_et_entre_apres_23h(env):
    env.run(env.service.buy("btc", "1000", compte="swing", stop="90"))
    env.run(env.service.buy("btc", "1000", compte="robot", stop="90"))
    env.clock.set(23, 30)
    env.quote("101")
    with pytest.raises(OrderRejected, match="Hors session"):
        env.run(env.service.buy("btc", "100", compte="robot", stop="90"))
    env.clock.set(23, 55)
    env.quote("102")
    closed = env.run(env.scheduler.tick())
    assert [c.account.name for c in closed] == ["robot"]
    env.clock.set(1, 0, days=2)
    env.quote("103")
    assert env.run(env.scheduler.tick()) == []
    swing = env.service.account("swing")
    assert "BTCUSDT" in env.storage.load_portfolio(swing.id).positions
    assert [o.kind for o in protection(env)] == [STOP]


def test_entree_swing_acceptee_apres_l_heure_limite(env):
    env.clock.set(23, 30)
    env.quote("100")
    result = env.run(env.service.buy("btc", "1000", compte="swing", stop="90"))
    assert result.order.side is Side.BUY


# --- Stop suiveur : règles pures ---


def trail(level="95", high="100", distance="5"):
    return PendingOrder(1, "BTCUSDT", TRAILING, Side.SELL, D(level), None, None, NOW, protection=True,
                        trail_distance=D(distance), trail_high=D(high))


def test_stop_suiveur_monte_et_ne_descend_jamais():
    assert raise_trail(trail(), D("108")) == (D("108"), D("103"))
    assert raise_trail(trail(), D("99")) is None
    assert raise_trail(trail(level="103", high="108"), D("104")) is None
    assert raise_trail(PendingOrder(1, "BTCUSDT", STOP, Side.SELL, D("95"), None, None, NOW), D("200")) is None


def test_stop_suiveur_declenche_sur_le_bid():
    order = trail()
    assert triggered(order, make_quote(bid="95", ask="96"))
    assert not triggered(order, make_quote(bid="95.01", ask="94"))


# --- Stop suiveur : live ---


def test_entree_avec_stop_suiveur(env):
    env.quote("100", "100.1")
    env.run(env.service.buy("btc", "1000", compte="swing", stop="95", trailing=True))
    (stop,) = protection(env)
    assert (stop.kind, stop.trigger_price, stop.trail_distance, stop.trail_high) == (TRAILING, D("95"), D("5"), D("100"))
    with pytest.raises(OrderRejected, match="ne peut pas être annulé"):
        env.service.cancel_order(stop.id, compte="swing")


def test_stop_suiveur_releve_a_la_cloture_de_l_heure(env):
    env.clock.set(10, 0)
    env.quote("100")
    env.run(env.service.buy("btc", "1000", compte="swing", stop="95", trailing=True))
    env.clock.set(10, 20)
    env.quote("110")  # plus haut de l'heure, stop pas encore relevé
    env.clock.set(10, 40)
    env.quote("106")
    assert protection(env)[0].trigger_price == D("95")
    env.clock.set(11, 0, 5)
    env.quote("104")  # bougie 10:00 close : stop relevé à 110 - 5
    (stop,) = protection(env)
    assert (stop.trigger_price, stop.trail_high) == (D("105"), D("110"))
    env.clock.set(11, 30)
    env.quote("104.9")  # sous le nouveau niveau : sortie au bid
    swing = env.service.account("swing")
    assert "BTCUSDT" not in env.storage.load_portfolio(swing.id).positions
    sell = env.service.history(compte="swing")[1][-1]
    assert (sell.kind, sell.price, sell.trigger_price) == (TRAILING, D("104.9"), D("105"))


def test_stop_suiveur_teste_avant_d_etre_releve(env):
    env.clock.set(10, 0)
    env.quote("100")
    env.run(env.service.buy("btc", "1000", compte="swing", stop="95", trailing=True))
    env.clock.set(10, 30)
    env.quote("110")
    env.clock.set(11, 0, 1)
    env.quote("94")  # premier prix de l'heure suivante sous l'ANCIEN niveau : exécuté à 94, pas relevé avant
    sell = env.service.history(compte="swing")[1][-1]
    assert (sell.kind, sell.price, sell.trigger_price) == (TRAILING, D("94"), D("95"))


# --- Taille au risque ---


def test_taille_au_risque_formule_et_plafonds():
    # 1 % de 10 000 = 100 de risque, distance 5 : 20 unités x 100 x 1,001 = 2 002
    assert risk_size(D("10000"), D("10000"), D("100"), D("95"), D("1"), D("50"), FEE_STANDARD) == D("2002.00")
    # plafond de la taille max du garde-fou : 20 % de 10 000
    assert risk_size(D("10000"), D("10000"), D("100"), D("99"), D("1"), D("20"), FEE_STANDARD) == D("2000.00")
    # plafond du cash libre
    assert risk_size(D("10000"), D("700"), D("100"), D("95"), D("1"), D("50"), FEE_STANDARD) == D("700.00")
    with pytest.raises(OrderRejected, match="sous l'entrée"):
        risk_size(D("10000"), D("10000"), D("100"), D("100"), D("1"), D("20"), FEE_STANDARD)


def test_strategie_swing_validee_et_relue_a_l_identique():
    s = strategies.validate(SWING_DEF)
    assert (s.horizon, s.intervalle, s.risque_pct, s.trailing) == (SWING, "1d", D("1"), True)
    assert strategies.validate(s.to_json()) == s and s.to_json()["horizon"] == "swing"
    assert "heures_actives" not in s.to_json()
    with pytest.raises(InvalidInput, match="heures_actives"):
        strategies.validate({**SWING_DEF, "heures_actives": {"debut": "06:00"}})
    intraday = {k: v for k, v in SWING_DEF.items() if k != "horizon"} | {"intervalle": "1h"}
    with pytest.raises(InvalidInput, match="stop.type"):  # stop suiveur : swing seulement
        strategies.validate(intraday)
    with pytest.raises(InvalidInput, match="objectif.type"):  # objectif 'aucun' : swing seulement
        strategies.validate(intraday | {"stop": {"type": "pct"}})
    with pytest.raises(InvalidInput, match="pas les deux"):
        strategies.validate({**SWING_DEF, "taille": {"risque_pct": "1", "pct_capital": "10"}})
    model = strategies.validate(strategies.model("tendance-ema"))
    assert model.horizon == INTRADAY and "horizon" not in model.to_json()


def test_strategie_et_compte_de_meme_horizon(env):
    with pytest.raises(InvalidInput, match="même horizon"):
        env.service.activate_strategy(compte="robot", definition=SWING_DEF)
    with pytest.raises(InvalidInput, match="même horizon"):
        env.service.activate_strategy(compte="swing", modele="tendance-ema")
    account, record, strategy = env.service.activate_strategy(compte="swing", definition=SWING_DEF)
    assert strategy.swing and record.account_id == account.id


def test_signal_swing_taille_au_risque_et_stop_suiveur(env):
    env.quote("100", "100")
    _, record, _ = env.service.activate_strategy(compte="swing", definition=SWING_DEF)
    plan = strategies.Plan(D("5"), None, None, None, trailing=True, swing=True)
    quotes = env.service._peek(["BTCUSDT"])
    signal = env.service.handle_entry_signal(record.id, NOW, plan, quotes, {})
    assert signal.decision == "ordre", signal
    # risque 1 % de 10 000 sur 5 de distance = 20 BTC à 100 ; plafonné à 20 % du compte = 2 000 USDT
    assert signal.details["montant"] == "2000.00" and signal.details["objectif"] is None
    (stop,) = protection(env)
    assert (stop.kind, stop.trigger_price, stop.trail_distance) == (TRAILING, D("95"), D("5"))


# --- Garde-fou swing ---


def state(**kw):
    base = dict(equity=D("10000"), session_pnl=D(0), entries=0)
    return RiskState(**{**base, **kw})


def request(amount="1000"):
    return EntryRequest("BTCUSDT", D(amount), D("100"), D("90"), "marche", NOW)


def test_perte_semaine_remplace_la_perte_de_session():
    rules = [r.rule for r in check_entry(request(), state(week_pnl=D("-420"), session_pnl=D("-420")), SWING_LIMITS)]
    assert MAX_WEEK_LOSS in rules and MAX_LOSS not in rules
    assert check_entry(request(), state(week_pnl=D("-300"), session_pnl=D("-300")), SWING_LIMITS) == []
    # intraday inchangé : la perte de session s'applique, pas la semaine
    rules = [r.rule for r in check_entry(request(), state(week_pnl=D("-420"), session_pnl=D("-420")), RiskLimits())]
    assert MAX_LOSS in rules and MAX_WEEK_LOSS not in rules


def test_exposition_totale_max():
    exposure = {"ETHUSDT": D("2000"), "SOLUSDT": D("2000"), "XRPUSDT": D("1500")}
    rules = [r.rule for r in check_entry(request("1000"), state(exposure=exposure), SWING_LIMITS)]
    assert rules == [MAX_EXPOSURE]
    assert check_entry(request("500"), state(exposure=exposure), SWING_LIMITS) == []
    assert check_entry(request("1000"), state(exposure=exposure), RiskLimits(max_positions=4)) == []


def test_perte_semaine_glissante_dans_le_service(env):
    env.run(env.service.buy("btc", "2000", compte="swing", stop="50"))
    env.quote("75")
    env.run(env.service.sell("btc", "tout", compte="swing"))  # ≈ -500 réalisés
    with pytest.raises(OrderRejected, match="7 jours glissants"):
        env.run(env.service.buy("btc", "100", compte="swing", stop="50"))
    env.clock.now += timedelta(days=7, minutes=1)
    env.quote("75")
    result = env.run(env.service.buy("btc", "100", compte="swing", stop="50"))  # sortie de la fenêtre
    assert result.order.side is Side.BUY


def test_limites_swing_modifiables_seulement_en_swing(env):
    _, limits = env.service.set_risk_limits({"semaine": "3", "exposition": "50"}, compte="swing")
    assert (limits.max_week_loss_pct, limits.max_exposure_pct) == (D("3"), D("50"))
    assert env.storage.risk_limits(env.service.account("swing").id) == limits
    with pytest.raises(InvalidInput, match="réservée aux comptes swing"):
        env.service.set_risk_limits({"semaine": "3"}, compte="robot")


def test_strategie_swing_hors_paires_live_refusee_a_l_activation(env):
    definition = {**SWING_DEF, "paire": "BNBUSDT"}
    assert strategies.validate(definition).paire == "BNBUSDT"  # admise au backtest
    with pytest.raises(InvalidInput, match="pas suivie en direct"):
        env.service.activate_strategy(compte="swing", definition=definition)