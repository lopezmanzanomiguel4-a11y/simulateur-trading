"""Migration v0 (étapes 1-3) -> v1 (étape 4) : sauvegarde, données intactes, annulation complète si échec."""

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from simtrade import migrations
from simtrade.migrations import MigrationError
from simtrade.models import MANUAL, MARKET
from simtrade.storage import Storage

D = Decimal

# Schéma exact de l'étape 3, figé ici : la migration doit toujours savoir partir de lui.
V0_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    initial_capital TEXT    NOT NULL,
    cash            TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_account ON accounts(active) WHERE active = 1;
CREATE TABLE IF NOT EXISTS positions (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    pair       TEXT    NOT NULL,
    quantity   TEXT    NOT NULL,
    cost_basis TEXT    NOT NULL,
    PRIMARY KEY (account_id, pair)
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    pair         TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    quantity     TEXT    NOT NULL,
    price        TEXT    NOT NULL,
    gross        TEXT    NOT NULL,
    fee          TEXT    NOT NULL,
    cash_flow    TEXT    NOT NULL,
    realized_pnl TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_by_account ON orders(account_id, id);
"""

RESET_AT = "2026-10-05T09:30:00+00:00"
ORDER_COLUMNS = "id, account_id, pair, side, quantity, price, gross, fee, cash_flow, realized_pnl, source, created_at"


def make_v0(path):
    conn = sqlite3.connect(path)
    conn.executescript(V0_SCHEMA)
    conn.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
        [(1, "10000", "8999", "2026-10-01T08:00:00+00:00", 0), (2, "2500.00", "1499.00", RESET_AT, 1)],
    )
    conn.executemany(
        "INSERT INTO positions VALUES (?, ?, ?, ?)",
        [(1, "BTCUSDT", "0.01", "1001"), (2, "ETHUSDT", "0.5", "1001")],
    )
    conn.executemany(
        f"INSERT INTO orders ({ORDER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "BTCUSDT", "achat", "0.01", "100000", "1000", "1", "-1001", "0", "Binance", "2026-10-01T08:01:00+00:00"),
            (2, 2, "ETHUSDT", "achat", "0.5", "2000", "1000", "1", "-1001", "0", "Binance", "2026-10-05T10:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()


def dump(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump()), conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_migration_conserve_tout(tmp_path):
    path = tmp_path / "v0.db"
    make_v0(path)
    orders_before = rows(path, f"SELECT {ORDER_COLUMNS} FROM orders ORDER BY id")
    positions_before = rows(path, "SELECT * FROM positions ORDER BY account_id")

    s = Storage(path)
    try:
        assert migrations.version(s.conn) == migrations.VERSION
        accounts = s.accounts()
        assert [(a.id, a.name, a.mode, a.current) for a in accounts] == [
            (1, "archive-1", MANUAL, False),
            (2, "principal", MANUAL, True),
        ]
        assert accounts[0].archived_at == datetime.fromisoformat(RESET_AT)  # date du reset
        assert accounts[1].archived_at is None
        assert rows(path, f"SELECT {ORDER_COLUMNS} FROM orders ORDER BY id") == orders_before
        assert rows(path, "SELECT * FROM positions ORDER BY account_id") == positions_before
        assert {(o.kind, o.slippage, o.trigger_price) for o in s.orders(1) + s.orders(2)} == {(MARKET, 0, None)}
        pf = s.load_portfolio()
        assert pf.account.id == 2 and pf.account.cash == D("1499.00") and pf.fees_paid == D("1")
        assert s.session_rules().close.strftime("%H:%M") == "23:55"

        backup = s.backup_path
        assert backup is not None and backup.exists() and backup.name.startswith(f"v0.db.avant-v{migrations.VERSION}-")
    finally:
        s.close()
    lines, version = dump(backup)
    assert version == 0 and rows(backup, "SELECT COUNT(*) FROM orders") == [(2,)]


def test_deuxieme_ouverture_ne_migre_ni_ne_sauvegarde(tmp_path):
    path = tmp_path / "v0.db"
    make_v0(path)
    Storage(path).close()
    s = Storage(path)
    try:
        assert s.backup_path is None
    finally:
        s.close()
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_base_neuve_creee_en_v1_sans_sauvegarde(tmp_path):
    s = Storage(tmp_path / "neuve.db")
    try:
        assert migrations.version(s.conn) == migrations.VERSION and s.backup_path is None
        assert s.active_account().name == "principal"
    finally:
        s.close()
    assert list(tmp_path.glob("*.bak")) == []


@pytest.mark.parametrize(
    "bad_step",
    [
        "ALTER TABLE accounts ADD COLUMN name TEXT",  # erreur SQL au milieu (colonne en double)
        "DELETE FROM orders WHERE id = 1",  # SQL valide, mais rattrapé par les contrôles avant COMMIT
    ],
)
def test_echec_au_milieu_annule_tout(tmp_path, monkeypatch, bad_step):
    path = tmp_path / "v0.db"
    make_v0(path)
    before = dump(path)
    steps = list(migrations.V1_STEPS)
    monkeypatch.setattr(migrations, "V1_STEPS", steps[:6] + [bad_step] + steps[6:])

    with pytest.raises(MigrationError, match="La base n'a pas été modifiée. Sauvegarde : ") as err:
        Storage(path)

    assert dump(path) == before  # même contenu, toujours en v0
    assert f".avant-v{migrations.VERSION}-" in str(err.value)
    monkeypatch.undo()
    s = Storage(path)  # nouvel essai sans la panne : la migration passe
    try:
        assert migrations.version(s.conn) == migrations.VERSION and len(s.orders(2)) == 1
    finally:
        s.close()
