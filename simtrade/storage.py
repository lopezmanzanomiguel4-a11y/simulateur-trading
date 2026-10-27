"""Persistance SQLite : comptes, positions, ordres.

Les montants sont stockés en TEXT pour garder la précision exacte des Decimal.
Un reset ne supprime rien : il archive le compte actif et en ouvre un nouveau.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from simtrade.engine import DEFAULT_CAPITAL, Portfolio, validate_capital
from simtrade.models import Account, Order, Position, Side, utcnow

SCHEMA = """
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


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Transaction exclusive en écriture ; réutilise la transaction en cours si elle existe."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- Comptes ---

    def active_account(self) -> Account:
        """Compte actif ; en crée un avec le capital par défaut au premier lancement."""
        with self.transaction():
            row = self.conn.execute("SELECT * FROM accounts WHERE active = 1").fetchone()
            if row is None:
                return self._open_account(DEFAULT_CAPITAL, utcnow())
            return _account(row)

    def accounts(self) -> list[Account]:
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_account(r) for r in rows]

    def reset(self, capital: object = DEFAULT_CAPITAL, now: datetime | None = None) -> Account:
        """Archive le compte actif (ordres conservés) et ouvre un compte neuf."""
        amount = validate_capital(capital)
        with self.transaction():
            self.conn.execute("UPDATE accounts SET active = 0 WHERE active = 1")
            return self._open_account(amount, now or utcnow())

    def _open_account(self, capital: Decimal, now: datetime) -> Account:
        cur = self.conn.execute(
            "INSERT INTO accounts (initial_capital, cash, created_at, active) VALUES (?, ?, ?, 1)",
            (str(capital), str(capital), now.isoformat()),
        )
        return Account(cur.lastrowid, capital, capital, now)

    # --- Portefeuille ---

    def load_portfolio(self) -> Portfolio:
        with self.transaction():
            account = self.active_account()
            positions = {
                r["pair"]: Position(r["pair"], Decimal(r["quantity"]), Decimal(r["cost_basis"]))
                for r in self.conn.execute("SELECT * FROM positions WHERE account_id = ?", (account.id,))
            }
            realized = Decimal(0)
            fees = Decimal(0)
            for r in self.conn.execute("SELECT realized_pnl, fee FROM orders WHERE account_id = ?", (account.id,)):
                realized += Decimal(r["realized_pnl"])
                fees += Decimal(r["fee"])
        return Portfolio(account, positions, realized, fees)

    def save_trade(self, portfolio: Portfolio, order: Order) -> Order:
        """Enregistre l'ordre et l'état du portefeuille qui en résulte, de façon atomique."""
        account = portfolio.account
        with self.transaction():
            self.conn.execute("UPDATE accounts SET cash = ? WHERE id = ?", (str(account.cash), account.id))
            position = portfolio.positions.get(order.pair)
            if position is None:
                self.conn.execute(
                    "DELETE FROM positions WHERE account_id = ? AND pair = ?", (account.id, order.pair)
                )
            else:
                self.conn.execute(
                    "INSERT INTO positions (account_id, pair, quantity, cost_basis) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(account_id, pair) DO UPDATE SET quantity = excluded.quantity, "
                    "cost_basis = excluded.cost_basis",
                    (account.id, order.pair, str(position.quantity), str(position.cost_basis)),
                )
            cur = self.conn.execute(
                "INSERT INTO orders (account_id, pair, side, quantity, price, gross, fee, cash_flow, "
                "realized_pnl, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account.id,
                    order.pair,
                    order.side.value,
                    str(order.quantity),
                    str(order.price),
                    str(order.gross),
                    str(order.fee),
                    str(order.cash_flow),
                    str(order.realized_pnl),
                    order.source,
                    order.created_at.isoformat(),
                ),
            )
        return replace(order, account_id=account.id, id=cur.lastrowid)

    def orders(self, account_id: int | None = None) -> list[Order]:
        """Ordres d'un compte (le compte actif par défaut), du plus ancien au plus récent."""
        if account_id is None:
            account_id = self.active_account().id
        rows = self.conn.execute("SELECT * FROM orders WHERE account_id = ? ORDER BY id", (account_id,))
        return [_order(r) for r in rows]


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        initial_capital=Decimal(row["initial_capital"]),
        cash=Decimal(row["cash"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        account_id=row["account_id"],
        pair=row["pair"],
        side=Side(row["side"]),
        quantity=Decimal(row["quantity"]),
        price=Decimal(row["price"]),
        gross=Decimal(row["gross"]),
        fee=Decimal(row["fee"]),
        cash_flow=Decimal(row["cash_flow"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
