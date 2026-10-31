"""API de l'étape 6 : modèles, activation (une seule stratégie active par compte), journal des signaux,
exécuteur lancé dans le lifespan. Thread SQLite réel, sans réseau."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from etape6 import TEST_STRATEGY
from simtrade.api import create_app
from simtrade.feeds import StaticFeed
from simtrade.hub import PriceHub
from simtrade.service import TradingService
from simtrade.storage import Storage

D = Decimal
PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def client(tmp_path):
    source = StaticFeed(PRICES)
    hub = PriceHub(source)
    for quote in source.quotes.values():
        hub.update(quote)
    db = tmp_path / "api6.db"
    with TestClient(create_app(service=lambda feed: TradingService(Storage(db), feed), hub=hub)) as c:
        c.post("/comptes", json={"nom": "robot", "capital": "10000", "mode": "auto"})
        yield c


def test_modeles_et_schema(client):
    body = client.get("/strategies").json()
    assert [m["nom"] for m in body["modeles"]] == ["cassure-range", "retour-vwap", "tendance-ema"]
    assert body["schema"]["objectif_minimum_pct"] == "0.3"
    assert body["schema"]["taille"]["pct_capital"] == {"type": "decimal", "defaut": "10", "min": "1", "max": "20"}


def test_activer_un_modele_avec_surcharges(client):
    r = client.put("/comptes/robot/strategie", json={"modele": "tendance-ema", "parametres": {"stop.multiple": "2.5", "croisement_ema.rapide": 8}})
    assert r.status_code == 200
    definition = r.json()["strategie"]["definition"]
    assert definition["stop"] == {"type": "atr", "multiple": "2.5", "n": 14}
    assert definition["entree"][0]["rapide"] == 8
    got = client.get("/comptes/robot/strategie").json()
    assert got["strategie"]["nom"] == "tendance-ema" and got["strategie"]["desactivee_le"] is None
    assert client.get("/info").json()["strategies"]["actives"] == [{"compte_id": 2, "nom": "tendance-ema"}]


def test_une_seule_strategie_active_par_compte(client):
    first = client.put("/comptes/robot/strategie", json={"modele": "tendance-ema"}).json()["strategie"]
    second = client.put("/comptes/robot/strategie", json={"definition": TEST_STRATEGY}).json()["strategie"]
    assert second["nom"] == "test-rsi" and second["id"] != first["id"]
    assert client.get("/comptes/robot/strategie").json()["strategie"]["id"] == second["id"]
    r = client.delete("/comptes/robot/strategie")
    assert r.status_code == 200 and r.json()["desactivee"]["raison"] == "desactivee"
    assert client.get("/comptes/robot/strategie").json()["strategie"] is None


def test_refus_compte_manuel_et_definition_invalide(client):
    r = client.put("/comptes/principal/strategie", json={"modele": "retour-vwap"})
    assert r.status_code == 400 and "manuel" in r.json()["erreur"]
    r = client.put("/comptes/robot/strategie", json={"modele": "retour-vwap", "parametres": {"objectif.type": "pct", "objectif.valeur": "0.2"}})
    assert r.status_code == 400 and "'objectif.valeur' hors bornes" in r.json()["erreur"]
    r = client.put("/comptes/robot/strategie", json={"modele": "inconnu"})
    assert r.status_code == 400 and "Modèle de stratégie inconnu" in r.json()["erreur"]
    r = client.put("/comptes/robot/strategie", json={})
    assert r.status_code == 400


def test_signaux_vides_et_filtre_invalide(client):
    assert client.get("/signaux").json() == {"signaux": []}
    r = client.get("/signaux", params={"decision": "achat"})
    assert r.status_code == 400 and "Décision inconnue" in r.json()["erreur"]


def test_signal_expose_par_la_route(client):
    """Un signal traité par le service (thread SQLite) apparaît dans GET /signaux."""
    from datetime import datetime, timezone

    from simtrade.strategies import Plan

    act = client.put("/comptes/robot/strategie", json={"definition": TEST_STRATEGY}).json()["strategie"]
    svc = client.app.state.service
    hub = client.app.state.hub
    candle = datetime(2026, 10, 28, 11, 55, tzinfo=timezone.utc)
    quotes = dict(hub.quotes)
    client.portal.call(svc.run, svc.handle_entry_signal, act["id"], candle, Plan(D("0.5"), None, None, D("2")), quotes, {"strategie": "test-rsi"})
    (sig,) = client.get("/signaux", params={"compte": "robot"}).json()["signaux"]
    assert (sig["type"], sig["decision"], sig["bougie"]) == ("entree", "ordre", "2026-10-28T11:55:00+00:00")
    assert sig["details"]["stop"] == "79600.99" and sig["ordre_id"] == 1
    assert client.get("/signaux", params={"decision": "refuse"}).json()["signaux"] == []
    assert client.get("/info").json()["strategies"]["evaluations"] >= 0


def test_reset_desactive_la_strategie(client):
    client.put("/comptes/robot/strategie", json={"modele": "retour-vwap"})
    client.post("/reset", params={"compte": "robot"}, json={"capital": "5000"})
    assert client.get("/comptes/robot/strategie").json()["strategie"] is None
