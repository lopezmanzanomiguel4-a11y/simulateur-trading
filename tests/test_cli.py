"""Commandes CLI de bout en bout, avec prix simulés et base temporaire."""

from decimal import Decimal

import pytest

from simtrade.cli import main
from simtrade.feeds import StaticFeed
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def run(tmp_path):
    db = tmp_path / "cli.db"

    def _run(*argv):
        return main(list(argv), service=TradingService(Storage(db), StaticFeed(PRICES)))

    _run.db = db
    return _run


def cash(db):
    s = Storage(db)
    try:
        return s.active_account().cash
    finally:
        s.close()


def test_prix_once(run, capsys):
    assert run("prix", "--once") == 0
    out = capsys.readouterr().out
    assert "BTCUSDT" in out and "80 001,00" in out


def test_achat_puis_vente(run, capsys):
    assert run("acheter", "sol", "1000") == 0
    out = capsys.readouterr().out
    assert "Achat exécuté" in out and "Frais" in out
    assert run("vendre", "SOLUSDT", "tout") == 0
    assert "P&L réalisé" in capsys.readouterr().out
    assert run("historique") == 0
    out = capsys.readouterr().out
    assert "achat" in out and "vente" in out and "frais totaux" in out


def test_portefeuille_affiche_la_performance(run, capsys):
    run("acheter", "btc", "5000")
    capsys.readouterr()
    assert run("portefeuille") == 0
    out = capsys.readouterr().out
    assert "Performance totale" in out and "BTCUSDT" in out


def test_refus_affiche_un_message_clair(run, capsys):
    assert run("vendre", "eth", "1") == 1
    assert "Aucune position sur ETHUSDT" in capsys.readouterr().err
    assert run("acheter", "eth", "20000") == 1
    assert "Solde insuffisant" in capsys.readouterr().err


def test_reset_annule_sans_confirmation(run, monkeypatch, capsys):
    run("acheter", "btc", "1000")
    before = cash(run.db)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run("reset", "2500") == 0
    assert "Annulé" in capsys.readouterr().out
    assert cash(run.db) == before < Decimal("10000")


def test_reset_confirme(run, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "o")
    assert run("reset", "2500") == 0
    assert cash(run.db) == Decimal("2500")


def test_reset_par_defaut_avec_yes(run):
    run("reset", "100", "--yes")
    assert run("reset", "--yes") == 0
    assert cash(run.db) == Decimal("10000")


def test_reset_capital_absurde_refuse_avant_confirmation(run, monkeypatch, capsys):
    def no_input(_):
        raise AssertionError("ne doit pas demander de confirmation")

    monkeypatch.setattr("builtins.input", no_input)
    assert run("reset", "20000000") == 1
    assert "Capital trop élevé" in capsys.readouterr().err
    assert cash(run.db) == Decimal("10000")
