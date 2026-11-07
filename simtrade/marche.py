"""Base des bougies (`marche.db`) : klines 1m et 1h définitives, séparée de la base de trading.

Une ligne par (paire, début de bougie), upsert idempotent : réenregistrer la même kline ne crée
jamais de doublon. Deux tables de même schéma : `klines_1m` (intraday) et `klines_1h` (swing, depuis
2019). Les autres intervalles ne sont pas stockés : 5m/15m sont agrégés depuis les 1m, 4h/1d depuis
les 1h (`market.aggregate`). Les prix et volumes sont stockés en TEXT pour garder les Decimal exacts.
Chaque méthode prend `interval` (« 1m » par défaut : appels d'avant inchangés).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from simtrade.history import from_ms, to_ms
from simtrade.market import ORIGIN_REST, Candle

SCHEMA_VERSION = 2  # 2 : table klines_1h (créée vide si absente, rien d'autre ne change)
MINUTE = timedelta(minutes=1)
MINUTE_MS = 60_000
STORED = {"1m": 60_000, "1h": 3_600_000}  # intervalles stockés -> durée en ms
DOWNLOAD = "telechargement"
LIVE = "live"
EMPTY_KEY = "binance_vide:"  # meta : plages renvoyées vides par Binance, par paire (JSON [[début_ms, fin_ms], ...])

TABLE = """
CREATE TABLE IF NOT EXISTS klines_{i} (
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
"""
SCHEMA = "".join(TABLE.replace("{i}", i) for i in STORED) + (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
)

UPSERT = """
INSERT INTO klines_{i} (pair, open_time, open, high, low, close, volume, notional, transactions, source)
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
            "INSERT INTO meta (key, value) VALUES ('version', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value "
            "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
            (str(SCHEMA_VERSION),),
        )
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        self.conn.close()

    def save(self, candles: Iterable[Candle], source: str = DOWNLOAD) -> int:
        """Enregistre des klines 1m et 1h, chacune dans sa table (les autres intervalles sont ignorés) ;
        renvoie le nombre de lignes écrites."""
        rows: dict[str, list[tuple]] = {}
        for c in candles:
            if c.interval in STORED:
                rows.setdefault(c.interval, []).append((
                    c.pair, to_ms(c.open_time), str(c.open), str(c.high), str(c.low), str(c.close),
                    str(c.volume), str(c.notional), c.transactions, source,
                ))
        if not rows:
            return 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for interval, batch in rows.items():
                self.conn.executemany(UPSERT.replace("{i}", interval), batch)
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return sum(len(b) for b in rows.values())

    def candles(self, pair: str, start: datetime, end: datetime, interval: str = "1m") -> list[Candle]:
        """Klines `interval` dont le début est dans [start, end), triées."""
        rows = self.conn.execute(
            f"SELECT open_time, open, high, low, close, volume, notional, transactions FROM {table(interval)} "
            "WHERE pair = ? AND open_time >= ? AND open_time < ? ORDER BY open_time",
            (pair, to_ms(start), to_ms(end)),
        )
        return [
            Candle(
                pair=pair,
                interval=interval,
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

    def bounds(self, pair: str, interval: str = "1m") -> tuple[datetime, datetime] | None:
        """Début de la première et de la dernière bougie enregistrées."""
        row = self.conn.execute(
            f"SELECT MIN(open_time), MAX(open_time) FROM {table(interval)} WHERE pair = ?", (pair,)
        ).fetchone()
        return None if row[0] is None else (from_ms(row[0]), from_ms(row[1]))

    def count(
        self, pair: str | None = None, start: datetime | None = None, end: datetime | None = None, interval: str = "1m"
    ) -> int:
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
        return self.conn.execute(f"SELECT COUNT(*) FROM {table(interval)}{where}", args).fetchone()[0]

    def gaps(self, pair: str, start: datetime, end: datetime, interval: str = "1m") -> list[tuple[datetime, datetime]]:
        """Bougies manquantes dans [start, end), en intervalles [début, fin)."""
        lo, hi, step = to_ms(start), to_ms(end), STORED[interval]
        rows = self.conn.execute(
            "SELECT prev, open_time FROM ("
            f"  SELECT open_time, LAG(open_time) OVER (ORDER BY open_time) AS prev FROM {table(interval)}"
            "  WHERE pair = ? AND open_time >= ? AND open_time < ?"
            ") WHERE prev IS NOT NULL AND open_time - prev > ?",
            (pair, lo, hi, step),
        ).fetchall()
        gaps = [(from_ms(prev + step), from_ms(cur)) for prev, cur in rows]
        first_last = self.conn.execute(
            f"SELECT MIN(open_time), MAX(open_time) FROM {table(interval)} "
            "WHERE pair = ? AND open_time >= ? AND open_time < ?",
            (pair, lo, hi),
        ).fetchone()
        if first_last[0] is None:
            return [(start, end)] if end > start else []
        if first_last[0] > lo:
            gaps.insert(0, (start, from_ms(first_last[0])))
        if first_last[1] + step < hi:
            gaps.append((from_ms(first_last[1] + step), end))
        return gaps

    def binance_empty(self, pair: str, interval: str = "1m") -> list[tuple[datetime, datetime]]:
        """Plages déjà demandées à Binance et renvoyées vides (panne de l'exchange, ou avant la cotation
        de la paire), triées, fusionnées."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (empty_key(pair, interval),)).fetchone()
        return [] if row is None else [(from_ms(a), from_ms(b)) for a, b in json.loads(row[0])]

    def add_binance_empty(self, pair: str, start: datetime, end: datetime, interval: str = "1m") -> None:
        if end <= start:
            return
        ranges = merge_ranges(self.binance_empty(pair, interval) + [(start, end)])
        value = json.dumps([[to_ms(a), to_ms(b)] for a, b in ranges])
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (empty_key(pair, interval), value),
        )

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


def table(interval: str) -> str:
    if interval not in STORED:
        raise ValueError(f"intervalle non stocké : {interval}")
    return f"klines_{interval}"


def empty_key(pair: str, interval: str = "1m") -> str:
    """Clé meta des plages vides : 'binance_vide:BTCUSDT' (1m, comme avant) ou 'binance_vide:1h:BTCUSDT'."""
    return EMPTY_KEY + pair if interval == "1m" else f"{EMPTY_KEY}{interval}:{pair}"


def missing_steps(gaps: Iterable[tuple[datetime, datetime]], interval: str = "1m") -> int:
    step = timedelta(milliseconds=STORED[interval])
    return sum((b - a) // step for a, b in gaps)


def missing_minutes(gaps: Iterable[tuple[datetime, datetime]]) -> int:
    return sum((b - a) // MINUTE for a, b in gaps)


Range = tuple[datetime, datetime]


def merge_ranges(ranges: Iterable[Range]) -> list[Range]:
    out: list[Range] = []
    for a, b in sorted(r for r in ranges if r[1] > r[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract_ranges(ranges: Iterable[Range], removed: Iterable[Range]) -> list[Range]:
    """`ranges` privées de `removed` (intervalles [début, fin))."""
    cuts = merge_ranges(removed)
    out: list[Range] = []
    for a, b in merge_ranges(ranges):
        for c, d in cuts:
            if d <= a or c >= b:
                continue
            if c > a:
                out.append((a, c))
            a = max(a, d)
            if a >= b:
                break
        if a < b:
            out.append((a, b))
    return out


def intersect_ranges(ranges: Iterable[Range], start: datetime, end: datetime) -> list[Range]:
    return [(max(a, start), min(b, end)) for a, b in merge_ranges(ranges) if b > start and a < end]
