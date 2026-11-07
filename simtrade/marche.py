"""Base des bougies (`marche.db`) : klines 1m définitives, séparée de la base de trading.

Une ligne par (paire, début de minute), upsert idempotent : réenregistrer la même kline ne crée
jamais de doublon. Les intervalles 5m et 15m ne sont pas stockés : ils sont agrégés depuis les 1m
(`market.aggregate`). Les prix et volumes sont stockés en TEXT pour garder les Decimal exacts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from simtrade.history import from_ms, to_ms
from simtrade.market import ORIGIN_REST, Candle

SCHEMA_VERSION = 1
MINUTE = timedelta(minutes=1)
MINUTE_MS = 60_000
DOWNLOAD = "telechargement"
LIVE = "live"

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines_1m (
    pair         TEXT    NOT NULL,
    open_time    INTEGER NOT NULL,  -- ms depuis l'époque Unix (UTC)
    open         TEXT    NOT NULL,
    high         TEXT    NOT NULL,
    low          TEXT    NOT NULL,
    close        TEXT    NOT NULL,
    volume       TEXT    NOT NULL,
    notional     TEXT    NOT NULL,
    transactions INTEGER NOT NULL,
    source       TEXT    NOT NULL,  -- 'telechargement' ou 'live' (premier enregistrement)
    PRIMARY KEY (pair, open_time)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

UPSERT = """
INSERT INTO klines_1m (pair, open_time, open, high, low, close, volume, notional, transactions, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (pair, open_time) DO UPDATE SET
    open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
    volume = excluded.volume, notional = excluded.notional, transactions = excluded.transactions
"""


class MarketStore:
    """Connexion à `marche.db`. À créer et utiliser dans un seul thread (le thread SQLite dans l'API)."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),)
        )
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    def save(self, candles: Iterable[Candle], source: str = DOWNLOAD) -> int:
        """Enregistre des klines 1m (les autres intervalles sont ignorés) ; renvoie le nombre de lignes écrites."""
        rows = [
            (
                c.pair, to_ms(c.open_time), str(c.open), str(c.high), str(c.low), str(c.close),
                str(c.volume), str(c.notional), c.transactions, source,
            )
            for c in candles
            if c.interval == "1m"
        ]
        if not rows:
            return 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(UPSERT, rows)
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return len(rows)

    def candles(self, pair: str, start: datetime, end: datetime) -> list[Candle]:
        """Klines 1m dont le début est dans [start, end), triées."""
        rows = self.conn.execute(
            "SELECT open_time, open, high, low, close, volume, notional, transactions FROM klines_1m "
            "WHERE pair = ? AND open_time >= ? AND open_time < ? ORDER BY open_time",
            (pair, to_ms(start), to_ms(end)),
        )
        return [
            Candle(
                pair=pair,
                interval="1m",
                open_time=from_ms(r[0]),
                open=Decimal(r[1]),
                high=Decimal(r[2]),
                low=Decimal(r[3]),
                close=Decimal(r[4]),
                volume=Decimal(r[5]),
                notional=Decimal(r[6]),
                transactions=r[7],
                origin=ORIGIN_REST,
            )
            for r in rows
        ]

    def bounds(self, pair: str) -> tuple[datetime, datetime] | None:
        """Début de la première et de la dernière minute enregistrées."""
        row = self.conn.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM klines_1m WHERE pair = ?", (pair,)
        ).fetchone()
        return None if row[0] is None else (from_ms(row[0]), from_ms(row[1]))

    def count(self, pair: str | None = None, start: datetime | None = None, end: datetime | None = None) -> int:
        clauses, args = [], []
        if pair is not None:
            clauses.append("pair = ?")
            args.append(pair)
        if start is not None:
            clauses.append("open_time >= ?")
            args.append(to_ms(start))
        if end is not None:
            clauses.append("open_time < ?")
            args.append(to_ms(end))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.conn.execute(f"SELECT COUNT(*) FROM klines_1m{where}", args).fetchone()[0]

    def gaps(self, pair: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Minutes manquantes dans [start, end), en intervalles [début, fin)."""
        lo, hi = to_ms(start), to_ms(end)
        rows = self.conn.execute(
            "SELECT prev, open_time FROM ("
            "  SELECT open_time, LAG(open_time) OVER (ORDER BY open_time) AS prev FROM klines_1m"
            "  WHERE pair = ? AND open_time >= ? AND open_time < ?"
            ") WHERE prev IS NOT NULL AND open_time - prev > ?",
            (pair, lo, hi, MINUTE_MS),
        ).fetchall()
        gaps = [(from_ms(prev + MINUTE_MS), from_ms(cur)) for prev, cur in rows]
        first_last = self.conn.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM klines_1m WHERE pair = ? AND open_time >= ? AND open_time < ?",
            (pair, lo, hi),
        ).fetchone()
        if first_last[0] is None:
            return [(start, end)] if end > start else []
        if first_last[0] > lo:
            gaps.insert(0, (start, from_ms(first_last[0])))
        if first_last[1] + MINUTE_MS < hi:
            gaps.append((from_ms(first_last[1] + MINUTE_MS), end))
        return gaps

    def size_bytes(self) -> int:
        """Taille de la base sur disque (fichier principal + WAL)."""
        if self.path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal"):
            p = Path(self.path + suffix)
            if p.exists():
                total += p.stat().st_size
        return total


def missing_minutes(gaps: Iterable[tuple[datetime, datetime]]) -> int:
    return sum((b - a) // MINUTE for a, b in gaps)
