"""API de l'étape 5 : entrée + stop, garde-fou, journal des refus, limites, boucle. Thread SQLite réel, sans réseau."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from simtrade.api import create_app
from simtrade.feeds import StaticFeed
from simtrade.hub import PriceHub
from simtrade.models import Quote, utcnow
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
    db = tmp_path / "api5.db"
    with TestClient(create_app(service=lambda feed: TradingService(Storage(db), feed), hub=hub)) as c:
        c.post("/comptes", json={"nom": "robot", "capital": "10000", "mode": "auto"})
        yield c


def push_quote(client, pair, bid, ask=None):
    quote = Quote(pair, D(bid), D(ask or bid), "Simulé", utcnow())
    client.portal.call(client.app.state.hub.update, quote)


def test_tout_le_capital_sans_stop_refuse_et_journalise(client):
    r = client.post("/ordres/achat", params={"compte": "robot"}, json={"paire": "btc", "montant": "10000"})
    assert r.status_code == 400
    assert "stop-loss obligatoire" in r.json()["erreur"] and "2 000,00 USDT" in r.json()["erreur"]
    (refus,) = client.get("/risque/refus", params={"compte": "robot"}).json()["refus"]
    assert refus["regles"] == ["stop_obligatoire", "taille_max"] and refus["moment"] == "marche"
    assert refus["montant"] == "10000" and refus["etat"]["valeur_compte"] == "10000.00"
    assert client.get("/info").json()["refus_session"] == 1
    assert client.get("/historique", params={"compte": "robot"}).json()["ordres"] == []


def test_entree_stop_execute_par_le_flux(client):
    r = client.post("/ordres/entree", params={"compte": "robot"},
                    json={"paire": "btc", "montant": "1500", "stop": "79000", "objectif": "82000"})
    assert r.status_code == 200
    stop, target = r.json()["protection"]
    assert (stop["type_ordre"], stop["protection"], stop["quantite"]) == ("stop", True, "tout")
    assert target["lie_a"] == stop["id"]

    r = client.delete(f"/ordres/{stop['id']}", params={"compte": "robot"})
    assert r.status_code == 400 and "ne peut pas être annulé" in r.json()["erreur"]
    r = client.put(f"/ordres/{stop['id']}/stop", json={"prix": "78000"})
    assert r.status_code == 400 and "vers le haut" in r.json()["erreur"]
    r = client.put(f"/ordres/{stop['id']}/stop", json={"prix": "79500"})
    assert r.status_code == 200 and r.json()["ordres"][0]["prix_declenchement"] == "79500"

    push_quote(client, "BTCUSDT", "79400", "79401")  # exécution dans le thread SQLite, avant la requête suivante
    last = client.get("/historique", params={"compte": "robot"}).json()["ordres"][-1]
    assert (last["type_ordre"], last["prix"], last["prix_declenchement"]) == ("stop", "79400", "79500")
    statuts = {o["id"]: o["statut"] for o in client.get("/ordres/attente", params={"compte": "robot", "tous": "true"}).json()["ordres"]}
    assert statuts[stop["id"]] == "execute" and statuts[target["id"]] == "annule"


def test_limites_du_compte(client):
    r = client.get("/comptes/robot/risque")
    assert r.json()["limites"] == {
        "taille_max_pct": "20", "perte_max_session_pct": "2", "entrees_max_session": 6,
        "positions_max": 2, "delai_entrees_s": 900, "stop_obligatoire": True,
    }
    r = client.put("/comptes/robot/risque", json={"taille_max_pct": "30", "delai_entrees": "5m"})
    assert r.status_code == 200 and r.json()["limites"]["taille_max_pct"] == "30"
    assert r.json()["limites"]["delai_entrees_s"] == 300
    r = client.put("/comptes/robot/risque", json={"positions_max": "0"})
    assert r.status_code == 400 and "Positions max invalide" in r.json()["erreur"]


def test_achat_limite_avec_stop_via_api(client):
    r = client.post("/ordres/limite", params={"compte": "robot"},
                    json={"paire": "sol", "sens": "achat", "prix": "99", "montant": "1000", "stop": "95"})
    assert r.status_code == 200 and r.json()["ordres"][0]["stop_attache"] == "95"
    r = client.post("/ordres/limite", params={"compte": "robot"},
                    json={"paire": "sol", "sens": "achat", "prix": "99", "montant": "1000"})
    assert r.status_code == 400 and "stop-loss obligatoire" in r.json()["erreur"]


def test_info_boucle(client):
    boucle = client.get("/info").json()["boucle"]
    assert set(boucle) == {"retard_max_ms", "dernier_retard_ms", "alertes", "seuil_ms"} and boucle["seuil_ms"] == 200
