"""Sessions intraday : dernière entrée, clôture forcée des comptes auto, rattrapage. Horloge simulée."""

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from etape4 import Clock, Env
from simtrade.errors import InvalidInput, OrderRejected
from simtrade.models import AUTO, CANCELLED, LATE_SESSION_CLOSE, LIMIT, REJECTED, SESSION_CLOSE, TARGET, Side
from simtrade.sessions import SessionRules, entry_allowed, validate_rules

D = Decimal
UTC = timezone.utc
DAY = date(2026, 10, 28)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "s.db")
    e.service.create_account("robot", "10000", AUTO)
    e.quote("100")
    yield e
    e.close()


def robot_positions(env):
    return env.service.load_portfolio(compte="robot").positions


# --- Règles pures ---


def test_derniere_entree_exclue():
    rules = SessionRules()
    assert entry_allowed(datetime(2026, 10, 28, 22, 59, 59, tzinfo=UTC), rules)
    assert not entry_allowed(datetime(2026, 10, 28, 23, 0, tzinfo=UTC), rules)
    assert entry_allowed(datetime(2026, 10, 29, 0, 0, tzinfo=UTC), rules)


@pytest.mark.parametrize("entry, close", [("23:56", "23:55"), ("00:00", "23:55"), ("25:00", "23:55"), ("23h", "23:55"), ("22:00", "9:5")])
def test_horaires_invalides_refuses(entry, close):
    with pytest.raises(InvalidInput):
        validate_rules(entry, close)


# --- Entrées ---


def test_entree_auto_refusee_a_partir_de_23h(env):
    env.clock.set(22, 59, 59)
    env.quote("100")
    env.run(env.service.buy("btc", "100", compte="robot"))
    env.clock.set(23, 0)
    env.quote("100")
    with pytest.raises(OrderRejected, match="Hors session"):
        env.run(env.service.buy("btc", "100", compte="robot"))
    with pytest.raises(OrderRejected, match="Hors session"):
        env.run(env.service.place_order("btc", LIMIT, "achat", "90", "100", compte="robot"))
    env.run(env.service.sell("btc", "tout", compte="robot"))  # sortir reste permis


def test_compte_manuel_jamais_soumis_aux_sessions(env):
    env.clock.set(23, 30)
    env.quote("100")
    env.run(env.service.buy("btc", "100"))
    env.clock.set(23, 55)
    env.quote("100")
    assert env.run(env.scheduler.tick()) == []
    assert "BTCUSDT" in env.service.load_portfolio().positions


def test_achat_limite_auto_declenche_hors_session_est_rejete(env):
    pending = env.run(env.service.place_order("btc", LIMIT, "achat", "90", "500", compte="robot"))
    env.clock.set(23, 10)
    env.quote("89")
    done = env.storage.get_pending(pending.id)
    assert (done.status, done.reason) == (REJECTED, "hors_session")
    assert robot_positions(env) == {}


def test_horaires_parametrables(env):
    env.service.set_session_rules("22:00", "23:00")
    assert env.service.session_rules() == SessionRules(time(22, 0), time(23, 0))
    env.clock.set(22, 0)
    env.quote("100")
    with pytest.raises(OrderRejected, match="après 22:00 UTC"):
        env.run(env.service.buy("btc", "100", compte="robot"))


# --- Clôture ---


def test_cloture_de_fin_de_session(env):
    env.run(env.service.buy("btc", "1001", compte="robot"))
    target = env.run(env.service.place_order("btc", TARGET, "vente", "150", "tout", compte="robot"))
    env.run(env.service.buy("btc", "1001"))  # compte manuel : ne doit pas bouger

    env.clock.set(23, 54, 59)
    env.quote("104")
    assert env.run(env.scheduler.tick()) == []  # pas encore l'heure
    env.clock.set(23, 55)
    env.quote("105", "105.1")
    (closed,) = env.run(env.scheduler.tick())

    assert closed.account.name == "robot" and closed.day == DAY and not closed.late and closed.cancelled == 1
    (order,) = closed.orders
    assert (order.kind, order.side, order.price, order.quantity) == (SESSION_CLOSE, Side.SELL, D("105"), D("10"))
    assert robot_positions(env) == {}
    assert env.storage.get_pending(target.id).status == CANCELLED
    assert env.storage.get_pending(target.id).reason == "fin_de_session"
    robot = env.service.account("robot")
    assert env.storage.sessions(robot.id) == [(DAY, env.clock(), False)]
    assert "BTCUSDT" in env.service.load_portfolio().positions  # manuel intact

    env.clock.set(23, 58)
    env.quote("103")
    assert env.run(env.scheduler.tick()) == []  # pas de double clôture


def test_cloture_reportee_tant_que_la_cotation_est_perimee(env):
    env.run(env.service.buy("btc", "1001", compte="robot"))
    env.clock.set(23, 55)
    env.quote("105", age=10)
    assert env.run(env.scheduler.tick()) == []
    assert "BTCUSDT" in robot_positions(env)

    env.clock.set(23, 56)
    env.quote("102")
    (closed,) = env.run(env.scheduler.tick())
    assert closed.late and closed.orders[0].kind == LATE_SESSION_CLOSE and closed.orders[0].price == D("102")
    assert robot_positions(env) == {}


def test_rattrapage_si_l_api_etait_arretee_a_la_cloture(tmp_path):
    clock = Clock()
    first = Env(tmp_path / "r.db", clock)
    first.service.create_account("robot", "10000", AUTO)
    first.quote("100")
    first.run(first.service.buy("btc", "1001", compte="robot"))
    first.close()  # arrêt avant 23:55

    clock.set(8, 0, days=1)  # redémarrage le lendemain matin
    second = Env(tmp_path / "r.db", clock)
    try:
        second.quote("90")
        (closed,) = second.run(second.scheduler.tick())
        assert closed.day == DAY and closed.late
        assert closed.orders[0].kind == LATE_SESSION_CLOSE and closed.orders[0].price == D("90")
        assert second.service.load_portfolio(compte="robot").positions == {}

        clock.set(8, 1, days=1)  # nouvelle session : on peut entrer, rien n'est fermé avant 23:55
        second.quote("91")
        second.run(second.service.buy("btc", "500", compte="robot"))
        assert second.run(second.scheduler.tick()) == []
    finally:
        second.close()


def test_ordres_en_attente_seuls_annules_a_la_cloture(env):
    pending = env.run(env.service.place_order("btc", LIMIT, "achat", "90", "500", compte="robot"))
    env.clock.set(23, 55)
    (closed,) = env.run(env.scheduler.tick())  # aucune position : pas besoin de cotation
    assert closed.orders == [] and closed.cancelled == 1
    assert env.storage.get_pending(pending.id).status == CANCELLED
