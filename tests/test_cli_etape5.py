"""CLI de l'étape 5 : entrer, risque, refus, deplacer-stop (prix simulés, base temporaire)."""

import pytest

from simtrade.cli import main
from simtrade.feeds import StaticFeed
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def run(tmp_path, capsys):
    db = tmp_path / "cli5.db"

    def _run(*argv):
        return main(list(argv), service=TradingService(Storage(db), StaticFeed(PRICES)))

    _run("comptes", "creer", "robot", "10000", "--auto")
    capsys.readouterr()
    return _run


def test_entrer_puis_deplacer_et_annuler(run, capsys):
    assert run("entrer", "btc", "1500", "--stop", "79000", "--objectif", "82000", "-c", "robot") == 0
    out = capsys.readouterr().out
    assert "Achat exécuté" in out and "Protection de la position" in out and "79 000,00" in out
    assert run("annuler", "1", "-c", "robot") == 1
    assert "ne peut pas être annulé" in capsys.readouterr().err
    assert run("deplacer-stop", "1", "78000", "-c", "robot") == 1
    assert "vers le haut" in capsys.readouterr().err
    assert run("deplacer-stop", "1", "79500", "-c", "robot") == 0
    assert "déplacé à 79 500,00" in capsys.readouterr().out
    assert run("annuler", "2", "-c", "robot") == 0  # l'objectif seul
    assert run("vendre", "btc", "tout", "-c", "robot") == 0  # la sortie n'est jamais bloquée


def test_acheter_sans_stop_refuse_puis_journal(run, capsys):
    assert run("acheter", "btc", "10000", "--compte", "robot") == 1
    err = capsys.readouterr().err
    assert "garde-fou" in err and "stop-loss obligatoire" in err
    assert run("refus") == 0
    out = capsys.readouterr().out
    assert "stop_obligatoire, taille_max" in out and "Dernier refus" in out


def test_risque_affiche_et_modifie(run, capsys):
    assert run("risque", "-c", "robot") == 0
    out = capsys.readouterr().out
    assert "20,00 % de la valeur du compte" in out and "15 min 00 s" in out
    assert run("risque", "-c", "robot", "taille=30", "delai=5m") == 0
    out = capsys.readouterr().out
    assert "Limites enregistrées" in out and "30,00 %" in out and "5 min 00 s" in out
    assert run("risque", "-c", "robot", "taille") == 1
    assert run("risque", "-c", "robot", "levier=3") == 1
    assert "Limite inconnue" in capsys.readouterr().err
    assert run("risque") == 0
    assert "compte manuel" in capsys.readouterr().out


def test_limite_avec_stop(run, capsys):
    assert run("limite", "sol", "achat", "99", "1000", "--stop", "95", "-c", "robot") == 0
    assert "stop de protection à 95,00" in capsys.readouterr().out
