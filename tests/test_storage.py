"""Persistance SQLite, reset et chemin de la base."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import quote
from simtrade.config import db_path
from simtrade.errors import InvalidCapital
from simtrade.storage import Storage

D = Decimal


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def trade(storage, pair="BTCUSDT", amount="1001", ask="100"):
    with storage.transaction():
        pf = storage.load_portfolio()
        return storage.save_trade(pf, pf.buy(quote(pair, ask=ask), amount))


def test_premier_lancement_cree_un_compte_a_10000(storage):
    account = storage.active_account()
    assert account.initial_capital == D("10000")
    assert account.cash == D("10000")


def test_portefeuille_survit_a_un_redemarrage(tmp_path):
    path = tmp_path / "persist.db"
    s = Storage(path)
    trade(s)
    with s.transaction():
        pf = s.load_portfolio()
        s.save_trade(pf, pf.sell(quote(bid="110"), "4"))
    s.close()

    s = Storage(path)
    pf = s.load_portfolio()
    assert pf.account.cash == D("8999") + D("439.56")
    assert pf.positions["BTCUSDT"].quantity == D("6")
    assert pf.positions["BTCUSDT"].cost_basis == D("600.6")
    assert pf.realized_pnl == D("39.16")
    assert pf.fees_paid == D("1.44")
    orders = s.orders()
    assert [o.side.value for o in orders] == ["achat", "vente"]
    assert orders[0].fee == D("1")
    s.close()


def test_vente_totale_supprime_la_position(storage):
    trade(storage)
    with storage.transaction():
        pf = storage.load_portfolio()
        storage.save_trade(pf, pf.sell(quote(bid="100"), "tout"))
    assert storage.load_portfolio().positions == {}


def test_reset_par_defaut_10000(storage):
    storage.reset(D("500"))
    account = storage.reset()
    assert account.initial_capital == D("10000")


def test_reset_avec_capital_et_date(storage):
    when = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
    trade(storage)
    account = storage.reset("2500", now=when)

    reloaded = storage.active_account()
    assert reloaded.id == account.id
    assert reloaded.initial_capital == D("2500")
    assert reloaded.cash == D("2500")
    assert reloaded.created_at == when
    pf = storage.load_portfolio()
    assert pf.positions == {}
    assert pf.realized_pnl == 0
    assert pf.fees_paid == 0


def test_reset_archive_l_historique(storage):
    old = storage.active_account()
    trade(storage)
    storage.reset("2500")
    assert storage.orders() == []  # historique du compte actif vide
    assert len(storage.orders(old.id)) == 1  # ancien ordre conservé
    accounts = storage.accounts()
    assert len(accounts) == 2


@pytest.mark.parametrize("raw", ["0", "-1", "10000000.01"])
def test_reset_refuse_capital_invalide_sans_rien_changer(storage, raw):
    before = storage.active_account()
    with pytest.raises(InvalidCapital):
        storage.reset(raw)
    assert storage.active_account() == before
    assert len(storage.accounts()) == 1


def test_un_seul_compte_actif(storage):
    for capital in ("100", "200", "300"):
        storage.reset(capital)
    actifs = storage.conn.execute("SELECT COUNT(*) FROM accounts WHERE active = 1").fetchone()[0]
    assert actifs == 1
    assert storage.active_account().initial_capital == D("300")


def test_echec_en_cours_de_transaction_annule_tout(storage):
    with pytest.raises(RuntimeError):
        with storage.transaction():
            pf = storage.load_portfolio()
            storage.save_trade(pf, pf.buy(quote(ask="100"), "1001"))
            raise RuntimeError("panne")
    assert storage.load_portfolio().account.cash == D("10000")
    assert storage.orders() == []


# --- Chemin de la base ---


def test_chemin_par_variable_d_environnement(monkeypatch, tmp_path):
    target = tmp_path / "ailleurs" / "ma.db"
    monkeypatch.setenv("SIMTRADE_DB", str(target))
    assert db_path() == target
    Storage(db_path()).close()
    assert target.exists()  # dossier parent créé automatiquement


def test_chemin_par_defaut_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMTRADE_DB", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert db_path() == tmp_path / "simtrade" / "simtrade.db"
