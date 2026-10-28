"""API HTTP de bout en bout et flux partagé, avec prix simulés, base temporaire et sans réseau."""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from simtrade.api import create_app
from simtrade.errors import FeedUnavailable
from simtrade.feeds import PriceFeed, StaticFeed
from simtrade.hub import CachedFeed, PriceHub
from simtrade.models import Quote, utcnow
from simtrade.service import TradingService
from simtrade.storage import Storage

D = Decimal
PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


class SilentFeed(PriceFeed):
    """Connexion qui ne renvoie jamais rien : le cache n'est jamais rafraîchi."""

    name = "Muet"

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        await asyncio.Event().wait()
        yield  # pragma: no cover


def make_client(tmp_path, hub: PriceHub) -> TestClient:
    db = tmp_path / "api.db"
    return TestClient(create_app(service=lambda feed: TradingService(Storage(db), feed), hub=hub))


@pytest.fixture
def client(tmp_path):
    source = StaticFeed(PRICES)
    hub = PriceHub(source)
    for quote in source.quotes.values():  # cache prêt avant la première requête
        hub.update(quote)
    with make_client(tmp_path, hub) as c:
        yield c


def buy(client, paire="btc", montant="500"):
    return client.post("/ordres/achat", json={"paire": paire, "montant": montant})


def sell(client, paire="btc", quantite="tout"):
    return client.post("/ordres/vente", json={"paire": paire, "quantite": quantite})


# --- Prix ---


def test_prix(client):
    r = client.get("/prix")
    assert r.status_code == 200
    cotations = {c["paire"]: c for c in r.json()["cotations"]}
    assert set(cotations) == set(PRICES)
    btc = cotations["BTCUSDT"]
    assert (btc["bid"], btc["ask"], btc["spread"], btc["source"]) == ("80000", "80001", "1", "Simulé")
    assert cotations["SOLUSDT"]["spread"] == "0.02"
    assert 0 <= btc["age_secondes"] < 5 and btc["received_at"]


def test_ws_prix_envoie_les_cotations_du_cache(client):
    with client.websocket_connect("/ws/prix") as ws:
        received = {ws.receive_json()["paire"] for _ in PRICES}
    assert received == set(PRICES)
    hub = client.app.state.hub
    for _ in range(100):  # le serveur traite la fermeture de façon asynchrone
        if not hub._subscribers:
            break
        time.sleep(0.01)
    assert not hub._subscribers  # pas d'abonné fantôme après déconnexion


# --- Ordres ---


def test_achat(client):
    r = buy(client, "btc", "500")
    assert r.status_code == 200
    body = r.json()
    o = body["ordre"]
    assert (o["type"], o["paire"], o["prix"], o["source"]) == ("achat", "BTCUSDT", "80001", "Simulé")
    assert D(body["solde"]) == D("10000") + D(o["flux_usdt"]) and D(o["flux_usdt"]) >= D("-500")
    assert all(isinstance(o[k], str) for k in ("quantite", "prix", "montant", "frais", "flux_usdt"))


def test_vente_partielle_puis_tout(client):
    qty = D(buy(client, "eth", "1000").json()["ordre"]["quantite"])
    r = sell(client, "eth", "0.1")
    assert r.status_code == 200
    assert r.json()["ordre"]["quantite"] == "0.10000000" and r.json()["ordre"]["prix"] == "2350"
    r = sell(client, "ETHUSDT", "tout")
    assert r.status_code == 200
    assert D(r.json()["ordre"]["quantite"]) == qty - D("0.1")
    assert client.get("/portefeuille").json()["positions"] == []


def test_format_decimal_sans_notation_scientifique(client):
    buy(client, "sol", "100")
    r = sell(client, "sol", "0.00000001")
    assert r.status_code == 200
    assert r.json()["ordre"]["quantite"] == "0.00000001"
    text = client.get("/historique").text
    assert '"0.00000001"' in text and "E-" not in text


def test_portefeuille(client):
    buy(client, "btc", "5000")
    v = client.get("/portefeuille").json()
    (pos,) = v["positions"]
    assert pos["paire"] == "BTCUSDT" and pos["bid"] == "80000"
    assert D(v["valeur_totale"]) == D(v["solde"]) + D(v["valeur_positions"])
    assert D(v["performance_pct"]) < 0  # spread + frais
    assert D(v["frais_payes"]) > 0 and v["compte"]["capital_initial"] == "10000"


def test_historique(client):
    buy(client, "btc", "500")
    sell(client, "btc", "tout")
    h = client.get("/historique").json()
    assert [o["type"] for o in h["ordres"]] == ["achat", "vente"]
    assert h["ordres"][0]["pnl_realise"] == "0"
    assert h["compte"]["id"] == 1


def test_reset_avec_et_sans_capital(client):
    buy(client, "btc", "500")
    r = client.post("/reset", json={"capital": "2500"})
    assert r.status_code == 200 and r.json()["compte"]["solde"] == "2500.00"
    assert client.get("/historique").json()["ordres"] == []
    r = client.post("/reset")
    assert r.status_code == 200 and r.json()["compte"]["capital_initial"] == "10000.00"


def test_info(client, tmp_path):
    info = client.get("/info").json()
    assert info["base"] == str(tmp_path / "api.db")
    assert info["compte_actif"]["id"] == 1 and info["comptes_archives"] == 0
    assert info["flux"]["source"] == "Simulé" and info["flux"]["derniere_cotation"]


# --- Erreurs ---


def test_paire_inconnue_400(client):
    r = buy(client, "doge", "100")
    assert r.status_code == 400
    assert r.json() == {"erreur": "Paire inconnue : 'doge'. Paires disponibles : BTCUSDT, ETHUSDT, SOLUSDT."}


def test_montant_illisible_400(client):
    r = buy(client, "btc", "abc")
    assert r.status_code == 400 and r.json() == {"erreur": "Montant invalide : 'abc' n'est pas un nombre."}


def test_capital_invalide_400(client):
    r = client.post("/reset", json={"capital": "0"})
    assert r.status_code == 400 and r.json() == {"erreur": "Le capital doit être strictement positif (reçu : 0)."}


def test_ordres_refuses_400(client):
    r = sell(client, "eth", "1")
    assert r.status_code == 400 and r.json() == {"erreur": "Aucune position sur ETHUSDT : rien à vendre."}
    r = buy(client, "eth", "20000")
    assert r.status_code == 400 and r.json()["erreur"].startswith("Solde insuffisant")


def test_nombre_json_refuse_400(client):
    r = client.post("/ordres/achat", json={"paire": "btc", "montant": 500.5})
    assert r.status_code == 400
    assert r.json() == {"erreur": "Champ 'montant' manquant ou invalide (chaîne attendue)."}
    r = client.post("/ordres/vente", json={"paire": "btc"})
    assert r.status_code == 400 and "quantite" in r.json()["erreur"]


def test_cotation_perimee_503(tmp_path):
    hub = PriceHub(SilentFeed())
    old = Quote("BTCUSDT", D("80000"), D("80001"), "Simulé", utcnow() - timedelta(seconds=10))
    hub.update(old)
    with make_client(tmp_path, hub) as c:
        r = c.get("/prix")
        assert r.status_code == 503 and r.json()["erreur"].startswith("Cotation périmée pour BTCUSDT")
        r = buy(c, "btc", "100")
        assert r.status_code == 503 and "périmée" in r.json()["erreur"]
        r = buy(c, "eth", "100")
        assert r.status_code == 503 and r.json()["erreur"] == "Pas encore de prix pour ETHUSDT (flux en connexion)."
        assert c.get("/info").json()["flux"]["connecte"] is False


# --- CachedFeed et PriceHub, sans HTTP ---


def test_cached_feed_age_maximal():
    hub = PriceHub(StaticFeed())
    now = utcnow()
    hub.update(Quote("BTCUSDT", D("1"), D("2"), "Test", now))
    feed = CachedFeed(hub, clock=lambda: now + timedelta(seconds=5))
    assert asyncio.run(feed.snapshot(["BTCUSDT"]))["BTCUSDT"].bid == D("1")
    feed.clock = lambda: now + timedelta(seconds=5.1)
    with pytest.raises(FeedUnavailable, match="périmée"):
        asyncio.run(feed.snapshot(["BTCUSDT"]))


class ScriptedFeed(PriceFeed):
    """Chaque connexion joue l'étape suivante : "vide", "erreur" ou "cotation"."""

    name = "Script"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    async def stream(self, pairs: Sequence[str]) -> AsyncIterator[Quote]:
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if step == "erreur":
            raise OSError("connexion refusée")
        if step == "cotation":
            yield Quote("BTCUSDT", D("1"), D("2"), self.name, utcnow())


def run_hub(feed: PriceFeed, n_sleeps: int) -> tuple[PriceHub, list[float]]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= n_sleeps:
            raise asyncio.CancelledError

    hub = PriceHub(feed, ["BTCUSDT"], sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(hub.run())
    return hub, delays


def test_hub_se_reconnecte_apres_la_fin_du_flux():
    feed = ScriptedFeed(["vide", "erreur", "cotation", "cotation"])
    hub, delays = run_hub(feed, 4)
    assert delays == [1, 2, 1, 1]  # le délai repart à 1 dès qu'une connexion a fourni un prix
    assert feed.calls == 4
    assert hub.quotes["BTCUSDT"].source == "Script"
    assert hub.connected is False and hub.last_error == "flux terminé"


def test_hub_backoff_plafonne_a_30_s():
    _, delays = run_hub(ScriptedFeed(["vide"]), 7)
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_hub_garde_la_raison_de_l_erreur():
    hub, _ = run_hub(ScriptedFeed(["erreur"]), 1)
    assert hub.last_error == "connexion refusée"
