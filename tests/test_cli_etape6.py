"""CLI de l'étape 6 : strategies, strategie (activer, desactiver), signaux. Base temporaire, sans réseau."""

import json

import pytest

from etape6 import TEST_STRATEGY
from simtrade.cli import main
from simtrade.feeds import StaticFeed
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def run(tmp_path, capsys):
    db = tmp_path / "cli6.db"

    def _run(*argv):
        return main(list(argv), service=TradingService(Storage(db), StaticFeed(PRICES)))

    _run("comptes", "creer", "robot", "10000", "--auto")
    capsys.readouterr()
    return _run


def test_strategies_liste_les_modeles(run, capsys):
    assert run("strategies") == 0
    out = capsys.readouterr().out
    assert "retour-vwap : BTCUSDT 5m" in out and "tendance-ema" in out and "cassure-range" in out
    assert '"objectif_minimum_pct": "0.3"' in out


def test_activer_afficher_desactiver(run, capsys):
    assert run("strategie", "activer", "retour-vwap", "-c", "robot", "stop.multiple=2", "rsi_extreme.seuil=35") == 0
    out = capsys.readouterr().out
    assert "Stratégie 'retour-vwap' activée sur le compte 'robot'" in out and '"seuil": "35"' in out
    assert run("strategie", "-c", "robot") == 0
    assert "stratégie 'retour-vwap' active depuis" in capsys.readouterr().out
    assert run("strategie", "desactiver", "-c", "robot") == 0
    assert "désactivée" in capsys.readouterr().out
    assert run("strategie", "-c", "robot") == 0
    assert "Aucune stratégie active" in capsys.readouterr().out


def test_activer_depuis_un_fichier(run, capsys, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(TEST_STRATEGY), encoding="utf-8")
    assert run("strategie", "activer", "--fichier", str(path), "-c", "robot") == 0
    assert "'test-rsi' activée" in capsys.readouterr().out


def test_refus_clairs(run, capsys):
    assert run("strategie", "activer", "retour-vwap", "-c", "principal") == 1
    assert "manuel" in capsys.readouterr().err
    assert run("strategie", "activer", "retour-vwap", "-c", "robot", "objectif.type=pct", "objectif.valeur=0.2") == 1
    assert "'objectif.valeur' hors bornes : 0.2 (de 0.3 à 10)" in capsys.readouterr().err
    assert run("strategie", "activer", "retour-vwap", "-c", "robot", "seuil") == 1
    assert "clé=valeur" in capsys.readouterr().err


def test_signaux_vide(run, capsys):
    assert run("signaux") == 0
    assert "Aucun signal." in capsys.readouterr().out
