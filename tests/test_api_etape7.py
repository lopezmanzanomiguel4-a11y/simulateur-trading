"""API de l'étape 7 : /journal, /stats (cohérents avec /historique), /catalogue. Thread SQLite réel, sans réseau."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from simtrade import api as api_mod
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
    db = tmp_path / "api7.db"
    with TestClient(create_app(service=lambda feed: TradingService(Storage(db), feed), hub=hub)) as c:
        c.post("/comptes", json={"nom": "m", "capital": "10000"})
        yield c


def test_journal_et_stats_coherents_avec_l_historique(client):
    client.post("/ordres/achat?compte=m", json={"paire": "BTCUSDT", "montant": "1000"})
    client.post("/ordres/vente?compte=m", json={"paire": "BTCUSDT", "quantite": "tout"})
    orders = client.get("/historique?compte=m").json()["ordres"]
    assert len(orders) == 2

    journal = client.get("/journal?compte=m").json()["journal"]
    assert [e["type"] for e in journal] == ["ordre", "ordre", "trade"]
    trade = journal[-1]["contenu"]
    assert trade["motif_sortie"] == "manuel" and trade["ordres"] == [o["id"] for o in orders]
    pnl = sum(D(o["flux_usdt"]) for o in orders)
    assert D(trade["pnl"]) == pnl
    assert D(trade["frais"]) == sum(D(o["frais"]) for o in orders)

    body = client.get("/stats?compte=m").json()
    s = body["stats"]
    assert s["trades"] == 1 and s["trades_gagnants"] == 0 and s["taux_reussite_pct"] == "0.0000"
    assert D(s["pnl"]) == pnl.quantize(D("0.0001"))  # position fermée : P&L = somme des flux
    assert D(s["frais"]) == sum(D(o["frais"]) for o in orders).quantize(D("0.0001"))
    assert s["sessions"] >= 1 and "sharpe" in s
    assert body["marquage"].startswith("clôture 1m")


def test_stats_parametres(client):
    assert client.get("/stats?compte=m&depuis=pas-une-date").status_code == 400
    assert client.get("/stats?compte=m&depuis=2030-01-02&jusqu_a=2030-01-01").status_code == 400
    body = client.get("/stats?compte=m&strategie=inconnue").json()
    assert body["stats"]["trades"] == 0


def test_catalogue_absent_puis_present(client, tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "CATALOGUE_PATH", tmp_path / "absent.json")
    assert client.get("/catalogue").json()["catalogue"] is None
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps({"version": 1, "entrees": []}), encoding="utf-8")
    monkeypatch.setattr(api_mod, "CATALOGUE_PATH", path)
    assert client.get("/catalogue").json()["catalogue"] == {"version": 1, "entrees": []}
