"""Garde-fou branché sur le service : entrées filtrées, sorties jamais bloquées. Horloge simulée, sans réseau."""

from datetime import timedelta
from decimal import Decimal

import pytest

from etape4 import Env
from simtrade.errors import OrderRejected
from simtrade.models import AUTO, CANCELLED, FILLED, LIMIT, PENDING, REJECTED, SESSION_CLOSE, STOP, TARGET, Side
from simtrade.risk import MAX_LOSS, MAX_POSITIONS, MAX_SIZE, MAX_TRADES, RiskRefused

D = Decimal


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "r.db")
    e.service.create_account("robot", "10000", AUTO)
    e.quote("100")
    yield e
    e.close()


def enter(env, amount="1001", stop="95", pair="btc", target=None):
    return env.run(env.service.buy(pair, amount, compte="robot", stop=stop, target=target))


def protection(env, pair="BTCUSDT"):
    robot = env.service.account("robot")
    return env.storage.protection_orders(robot.id, pair)


def robot_orders(env):
    return env.service.history(compte="robot")[1]


def later(env, minutes):
    env.clock.now += timedelta(minutes=minutes)


# --- Entrée + stop ---


def test_entree_pose_le_stop_de_protection(env):
    result = enter(env, target="110")
    assert result.order.side is Side.BUY and result.order.quantity == D("10")
    stop, target = protection(env)
    assert (stop.kind, stop.trigger_price, stop.quantity, stop.protection) == (STOP, D("95"), None, True)
    assert (target.kind, target.trigger_price, target.group_id) == (TARGET, D("110"), stop.id)


def test_tout_le_capital_sans_stop_refuse_et_journalise(env):
    with pytest.raises(RiskRefused) as err:
        env.run(env.service.buy("btc", "10000", compte="robot"))
    assert err.value.rules == ["stop_obligatoire", "taille_max"]
    assert robot_orders(env) == []
    (record,) = env.service.refusals(compte="robot")
    assert record.rules == ("stop_obligatoire", "taille_max") and record.moment == "marche"
    assert record.state["valeur_compte"] == "10000.00" and record.amount == D("10000")


def test_echec_du_stop_annule_l_achat(env):
    with pytest.raises(OrderRejected, match="exécuté immédiatement"):
        enter(env, stop="95", target="99")  # objectif sous le bid : refusé après l'achat simulé
    assert robot_orders(env) == [] and protection(env) == []
    assert env.service.account("robot").cash == D("10000")


def test_compte_manuel_jamais_filtre(env):
    env.run(env.service.buy("btc", "10000"))  # tout le capital, sans stop
    assert env.service.refusals() == []


# --- Achat limite avec stop ---


def test_achat_limite_pose_le_stop_a_l_execution(env):
    pending = env.run(env.service.place_order("btc", LIMIT, "achat", "95", "1000", compte="robot", stop="90", target="110"))
    assert (pending.stop_price, pending.target_price) == (D("90"), D("110"))
    assert protection(env) == []
    env.quote("94.9", "95")
    assert env.storage.get_pending(pending.id).status == FILLED
    stop, target = protection(env)
    assert (stop.trigger_price, target.trigger_price) == (D("90"), D("110"))


def test_achat_limite_sans_stop_refuse_sur_compte_auto(env):
    with pytest.raises(RiskRefused, match="stop-loss obligatoire"):
        env.run(env.service.place_order("btc", LIMIT, "achat", "95", "1000", compte="robot"))
    assert env.service.refusals(compte="robot")[0].moment == "pose_limite"


def test_achat_limite_rejete_si_le_stop_est_deja_touche(env):
    pending = env.run(env.service.place_order("btc", LIMIT, "achat", "95", "1000", compte="robot", stop="90"))
    env.quote("89", "89.1")  # le prix traverse la limite ET le stop
    done = env.storage.get_pending(pending.id)
    assert (done.status, done.reason) == (REJECTED, "stop_deja_touche")
    assert robot_orders(env) == []
    assert env.service.refusals(compte="robot")[0].rules == ("stop_deja_touche",)


def test_achat_limite_rejete_au_declenchement_par_le_garde_fou(env):
    env.service.set_risk_limits({"perte": "0.3"}, compte="robot")
    env.quote("2000", pair="ETHUSDT")
    pending = env.run(env.service.place_order("eth", LIMIT, "achat", "1950", "500", compte="robot", stop="1900"))
    enter(env)  # BTC
    env.quote("96.5", "96.6")  # BTC : latent -36 < -30 -> arrêt de session
    env.run(env.service.record_halts())
    env.quote("1949", "1950", pair="ETHUSDT")  # la limite ETH est touchée après l'arrêt
    done = env.storage.get_pending(pending.id)
    assert (done.status, done.reason) == (REJECTED, "garde_fou:perte_max_session")
    assert env.service.refusals(compte="robot")[0].moment == "declenchement_limite"
    assert [o.pair for o in robot_orders(env)] == ["BTCUSDT"]


# --- Stop de protection : remplacement, annulation, déplacement ---


def test_nouvelle_entree_remplace_le_stop(env):
    enter(env, amount="500", stop="95")
    old = protection(env)[0]
    later(env, 15)
    env.quote("100")
    enter(env, amount="500", stop="97")
    (current,) = protection(env)
    assert current.trigger_price == D("97")
    replaced = env.storage.get_pending(old.id)
    assert (replaced.status, replaced.reason) == (CANCELLED, "remplace")


def test_annulation_du_dernier_stop_refusee_objectif_seul_accepte(env):
    enter(env, target="110")
    stop, target = protection(env)
    with pytest.raises(OrderRejected, match="ne peut pas être annulé tant qu'elle est ouverte"):
        env.service.cancel_order(stop.id)
    assert env.storage.get_pending(stop.id).status == PENDING
    cancelled = env.service.cancel_order(target.id)
    assert [o.id for o in cancelled] == [target.id]
    assert env.storage.get_pending(stop.id).status == PENDING


def test_deplacer_le_stop_seulement_vers_le_haut(env):
    enter(env)
    (stop,) = protection(env)
    moved = env.run(env.service.move_stop(stop.id, "97"))
    assert moved.trigger_price == D("97") and env.storage.get_pending(stop.id).trigger_price == D("97")
    with pytest.raises(OrderRejected, match="ne se déplace que vers le haut"):
        env.run(env.service.move_stop(stop.id, "96"))
    with pytest.raises(OrderRejected, match="exécuté immédiatement"):
        env.run(env.service.move_stop(stop.id, "100.5"))
    env.quote("96.9")
    assert robot_orders(env)[-1].kind == STOP  # le stop déplacé est bien celui qui part


def test_vente_toujours_possible_malgre_le_stop(env):
    enter(env)
    env.run(env.service.sell("btc", "4", compte="robot"))  # le stop ne réserve rien
    (stop,) = protection(env)
    assert stop.status == PENDING
    env.run(env.service.sell("btc", "tout", compte="robot"))
    assert protection(env) == []
    closed = env.storage.get_pending(stop.id)
    assert (closed.status, closed.reason) == (CANCELLED, "position_fermee")


def test_stop_de_protection_vend_toute_la_position(env):
    enter(env)
    env.quote("94", "94.1")
    sell = robot_orders(env)[-1]
    assert (sell.kind, sell.quantity, sell.price, sell.slippage) == (STOP, D("10"), D("94"), D("10"))


# --- Perte max, arrêt de session, sorties jamais bloquées ---


def breach(env):
    """Perte max à 0,3 % (30 USDT) ; bid 96,5 -> latent 965 - 1001 = -36."""
    env.service.set_risk_limits({"perte": "0.3"}, compte="robot")
    enter(env)
    env.quote("96.5", "96.6")
    env.quote("2000", pair="ETHUSDT")
    (halted,) = env.run(env.service.record_halts())
    assert halted.name == "robot"


def test_stop_part_meme_apres_la_perte_max(env):
    breach(env)
    with pytest.raises(RiskRefused) as err:
        enter(env, pair="eth", amount="100", stop="1")
    assert MAX_LOSS in err.value.rules
    env.quote("94.9")
    sell = robot_orders(env)[-1]
    assert sell.kind == STOP and sell.side is Side.SELL


def test_entree_refusee_apres_arret_meme_si_le_prix_remonte(env):
    breach(env)
    env.quote("100.5")  # le latent redevient positif
    later(env, 20)
    env.quote("100.5")
    with pytest.raises(RiskRefused) as err:
        enter(env, amount="100", stop="99")
    assert err.value.rules == [MAX_LOSS]


def test_cloture_de_session_jamais_bloquee(env):
    env.service.set_risk_limits({"trades": "1"}, compte="robot")
    breach(env)
    with pytest.raises(RiskRefused) as err:
        enter(env, pair="eth", amount="100", stop="1")
    assert {MAX_LOSS, MAX_TRADES} <= set(err.value.rules)
    env.clock.set(23, 55)
    env.quote("96.5")
    (closed,) = env.run(env.scheduler.tick())
    assert [o.kind for o in closed.orders] == [SESSION_CLOSE]
    assert env.service.load_portfolio(compte="robot").positions == {}


def test_session_suivante_de_nouveau_ouverte(env):
    breach(env)
    env.clock.set(23, 55)
    env.quote("96.5")
    env.run(env.scheduler.tick())
    env.clock.set(9, 0, days=1)
    env.quote("100")
    enter(env, amount="500", stop="95")  # nouvel arrêt non hérité


# --- Fréquence et nombre de positions ---


def test_delai_entre_deux_entrees(env):
    enter(env, amount="500")
    later(env, 10)
    env.quote("100")
    with pytest.raises(RiskRefused, match="encore 5 min"):
        enter(env, amount="500")
    later(env, 5)
    env.quote("100")
    enter(env, amount="500")


def test_positions_max(env):
    env.quote("2000", pair="ETHUSDT")
    env.quote("100", pair="SOLUSDT")
    enter(env, pair="btc", amount="1000")
    enter(env, pair="eth", amount="1000", stop="1900")
    with pytest.raises(RiskRefused) as err:
        enter(env, pair="sol", amount="1000", stop="90")
    assert err.value.rules == [MAX_POSITIONS]


def test_prix_perime_d_une_position(env):
    enter(env)
    env.quote("100", age=6)  # BTC périmé
    env.quote("2000", pair="ETHUSDT")
    with pytest.raises(RiskRefused, match="pas de cotation fraîche pour BTCUSDT"):
        enter(env, pair="eth", amount="500", stop="1900")


def test_taille_max_via_le_service(env):
    with pytest.raises(RiskRefused) as err:
        enter(env, amount="2500")
    assert err.value.rules == [MAX_SIZE]
    env.service.set_risk_limits({"taille": "30"}, compte="robot")
    enter(env, amount="2500")
    assert env.service.risk_limits(compte="robot")[1].max_position_pct == D("30")


def test_reset_conserve_les_limites(env):
    env.service.set_risk_limits({"taille": "50"}, compte="robot")
    env.service.reset("5000", compte="robot")
    assert env.service.risk_limits(compte="robot")[1].max_position_pct == D("50")
