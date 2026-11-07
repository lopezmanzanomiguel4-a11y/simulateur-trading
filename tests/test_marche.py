"""Base des bougies (marche.db) et agrégation 1m → 5m/15m, sans réseau."""

from datetime import timedelta

import pytest

from etape7 import MIN, T0, D, kline_row, minute_rows
from simtrade import config
from simtrade.history import parse_kline
from simtrade.market import aggregate
from simtrade.marche import MarketStore, missing_minutes


def candles(rows):
    return [parse_kline(r, "BTCUSDT", "1m") for r in rows]


@pytest.fixture
def store(tmp_path):
    s = MarketStore(tmp_path / "marche.db")
    yield s
    s.close()


def test_upsert_idempotent(store):
    cs = candles(minute_rows(T0, 30))
    assert store.save(cs) == 30
    assert store.save(cs) == 30  # deux fois : mêmes lignes
    assert store.count("BTCUSDT") == 30
    assert store.candles("BTCUSDT", T0, T0 + 30 * MIN) == cs  # Decimal exacts, mêmes bougies


def test_upsert_remplace_les_valeurs(store):
    store.save(candles([kline_row(T0, c="100")]))
    store.save(candles([kline_row(T0, c="105")]), source="live")
    (got,) = store.candles("BTCUSDT", T0, T0 + MIN)
    assert got.close == D("105")
    assert store.count() == 1


def test_autres_intervalles_ignores(store):
    assert store.save([parse_kline(kline_row(T0, seconds=300), "BTCUSDT", "5m")]) == 0


def test_trous(store):
    store.save(candles(minute_rows(T0, 20, skip={3, 4, 10})))
    gaps = store.gaps("BTCUSDT", T0 - 2 * MIN, T0 + 22 * MIN)
    assert gaps == [
        (T0 - 2 * MIN, T0),
        (T0 + 3 * MIN, T0 + 5 * MIN),
        (T0 + 10 * MIN, T0 + 11 * MIN),
        (T0 + 20 * MIN, T0 + 22 * MIN),
    ]
    assert missing_minutes(gaps) == 7
    assert store.gaps("ETHUSDT", T0, T0 + 5 * MIN) == [(T0, T0 + 5 * MIN)]
    assert store.bounds("BTCUSDT") == (T0, T0 + 19 * MIN)


def test_chemin_a_cote_de_la_base_de_trading(tmp_path, monkeypatch):
    monkeypatch.delenv(config.MARCHE_ENV_VAR, raising=False)
    assert config.marche_db_path(tmp_path / "x" / "trading.db") == tmp_path / "x" / "marche.db"
    monkeypatch.setenv(config.DB_ENV_VAR, str(tmp_path / "tmp" / "essai.db"))
    assert config.marche_db_path() == tmp_path / "tmp" / "marche.db"  # SIMTRADE_DB temporaire → bougies aussi
    monkeypatch.setenv(config.MARCHE_ENV_VAR, str(tmp_path / "ailleurs.db"))
    assert config.marche_db_path(tmp_path / "x" / "trading.db") == tmp_path / "ailleurs.db"
    monkeypatch.delenv(config.MARCHE_ENV_VAR)
    assert config.marche_db_path(":memory:") == ":memory:"


def test_agregat_egal_aux_klines_de_l_intervalle():
    # 15 minutes, et les klines 5m que Binance renverrait pour la même période (calculées à la main)
    rows = [
        kline_row(T0 + 0 * MIN, "100", "102", "99.5", "101", "1", "100.5", 3),
        kline_row(T0 + 1 * MIN, "101", "103", "100", "102", "2", "203", 4),
        kline_row(T0 + 2 * MIN, "102", "102.5", "98", "99", "1.5", "150", 5),
        kline_row(T0 + 3 * MIN, "99", "100", "97", "98", "0.5", "49", 1),
        kline_row(T0 + 4 * MIN, "98", "99", "97.5", "98.5", "3", "295.5", 7),
    ]
    for i in range(5, 15):
        rows.append(kline_row(T0 + i * MIN, "98.5", "99", "98", "98.5", "1", "98.5", 1))
    expected_5m = [
        parse_kline(kline_row(T0, "100", "103", "97", "98.5", "8", "798", 20, seconds=300), "BTCUSDT", "5m"),
        parse_kline(kline_row(T0 + 5 * MIN, "98.5", "99", "98", "98.5", "5", "492.5", 5, seconds=300), "BTCUSDT", "5m"),
        parse_kline(kline_row(T0 + 10 * MIN, "98.5", "99", "98", "98.5", "5", "492.5", 5, seconds=300), "BTCUSDT", "5m"),
    ]
    expected_15m = [parse_kline(kline_row(T0, "100", "103", "97", "98.5", "18", "1783", 30, seconds=900), "BTCUSDT", "15m")]
    ones = candles(rows)
    assert aggregate(ones, "5m") == expected_5m
    assert aggregate(ones, "15m") == expected_15m


def test_agregat_minute_manquante_incomplet():
    ones = candles(minute_rows(T0, 10, skip={7}))
    five = aggregate(ones, "5m")
    assert [c.complete for c in five] == [True, False]
    assert five[1].open_time == T0 + 5 * MIN
