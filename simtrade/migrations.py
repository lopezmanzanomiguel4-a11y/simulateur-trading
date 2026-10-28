"""Migrations du schéma SQLite, suivies par PRAGMA user_version.

v0 : étapes 1 à 3 (un seul compte actif, ordres au marché).
v1 : étape 4 (comptes nommés manuel/auto en parallèle, ordres en attente, sessions, réglages).

Une base existante est d'abord sauvegardée (sqlite3 backup, cohérent même base ouverte), puis
migrée dans UNE transaction : en cas d'échec, ROLLBACK complet et la base reste en v0.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from simtrade.errors import SimTradeError
from simtrade.models import utcnow
from simtrade.sessions import DEFAULT_CLOSE, DEFAULT_LAST_ENTRY

VERSION = 1

V1_STEPS: list[str] = [
    "ALTER TABLE accounts ADD COLUMN name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE accounts ADD COLUMN mode TEXT NOT NULL DEFAULT 'manuel'",
    "ALTER TABLE accounts ADD COLUMN archived_at TEXT",
    "UPDATE accounts SET name = CASE WHEN active = 1 THEN 'principal' ELSE 'archive-' || id END",
    # Un ancien compte inactif a été archivé par le reset qui a ouvert le compte suivant.
    "UPDATE accounts SET archived_at = COALESCE("
    "(SELECT n.created_at FROM accounts n WHERE n.id > accounts.id ORDER BY n.id LIMIT 1), created_at) "
    "WHERE active = 0",
    "CREATE UNIQUE INDEX open_account_name ON accounts(name) WHERE archived_at IS NULL",
    "ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'marche'",
    "ALTER TABLE orders ADD COLUMN trigger_price TEXT",
    "ALTER TABLE orders ADD COLUMN slippage TEXT NOT NULL DEFAULT '0'",
    "ALTER TABLE orders ADD COLUMN pending_id INTEGER",
    """CREATE TABLE pending_orders (
        id            INTEGER PRIMARY KEY,
        account_id    INTEGER NOT NULL REFERENCES accounts(id),
        pair          TEXT    NOT NULL,
        kind          TEXT    NOT NULL,
        side          TEXT    NOT NULL,
        trigger_price TEXT    NOT NULL,
        amount        TEXT,
        quantity      TEXT,
        group_id      INTEGER,
        status        TEXT    NOT NULL,
        reason        TEXT,
        created_at    TEXT    NOT NULL,
        closed_at     TEXT,
        order_id      INTEGER REFERENCES orders(id)
    )""",
    "CREATE INDEX pending_by_status ON pending_orders(status, account_id)",
    """CREATE TABLE sessions (
        id         INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id),
        date       TEXT    NOT NULL,
        closed_at  TEXT    NOT NULL,
        late       INTEGER NOT NULL
    )""",
    "CREATE INDEX sessions_by_account ON sessions(account_id, closed_at)",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    f"INSERT INTO settings (key, value) VALUES ('last_entry', '{DEFAULT_LAST_ENTRY:%H:%M}'), "
    f"('close', '{DEFAULT_CLOSE:%H:%M}')",
]

KEPT_TABLES = ("accounts", "positions", "orders")


class MigrationError(SimTradeError):
    """La migration a échoué ; la base n'a pas été modifiée."""


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM accounts)").fetchone()[0] == 1


def backup(conn: sqlite3.Connection, path: str, now: datetime) -> Path:
    target = Path(f"{path}.avant-v{VERSION}-{now:%Y%m%d-%H%M%S}.bak")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KEPT_TABLES}


def _verify(conn: sqlite3.Connection, before: dict[str, int]) -> None:
    after = _counts(conn)
    if after != before:
        raise MigrationError(f"nombre de lignes modifié ({before} avant, {after} après)")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("références entre tables cassées (foreign_key_check)")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"intégrité : {integrity}")


def migrate(conn: sqlite3.Connection, path: str, now: datetime | None = None) -> Path | None:
    """Amène la base en v1. Renvoie le chemin de la sauvegarde (None si rien à sauvegarder)."""
    if version(conn) >= VERSION:
        return None
    saved = backup(conn, path, now or utcnow()) if path != ":memory:" and has_data(conn) else None
    before = _counts(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for step in V1_STEPS:
            conn.execute(step)
        _verify(conn, before)
        conn.execute(f"PRAGMA user_version = {VERSION}")
    except BaseException as exc:
        conn.execute("ROLLBACK")
        where = f" Sauvegarde : {saved}" if saved else ""
        raise MigrationError(
            f"Migration de la base vers la version {VERSION} impossible ({exc}). "
            f"La base n'a pas été modifiée.{where}"
        ) from exc
    conn.execute("COMMIT")
    return saved
