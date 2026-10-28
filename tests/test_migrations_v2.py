"""Migration v1 (étape 4) -> v2 (étape 5), et v0 -> v2 d'une traite ; échec -> base intacte."""

import sqlite3
from decimal import Decimal

import pytest

from simtrade import migrations
from simtrade.migrations import MigrationError
from simtrade.risk import RiskLimits
from simtrade.storage import Storage
from test_migrations import dump, make_v0, rows

D = Decimal


def make_v1(tmp_path, monkeypatch):
    """Base de l'étape 4 : v0 migrée en v1 seulement, avec un ordre en attente."""
    path = tmp_path / "v1.db"
    make_v0(path)
    with monkeypatch.context() as m:
        m.setattr(migrations, "VERSION", 1)
        s = Storage(path)
        s.conn.execute(
            "INSERT INTO pending_orders (account_id, pair, kind, side, trigger_price, amount, quantity, status, "
            "created_at) VALUES (2, 'ETHUSDT', 'stop', 'vente', '1900', NULL, NULL, 'en_attente', "
            "'2026-10-27T10:00:00+00:00')"
        )
        assert migrations.version(s.conn) == 1
        s.close()
    for bak in tmp_path.glob("*.bak"):
        bak.unlink()
    return path


def test_v1_vers_v2(tmp_path, monkeypatch):
    path = make_v1(tmp_path, monkeypatch)
    before = rows(path, "SELECT * FROM orders ORDER BY id"), rows(path, "SELECT * FROM pending_orders")
    s = Storage(path)
    try:
        assert migrations.version(s.conn) == 2
        assert s.backup_path.name.startswith("v1.db.avant-v2-")
        assert rows(path, "SELECT * FROM orders ORDER BY id") == before[0]
        (pending,) = s.pending_orders()
        assert (pending.pair, pending.protection, pending.stop_price) == ("ETHUSDT", False, None)
        assert s.risk_limits(2) == RiskLimits()  # pas de ligne : défauts
        assert s.refusals() == [] and not s.session_halted(2, pending.created_at.date())
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        s.close()
    assert dump(s.backup_path)[1] == 1  # la sauvegarde est la base v1


def test_v0_vers_v2_en_une_ouverture(tmp_path):
    path = tmp_path / "v0.db"
    make_v0(path)
    s = Storage(path)
    try:
        assert migrations.version(s.conn) == 2 and s.backup_path.name.startswith("v0.db.avant-v2-")
        assert [a.name for a in s.accounts()] == ["archive-1", "principal"]
    finally:
        s.close()


def test_echec_v1_vers_v2_laisse_la_base_en_v1(tmp_path, monkeypatch):
    path = make_v1(tmp_path, monkeypatch)
    before = dump(path)
    monkeypatch.setattr(migrations, "V2_STEPS", migrations.V2_STEPS[:3] + ["CREATE TABLE accounts (x)"] + migrations.V2_STEPS[3:])
    with pytest.raises(MigrationError, match="La base n'a pas été modifiée. Sauvegarde : .*avant-v2-"):
        Storage(path)
    assert dump(path) == before


def test_echec_sur_une_base_v0_ne_la_passe_pas_en_wal(tmp_path, monkeypatch):
    path = tmp_path / "v0.db"
    make_v0(path)
    before = dump(path)
    monkeypatch.setattr(migrations, "V2_STEPS", ["CREATE TABLE accounts (x)"])
    with pytest.raises(MigrationError):
        Storage(path)
    assert dump(path) == before  # v1 annulée aussi : tout ou rien
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
