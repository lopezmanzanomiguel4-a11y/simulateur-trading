"""API HTTP de bout en bout et flux partagé, avec prix simulés, base temporaire et sans réseau."""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from simtrade.api import create_app
from simtrade.errors import FeedUnavailable
from simtrade.feeds import PriceFeed, StaticFeed
from simtrade.hub import CachedFeed, PriceHub
from simtrade.models import Quote, Trade, utcnow
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
    assert not any(c["perime"] for c in cotations.values())


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


def test_corps_non_json_400(client):
    for body in ("pas du json", '{"paire": "btc", '):
        r = client.post("/ordres/achat", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.json() == {"erreur": "Corps de requête JSON invalide."}


def test_cotation_perimee_503(tmp_path):
    hub = PriceHub(SilentFeed())
    old = Quote("BTCUSDT", D("80000"), D("80001"), "Simulé", utcnow() - timedelta(seconds=10))
    hub.update(old)
    with make_client(tmp_path, hub) as c:
        r = buy(c, "btc", "100")
        assert r.status_code == 503 and "périmée" in r.json()["erreur"]
        r = buy(c, "eth", "100")
        assert r.status_code == 503 and r.json()["erreur"] == "Pas encore de prix pour ETHUSDT (flux en connexion)."
        assert c.get("/info").json()["flux"]["connecte"] is False


def test_prix_renvoie_aussi_les_cotations_perimees(tmp_path):
    hub = PriceHub(SilentFeed())
    hub.update(Quote("BTCUSDT", D("80000"), D("80001"), "Simulé", utcnow() - timedelta(seconds=10)))
    hub.update(Quote("ETHUSDT", D("2350"), D("2350.5"), "Simulé", utcnow()))
    with make_client(tmp_path, hub) as c:
        r = c.get("/prix")
    assert r.status_code == 200
    perime = {q["paire"]: q["perime"] for q in r.json()["cotations"]}
    assert perime == {"BTCUSDT": True, "ETHUSDT": False}


def test_prix_cache_vide_503(tmp_path):
    with make_client(tmp_path, PriceHub(SilentFeed())) as c:
        r = c.get("/prix")
    assert r.status_code == 503
    assert r.json() == {"erreur": "Aucune cotation reçue pour l'instant (flux en connexion)."}


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


# --- Bougies et indicateurs ---

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(minutes=3, seconds=30)  # la bougie de 00:03 est en cours


def market_hub() -> PriceHub:
    """Hub sans flux actif, horloge figée ; transactions datées (clôtures 10, 12, 11 puis 13 en cours)."""
    hub = PriceHub(SilentFeed(), clock=lambda: NOW)
    for minute, price, qty in ((0, "10", "1"), (1, "12", "1"), (2, "11", "2"), (3, "13", "1")):
        t = T0 + timedelta(minutes=minute, seconds=2)
        hub.update_trade(Trade("BTCUSDT", D(price), D(qty), "Test", t, t, 2))
    return hub


@pytest.fixture
def market_client(tmp_path):
    with make_client(tmp_path, market_hub()) as c:
        yield c


def test_bougies(market_client):
    r = market_client.get("/bougies/btc", params={"intervalle": "1m", "limite": "2"})
    assert r.status_code == 200
    body = r.json()
    assert (body["paire"], body["intervalle"]) == ("BTCUSDT", "1m")
    closed, current = body["bougies"]
    assert closed == {
        "ouverture_le": "2025-01-01T00:02:00+00:00",
        "ouverture": "11",
        "haut": "11",
        "bas": "11",
        "cloture": "11",
        "volume": "2",
        "notional": "22",
        "transactions": 2,
        "origine": "live",
        "complete": True,
        "cloturee": True,
    }
    assert current["cloturee"] is False and current["cloture"] == "13"


def test_bougies_premiere_bougie_incomplete(market_client):
    first = market_client.get("/bougies/btc").json()["bougies"][0]
    assert first["complete"] is False  # au démarrage, le début de la minute a pu être manqué


def test_bougies_autre_intervalle(market_client):
    (c,) = market_client.get("/bougies/BTCUSDT", params={"intervalle": "1h"}).json()["bougies"]
    assert (c["ouverture"], c["haut"], c["bas"], c["cloture"], c["volume"]) == ("10", "13", "10", "13", "5")


def test_indicateurs_sur_bougies_cloturees_par_defaut(market_client):
    r = market_client.get("/indicateurs/btc", params={"sma": "3", "ema": "3", "rsi": "2"})
    assert r.status_code == 200
    i = r.json()
    assert i["inclure_courante"] is False and i["nb_bougies"] == 3 and i["dernier_prix"] == "11"
    assert i["derniere_bougie"] == {"ouverture_le": "2025-01-01T00:02:00+00:00", "complete": True, "cloturee": True}
    assert i["sma"] == {"periode": 3, "valeur": "11.00000000"}  # (10 + 12 + 11) / 3
    assert i["ema"] == {"periode": 3, "valeur": "11.00000000"}  # = amorce SMA
    assert i["rsi"] == {"periode": 2, "valeur": "66.66666667"}  # hausse 1, baisse 0,5 : 100 - 100/3
    assert i["variation_pct"] == "10.00000000"  # (11 - 10) / 10 x 100
    assert i["vwap"] == {
        "valeur": "11.00000000",  # (10 + 12 + 22) / 4
        "ancrage": "jour_utc",
        "debut": "2025-01-01T00:00:00+00:00",
        "fin": "2025-01-01T00:03:00+00:00",
        "nb_bougies": 3,
        "volume": "4",
    }


def test_indicateurs_avec_la_bougie_en_cours(market_client):
    params = {"sma": "3", "ema": "3", "rsi": "2", "inclure_courante": "true"}
    i = market_client.get("/indicateurs/btc", params=params).json()
    assert i["inclure_courante"] is True and i["nb_bougies"] == 4 and i["dernier_prix"] == "13"
    assert i["derniere_bougie"]["cloturee"] is False
    assert i["sma"]["valeur"] == "12.00000000" and i["ema"]["valeur"] == "12.00000000"
    assert i["rsi"]["valeur"] == "85.71428571" and i["variation_pct"] == "30.00000000"
    assert i["vwap"]["valeur"] == "11.40000000" and i["vwap"]["fin"] == "2025-01-01T00:03:30+00:00"


def test_vwap_glissant_sur_la_fenetre(market_client):
    i = market_client.get("/indicateurs/btc", params={"vwap": "fenetre", "fenetre": "2", "sma": "3"}).json()
    assert i["nb_bougies"] == 2 and i["sma"]["valeur"] is None  # pas assez de bougies
    assert i["vwap"]["ancrage"] == "fenetre" and i["vwap"]["valeur"] == "11.33333333"  # (12 + 22) / 3
    assert i["vwap"]["debut"] == "2025-01-01T00:01:00+00:00" and i["volume"] == "3"


def test_qualite_dans_les_reponses(market_client):
    for url in ("/bougies/btc", "/indicateurs/btc"):
        q = market_client.get(url).json()["qualite"]
        assert q["prix_perime"] is True  # aucune cotation
        assert q["flux_transactions_muet"] is True  # flux de transactions jamais connecté
        assert q["historique"] == "desactive" and q["vwap_fiable"] is False
        assert q["source_transactions"] == "Test" and q["bougie_courante_complete"] is True
        assert {"prix_perime", "flux_transactions_muet", "historique_desactive"} <= set(q["raisons"])


def test_marche_vide(tmp_path):
    with make_client(tmp_path, PriceHub(SilentFeed())) as c:
        assert c.get("/bougies/eth").json()["bougies"] == []
        i = c.get("/indicateurs/eth").json()
    assert i["nb_bougies"] == 0 and i["debut"] is None and i["dernier_prix"] is None and i["derniere_bougie"] is None
    assert i["sma"]["valeur"] is None and i["ema"]["valeur"] is None and i["rsi"]["valeur"] is None
    assert i["vwap"]["valeur"] is None and i["variation_pct"] is None and i["volume"] == "0"
    assert "volume_nul" in i["qualite"]["raisons"]


@pytest.mark.parametrize(
    "url, params, message",
    [
        ("/bougies/doge", {}, "Paire inconnue : 'doge'. Paires disponibles : BTCUSDT, ETHUSDT, SOLUSDT."),
        ("/bougies/btc", {"intervalle": "2m"}, "Paramètre 'intervalle' invalide : '2m'. Valeurs possibles : 1m, 5m, 15m, 1h."),
        ("/bougies/btc", {"limite": "abc"}, "Paramètre 'limite' invalide : 'abc' n'est pas un entier."),
        ("/bougies/btc", {"limite": "0"}, "Paramètre 'limite' hors bornes : minimum 1 (reçu : 0)."),
        ("/bougies/btc", {"limite": "1441"}, "Paramètre 'limite' hors bornes : maximum 1440 (reçu : 1441)."),
        ("/indicateurs/btc", {"rsi": "1440"}, "Paramètre 'rsi' hors bornes : maximum 1439 (reçu : 1440)."),
        ("/indicateurs/btc", {"sma": "1.5"}, "Paramètre 'sma' invalide : '1.5' n'est pas un entier."),
        (
            "/indicateurs/btc",
            {"inclure_courante": "peut-être"},
            "Paramètre 'inclure_courante' invalide : 'peut-être' (true ou false attendu).",
        ),
        (
            "/indicateurs/btc",
            {"vwap": "semaine"},
            "Paramètre 'vwap' invalide : 'semaine'. Valeurs possibles : jour_utc, fenetre.",
        ),
    ],
)
def test_parametres_de_marche_invalides_400(market_client, url, params, message):
    r = market_client.get(url, params=params)
    assert r.status_code == 400 and r.json() == {"erreur": message}


def test_info_flux_volume_historique_et_horloge(client):
    info = client.get("/info").json()
    vol = info["flux_volume"]
    assert vol["actif"] is True  # StaticFeed fournit des transactions
    assert set(vol) == {"actif", "connecte", "derniere_transaction", "derniere_erreur", "reconnexions", "source", "repli"}
    assert info["historique"]["etat"] == "desactive"  # feed fourni sans historique : rien à télécharger
    assert info["horloge"] == {"ecart_ms": None, "aller_retour_ms": None, "mesure_le": None, "erreur": None}


# --- Étape 4 : ordres en attente, comptes, session ---


def push_quote(client, pair, bid, ask=None):
    """Cotation live injectée dans la boucle de l'API (le watcher utilise la connexion SQLite de ce thread)."""
    quote = Quote(pair, D(bid), D(ask or bid), "Simulé", utcnow())
    client.portal.call(client.app.state.hub.update, quote)


def test_stop_pose_puis_execute_par_le_flux(client):
    buy(client, "btc", "8000")
    r = client.post("/ordres/stop", json={"paire": "btc", "prix": "79000"})
    assert r.status_code == 200
    (stop,) = r.json()["ordres"]
    assert (stop["type_ordre"], stop["sens"], stop["quantite"], stop["statut"]) == ("stop", "vente", "tout", "en_attente")
    assert [o["id"] for o in client.get("/ordres/attente").json()["ordres"]] == [stop["id"]]

    push_quote(client, "BTCUSDT", "78500", "78501")  # le prix saute sous le stop

    last = client.get("/historique").json()["ordres"][-1]
    assert (last["type_ordre"], last["prix"], last["prix_declenchement"]) == ("stop", "78500", "79000")
    assert D(last["glissement"]) == (D("500") * D(last["quantite"])).quantize(D("0.00000001")) > 0  # (79000 - 78500) x q
    assert last["ordre_attente_id"] == stop["id"]
    assert client.get("/ordres/attente").json()["ordres"] == []
    tous = client.get("/ordres/attente", params={"tous": "true"}).json()["ordres"]
    assert tous[0]["statut"] == "execute" and tous[0]["ordre_execute_id"] == last["id"]


def test_stop_objectif_et_annulation(client):
    buy(client, "eth", "1000")
    r = client.post("/ordres/stop-objectif", json={"paire": "eth", "stop": "2300", "objectif": "2500"})
    stop, target = r.json()["ordres"]
    assert stop["lie_a"] == target["lie_a"] == stop["id"]
    r = client.delete(f"/ordres/{target['id']}")
    assert r.status_code == 200 and {o["id"] for o in r.json()["annules"]} == {stop["id"], target["id"]}
    r = client.delete(f"/ordres/{target['id']}")
    assert r.status_code == 400 and "n'est plus en attente" in r.json()["erreur"]


def test_ordre_limite_et_refus(client):
    r = client.post("/ordres/limite", json={"paire": "sol", "sens": "achat", "prix": "99", "montant": "500"})
    assert r.status_code == 200 and r.json()["ordres"][0]["montant"] == "500"
    r = client.post("/ordres/limite", json={"paire": "sol", "sens": "achat", "prix": "99"})
    assert r.status_code == 400 and "montant" in r.json()["erreur"]
    r = client.post("/ordres/stop", json={"paire": "btc", "prix": "79000"})
    assert r.status_code == 400 and r.json()["erreur"] == "Aucune position sur BTCUSDT : rien à vendre."
    buy(client, "btc", "1000")
    r = client.post("/ordres/stop", json={"paire": "btc", "prix": "81000"})  # au-dessus du bid
    assert r.status_code == 400 and "exécuté immédiatement" in r.json()["erreur"]


def test_comptes_en_parallele_via_api(client):
    r = client.post("/comptes", json={"nom": "robot", "capital": "2000", "mode": "auto"})
    assert r.status_code == 200 and r.json()["compte"]["mode"] == "auto"
    assert [c["nom"] for c in client.get("/comptes").json()["comptes"]] == ["principal", "robot"]

    r = client.post("/ordres/entree", params={"compte": "robot"}, json={"paire": "sol", "montant": "400", "stop": "90"})
    assert r.status_code == 200
    assert client.get("/portefeuille", params={"compte": "robot"}).json()["positions"][0]["paire"] == "SOLUSDT"
    assert client.get("/portefeuille").json()["positions"] == []  # compte courant intact
    assert client.get("/historique", params={"compte": "robot"}).json()["compte"]["nom"] == "robot"

    r = client.post("/comptes/robot/archiver")
    assert r.status_code == 200 and r.json()["compte"]["archive_le"]
    r = client.get("/portefeuille", params={"compte": "robot"})
    assert r.status_code == 400 and "inconnu" in r.json()["erreur"]
    info = client.get("/info").json()
    assert info["comptes_ouverts"] == ["principal"] and info["comptes_archives"] == 1


def test_session_lecture_et_reglage(client):
    assert client.get("/session").json() == {"derniere_entree": "23:00", "cloture": "23:55", "fuseau": "UTC"}
    r = client.put("/session", json={"derniere_entree": "23:58", "cloture": "23:50"})
    assert r.status_code == 400 and "dernière entrée" in r.json()["erreur"]
    r = client.put("/session", json={"derniere_entree": "22:00", "cloture": "23:30"})
    assert r.status_code == 200 and r.json()["cloture"] == "23:30"
    assert client.get("/info").json()["session"]["derniere_entree"] == "22:00"