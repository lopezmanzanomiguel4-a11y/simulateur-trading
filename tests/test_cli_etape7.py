"""CLI de l'étape 7 : journal, stats, marche etat. Base temporaire, sans réseau."""

import json
from datetime import timedelta

import pytest

from etape7 import MIN, T0, kline_row
from simtrade.cli import main
from simtrade.feeds import StaticFeed
from simtrade.history import parse_kline
from simtrade.marche import MarketStore
from simtrade.service import TradingService
from simtrade.storage import Storage

PRICES = {"BTCUSDT": ("80000", "80001"), "ETHUSDT": ("2350", "2350.5"), "SOLUSDT": ("100", "100.02")}


@pytest.fixture
def run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SIMTRADE_MARCHE_DB", raising=False)
    db = tmp_path / "cli7.db"

    def _run(*argv):
        return main(list(argv), service=TradingService(Storage(db), StaticFeed(PRICES)))

    _run("comptes", "creer", "m", "10000")
    capsys.readouterr()
    return _run


def test_journal_et_stats(run, capsys):
    assert run("acheter", "BTCUSDT", "1000", "--compte", "m") == 0
    assert run("vendre", "BTCUSDT", "tout", "--compte", "m") == 0
    capsys.readouterr()
    assert run("journal", "--compte", "m") == 0
    out = capsys.readouterr().out
    assert out.count("ordre") >= 2 and "trade" in out and "sortie : manuel" in out
    assert run("stats", "--compte", "m") == 0
    out = capsys.readouterr().out
    assert "Taux de réussite" in out and "0,0 % (0/1)" in out and "Sessions (effectif)" in out


def test_journal_vide(run, capsys):
    assert run("journal", "--compte", "m") == 0
    assert "Journal vide." in capsys.readouterr().out


def test_marche_etat(run, capsys, tmp_path):
    store = MarketStore(tmp_path / "marche.db")
    store.save([parse_kline(kline_row(T0 + i * MIN), "BTCUSDT", "1m") for i in range(10) if i != 4])
    store.close()
    assert run("marche", "etat") == 0
    out = capsys.readouterr().out
    assert "marche.db" in out and "BTCUSDT" in out and "1 (1 min)" in out


def test_stats_date_invalide(run, capsys):
    assert run("stats", "--compte", "m", "--depuis", "hier") == 1
    assert "invalide" in capsys.readouterr().err


def test_marche_etat_distingue_trous_binance(run, capsys, tmp_path):
    store = MarketStore(tmp_path / "marche.db")
    store.save([parse_kline(kline_row(T0 + i * MIN), "BTCUSDT", "1m") for i in range(20) if i not in (4, 10, 11)])
    store.add_binance_empty("BTCUSDT", T0 + 10 * MIN, T0 + 12 * MIN)
    store.close()
    assert run("marche", "etat") == 0
    out = capsys.readouterr().out
    assert "Trous Binance" in out and "À télécharger" in out
    line = next(l for l in out.splitlines() if l.startswith("BTCUSDT"))
    assert "1 (2 min)" in line and "1 (1 min)" in line


def test_backtest_cli(run, capsys, tmp_path):
    from test_backtest import ones
    from test_references import walk_prices

    from etape7 import T0

    store = MarketStore(tmp_path / "marche.db")
    store.save(ones(T0, walk_prices(3 * 1440, seed=3)))
    store.close()
    day = T0 + timedelta(days=2)
    assert run("backtest", "retour-vwap", "ecart_vwap.seuil_pct=0.4", "--depuis", f"{day:%Y-%m-%d}",
               "--jusqu-a", f"{day + timedelta(days=1):%Y-%m-%d}") == 0
    out = capsys.readouterr().out
    assert "Backtest retour-vwap sur BTCUSDT 5m" in out and "Clôtures évaluées : 288" in out
    assert run("backtest", "retour-vwap", "inconnu.x=1", "--depuis", "2026-01-01", "--jusqu-a", "2026-01-02") == 1


def test_catalogue_cli_absent_puis_affiche(run, capsys, tmp_path):
    assert run("catalogue", "--sortie", str(tmp_path)) == 0
    assert "Aucun catalogue" in capsys.readouterr().out
    data = {"version": 1, "cree_le": "2026-11-07T18:00:00+00:00", "commit": "abc1234",
            "periodes": {"hors_echantillon": ["2026-09-07T00:00:00+00:00", "2026-11-07T00:00:00+00:00"]},
            "entrees": [], "resultats": [{"modele": "retour-vwap", "paire": "BTCUSDT", "statut": "rejetee_en_echantillon",
                                          "pnl_hors_echantillon": None, "trades_hors_echantillon": None,
                                          "percentile_tirages": None}]}
    (tmp_path / "catalogue.json").write_text(json.dumps(data), encoding="utf-8")
    assert run("catalogue", "--sortie", str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "rejetee_en_echantillon" in out and "catalogue vide" in out

