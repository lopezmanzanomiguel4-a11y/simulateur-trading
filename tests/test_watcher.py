"""Ordres en attente exécutés par le flux live (hub -> watcher -> service), sans réseau."""

from decimal import Decimal

import pytest

from etape4 import Env
from simtrade.errors import OrderRejected
from simtrade.models import CANCELLED, FILLED, LIMIT, PENDING, STOP, TARGET, Side
from simtrade.service import TradingService
from simtrade.storage import Storage

D = Decimal


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "w.db")
    e.quote("100")
    e.run(e.service.buy("btc", "1001"))  # 10 BTC à 100, 1 USDT de frais
    yield e
    e.close()


def test_stop_execute_quand_le_bid_le_touche(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.quote("99")
    assert env.storage.orders()[-1].side is Side.BUY  # rien d'exécuté au-dessus du stop
    env.quote("98", "98.2")

    order = env.storage.orders()[-1]
    assert (order.kind, order.side, order.price, order.quantity) == (STOP, Side.SELL, D("98"), D("10"))
    assert order.trigger_price == D("98") and order.slippage == 0 and order.pending_id == stop.id
    assert order.cash_flow == D("980") - D("0.98")
    done = env.storage.get_pending(stop.id)
    assert done.status == FILLED and done.order_id == order.id
    assert env.storage.load_portfolio().positions == {}
    assert env.watcher.index == {}


def test_stop_saute_execute_au_prix_disponible_avec_glissement(env):
    env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.quote("95", "95.1")  # le prix saute par-dessus le stop

    order = env.storage.orders()[-1]
    assert order.price == D("95")  # pas 98 : on vend au bid disponible
    assert order.slippage == D("30")  # (98 - 95) x 10
    assert order.realized_pnl == D("950") - D("0.95") - D("1001")


def test_achat_limite_execute_a_l_ask_avec_amelioration(env):
    env.quote("2010", "2010.5", pair="ETHUSDT")
    pending = env.run(env.service.place_order("eth", LIMIT, "achat", "2000", "1001"))
    env.quote("2001", "2001.5", pair="ETHUSDT")
    assert env.storage.get_pending(pending.id).status == PENDING
    env.quote("1990", "1990.5", pair="ETHUSDT")

    order = env.storage.orders()[-1]
    assert (order.kind, order.side, order.price) == (LIMIT, Side.BUY, D("1990.5"))
    expected = ((D("1990.5") - D("2000")) * order.quantity).quantize(D("0.00000001"))
    assert order.slippage == expected < 0  # négatif : amélioration
    assert order.cash_flow >= D("-1001")


def test_stop_objectif_lies_l_objectif_annule_le_stop(env):
    stop, target = env.run(env.service.place_bracket("btc", "95", "110"))
    assert stop.group_id == target.group_id == stop.id
    env.quote("111")

    assert env.storage.orders()[-1].kind == TARGET
    assert env.storage.get_pending(target.id).status == FILLED
    cancelled = env.storage.get_pending(stop.id)
    assert (cancelled.status, cancelled.reason) == (CANCELLED, "oco")
    env.quote("90")  # le stop annulé ne part plus
    assert len(env.storage.orders()) == 2


def test_stop_objectif_lies_le_stop_annule_l_objectif(env):
    stop, target = env.run(env.service.place_bracket("btc", "95", "110", "4"))
    env.quote("94")
    order = env.storage.orders()[-1]
    assert (order.kind, order.quantity, order.slippage) == (STOP, D("4"), D("4"))
    assert env.storage.get_pending(target.id).status == CANCELLED
    assert env.storage.load_portfolio().positions["BTCUSDT"].quantity == D("6")


def test_cotation_perimee_ne_declenche_rien(env):
    env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.quote("90", age=6)  # plus de 5 s
    assert len(env.storage.orders()) == 1
    env.quote("91")  # première cotation fraîche : exécution à ce prix
    order = env.storage.orders()[-1]
    assert order.price == D("91") and order.slippage == D("70")


def test_ordres_reserves_bloquent_ventes_et_achats(env):
    env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    with pytest.raises(OrderRejected, match="réservée"):
        env.run(env.service.sell("btc", "1"))
    with pytest.raises(OrderRejected, match="réservée"):
        env.run(env.service.place_order("btc", TARGET, "vente", "120", "tout"))
    env.quote("2010", "2010.5", pair="ETHUSDT")
    env.run(env.service.place_order("eth", LIMIT, "achat", "1000", "8000"))
    with pytest.raises(OrderRejected, match="réservés par des ordres en attente"):
        env.run(env.service.buy("sol", "1000"))  # 8999 - 8000 = 999 disponibles


def test_annulation_libere_la_reservation(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    cancelled = env.service.cancel_order(stop.id)
    assert [o.status for o in cancelled] == [CANCELLED]
    assert env.watcher.index == {}
    env.run(env.service.sell("btc", "tout"))
    with pytest.raises(OrderRejected, match="n'est plus en attente"):
        env.service.cancel_order(stop.id)


def test_ordre_annule_par_une_autre_connexion_n_est_pas_execute(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    other = TradingService(Storage(env.path), env.feed)  # la CLI pendant que l'API tourne
    other.cancel_order(stop.id)
    other.storage.close()

    assert stop.id in [o.id for o in env.watcher.index["BTCUSDT"]]  # index pas encore rechargé
    env.quote("90")
    assert len(env.storage.orders()) == 1  # statut relu dans la transaction : rien d'exécuté
    env.watcher.refresh()
    assert env.watcher.index == {}


def test_ordre_pose_par_une_autre_connexion_est_pris_en_compte(env):
    other = TradingService(Storage(env.path), env.feed)
    env.run(other.place_order("btc", STOP, "vente", "98", "tout"))
    other.storage.close()

    assert env.watcher.index == {}
    env.watcher.refresh()  # PRAGMA data_version a changé
    env.quote("97")
    assert env.storage.orders()[-1].kind == STOP


def test_ordre_en_attente_survit_au_redemarrage(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.close()

    restarted = Env(env.path, env.clock)
    try:
        assert [o.id for o in restarted.watcher.index["BTCUSDT"]] == [stop.id]
        restarted.quote("96")  # le prix est passé sous le stop pendant l'arrêt
        order = restarted.storage.orders()[-1]
        assert (order.kind, order.price, order.slippage) == (STOP, D("96"), D("20"))
    finally:
        restarted.close()


def test_quantite_plafonnee_a_la_position(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "10"))
    # La position diminue sans passer par le service (cas défensif) : la vente est plafonnée.
    env.storage.conn.execute("UPDATE positions SET quantity = '4', cost_basis = '400.4'")
    env.quote("97")
    assert env.storage.orders()[-1].quantity == D("4")
    assert env.storage.get_pending(stop.id).status == FILLED


def test_position_disparue_annule_l_ordre(env):
    stop = env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.storage.conn.execute("DELETE FROM positions")
    env.quote("97")
    done = env.storage.get_pending(stop.id)
    assert (done.status, done.reason) == (CANCELLED, "plus_de_position")
    assert len(env.storage.orders()) == 1


def test_erreur_d_ecouteur_ne_coupe_pas_le_hub(env):
    def broken(quote):
        raise RuntimeError("panne")

    env.hub.listeners.insert(0, broken)
    env.run(env.service.place_order("btc", STOP, "vente", "98", "tout"))
    env.quote("97")  # l'écouteur cassé est journalisé, le watcher passe quand même
    assert env.storage.orders()[-1].kind == STOP
    assert env.hub.quotes["BTCUSDT"].bid == D("97")
