"""Persistance SQLite : comptes, positions, ordres, ordres en attente, sessions, réglages.

Les montants sont stockés en TEXT pour garder la précision exacte des Decimal.
Un reset ne supprime rien : il archive le compte et en ouvre un nouveau du même nom.
Plusieurs comptes peuvent être ouverts en parallèle ; le compte « courant » (active = 1)
est celui utilisé quand aucun compte n'est précisé.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from simtrade import migrations
from simtrade.engine import DEFAULT_CAPITAL, Portfolio, validate_capital
from simtrade.errors import InvalidInput
from simtrade.risk import RiskLimits
from simtrade.models import (
    CANCELLED,
    MANUAL,
    MODES,
    PENDING,
    Account,
    Order,
    PendingOrder,
    Position,
    Side,
    utcnow,
)
from simtrade.sessions import SessionRules, parse_hhmm

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
# Schéma de départ (v0) ; les ajouts de l'étape 4 sont dans migrations.py.

DEFAULT_NAME = "principal"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        try:
            self.backup_path = migrations.migrate(self.conn, self.path)
        except BaseException:
            self.conn.close()
            raise
        if self.path != ":memory:":
            # Lecteurs et écrivain ne se bloquent plus entre eux (API et CLI sur la même base).
            self.conn.execute("PRAGMA journal_mode = WAL")

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

    def data_version(self) -> int:
        """Change quand une AUTRE connexion (la CLI pendant que l'API tourne) a écrit dans la base."""
        return self.conn.execute("PRAGMA data_version").fetchone()[0]

    # --- Comptes ---

    def active_account(self) -> Account:
        """Compte courant ; en crée un avec le capital par défaut au premier lancement."""
        with self.transaction():
            row = self.conn.execute("SELECT * FROM accounts WHERE active = 1").fetchone()
            if row is not None:
                return _account(row)
            row = self.conn.execute(
                "SELECT * FROM accounts WHERE name = ? AND archived_at IS NULL", (DEFAULT_NAME,)
            ).fetchone()
            if row is not None:
                self.conn.execute("UPDATE accounts SET active = 1 WHERE id = ?", (row["id"],))
                return replace(_account(row), current=True)
            return self._open_account(DEFAULT_CAPITAL, utcnow())

    def accounts(self) -> list[Account]:
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_account(r) for r in rows]

    def open_accounts(self) -> list[Account]:
        self.active_account()  # premier lancement
        rows = self.conn.execute("SELECT * FROM accounts WHERE archived_at IS NULL ORDER BY id").fetchall()
        return [_account(r) for r in rows]

    def account(self, ref: object = None, *, open_only: bool = True) -> Account:
        """Compte par nom (parmi les ouverts) ou par numéro ; le compte courant si `ref` est None."""
        current = self.active_account()  # premier lancement : crée le compte principal
        if ref is None or (isinstance(ref, str) and not ref.strip()):
            return current
        text = str(ref).strip().lower()
        if text.isdigit():
            row = self.conn.execute("SELECT * FROM accounts WHERE id = ?", (int(text),)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM accounts WHERE name = ? AND archived_at IS NULL", (text,)
            ).fetchone()
        if row is None:
            names = ", ".join(a.name for a in self.open_accounts())
            raise InvalidInput(f"Compte inconnu : '{ref}'. Comptes ouverts : {names}.")
        account = _account(row)
        if open_only and account.archived:
            raise InvalidInput(f"Le compte n°{account.id} ({account.name}) est archivé.")
        return account

    def create_account(
        self, name: object, capital: object = DEFAULT_CAPITAL, mode: str = MANUAL, now: datetime | None = None
    ) -> Account:
        clean = str(name).strip().lower()
        if not NAME_RE.fullmatch(clean) or clean.isdigit():
            raise InvalidInput(
                f"Nom de compte invalide : '{name}' (1 à 32 caractères parmi a-z, 0-9, - et _, pas uniquement des chiffres)."
            )
        if mode not in MODES:
            raise InvalidInput(f"Mode de compte invalide : '{mode}' ({' ou '.join(MODES)}).")
        amount = validate_capital(capital)
        with self.transaction():
            self.active_account()  # le compte principal existe avant les autres
            taken = self.conn.execute(
                "SELECT 1 FROM accounts WHERE name = ? AND archived_at IS NULL", (clean,)
            ).fetchone()
            if taken:
                raise InvalidInput(f"Un compte ouvert s'appelle déjà '{clean}'.")
            return self._open_account(amount, now or utcnow(), clean, mode, current=False)

    def select_account(self, ref: object) -> Account:
        with self.transaction():
            account = self.account(ref)
            self.conn.execute("UPDATE accounts SET active = 0 WHERE active = 1")
            self.conn.execute("UPDATE accounts SET active = 1 WHERE id = ?", (account.id,))
        return replace(account, current=True)

    def archive_account(self, ref: object, now: datetime | None = None) -> Account:
        now = now or utcnow()
        with self.transaction():
            account = self.account(ref)
            if account.current:
                raise InvalidInput(
                    f"'{account.name}' est le compte courant : choisis-en un autre avant de l'archiver (ou utilise reset)."
                )
            self._archive(account.id, now, "archive")
        return replace(account, archived_at=now)

    def reset(self, capital: object = DEFAULT_CAPITAL, now: datetime | None = None, account: object = None) -> Account:
        """Archive le compte (le courant par défaut, ordres conservés) et ouvre un compte neuf du même nom."""
        amount = validate_capital(capital)
        now = now or utcnow()
        with self.transaction():
            old = self.account(account)
            self._archive(old.id, now, "reset")
            new = self._open_account(amount, now, old.name, old.mode, current=old.current)
            self.conn.execute(
                "INSERT INTO risk_limits SELECT ?, max_position_pct, max_session_loss_pct, max_trades, "
                "max_positions, min_entry_gap_s FROM risk_limits WHERE account_id = ?",
                (new.id, old.id),
            )
            return new

    def _archive(self, account_id: int, now: datetime, reason: str) -> None:
        self.cancel_account_pending(account_id, reason, now)
        self.conn.execute(
            "UPDATE accounts SET active = 0, archived_at = ? WHERE id = ?", (now.isoformat(), account_id)
        )

    def _open_account(
        self, capital: Decimal, now: datetime, name: str = DEFAULT_NAME, mode: str = MANUAL, current: bool = True
    ) -> Account:
        cur = self.conn.execute(
            "INSERT INTO accounts (initial_capital, cash, created_at, active, name, mode) VALUES (?, ?, ?, ?, ?, ?)",
            (str(capital), str(capital), now.isoformat(), 1 if current else 0, name, mode),
        )
        return Account(cur.lastrowid, capital, capital, now, name, mode, None, current)

    # --- Portefeuille ---

    def load_portfolio(self, account_id: int | None = None, *, exclude_pending: int | None = None) -> Portfolio:
        """Portefeuille du compte (courant par défaut), avec ce que bloquent ses ordres en attente.

        `exclude_pending` : ordre en attente (et son ordre lié) à ne pas compter, pour l'exécuter.
        """
        with self.transaction():
            account = self.active_account() if account_id is None else self.account(account_id, open_only=False)
            positions = {
                r["pair"]: Position(r["pair"], Decimal(r["quantity"]), Decimal(r["cost_basis"]))
                for r in self.conn.execute("SELECT * FROM positions WHERE account_id = ?", (account.id,))
            }
            realized = Decimal(0)
            fees = Decimal(0)
            for r in self.conn.execute("SELECT realized_pnl, fee FROM orders WHERE account_id = ?", (account.id,)):
                realized += Decimal(r["realized_pnl"])
                fees += Decimal(r["fee"])
            pending = self.pending_orders(account.id)
        if exclude_pending is not None:
            excluded = next((p for p in pending if p.id == exclude_pending), None)
            group = excluded.group_id if excluded else None
            pending = [p for p in pending if p.id != exclude_pending and (group is None or p.group_id != group)]
        reserved_cash, reserved_qty = _reservations(pending, positions)
        return Portfolio(account, positions, realized, fees, reserved_cash=reserved_cash, reserved_qty=reserved_qty)

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
                "realized_pnl, source, created_at, kind, trigger_price, slippage, pending_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    order.kind,
                    _opt_str(order.trigger_price),
                    str(order.slippage),
                    order.pending_id,
                ),
            )
        return replace(order, account_id=account.id, id=cur.lastrowid)

    def orders(self, account_id: int | None = None) -> list[Order]:
        """Ordres d'un compte (le compte actif par défaut), du plus ancien au plus récent."""
        if account_id is None:
            account_id = self.active_account().id
        rows = self.conn.execute("SELECT * FROM orders WHERE account_id = ? ORDER BY id", (account_id,))
        return [_order(r) for r in rows]

    # --- Ordres en attente ---

    def add_pending(self, orders: Sequence[PendingOrder]) -> list[PendingOrder]:
        """Enregistre un ordre, ou deux ordres liés (stop + objectif) qui partagent un groupe."""
        saved: list[PendingOrder] = []
        with self.transaction():
            for order in orders:
                cur = self.conn.execute(
                    "INSERT INTO pending_orders (account_id, pair, kind, side, trigger_price, amount, quantity, "
                    "status, created_at, protection, stop_price, target_price) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        order.account_id,
                        order.pair,
                        order.kind,
                        order.side.value,
                        str(order.trigger_price),
                        _opt_str(order.amount),
                        _opt_str(order.quantity),
                        PENDING,
                        order.created_at.isoformat(),
                        int(order.protection),
                        _opt_str(order.stop_price),
                        _opt_str(order.target_price),
                    ),
                )
                saved.append(replace(order, id=cur.lastrowid, status=PENDING))
            if len(saved) > 1:
                group = saved[0].id
                self.conn.execute(
                    f"UPDATE pending_orders SET group_id = ? WHERE id IN ({','.join('?' * len(saved))})",
                    (group, *(o.id for o in saved)),
                )
                saved = [replace(o, group_id=group) for o in saved]
        return saved

    def pending_orders(self, account_id: int | None = None, *, all_statuses: bool = False) -> list[PendingOrder]:
        """Ordres en attente d'un compte (tous les comptes si None), du plus ancien au plus récent."""
        sql, args = "SELECT * FROM pending_orders WHERE 1 = 1", []
        if account_id is not None:
            sql += " AND account_id = ?"
            args.append(account_id)
        if not all_statuses:
            sql += " AND status = ?"
            args.append(PENDING)
        return [_pending(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def get_pending(self, pending_id: int) -> PendingOrder | None:
        row = self.conn.execute("SELECT * FROM pending_orders WHERE id = ?", (pending_id,)).fetchone()
        return None if row is None else _pending(row)

    def group(self, order: PendingOrder) -> list[PendingOrder]:
        if order.group_id is None:
            return [order]
        rows = self.conn.execute("SELECT * FROM pending_orders WHERE group_id = ? ORDER BY id", (order.group_id,))
        return [_pending(r) for r in rows]

    def close_pending(
        self, pending_id: int, status: str, reason: str | None, now: datetime, order_id: int | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE pending_orders SET status = ?, reason = ?, closed_at = ?, order_id = ? "
            "WHERE id = ? AND status = ?",
            (status, reason, now.isoformat(), order_id, pending_id, PENDING),
        )

    def protection_orders(self, account_id: int, pair: str) -> list[PendingOrder]:
        """Stop de protection (et objectif lié) en attente sur la paire."""
        return [o for o in self.pending_orders(account_id) if o.pair == pair and o.protection]

    def set_trigger_price(self, pending_id: int, price: Decimal) -> None:
        self.conn.execute(
            "UPDATE pending_orders SET trigger_price = ? WHERE id = ? AND status = ?", (str(price), pending_id, PENDING)
        )

    def cancel_account_pending(self, account_id: int, reason: str, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE pending_orders SET status = ?, reason = ?, closed_at = ? WHERE account_id = ? AND status = ?",
            (CANCELLED, reason, now.isoformat(), account_id, PENDING),
        )
        return cur.rowcount

    # --- Sessions ---

    def record_session(self, account_id: int, day: date, now: datetime, late: bool) -> None:
        self.conn.execute(
            "INSERT INTO sessions (account_id, date, closed_at, late) VALUES (?, ?, ?, ?)",
            (account_id, day.isoformat(), now.isoformat(), int(late)),
        )

    def sessions(self, account_id: int) -> list[tuple[date, datetime, bool]]:
        rows = self.conn.execute("SELECT * FROM sessions WHERE account_id = ? ORDER BY id", (account_id,))
        return [(date.fromisoformat(r["date"]), datetime.fromisoformat(r["closed_at"]), bool(r["late"])) for r in rows]

    def last_session_close(self, account_id: int) -> datetime | None:
        closes = [closed for _, closed, _ in self.sessions(account_id)]
        return max(closes) if closes else None

    def first_activity_since(self, account_id: int, since: datetime | None) -> datetime | None:
        """Plus ancien ordre (exécuté ou en attente) du compte après `since` : début de la session à fermer."""
        times = [o.created_at for o in self.orders(account_id)]
        times += [p.created_at for p in self.pending_orders(account_id)]
        times = [t for t in times if since is None or t > since]
        return min(times) if times else None

    # --- Réglages ---

    def session_rules(self) -> SessionRules:
        values = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}
        return SessionRules(
            parse_hhmm(values["last_entry"], "Heure de dernière entrée"), parse_hhmm(values["close"], "Heure de clôture")
        )

    def set_session_rules(self, rules: SessionRules) -> None:
        with self.transaction():
            for key, value in (("last_entry", rules.last_entry), ("close", rules.close)):
                self.conn.execute("UPDATE settings SET value = ? WHERE key = ?", (f"{value:%H:%M}", key))

    # --- Garde-fou ---

    def risk_limits(self, account_id: int) -> RiskLimits:
        """Limites du compte ; les valeurs par défaut de risk.py s'il n'en a pas."""
        row = self.conn.execute("SELECT * FROM risk_limits WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            return RiskLimits()
        return RiskLimits(
            Decimal(row["max_position_pct"]),
            Decimal(row["max_session_loss_pct"]),
            row["max_trades"],
            row["max_positions"],
            timedelta(seconds=row["min_entry_gap_s"]),
        )

    def set_risk_limits(self, account_id: int, limits: RiskLimits) -> None:
        self.conn.execute(
            "INSERT INTO risk_limits VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
            "max_position_pct = excluded.max_position_pct, max_session_loss_pct = excluded.max_session_loss_pct, "
            "max_trades = excluded.max_trades, max_positions = excluded.max_positions, "
            "min_entry_gap_s = excluded.min_entry_gap_s",
            (
                account_id,
                str(limits.max_position_pct),
                str(limits.max_session_loss_pct),
                limits.max_trades,
                limits.max_positions,
                int(limits.min_entry_gap.total_seconds()),
            ),
        )

    def add_refusal(self, refusal: RefusalRecord) -> RefusalRecord:
        cur = self.conn.execute(
            "INSERT INTO risk_refusals (account_id, created_at, moment, pair, amount, stop_price, pending_id, "
            "rules, message, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                refusal.account_id,
                refusal.created_at.isoformat(),
                refusal.moment,
                refusal.pair,
                _opt_str(refusal.amount),
                _opt_str(refusal.stop_price),
                refusal.pending_id,
                ",".join(refusal.rules),
                refusal.message,
                json.dumps(refusal.state, ensure_ascii=False),
            ),
        )
        return replace(refusal, id=cur.lastrowid)

    def refusals(self, account_id: int | None = None, limit: int = 100) -> list[RefusalRecord]:
        """Refus du garde-fou, du plus récent au plus ancien."""
        sql, args = "SELECT * FROM risk_refusals", []
        if account_id is not None:
            sql += " WHERE account_id = ?"
            args.append(account_id)
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [
            RefusalRecord(
                account_id=r["account_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                moment=r["moment"],
                pair=r["pair"],
                amount=_opt_dec(r["amount"]),
                stop_price=_opt_dec(r["stop_price"]),
                pending_id=r["pending_id"],
                rules=tuple(r["rules"].split(",")),
                message=r["message"],
                state=json.loads(r["state"]),
                id=r["id"],
            )
            for r in rows
        ]

    def halt_session(self, account_id: int, day: date, now: datetime, reason: str) -> None:
        if not self.session_halted(account_id, day):
            self.conn.execute(
                "INSERT INTO session_halts (account_id, date, created_at, reason) VALUES (?, ?, ?, ?)",
                (account_id, day.isoformat(), now.isoformat(), reason),
            )

    def session_halted(self, account_id: int, day: date) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM session_halts WHERE account_id = ? AND date = ?", (account_id, day.isoformat())
        ).fetchone()
        return row is not None


@dataclass(frozen=True)
class RefusalRecord:
    """Entrée du journal des refus du garde-fou."""

    account_id: int
    created_at: datetime
    moment: str
    pair: str
    amount: Decimal | None
    stop_price: Decimal | None
    pending_id: int | None
    rules: tuple[str, ...]
    message: str
    state: dict
    id: int | None = None


def _reservations(
    pending: Sequence[PendingOrder], positions: dict[str, Position]
) -> tuple[Decimal, dict[str, Decimal]]:
    """USDT bloqués par les achats limites ; quantités bloquées par les ventes (un groupe lié compte une fois).

    Les stops de protection (et leur objectif) ne bloquent rien : une vente reste toujours possible.
    """
    cash = sum((p.amount for p in pending if p.side is Side.BUY and p.amount is not None), Decimal(0))
    by_group: dict[tuple[str, object], Decimal] = {}
    for p in pending:
        if p.side is not Side.SELL or p.protection:
            continue
        held = positions[p.pair].quantity if p.pair in positions else Decimal(0)
        qty = held if p.quantity is None else p.quantity
        key = (p.pair, p.group_id if p.group_id is not None else ("seul", p.id))
        by_group[key] = max(by_group.get(key, Decimal(0)), qty)
    quantities: dict[str, Decimal] = {}
    for (pair, _), qty in by_group.items():
        quantities[pair] = quantities.get(pair, Decimal(0)) + qty
    return cash, quantities


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _opt_dec(raw: str | None) -> Decimal | None:
    return None if raw is None else Decimal(raw)


def _opt_dt(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        initial_capital=Decimal(row["initial_capital"]),
        cash=Decimal(row["cash"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        name=row["name"],
        mode=row["mode"],
        archived_at=_opt_dt(row["archived_at"]),
        current=bool(row["active"]),
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
        kind=row["kind"],
        trigger_price=_opt_dec(row["trigger_price"]),
        slippage=Decimal(row["slippage"]),
        pending_id=row["pending_id"],
    )


def _pending(row: sqlite3.Row) -> PendingOrder:
    return PendingOrder(
        id=row["id"],
        account_id=row["account_id"],
        pair=row["pair"],
        kind=row["kind"],
        side=Side(row["side"]),
        trigger_price=Decimal(row["trigger_price"]),
        amount=_opt_dec(row["amount"]),
        quantity=_opt_dec(row["quantity"]),
        group_id=row["group_id"],
        status=row["status"],
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
        closed_at=_opt_dt(row["closed_at"]),
        order_id=row["order_id"],
        protection=bool(row["protection"]),
        stop_price=_opt_dec(row["stop_price"]),
        target_price=_opt_dec(row["target_price"]),
    )
