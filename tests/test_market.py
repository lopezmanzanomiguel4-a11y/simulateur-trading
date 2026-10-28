"""Bougies et indicateurs avec des données fixes, sans réseau."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from simtrade import market
from simtrade.market import Candle, CandleSeries, MarketData, bucket_start
from simtrade.models import Quote, Trade

D = Decimal
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def closes(*values: str) -> list[Decimal]:
    return [D(v) for v in values]


def candle(open_: str, close: str, volume: str = "0", notional: str = "0") -> Candle:
    return Candle("BTCUSDT", "1m", T0, D(open_), D(open_), D(close), D(close), D(volume), D(notional))


# --- Découpage en bougies ---


def test_bucket_start():
    assert bucket_start(datetime(2025, 3, 4, 10, 7, 30, tzinfo=timezone.utc), 300) == datetime(
        2025, 3, 4, 10, 5, tzinfo=timezone.utc
    )
    assert bucket_start(at(1), 60) == at(1)  # le début d'une bougie lui appartient
    assert bucket_start(at(1, -0.000001), 60) == at(0)


def test_ohlc_d_une_bougie():
    s = CandleSeries("BTCUSDT", "1m")
    for sec, price in ((5, "100"), (20, "105"), (40, "98"), (59, "101")):
        s.add_price(at(0, sec), D(price))
    (c,) = s.last()
    assert (c.open, c.high, c.low, c.close, c.ticks) == (D("100"), D("105"), D("98"), D("101"), 4)
    assert c.open_time == T0 and c.close_time == at(1)
    assert not c.is_closed(at(0, 59)) and c.is_closed(at(1))


def test_trous_combles_par_des_bougies_plates():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_price(at(0, 10), D("100"))
    s.add_price(at(3, 10), D("110"))
    candles = s.last()
    assert [c.open_time for c in candles] == [at(0), at(1), at(2), at(3)]
    for flat in candles[1:3]:
        assert (flat.open, flat.high, flat.low, flat.close) == (D("100"),) * 4
        assert flat.ticks == 0 and flat.volume == 0
    assert (candles[3].open, candles[3].close, candles[3].ticks) == (D("110"), D("110"), 1)


def test_cotation_en_retard_ignoree():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_price(at(0, 10), D("100"))
    s.add_price(at(2, 10), D("110"))
    s.add_price(at(1, 30), D("1"))
    assert [c.low for c in s.last()] == [D("100"), D("100"), D("110")]


def test_nombre_de_bougies_plafonne():
    s = CandleSeries("BTCUSDT", "1m", maxlen=3)
    s.add_price(at(0), D("100"))
    s.add_price(at(10), D("110"))  # 9 bougies manquantes, seules les plus récentes sont gardées
    assert [c.open_time for c in s.last()] == [at(8), at(9), at(10)]
    assert s.last(2) == s.last()[-2:] and s.last(0) == []


def test_intervalle_d_une_heure():
    s = CandleSeries("BTCUSDT", "1h")
    for minute, price in ((1, "100"), (15, "120"), (30, "90"), (59, "95")):
        s.add_price(at(minute), D(price))
    (c,) = s.last()
    assert (c.open, c.high, c.low, c.close, c.ticks) == (D("100"), D("120"), D("90"), D("95"), 4)


def test_intervalle_inconnu():
    with pytest.raises(ValueError):
        CandleSeries("BTCUSDT", "2m")


# --- Transactions (volume) ---


def test_volume_et_notional():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_price(at(0, 1), D("100"))
    s.add_trade(at(0, 2), D("100"), D("0.5"))
    s.add_trade(at(0, 3), D("102"), D("1.5"))
    (c,) = s.last()
    assert (c.volume, c.notional) == (D("2.0"), D("203.0"))
    assert c.close == D("100")  # les transactions ne changent pas l'OHLC (mid)


def test_transaction_avant_toute_cotation_ignoree():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_trade(at(0), D("100"), D("1"))
    assert s.last() == []


def test_transaction_en_retard_va_dans_sa_bougie():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_price(at(0, 10), D("100"))
    s.add_price(at(1, 10), D("101"))
    s.add_trade(at(0, 50), D("100"), D("2"))
    assert [c.volume for c in s.last()] == [D("2"), D("0")]


def test_transaction_en_avance_ouvre_une_bougie():
    s = CandleSeries("BTCUSDT", "1m")
    s.add_price(at(0, 10), D("100"))
    s.add_trade(at(1, 5), D("103"), D("1"))
    s.add_price(at(1, 10), D("104"))
    c = s.last()[-1]
    assert c.open_time == at(1) and c.volume == D("1")
    assert (c.open, c.high, c.low, c.close, c.ticks) == (D("104"),) * 4 + (1,)


def test_market_data_utilise_le_mid_pour_tous_les_intervalles():
    m = MarketData(["BTCUSDT"])
    m.add_quote(Quote("BTCUSDT", D("100"), D("101"), "Test", at(0, 5)))
    m.add_trade(Trade("BTCUSDT", D("100.5"), D("2"), "Test", at(0, 6)))
    m.add_quote(Quote("ETHUSDT", D("1"), D("2"), "Test", at(0, 7)))  # paire non suivie : ignorée
    for interval in market.INTERVALS:
        (c,) = m.candles("BTCUSDT", interval)
        assert c.close == D("100.5") and c.volume == D("2") and c.notional == D("201.0")
    assert ("ETHUSDT", "1m") not in m.series


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
