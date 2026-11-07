"""Téléchargement des klines 1m (faux Binance par httpx.MockTransport), sans réseau."""

import asyncio
from types import SimpleNamespace

import pytest

from etape7 import MIN, T0, FakeBinance, minute_rows
from simtrade.download import MIN_FREE_BYTES, DiskFull, Downloader
from simtrade.errors import FeedUnavailable
from simtrade.history import BinanceHistory, RateLimited
from simtrade.marche import MarketStore

END = T0 + 25 * MIN
NOW = T0 + 60 * MIN


def big_disk(path):
    return SimpleNamespace(free=100 * 1024**3)


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def make(tmp_path, fake, page_size=10, disk=big_disk, store=None):
    store = store or MarketStore(tmp_path / "marche.db")
    sleeps = Sleeps()
    history = BinanceHistory(fake.client(), page_size=page_size)
    d = Downloader(store, history, sleep=sleeps, disk_usage=disk, clock=lambda: NOW)
    return d, store, sleeps


def run(d, pairs=("BTCUSDT",), start=T0, end=END):
    return asyncio.run(d.download(list(pairs), start, end))


def test_pagination(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25)})
    d, store, _ = make(tmp_path, fake)
    report = run(d)
    assert store.count("BTCUSDT") == 25
    assert report.pairs[0].rows == 25
    from simtrade.history import to_ms
    assert [int(r["startTime"]) for r in fake.requests] == [to_ms(T0 + k * MIN) for k in (0, 10, 20)]  # 10 + 10 + 5
    assert all(int(r["endTime"]) == to_ms(END) - 1 for r in fake.requests)
    assert all(r["limit"] == "10" for r in fake.requests)


def test_reprise_et_idempotence(tmp_path):
    rows = minute_rows(T0, 25)
    fake = FakeBinance({"BTCUSDT": rows})
    d, store, _ = make(tmp_path, fake)
    run(d, end=T0 + 12 * MIN)  # premier téléchargement arrêté après 12 minutes
    assert store.count("BTCUSDT") == 12
    fake.requests.clear()
    report = run(d)
    assert store.count("BTCUSDT") == 25
    assert report.pairs[0].rows == 13  # reprise après la dernière minute enregistrée
    from simtrade.history import to_ms
    assert int(fake.requests[0]["startTime"]) == to_ms(T0 + 12 * MIN)
    before = store.candles("BTCUSDT", T0, END)
    fake.requests.clear()
    report = run(d)  # deux fois : rien de plus, mêmes lignes
    assert report.pairs[0].rows == 0 and fake.requests == []
    assert store.candles("BTCUSDT", T0, END) == before


def test_trous_rapportes(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25, skip={5, 6, 7})})
    d, store, _ = make(tmp_path, fake)
    report = run(d)
    assert report.pairs[0].gaps == [(T0 + 5 * MIN, T0 + 8 * MIN)]
    assert report.pairs[0].missing == 3
    assert store.count("BTCUSDT") == 22


def test_429_puis_reprise(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25)}, fail_first=2, status=429, retry_after="7")
    d, store, sleeps = make(tmp_path, fake)
    report = run(d)
    assert sleeps.calls == [7.0, 7.0]
    assert report.waits == [7.0, 7.0]
    assert store.count("BTCUSDT") == 25


def test_418_sans_retry_after(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 5)}, fail_first=1, status=418, retry_after="")
    d, store, sleeps = make(tmp_path, fake)
    run(d)
    assert sleeps.calls == [60.0]
    assert store.count("BTCUSDT") == 5


def test_429_message_inchange_pour_le_live():
    fake = FakeBinance({}, fail_first=1)
    history = BinanceHistory(fake.client())
    with pytest.raises(RateLimited, match="Historique Binance indisponible") as exc:
        asyncio.run(history.klines("BTCUSDT", "1m", 10))
    assert isinstance(exc.value, FeedUnavailable) and exc.value.retry_after == 7


def test_poids_eleve_attend_la_minute_suivante(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 5)}, weight=5000)
    d, _, sleeps = make(tmp_path, fake)
    run(d)
    assert sleeps.calls == [61 - NOW.second]
    assert d.history.used_weight == 5000


def test_refus_espace_disque(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25)})
    tight = lambda path: SimpleNamespace(free=MIN_FREE_BYTES + 1000)  # 25 lignes x 200 o > 1000 o
    d, store, _ = make(tmp_path, fake, disk=tight)
    with pytest.raises(DiskFull, match="garder au moins 5 Go"):
        run(d)
    assert fake.requests == [] and store.count() == 0


def test_minutes_cloturees_seulement(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 80)})
    d, store, _ = make(tmp_path, fake)
    run(d, end=T0 + 200 * MIN)  # fin dans le futur : bornée à la minute en cours (NOW)
    assert store.count("BTCUSDT") == 60


def test_plusieurs_paires(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25), "ETHUSDT": minute_rows(T0, 25, skip={0})})
    d, store, _ = make(tmp_path, fake)
    report = run(d, pairs=("BTCUSDT", "ETHUSDT"))
    assert store.count("BTCUSDT") == 25 and store.count("ETHUSDT") == 24
    assert report.pairs[1].gaps == [(T0, T0 + MIN)]


def test_comble_l_ecart_entre_telechargement_et_live(tmp_path):
    from simtrade.history import parse_kline, to_ms
    from simtrade.marche import LIVE

    rows = minute_rows(T0, 40)
    store = MarketStore(tmp_path / "marche.db")
    store.save([parse_kline(r, "BTCUSDT", "1m") for r in rows[:10]])  # bloc téléchargé
    store.save([parse_kline(r, "BTCUSDT", "1m") for r in rows[30:40]], LIVE)  # bloc live, plus loin
    fake = FakeBinance({"BTCUSDT": rows})
    d, store, _ = make(tmp_path, fake, store=store)
    report = run(d, end=T0 + 40 * MIN)
    # seul l'écart [10, 30) est demandé (pages de 10), rien avant ni après
    assert [int(r["startTime"]) for r in fake.requests] == [to_ms(T0 + 10 * MIN), to_ms(T0 + 20 * MIN)]
    assert {int(r["endTime"]) for r in fake.requests} == {to_ms(T0 + 30 * MIN) - 1}
    assert report.pairs[0].rows == 20 and store.count("BTCUSDT") == 40
    assert report.pairs[0].gaps == [] and store.gaps("BTCUSDT", T0, T0 + 40 * MIN) == []


def test_plage_vide_chez_binance_demandee_une_seule_fois(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 25, skip=set(range(8, 14)))})  # panne de Binance 8–13
    d, store, _ = make(tmp_path, fake)
    report = run(d)
    pr = report.pairs[0]
    assert pr.gaps == [(T0 + 8 * MIN, T0 + 14 * MIN)]
    assert pr.binance == [(T0 + 8 * MIN, T0 + 14 * MIN)] and pr.pending == []
    assert store.binance_empty("BTCUSDT") == [(T0 + 8 * MIN, T0 + 14 * MIN)]
    fake.requests.clear()
    report = run(d)  # relance : la plage vide n'est plus redemandée
    assert fake.requests == []
    assert report.pairs[0].binance == [(T0 + 8 * MIN, T0 + 14 * MIN)] and report.pairs[0].pending == []


def test_plage_vide_recente_pas_memorisee(tmp_path):
    fake = FakeBinance({"BTCUSDT": minute_rows(T0, 55)})  # Binance n'a pas encore publié 55–59
    d, store, _ = make(tmp_path, fake)
    report = run(d, end=NOW)
    assert store.binance_empty("BTCUSDT") == [] and report.pairs[0].pending == [(T0 + 55 * MIN, NOW)]
