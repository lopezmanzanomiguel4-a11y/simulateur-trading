"""Migration v2 (étape 5) -> v3 (étape 6) : tables des stratégies et des signaux, contraintes d'unicité."""

import sqlite3
from datetime import datetime, timezone

import pytest

from simtrade import migrations
from simtrade.models import AUTO
from simtrade.storage import SignalRecord, Storage
from test_migrations import make_v0, rows

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def make_v2(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    make_v0(path)
    with monkeypatch.context() as m:
        m.setattr(migrations, "VERSION", 2)
        s = Storage(path)
        assert migrations.version(s.conn) == 2
        s.close()
    for bak in tmp_path.glob("*.bak"):
        bak.unlink()
    return path


def test_v2_vers_v3(tmp_path, monkeypatch):
    path = make_v2(tmp_path, monkeypatch)
    before = rows(path, "SELECT * FROM orders ORDER BY id"), rows(path, "SELECT * FROM accounts ORDER BY id")
    with monkeypatch.context() as m:  # étape 7c : la dernière version est 4, ce test vérifie le passage à 3
        m.setattr(migrations, "VERSION", 3)
        s = Storage(path)
    try:
        assert migrations.version(s.conn) == 3
        assert s.backup_path is not None and s.backup_path.name.startswith("v2.db.avant-v3-")
        tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"strategy_activations", "signals"} <= tables
    finally:
        s.close()
    assert (rows(path, "SELECT * FROM orders ORDER BY id"), rows(path, "SELECT * FROM accounts ORDER BY id")) == before


def signal(account_id, activation_id, kind="entree"):
    return SignalRecord(account_id, activation_id, "BTCUSDT", "5m", NOW, kind, NOW, "ignore", ("x",), None, None, {})


def test_une_activation_et_une_decision_par_bougie(tmp_path):
    s = Storage(tmp_path / "u.db")
    try:
        account = s.create_account("robot", "1000", AUTO, NOW)
        first = s.activate_strategy(account.id, "a", "{}", NOW)
        second = s.activate_strategy(account.id, "b", "{}", NOW)
        assert s.activation(first.id).reason == "remplacee" and s.active_strategy(account.id).id == second.id
        with pytest.raises(sqlite3.IntegrityError):  # l'index interdit deux actives, même en SQL direct
            s.conn.execute(
                "INSERT INTO strategy_activations (account_id, name, definition, activated_at) VALUES (?, 'c', '{}', ?)",
                (account.id, NOW.isoformat()),
            )
        s.add_signal(signal(account.id, second.id))
        s.add_signal(signal(account.id, second.id, "sortie"))  # autre type : permis
        with pytest.raises(sqlite3.IntegrityError):
            s.add_signal(signal(account.id, second.id))
        assert s.has_signal(second.id, NOW, "entree") and len(s.signals()) == 2
    finally:
        s.close()
