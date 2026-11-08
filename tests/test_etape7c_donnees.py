"""Étape 7c, lot B : klines 1h, agrégation 4h / 1d, backtest swing (stop suiveur), univers, modèle d'essai."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from etape7 import FakeBinance, kline_row
from simtrade import backtest, bricks, catalogue, strategies
from simtrade.backtest import BacktestConfig
from simtrade.download import Downloader
from simtrade.errors import InvalidInput
from simtrade.history import BinanceHistory
from simtrade.marche import SCHEMA_VERSION, MarketStore, empty_key
from simtrade.market import Candle, aggregate
from simtrade.models import PAIRS, SESSION_CLOSE, STOP, SWING_PAIRS, TARGET, TRAILING, Side
from simtrade.runner import StrategyRunner

D = Decimal
UTC = timezone.utc
H = timedelta(hours=1)
H0 = datetime(2026, 3, 1, tzinfo=UTC)
NOSPREAD = BacktestConfig(spread_pct=D(0), full_window=False)

SWING_1H = {
    "nom": "swing-test",
    "horizon": "swing",
    "paire": "BTCUSDT",
    "intervalle": "1h",
    "entree": [{"brique": "au_dessus_ema", "n": 20}],
    "stop": {"type": "suiveur_pct", "valeur": "5"},
    "objectif": {"type": "aucun"},
    "taille": {"risque_pct": "1"},
}


def hour(t: datetime, o, h=None, l=None, c=None, pair="BTCUSDT", interval="1h") -> Candle:
    o = D(o)
    h, l, c = D(h if h is not None else o), D(l if l is not None else o), D(c if c is not None else o)
    return Candle(pair, interval, t, o, h, l, c, D(1), c, 1, "binance")


def hours(start: datetime, bars) -> list[Candle]:
    """bars : prix (barre plate) ou tuple (ouverture, haut, bas, clôture)."""
    return [hour(start + i * H, *(b if isinstance(b, tuple) else (b,))) for i, b in enumerate(bars)]


def swing(**changes) -> strategies.Strategy:
    return strategies.validate({**SWING_1H, **changes})


def once():
    """Une seule entrée au hasard : la première clôture éligible."""
    calls = iter([True])
    return lambda: next(calls, False)


# --- marche.db : klines 1h ---


def test_klines_1h_upsert_tables_separees(tmp_path):
    store = MarketStore(tmp_path / "marche.db")
    one = Candle("BTCUSDT", "1m", H0, D(1), D(1), D(1), D(1), D(1), D(1), 1, "binance")
    assert store.save(hours(H0, [100, 101, 102]) + [one]) == 4
    assert store.save([hour(H0, 100, 105, 99, 104)]) == 1  # upsert : pas de doublon
    assert store.count("BTCUSDT", interval="1h") == 3 and store.count("BTCUSDT") == 1
    got = store.candles("BTCUSDT", H0, H0 + 3 * H, "1h")
    assert [c.interval for c in got] == ["1h"] * 3 and got[0].high == D(105)
    assert store.bounds("BTCUSDT", "1h") == (H0, H0 + 2 * H)
    assert store.candles("BTCUSDT", H0, H0 + 3 * H) == [store.candles("BTCUSDT", H0, H0 + H)[0]]


def test_trous_1h_et_plages_vides_par_intervalle(tmp_path):
    store = MarketStore(tmp_path / "marche.db")
    store.save([hour(H0 + k * H, 100) for k in (0, 1, 4)])
    assert store.gaps("BTCUSDT", H0, H0 + 6 * H, "1h") == [(H0 + 2 * H, H0 + 4 * H), (H0 + 5 * H, H0 + 6 * H)]
    store.add_binance_empty("BTCUSDT", H0, H0 + H, "1h")
    assert store.binance_empty("BTCUSDT", "1h") == [(H0, H0 + H)]
    assert store.binance_empty("BTCUSDT") == []  # les plages 1m restent séparées
    assert empty_key("BTCUSDT") == "binance_vide:BTCUSDT" and empty_key("BTCUSDT", "1h") == "binance_vide:1h:BTCUSDT"


def test_base_v1_passe_en_v2_sans_toucher_aux_1m(tmp_path):
    path = tmp_path / "marche.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE klines_1m (pair TEXT NOT NULL, open_time INTEGER NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL,"
        " low TEXT NOT NULL, close TEXT NOT NULL, volume TEXT NOT NULL, notional TEXT NOT NULL,"
        " transactions INTEGER NOT NULL, source TEXT NOT NULL, PRIMARY KEY (pair, open_time)) WITHOUT ROWID;"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('version', '1');"
        "INSERT INTO klines_1m VALUES ('BTCUSDT', 0, '1', '1', '1', '1', '1', '1', 1, 'telechargement');"
    )
    conn.commit()
    conn.close()
    store = MarketStore(path)
    assert store.conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()[0] == str(SCHEMA_VERSION) == "2"
    assert store.count("BTCUSDT") == 1 and store.count("BTCUSDT", interval="1h") == 0


def test_telechargement_1h_reprise_et_plage_vide_avant_cotation(tmp_path):
    rows = [kline_row(H0 + k * H, seconds=3600) for k in range(5, 30)]  # la paire « cote » à partir de H0 + 5 h
    fake = FakeBinance({"BTCUSDT": rows})
    store = MarketStore(tmp_path / "marche.db")
    history = BinanceHistory(fake.client(), page_size=10)

    async def nosleep(_):
        return None

    d = Downloader(store, history, sleep=nosleep, disk_usage=lambda p: SimpleNamespace(free=10**12),
                   clock=lambda: H0 + 40 * H, interval="1h")
    report = asyncio.run(d.download(["BTCUSDT"], H0, H0 + 20 * H))
    assert store.count("BTCUSDT", interval="1h") == 15 and store.count("BTCUSDT") == 0
    assert all(r["interval"] == "1h" for r in fake.requests)
    assert store.binance_empty("BTCUSDT", "1h") == [(H0, H0 + 5 * H)]  # avant la cotation : mémorisé
    assert report.pairs[0].pending == [] and report.pairs[0].missing == 5
    fake.requests.clear()
    asyncio.run(d.download(["BTCUSDT"], H0, H0 + 30 * H))  # reprise : seulement les heures manquantes
    assert store.count("BTCUSDT", interval="1h") == 25
    from simtrade.history import to_ms
    assert int(fake.requests[0]["startTime"]) == to_ms(H0 + 20 * H)


# --- Agrégation ---


def test_agregation_1h_vers_4h_et_1d():
    day = hours(H0, [(100 + k, 110 + k, 90 + k, 101 + k) for k in range(24)])
    four = aggregate(day, "4h")
    assert [c.open_time for c in four] == [H0 + 4 * k * H for k in range(6)]
    assert (four[0].open, four[0].high, four[0].low, four[0].close) == (D(100), D(113), D(90), D(104))
    assert all(c.complete and c.interval == "4h" for c in four)
    daily = aggregate(day, "1d")
    assert len(daily) == 1 and (daily[0].open, daily[0].high, daily[0].low, daily[0].close) == (D(100), D(133), D(90), D(124))
    assert daily[0].close_time == H0 + 24 * H and daily[0].volume == D(24)
    assert aggregate(day[:23], "1d")[0].complete is False  # une heure manque


# --- Backtest swing ---


def test_entree_a_l_ouverture_de_la_barre_suivante_et_frais():
    data = hours(H0, [100, (102, 103, 101, 102), 102, 102])
    r = backtest.run(swing(), data, H0 + H, H0 + 4 * H, NOSPREAD, entries=once())
    buy = r.orders[0]
    assert buy.side is Side.BUY and buy.price == D(102) and buy.created_at == H0 + H + timedelta(seconds=1)
    assert abs(buy.fee / (buy.price * buy.quantity) - D("0.001")) < D("0.000001")  # frais 0,1 % par défaut
    entry = next(s for s in r.signals if s.decision == "ordre")
    assert entry.details["objectif"] is None and D(entry.details["distance_suiveur"]) > 0
    # taille au risque : 1 % de 10 000 = 100 USDT risqués sur une distance de 5 % -> ~2 000 USDT (plafond 20 %)
    assert D(entry.details["montant"]) == D("2000.00")


def test_suiveur_teste_au_niveau_d_avant_puis_releve():
    # entrée à 100 (stop 95, distance 5) ; barre d'entrée : haut 110 (stop relevé à 105) mais bas 96 -> pas de sortie
    data = hours(H0, [100, (100, 110, 96, 108), (108, 109, 104, 106), 106])
    r = backtest.run(swing(), data, H0 + H, H0 + 4 * H, NOSPREAD, entries=once())
    sells = [o for o in r.orders if o.side is Side.SELL]
    assert len(sells) == 1
    assert sells[0].kind == TRAILING and sells[0].price == D(105) and sells[0].created_at == H0 + 2 * H + 30 * timedelta(minutes=1)
    assert r.trades[0].exit_reason == "stop"


def test_suiveur_ouverture_sous_le_niveau_sortie_a_l_ouverture():
    data = hours(H0, [100, (100, 110, 99, 108), (101, 102, 100, 101), 101])
    r = backtest.run(swing(), data, H0 + H, H0 + 4 * H, NOSPREAD, entries=once())
    sell = next(o for o in r.orders if o.side is Side.SELL)
    assert sell.price == D(101) and sell.trigger_price == D(105) and sell.created_at == H0 + 2 * H


def test_suiveur_ne_descend_jamais():
    data = hours(H0, [100, (100, 110, 100, 108), (108, 108, 106, 106), (106, 106, 104, 105)])
    r = backtest.run(swing(), data, H0 + H, H0 + 4 * H, NOSPREAD, entries=once())
    sell = next(o for o in r.orders if o.side is Side.SELL)
    assert sell.price == D(105) and sell.created_at == H0 + 3 * H + 30 * timedelta(minutes=1)


def test_stop_avant_objectif_dans_la_meme_barre():
    s = swing(stop={"type": "pct", "valeur": "2"}, objectif={"type": "pct", "valeur": "3"})
    data = hours(H0, [100, (100, 104, 97, 100), 100])
    r = backtest.run(s, data, H0 + H, H0 + 3 * H, NOSPREAD, entries=once())
    sell = next(o for o in r.orders if o.side is Side.SELL)
    assert sell.kind == STOP and sell.price == D(98)


def test_objectif_touche_sans_stop():
    s = swing(stop={"type": "pct", "valeur": "2"}, objectif={"type": "pct", "valeur": "3"})
    data = hours(H0, [100, (100, 104, 99, 103), 103])
    r = backtest.run(s, data, H0 + H, H0 + 3 * H, NOSPREAD, entries=once())
    assert next(o for o in r.orders if o.side is Side.SELL).kind == TARGET


def test_swing_garde_la_position_apres_23h55_plusieurs_jours():
    start = datetime(2026, 3, 1, 21, tzinfo=UTC)
    data = hours(start, [100 + k * D("0.1") for k in range(60)])  # hausse lente sur 2,5 jours
    r = backtest.run(swing(), data, start + H, start + 60 * H, NOSPREAD, entries=once())
    assert [o.side for o in r.orders] == [Side.BUY]
    assert not any(o.kind == SESSION_CLOSE for o in r.orders) and r.halts == []
    assert r.curve.points[-1][0] == start + 60 * H and len(r.curve.points) == 59  # une valeur par heure


def test_donnees_et_horizon_coherents():
    with pytest.raises(InvalidInput, match="klines 1h"):
        backtest.run(swing(), [hour(H0, 100, interval="1m")], H0, H0 + H)
    intraday = strategies.validate({**strategies.model("tendance-1h")})
    with pytest.raises(InvalidInput, match="klines 1m"):
        backtest.run(intraday, hours(H0, [100, 100]), H0, H0 + H)
    with pytest.raises(InvalidInput, match="intervalle invalide"):
        backtest.Market(hours(H0, [100]), "15m")


def test_garde_fou_swing_par_defaut_dans_le_backtest():
    r = backtest.run(swing(), hours(H0, [100, 100, 100]), H0 + H, H0 + 3 * H, NOSPREAD, entries=once())
    assert r.config.limits.swing and r.config.limits.max_week_loss_pct == D(4)
    assert r.config.fee_rate == D("0.001")


# --- Brique, univers, modèle d'essai ---


def test_cassure_donchian():
    candles = hours(H0, [(100, 105, 95, 100)] * 20 + [(100, 106, 99, 105.5)])
    view = bricks.MarketView(H0 + 21 * H, candles, [])
    res = bricks.cassure_donchian(view, {"n": 20})
    assert res.ok is True and res.values["donchian_haut"] == D(105)
    assert bricks.cassure_donchian(bricks.MarketView(H0 + 20 * H, candles[:20], []), {"n": 20}).ok is None
    with pytest.raises(InvalidInput):  # réservée au swing
        strategies.validate({**strategies.model("tendance-1h"), "entree": [{"brique": "cassure_donchian"}]})


def test_univers_swing_10_paires_live_inchange():
    assert len(SWING_PAIRS) == 10 and set(PAIRS) <= set(SWING_PAIRS)
    assert PAIRS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_modele_d_essai_valide_hors_catalogue_et_rejouable():
    raw = strategies.model("essai-donchian")
    s = strategies.validate(raw)
    assert (s.swing, s.intervalle, s.trailing, s.risque_pct, s.objectif.name) == (True, "1d", True, D(1), "aucun")
    assert strategies.validate(s.to_json()) == s
    assert "essai-donchian" not in strategies.models() and "essai-donchian" not in catalogue.GRID
    # 30 jours plats puis 10 jours de hausse : cassure : rejoué sur des klines 1h agrégées en 1d
    bars = [(100, 101, 99, 100)] * (24 * 30) + [(100 + k / 12, 101 + k / 12, 99 + k / 12, 100 + k / 12) for k in range(24 * 10)]
    data = hours(H0, bars)
    r = backtest.run(s, data, H0 + 25 * 24 * H, H0 + 40 * 24 * H, NOSPREAD)
    assert r.decisions == 15 and r.entries >= 1
    assert r.orders[0].created_at.hour == 0  # décision à la clôture 1d (00:00 UTC), entrée sur la barre 1h suivante


# --- Live : fenêtre swing ---


class _Svc:
    def __init__(self, stored):
        self.stored = stored
        self.clock = lambda: None

    async def run(self, fn, *args):
        return fn(*args)

    def swing_history(self, pair, start, end):
        return [c for c in self.stored if start <= c.open_time < end]


def test_fenetre_live_swing_marche_db_plus_hub():
    stored = hours(H0, [100] * 48)
    recent = [hour(H0 + 47 * H, 100, 120, 99, 118)] + hours(H0 + 48 * H, [118] * 3)  # le hub fait foi
    hub = SimpleNamespace(market=SimpleNamespace(candles=lambda pair, iv: recent if iv == "1h" else []))
    runner = StrategyRunner(_Svc(stored), hub, clock=lambda: H0 + 51 * H + timedelta(seconds=5))
    s = swing(intervalle="1d")
    got = asyncio.run(runner.swing_candles(s, H0 + 51 * H + timedelta(seconds=5)))
    assert [c.open_time for c in got] == [H0, H0 + 24 * H]  # jour en cours (pas clos) exclu
    assert got[-1].high == D(120) and got[-1].close == D(118) and got[-1].complete


def test_modele_d_essai_valide_sur_les_10_paires_swing_seulement():
    for pair in SWING_PAIRS:
        s = strategies.validate(strategies.with_overrides(strategies.model("essai-donchian"), {"paire": pair}))
        assert s.paire == pair
    with pytest.raises(InvalidInput, match="paire"):
        strategies.validate(strategies.with_overrides(strategies.model("tendance-ema"), {"paire": "BNBUSDT"}))