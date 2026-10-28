"""Comptes multiples en parallèle : capital séparé, compte courant, reset et archivage."""

from decimal import Decimal

import pytest

from etape4 import Env
from simtrade.errors import InvalidInput
from simtrade.models import AUTO, CANCELLED, MANUAL, STOP

D = Decimal


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path / "a.db")
    e.quote("100")
    yield e
    e.close()


def test_deux_comptes_en_parallele_avec_leur_capital(env):
    a = env.service.create_account("a", "1000")
    b = env.service.create_account("b", "5000", AUTO)
    env.run(env.service.buy("btc", "500.5", compte="a"))

    assert env.service.account("a").cash == D("499.5")  # 5 BTC à 100 + 0,5 de frais
    assert env.service.account("b").cash == D("5000")
    assert env.service.account().cash == D("10000")  # principal, compte courant
    assert [x.name for x in env.service.open_accounts()] == ["principal", "a", "b"]
    assert (a.mode, b.mode) == (MANUAL, AUTO)
    assert env.service.load_portfolio(compte=a.id).positions["BTCUSDT"].quantity == D("5")


def test_choisir_le_compte_courant(env):
    env.service.create_account("perso", "2000")
    env.service.select_account("perso")
    env.run(env.service.buy("btc", "100.1"))
    assert env.service.account().name == "perso"
    assert env.service.account("perso").cash == D("1899.9")
    assert env.service.account("principal").cash == D("10000")
    current = env.storage.conn.execute("SELECT COUNT(*) FROM accounts WHERE active = 1").fetchone()[0]
    assert current == 1


def test_reset_n_archive_que_le_compte_vise(env):
    env.service.create_account("robot", "3000", AUTO)
    env.run(env.service.buy("btc", "501", compte="robot", stop="80"))  # <= 20 % de 3000 (garde-fou)
    stop = env.run(env.service.place_order("btc", STOP, "vente", "90", "tout", compte="robot"))
    old = env.service.account("robot")

    new = env.service.reset("4000", compte="robot")
    assert (new.name, new.mode, new.cash, new.current) == ("robot", AUTO, D("4000"), False)
    assert new.id != old.id
    assert env.service.account(old.id).archived
    assert (env.storage.get_pending(stop.id).status, env.storage.get_pending(stop.id).reason) == (CANCELLED, "reset")
    assert env.service.account().name == "principal"  # compte courant inchangé
    assert env.service.load_portfolio(compte="robot").positions == {}
    assert len(env.storage.orders(old.id)) == 1  # historique conservé


def test_nom_en_double_ou_invalide_refuse(env):
    env.service.create_account("robot")
    with pytest.raises(InvalidInput, match="déjà"):
        env.service.create_account("ROBOT")
    for bad in ("", "12", "a b", "x" * 33):
        with pytest.raises(InvalidInput, match="Nom de compte invalide"):
            env.service.create_account(bad)
    with pytest.raises(InvalidInput, match="Mode"):
        env.service.create_account("z", mode="semi")


def test_compte_archive_n_accepte_plus_d_ordre(env):
    robot = env.service.create_account("robot")
    env.service.archive_account("robot")
    with pytest.raises(InvalidInput, match="inconnu"):
        env.run(env.service.buy("btc", "100", compte="robot"))
    with pytest.raises(InvalidInput, match="archivé"):
        env.run(env.service.buy("btc", "100", compte=robot.id))
    env.service.create_account("robot")  # le nom est de nouveau libre


def test_archiver_le_compte_courant_refuse(env):
    with pytest.raises(InvalidInput, match="compte courant"):
        env.service.archive_account("principal")


def test_compte_inconnu(env):
    with pytest.raises(InvalidInput, match="Compte inconnu : 'fantome'"):
        env.service.account("fantome")
