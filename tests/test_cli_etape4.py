"""Commandes CLI de l'étape 4 : ordres en attente, comptes, session (prix simulés, base temporaire)."""

import pytest

from simtrade.cli import main
from simtrade.feeds import StaticFeed
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def run(tmp_path):
    db = tmp_path / "cli4.db"

    def _run(*argv):
        return main(list(argv), service=TradingService(Storage(db), StaticFeed(PRICES)))

    return _run


def test_stop_ordres_et_annulation(run, capsys):
    run("acheter", "btc", "1000")
    capsys.readouterr()
    assert run("stop", "btc", "70000") == 0
    out = capsys.readouterr().out
    assert "Ordre stop posé" in out and "70 000,00" in out and "tout" in out and "API tourne" in out
    assert run("ordres") == 0
    assert "en_attente" in capsys.readouterr().out
    assert run("vendre", "btc", "tout") == 1
    assert "réservée" in capsys.readouterr().err
    assert run("annuler", "1") == 0
    assert "ordre n°1 (stop BTCUSDT)" in capsys.readouterr().out
    assert run("ordres", "--tous") == 0
    assert "annule (annulation)" in capsys.readouterr().out


def test_stop_objectif_et_limite(run, capsys):
    run("acheter", "eth", "1000")
    capsys.readouterr()
    assert run("stop-objectif", "eth", "2200", "2600") == 0
    assert "liés" in capsys.readouterr().out
    assert run("limite", "sol", "achat", "90", "200") == 0
    assert "Ordre limite posé" in capsys.readouterr().out
    assert run("objectif", "eth", "2700") == 1
    assert "Toute la position ETHUSDT est réservée" in capsys.readouterr().err
    run("acheter", "sol", "500")
    capsys.readouterr()
    assert run("objectif", "sol", "90") == 1  # sous le bid : partirait tout de suite
    assert "exécuté immédiatement" in capsys.readouterr().err
    assert run("stop", "sol", "80", "1") == 0
    assert run("stop", "sol", "70") == 1
    assert "précise une quantité (au plus 3,99400619)" in capsys.readouterr().err


def test_comptes_en_parallele(run, capsys):
    assert run("comptes", "creer", "robot", "500", "--auto") == 0
    assert "Compte 'robot' (n°2, auto)" in capsys.readouterr().out
    assert run("entrer", "sol", "100", "--stop", "90", "--compte", "robot") == 0
    capsys.readouterr()
    assert run("comptes") == 0
    out = capsys.readouterr().out
    assert "* principal" in out and "robot" in out and "400,00 USDT" in out
    assert run("portefeuille", "-c", "robot") == 0
    assert "(robot, auto)" in capsys.readouterr().out
    assert run("comptes", "choisir", "robot") == 0
    assert run("comptes", "archiver", "principal") == 0
    assert run("comptes", "archiver", "robot") == 1
    assert "compte courant" in capsys.readouterr().err


def test_session(run, capsys):
    assert run("session") == 0
    assert "23:00 UTC" in capsys.readouterr().out
    assert run("session", "22:00", "23:50") == 0
    assert "clôture forcée à 23:50 UTC" in capsys.readouterr().out
    assert run("session", "22:00") == 1
    assert run("session", "23:59", "23:50") == 1
    assert run("info") == 0
    assert "dernière entrée 22:00, clôture 23:50" in capsys.readouterr().out
