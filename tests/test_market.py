"""Bougies (sur transactions, raccord avec les klines) et indicateurs, avec des données fixes, sans réseau."""

import typing
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from simtrade import market
from simtrade.market import ORIGIN_LIVE, ORIGIN_REST, Candle, CandleSeries, MarketData, bucket_start
from simtrade.models import Trade

D = Decimal
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
GRACE = timedelta(seconds=2)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def closes(*values: str) -> list[Decimal]:
    return [D(v) for v in values]


def candle(open_: str, close: str, volume: str = "0", notional: str = "0", minute: int = 0) -> Candle:
    return Candle("BTCUSDT", "1m", at(minute), D(open_), D(open_), D(close), D(close), D(volume), D(notional))


def kline(minute: int, o: str, h: str, low: str, c: str, volume: str = "1", trades: int = 1) -> Candle:
    return Candle(
        "BTCUSDT", "1m", at(minute), D(o), D(h), D(low), D(c), D(volume), D(c) * D(volume), trades, ORIGIN_REST
    )


def series_with(*trades: tuple[float, str, str]) -> CandleSeries:
    """Série 1m sans trou initial : (secondes depuis T0, prix, quantité)."""
    s = CandleSeries("BTCUSDT", "1m")
    s.live_since = T0 - timedelta(minutes=1)  # flux actif depuis avant la première bougie
    for sec, price, qty in trades:
        s.add_trade(at(0, sec), D(price), D(qty))
    return s


def test_intervalles_coherents():
    assert set(typing.get_args(market.IntervalName)) == set(market.INTERVALS) == set(market.MAX_CANDLES)
    assert set(typing.get_args(market.VwapAnchor)) == set(market.VWAP_ANCHORS)


# --- Découpage en bougies ---


def test_bucket_start():
    assert bucket_start(datetime(2025, 3, 4, 10, 7, 30, tzinfo=timezone.utc), 300) == datetime(
        2025, 3, 4, 10, 5, tzinfo=timezone.utc
    )
    assert bucket_start(at(1), 60) == at(1)  # le début d'une bougie lui appartient
    assert bucket_start(at(1, -0.001), 60) == at(0)


def test_ohlc_sur_les_transactions():
    s = CandleSeries("BTCUSDT", "1m")
    for sec, price, qty in ((5, "100", "1"), (20, "105", "0.5"), (40, "98", "2"), (59, "101", "1")):
        s.add_trade(at(0, sec), D(price), D(qty), count=2)
    (c,) = s.last()
    assert (c.open, c.high, c.low, c.close) == (D("100"), D("105"), D("98"), D("101"))
    assert (c.volume, c.notional, c.transactions) == (D("4.5"), D("100") + D("52.5") + D("196") + D("101"), 8)
    assert c.origin == ORIGIN_LIVE and c.close_time == at(1)
    assert not c.is_closed(at(0, 59)) and c.is_closed(at(1))


def test_premiere_bougie_incomplete_puis_complete():
    s = CandleSeries("BTCUSDT", "1m")  # au démarrage : on a pu rater le début de la minute
    s.add_trade(at(0, 30), D("100"), D("1"))
    s.add_trade(at(1, 0), D("101"), D("1"))
    assert [c.complete for c in s.last()] == [False, True]
    assert s.live_since == at(0, 30)


def test_bougie_commencant_a_la_premiere_transaction_recue_incomplete():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_trade(at(1, 0), D("100"), D("1"))  # pile au début : une transaction du même instant a pu manquer
    assert not s.last()[0].complete


def test_trous_combles_par_des_bougies_plates():
    s = series_with((10, "100", "1"), (190, "110", "2"))  # 00:00:10 puis 00:03:10
    candles = s.last()
    assert [c.open_time for c in candles] == [at(0), at(1), at(2), at(3)]
    for flat in candles[1:3]:
        assert (flat.open, flat.high, flat.low, flat.close) == (D("100"),) * 4
        assert flat.volume == 0 and flat.transactions == 0
    assert (candles[3].open, candles[3].close, candles[3].volume) == (D("110"), D("110"), D("2"))


def test_bougies_apres_une_coupure_marquees_incompletes():
    s = series_with((10, "100", "1"))
    s.mark_gap()
    s.add_trade(at(3, 10), D("110"), D("1"))
    assert [c.complete for c in s.last()] == [True, False, False, False]


def test_transaction_en_retard():
    s = series_with((10, "100", "1"), (70, "101", "1"))
    s.add_trade(at(0, 50), D("90"), D("2"))  # bougie complète : ignorée (une kline la comptera)
    assert s.last()[0].volume == D("1")
    s.candles[0].complete = False
    s.add_trade(at(0, 50), D("90"), D("2"))  # bougie live incomplète : ajoutée, sans changer la clôture
    c = s.last()[0]
    assert (c.volume, c.low, c.close) == (D("3"), D("90"), D("100"))


def test_nombre_de_bougies_plafonne():
    s = CandleSeries("BTCUSDT", "1m", maxlen=3)
    s.add_trade(at(0), D("100"), D("1"))
    s.add_trade(at(10), D("110"), D("1"))  # 9 bougies manquantes, seules les plus récentes sont gardées
    assert [c.open_time for c in s.last()] == [at(8), at(9), at(10)]
    assert s.last(2) == s.last()[-2:] and s.last(0) == []
    assert CandleSeries("BTCUSDT", "1m").maxlen == 1440 and CandleSeries("BTCUSDT", "1h").maxlen == 500


def test_intervalle_d_une_heure():
    s = CandleSeries("BTCUSDT", "1h")
    for minute, price in ((1, "100"), (15, "120"), (30, "90"), (59, "95")):
        s.add_trade(at(minute), D(price), D("1"))
    (c,) = s.last()
    assert (c.open, c.high, c.low, c.close, c.volume) == (D("100"), D("120"), D("90"), D("95"), D("4"))


def test_intervalle_inconnu():
    with pytest.raises(ValueError):
        CandleSeries("BTCUSDT", "2m")


def test_market_data_date_avec_l_heure_de_l_exchange():
    m = MarketData(["BTCUSDT"])
    # reçue à 00:01:00.2 mais exécutée à 00:00:59.9 : elle appartient à la bougie de 00:00
    m.add_trade(Trade("BTCUSDT", D("100"), D("2"), "Test", at(1, 0.2), at(0, 59.9), count=3))
    m.add_trade(Trade("ETHUSDT", D("1"), D("1"), "Test", at(0, 7), at(0, 7)))  # paire non suivie : ignorée
    for interval in market.INTERVALS:
        (c,) = m.candles("BTCUSDT", interval)
        assert c.open_time == at(0) and c.volume == D("2") and c.transactions == 3
    assert ("ETHUSDT", "1m") not in m.series


# --- Raccord avec les klines ---


def test_kline_cloturee_remplace_la_bougie_live():
    s = series_with((10, "100", "1"), (70, "101", "1"))
    replaced = s.merge([kline(0, "99", "102", "98", "100.5", "5", 40)], now=at(1, 30))
    c = s.last()[0]
    assert (c.open, c.high, c.close, c.volume, c.transactions) == (D("99"), D("102"), D("100.5"), D("5"), 40)
    assert c.origin == ORIGIN_REST and c.complete
    ((old, new),) = replaced
    assert old.volume == D("1") and old.origin == ORIGIN_LIVE and new is c  # copie live gardée pour mesurer l'écart


def test_kline_en_cours_amorce_la_bougie_live():
    s = CandleSeries("BTCUSDT", "1m")
    s.merge([kline(0, "100", "101", "99", "100", "3", 10), kline(1, "100", "100", "100", "100", "1", 2)], now=at(1, 20))
    old, current = s.last()
    assert old.origin == ORIGIN_REST and old.complete
    assert current.origin == ORIGIN_LIVE and not current.complete  # instant du raccord ambigu
    s.add_trade(at(1, 25), D("103"), D("1"))
    assert (current.high, current.close, current.volume, current.transactions) == (D("103"), D("103"), D("2"), 3)


def test_raccord_sans_doublon():
    s = series_with((10, "100", "1"), (70, "101", "1"), (130, "102", "1"))
    s.merge([kline(1, "101", "101", "101", "101"), kline(2, "102", "102", "102", "102")], now=at(2, 30))
    assert [c.open_time for c in s.last()] == [at(0), at(1), at(2)]
    assert [c.origin for c in s.last()] == [ORIGIN_LIVE, ORIGIN_REST, ORIGIN_LIVE]


def test_trou_comble_par_l_historique():
    s = series_with((10, "100", "1"))
    s.mark_gap()  # coupure de 00:01 à 00:04
    klines = [kline(m, "100", "100", "100", str(100 + m)) for m in range(0, 5)]
    s.merge(klines, now=at(4, 10))
    assert [c.open_time for c in s.last()] == [at(m) for m in range(5)]


def test_merge_closed_only_ne_touche_pas_la_bougie_en_cours():
    s = series_with((10, "100", "1"), (70, "101", "1"))
    s.merge([kline(0, "99", "99", "99", "99"), kline(1, "1", "1", "1", "1")], now=at(1, 30), closed_only=True)
    assert s.last()[0].origin == ORIGIN_REST
    assert s.last()[1].close == D("101") and s.last()[1].origin == ORIGIN_LIVE


def test_merge_ignore_une_autre_serie():
    s = series_with((10, "100", "1"))
    other = Candle("ETHUSDT", "1m", at(0), D(1), D(1), D(1), D(1))
    assert s.merge([other], now=at(2)) == [] and s.last()[0].close == D("100")


def test_resume_from():
    s = CandleSeries("BTCUSDT", "1m", maxlen=10)
    assert s.resume_from(at(0)) is None  # rien en mémoire : rechargement complet
    s.add_trade(at(0, 5), D("100"), D("1"))
    s.add_trade(at(2, 5), D("101"), D("1"))
    assert s.resume_from(at(5)) == at(0)  # plus ancienne bougie live non vérifiée
    s.mark_checked(at(5), GRACE)
    assert s.resume_from(at(5)) == at(2)  # toutes vérifiées : dernière bougie
    assert s.resume_from(at(12)) is None  # trou plus grand que la série : rechargement complet


def test_bougies_live_a_verifier():
    s = series_with((10, "100", "1"), (70, "101", "1"))
    assert s.pending_check(at(1, 1), GRACE) is None  # délai de grâce pas écoulé
    assert s.pending_check(at(1, 2), GRACE) == at(0)
    s.mark_checked(at(1, 2), GRACE)
    assert s.pending_check(at(1, 30), GRACE) is None
    assert s.pending_check(at(2, 2), GRACE) == at(1)


# --- Indicateurs ---


def test_sma():
    assert market.sma(closes("1", "2", "3", "4", "5"), 3) == D("4.00000000")
    assert market.sma(closes("1", "2", "4"), 3) == D("2.33333333")
    assert market.sma(closes("1", "2"), 3) is None


def test_ema():
    # alpha = 0,5 ; amorce = (1 + 2 + 3) / 3 = 2 ; puis 3 ; puis 4
    assert market.ema(closes("1", "2", "3", "4", "5"), 3) == D("4.00000000")
    # alpha = 2/3 ; amorce = 15 ; 2/3 x 30 + 1/3 x 15 = 25 (l'arrondi absorbe l'inexactitude de 2/3)
    assert market.ema(closes("10", "20", "30"), 2) == D("25.00000000")
    assert market.ema(closes("1", "2", "3"), 3) == D("2.00000000")  # = SMA quand il n'y a que n clôtures
    assert market.ema(closes("1"), 2) is None


def test_rsi_wilder():
    # variations +2, -1, +2 ; amorce : hausse 1, baisse 0,5 ; lissage : hausse 1,5, baisse 0,25 ; RS = 6
    assert market.rsi(closes("10", "12", "11", "13"), 2) == D("85.71428571")  # 100 - 100/7


def test_rsi_cas_limites():
    assert market.rsi(closes("1", "2", "3"), 2) == D("100")
    assert market.rsi(closes("5", "5", "5"), 2) == D("50")
    assert market.rsi(closes("3", "2", "1"), 2) == D(0)
    assert market.rsi(closes("1", "2"), 2) is None  # il faut n + 1 clôtures


def test_vwap():
    candles = [candle("100", "100", "1", "100"), candle("200", "200", "3", "600")]
    assert market.vwap(candles) == D("175.00000000")
    assert market.vwap([candle("100", "100")]) is None  # pas de volume
    assert market.vwap([]) is None


def test_variation_pct():
    candles = [candle("100", "105"), candle("105", "110")]
    assert market.variation_pct(candles) == D("10.00000000")
    assert market.variation_pct([candle("200", "150")]) == D("-25.00000000")
    assert market.variation_pct([]) is None


def test_bougies_cloturees():
    candles = [candle("1", "1", minute=0), candle("1", "1", minute=1)]
    assert market.closed(candles, at(1, 30)) == candles[:1]
    assert market.closed(candles, at(2)) == candles


def test_vwap_de_session_remis_a_zero_a_minuit_utc():
    day = datetime(2025, 1, 2, tzinfo=timezone.utc)
    before = Candle("BTCUSDT", "1m", day - timedelta(minutes=1), D(1), D(1), D(1), D(1), D("10"), D("1000"))
    first = Candle("BTCUSDT", "1m", day, D(1), D(1), D(1), D(1), D("1"), D("100"))
    second = Candle("BTCUSDT", "1m", day + timedelta(minutes=1), D(1), D(1), D(1), D(1), D("3"), D("330"))
    r = market.vwap_session([before, first, second], now=day + timedelta(minutes=2, seconds=5))
    assert r.value == D("107.50000000")  # (100 + 330) / 4 : la bougie de 23:59 est exclue
    assert (r.anchor, r.start, r.end, r.count, r.volume) == ("jour_utc", day, day + timedelta(minutes=2), 2, D("4"))
    empty = market.vwap_session([before], now=day + timedelta(seconds=5))
    assert empty.value is None and empty.count == 0 and empty.start == day and empty.end is None


def test_vwap_glissant():
    candles = [candle("100", "100", "1", "100"), candle("200", "200", "3", "600", minute=1)]
    r = market.vwap_window(candles)
    assert (r.value, r.anchor, r.start, r.end, r.count) == (D("175.00000000"), "fenetre", at(0), at(2), 2)


def test_resultats_en_decimal():
    values = closes("10", "12", "11", "13")
    candles = [candle("10", "13", "1", "12")]
    results = [
        market.sma(values, 2),
        market.ema(values, 2),
        market.rsi(values, 2),
        market.vwap(candles),
        market.variation_pct(candles),
    ]
    assert all(isinstance(r, Decimal) for r in results)


# --- Qualité des données ---


def quality(**changes):
    now = at(10)
    params = dict(
        now=now,
        quote_time=now - timedelta(seconds=1),
        max_quote_age=5.0,
        trades_connected=True,
        last_trade_time=now - timedelta(seconds=1),
        trade_source="Binance",
        fallback=False,
        history="complet",
        last_sync=now,
        current=None,
        clock_offset=0.05,
        vwap_volume=D("1"),
    )
    params.update(changes)
    return market.assess_quality(**params)


def test_qualite_sans_probleme():
    q = quality()
    assert q.vwap_fiable and q.raisons == () and not q.prix_perime and not q.flux_transactions_muet


@pytest.mark.parametrize(
    "changes, reason, vwap_ok",
    [
        ({"quote_time": None}, "prix_perime", True),
        ({"quote_time": at(10) - timedelta(seconds=6)}, "prix_perime", True),
        ({"trades_connected": False}, "flux_transactions_muet", False),
        ({"last_trade_time": at(10) - timedelta(seconds=31)}, "flux_transactions_muet", False),
        ({"last_trade_time": None}, "flux_transactions_muet", False),
        ({"fallback": True, "trade_source": "Coinbase"}, "repli_coinbase", False),
        ({"history": "partiel"}, "historique_partiel", False),
        ({"history": "indisponible"}, "historique_indisponible", False),
        ({"vwap_volume": D(0)}, "volume_nul", False),
        ({"clock_offset": 1.5}, "horloge_decalee", True),
        ({"clock_offset": -1.2}, "horloge_decalee", True),
        ({"current": Candle("BTCUSDT", "1m", at(10), D(1), D(1), D(1), D(1), complete=False)}, "bougie_courante_incomplete", True),
    ],
)
def test_qualite_raisons(changes, reason, vwap_ok):
    q = quality(**changes)
    assert q.raisons == (reason,) and q.vwap_fiable is vwap_ok


def test_qualite_ecart_d_horloge_tolere():
    assert quality(clock_offset=0.2).raisons == () and quality(clock_offset=None).raisons == ()
