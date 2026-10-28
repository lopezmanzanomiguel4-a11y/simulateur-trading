"""Historique Binance (klines simulées par httpx.MockTransport) et raccord avec le live, sans réseau."""

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from conftest import NetworkAccess
from simtrade.errors import FeedUnavailable
from simtrade.feeds import BinanceFeed, CoinbaseFeed, FallbackFeed, StaticFeed
from simtrade.history import BinanceHistory, HistorySource, from_ms, parse_kline, to_ms
from simtrade.hub import PriceHub
from simtrade.market import INTERVALS, ORIGIN_LIVE, ORIGIN_REST, Candle, MarketData
from simtrade.models import Trade

D = Decimal
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def row(minute: int, close: str = "100", volume: str = "1", trades: int = 1, interval: str = "1m") -> list:
    """Ligne de /api/v3/klines, au format Binance (nombres en chaîne, heures en ms)."""
    step = INTERVALS[interval] * 1000
    open_ms = to_ms(at(minute))
    quote_volume = str(D(close) * D(volume))
    return [open_ms, close, close, close, close, volume, open_ms + step - 1, quote_volume, trades, "0", "0", "0"]


# --- Garde réseau ---


def test_aucun_test_ne_peut_joindre_binance():
    with pytest.raises(NetworkAccess):
        socket.getaddrinfo("api.binance.com", 443)
    with pytest.raises(NetworkAccess):
        socket.create_connection(("52.84.150.1", 443), timeout=0.1)


# --- Parsing et pagination ---


def test_parse_kline():
    raw = [1735689600000, "93576.00", "93610.93", "93537.50", "93610.93", "8.21827", 1735689659999,
           "769122.84", 1286, "4.4", "411000", "0"]
    c = parse_kline(raw, "BTCUSDT", "1m")
    assert c.open_time == T0 and c.close_time == at(1)
    assert (c.open, c.high, c.low, c.close) == (D("93576"), D("93610.93"), D("93537.5"), D("93610.93"))
    assert (c.volume, c.notional, c.transactions, c.origin) == (D("8.21827"), D("769122.84"), 1286, ORIGIN_REST)


def test_conversion_ms_exacte():
    assert from_ms(to_ms(at(3, 0.123))) == at(3, 0.123)
    assert from_ms("1735689600000") == T0


def mock_binance(rows: list[list], requests: list[dict], status: int = 200) -> httpx.AsyncClient:
    """Imite /api/v3/klines (startTime, endTime, limit) et /api/v3/time."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params | {"path": request.url.path})
        if status != 200:
            return httpx.Response(status, json={"code": -1003, "msg": "Too many requests"})
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": to_ms(at(9, 30))})
        limit = int(params["limit"])
        selected = rows
        if "startTime" in params:
            selected = [r for r in selected if r[0] >= int(params["startTime"])][:limit]
        else:
            if "endTime" in params:
                selected = [r for r in selected if r[0] <= int(params["endTime"])]
            selected = selected[-limit:]
        return httpx.Response(200, json=selected)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.binance.com")


def test_pagination_vers_le_passe():
    requests: list[dict] = []
    history = BinanceHistory(mock_binance([row(m) for m in range(10)], requests), page_size=3)
    candles = asyncio.run(history.klines("BTCUSDT", "1m", 7))
    assert [c.open_time for c in candles] == [at(m) for m in range(3, 10)]
    assert [(r["limit"], r.get("endTime")) for r in requests] == [
        ("3", None),
        ("3", str(to_ms(at(7)) - 1)),
        ("1", str(to_ms(at(4)) - 1)),
    ]
    assert all(r["symbol"] == "BTCUSDT" and r["interval"] == "1m" for r in requests)


def test_pagination_s_arrete_au_debut_de_l_historique():
    requests: list[dict] = []
    history = BinanceHistory(mock_binance([row(m) for m in range(5)], requests), page_size=3)
    candles = asyncio.run(history.klines("BTCUSDT", "1m", 7))
    assert [c.open_time for c in candles] == [at(m) for m in range(5)] and len(requests) == 2


def test_pagination_depuis_une_date():
    requests: list[dict] = []
    history = BinanceHistory(mock_binance([row(m) for m in range(10)], requests), page_size=3)
    candles = asyncio.run(history.klines("BTCUSDT", "1m", 1440, start=at(2)))
    assert [c.open_time for c in candles] == [at(m) for m in range(2, 10)]
    assert [r["startTime"] for r in requests] == [str(to_ms(at(m))) for m in (2, 5, 8)]
    assert len(asyncio.run(history.klines("BTCUSDT", "1m", 4, start=at(2)))) == 4  # les plus récentes


def test_page_par_defaut_de_1000():
    requests: list[dict] = []
    asyncio.run(BinanceHistory(mock_binance([row(0)], requests)).klines("BTCUSDT", "1m", 1440))
    assert requests[0]["limit"] == "1000"


def test_erreur_http():
    history = BinanceHistory(mock_binance([], [], status=429))
    with pytest.raises(FeedUnavailable, match="Historique Binance indisponible"):
        asyncio.run(history.klines("BTCUSDT", "1m", 10))


def test_heure_du_serveur():
    history = BinanceHistory(mock_binance([], []))
    assert asyncio.run(history.server_time()) == at(9, 30)


# --- Raccord dans le hub (historique simulé) ---


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeHistory(HistorySource):
    """Klines scriptées par série ; `during` est appelé pendant la requête (transactions qui arrivent)."""

    name = "Faux"

    def __init__(self, clock: Clock, klines=None, server_offset: float = 0.0):
        self.clock = clock
        self.data: dict[tuple[str, str], list[Candle]] = klines or {}
        self.server_offset = server_offset
        self.calls: list[tuple[str, str, int, datetime | None]] = []
        self.during: dict[tuple[str, str], callable] = {}
        self.fail: Exception | None = None
        self.clock_fail = False

    async def klines(self, pair, interval, limit, start=None):
        self.calls.append((pair, interval, limit, start))
        if self.fail:
            raise self.fail
        if (pair, interval) in self.during:
            self.during[(pair, interval)]()
        candles = [c for c in self.data.get((pair, interval), []) if start is None or c.open_time >= start]
        return candles[-limit:]

    async def server_time(self):
        if self.clock_fail:
            raise OSError("délai dépassé")
        return self.clock() + timedelta(seconds=self.server_offset)


def kl(minute: int, close: str, volume: str, trades: int = 1, interval: str = "1m") -> Candle:
    c = D(close)
    return Candle("BTCUSDT", interval, at(minute), c, c, c, c, D(volume), c * D(volume), trades, ORIGIN_REST)


def trade(minute: float, seconds: float, price: str, qty: str, count: int = 1, source: str = "Binance") -> Trade:
    t = at(minute, seconds)
    return Trade("BTCUSDT", D(price), D(qty), source, t, t, count)


def make_hub(clock: Clock, history: FakeHistory, intervals=("1m",)) -> PriceHub:
    return PriceHub(
        StaticFeed(), ["BTCUSDT"], history=history, clock=clock, market=MarketData(["BTCUSDT"], intervals)
    )


def test_demarrage_tampon_et_amorce_de_la_bougie_en_cours():
    clock = Clock(at(1, 20))
    history = FakeHistory(
        clock,
        {
            ("BTCUSDT", "1m"): [kl(0, "100", "4", 40), kl(1, "101", "1", 2)],
            ("BTCUSDT", "5m"): [kl(0, "100", "5", 42, interval="5m")],
        },
    )
    hub = make_hub(clock, history, ("1m", "5m"))
    hub.begin_trade_session()
    # A : reçue avant la réponse 1m, supposée déjà comptée par les klines (ignorée partout).
    history.during[("BTCUSDT", "1m")] = lambda: hub.update_trade(trade(1, 10, "101", "0.5"))
    # B : reçue après la réponse 1m mais avant la réponse 5m : ajoutée en 1m, ignorée en 5m.
    history.during[("BTCUSDT", "5m")] = lambda: hub.update_trade(trade(1, 15, "103", "2", count=3))

    assert asyncio.run(hub.resync()) is True
    past, current = hub.market.candles("BTCUSDT", "1m")
    assert past.origin == ORIGIN_REST and past.complete and past.volume == D("4")
    assert current.origin == ORIGIN_LIVE and not current.complete
    assert (current.volume, current.transactions, current.close, current.high) == (D("3"), 5, D("103"), D("103"))
    (five,) = hub.market.candles("BTCUSDT", "5m")
    assert five.volume == D("5") and five.transactions == 42
    assert hub.history_overall() == "complet" and hub.last_sync == clock.now
    assert [c[3] for c in history.calls] == [None, None]  # rien en mémoire : chargement complet
    assert history.calls[0][2] == 1440 and history.calls[1][2] == 500
    # le tampon est vidé : les transactions suivantes vont directement dans les bougies
    hub.update_trade(trade(1, 30, "104", "1"))
    assert hub.market.candles("BTCUSDT", "1m")[-1].volume == D("4")


def test_reconciliation_a_la_cloture():
    clock = Clock(at(1, 20))
    history = FakeHistory(clock, {("BTCUSDT", "1m"): [kl(0, "100", "4", 40), kl(1, "101", "1", 2)]})
    hub = make_hub(clock, history)
    hub.begin_trade_session()
    asyncio.run(hub.resync())
    hub.update_trade(trade(1, 30, "102", "1"))
    hub.update_trade(trade(2, 1, "105", "1"))  # ouvre la bougie de 00:02, complète (pas de coupure)

    clock.now = at(2, 1)
    asyncio.run(hub.reconcile())
    assert len(history.calls) == 1  # 00:01 clôturée depuis moins de 2 s : pas encore demandée

    clock.now = at(2, 3)
    history.data[("BTCUSDT", "1m")] = [kl(0, "100", "4", 40), kl(1, "102", "9", 77), kl(2, "105", "1", 1)]
    asyncio.run(hub.reconcile())
    assert history.calls[-1][3] == at(1)  # depuis la plus ancienne bougie live clôturée
    m0, m1, m2 = hub.market.candles("BTCUSDT", "1m")
    assert m1.origin == ORIGIN_REST and m1.volume == D("9") and m1.transactions == 77
    assert m2.origin == ORIGIN_LIVE and m2.complete and m2.volume == D("1")  # bougie en cours : intacte
    ((live, rest),) = hub.reconciliations
    assert live.open_time == at(1) and live.volume == D("2") and not live.complete  # amorce + 1 transaction
    assert rest is m1


def test_reconnexion_comble_le_trou():
    clock = Clock(at(0, 30))
    history = FakeHistory(clock, {("BTCUSDT", "1m"): [kl(0, "100", "1")]})
    hub = make_hub(clock, history)
    hub.begin_trade_session()
    asyncio.run(hub.resync())
    hub.update_trade(trade(1, 5, "101", "1"))
    # coupure de 00:01:10 à 00:04:20, puis nouvelle connexion
    clock.now = at(4, 20)
    history.data[("BTCUSDT", "1m")] = [kl(m, str(100 + m), "2") for m in range(5)]
    hub.begin_trade_session()
    assert hub.history_overall() == "partiel"
    asyncio.run(hub.resync())
    assert history.calls[-1][3] == at(0)  # reprise à la plus ancienne bougie live (l'amorce de 00:00)
    candles = hub.market.candles("BTCUSDT", "1m")
    assert [c.open_time for c in candles] == [at(m) for m in range(5)]  # ni trou ni doublon
    assert [c.origin for c in candles] == [ORIGIN_REST] * 4 + [ORIGIN_LIVE]
    assert hub.history_overall() == "complet"


def test_flux_demarre_apres_la_minute_de_l_instantane():
    """Cas réel constaté : horloge locale en retard d'1 s, instantané REST pris à 00:00:59 (heure locale),
    première transaction websocket reçue à 00:01:00,5 (heure exchange). Les transactions de 00:01:00
    à 00:01:00,5 n'ont jamais été reçues : la bougie de 00:01 doit être marquée incomplète."""
    clock = Clock(at(0, 59.2))
    history = FakeHistory(clock, {("BTCUSDT", "1m"): [kl(0, "100", "4", 40)]})
    hub = make_hub(clock, history)
    hub.begin_trade_session()
    asyncio.run(hub.resync())  # tampon vide à la réponse : rien n'est encore reçu
    hub.update_trade(trade(1, 0.5, "101", "1"))
    hub.update_trade(trade(2, 0.1, "102", "1"))
    m0, m1, m2 = hub.market.candles("BTCUSDT", "1m")
    assert not m0.complete and not m1.complete  # m1 était marquée complète avant la correction
    assert m2.complete  # le flux livrait déjà avant 00:02


def test_historique_injoignable():
    clock = Clock(at(1, 20))
    history = FakeHistory(clock)
    history.fail = FeedUnavailable("Historique Binance indisponible : 418")
    hub = make_hub(clock, history)
    hub.begin_trade_session()
    history.during = {}
    hub._buffer = [trade(1, 10, "100", "1")]  # reçue pendant la tentative
    assert asyncio.run(hub.resync()) is False
    assert hub.history_overall() == "indisponible" and "418" in hub.history_error
    (c,) = hub.market.candles("BTCUSDT", "1m")
    assert c.volume == D("1") and not c.complete  # appliquée directement, bougie marquée incomplète
    assert hub._buffer is None


def test_ecart_d_horloge_mesure_et_signale():
    clock = Clock(at(1, 20))
    history = FakeHistory(clock, server_offset=1.5)
    hub = make_hub(clock, history)
    asyncio.run(hub.measure_clock())
    assert hub.clock_offset.offset == pytest.approx(1.5) and hub.clock_offset.measured_at == clock.now
    assert "horloge_decalee" in hub.quality("BTCUSDT", "1m", D(1)).raisons
    history.server_offset = 0.2
    asyncio.run(hub.measure_clock())
    assert "horloge_decalee" not in hub.quality("BTCUSDT", "1m", D(1)).raisons
    history.clock_fail = True
    asyncio.run(hub.measure_clock())
    assert hub.clock_error == "délai dépassé"


def test_repli_coinbase_signale():
    hub = PriceHub(FallbackFeed([BinanceFeed(), CoinbaseFeed()]), ["BTCUSDT"])
    assert hub.primary_source == "Binance" and not hub.trade_fallback
    hub.update_trade(trade(0, 1, "100", "1", source="Coinbase"))
    q = hub.quality("BTCUSDT", "1m", D(1))
    assert q.repli_coinbase and q.source_transactions == "Coinbase" and not q.vwap_fiable
    assert "repli_coinbase" in q.raisons


def test_hub_sans_historique():
    hub = PriceHub(StaticFeed(), ["BTCUSDT"])
    assert hub.history is None and hub.history_overall() == "desactive"
    hub.begin_trade_session()
    assert asyncio.run(hub.resync()) is True and hub._buffer is None


def test_run_complet_avec_les_trois_taches():
    now = datetime.now(timezone.utc)
    clock = Clock(now)
    source = StaticFeed({"BTCUSDT": ("100", "102")})
    source.add_trade("BTCUSDT", "101", "2", traded_at=now)
    history = FakeHistory(clock, {("BTCUSDT", "1m"): []})

    async def forever(delay: float) -> None:
        await asyncio.Event().wait()

    async def main() -> PriceHub:
        hub = PriceHub(
            source, ["BTCUSDT"], sleep=forever, history=history, clock=clock,
            market=MarketData(["BTCUSDT"], ["1m"]), reconcile_every=0.01,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(hub.run(), 0.3)
        return hub

    hub = asyncio.run(main())
    assert history.calls and history.calls[0][3] is None  # resynchro au début de la connexion
    (c,) = hub.market.candles("BTCUSDT", "1m")
    assert c.volume == D("2") and hub.trade_status.last_error == "flux terminé"
    assert hub.last_quote_at is not None and hub.clock_offset is not None
