"""Téléchargement de klines 1m ou 1h Binance vers `marche.db` (CLI : python -m simtrade marche telecharger).

`Downloader(interval="1h")` : klines 1h du swing (depuis 2019 ; avant la cotation d'une paire, Binance
renvoie une page vide, mémorisée comme plage vide : pas redemandée à la reprise).

- pages de 1000 minutes (`BinanceHistory.page`), enregistrées une à une : un arrêt en cours de route
  ne perd que la page en cours, et la commande suivante reprend là où on s'est arrêté ;
- seules les minutes clôturées sont téléchargées (fin bornée à la minute en cours) ;
- poids : au-delà de `WEIGHT_PAUSE` dans la minute (en-tête X-MBX-USED-WEIGHT-1M), on attend la
  minute suivante ; sur 429/418, on attend `Retry-After` (60 s par défaut) puis on reprend la même page ;
- refus avant tout téléchargement si l'espace libre estimé après coup tombe sous `MIN_FREE_BYTES`.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from simtrade.errors import SimTradeError
from simtrade.history import BinanceHistory, RateLimited
from simtrade.market import bucket_start
from simtrade.marche import DOWNLOAD, STORED, MarketStore, intersect_ranges, missing_steps, subtract_ranges
from simtrade.models import utcnow

MIN_FREE_BYTES = 5 * 1024**3  # 5 Go libres en permanence
BYTES_PER_ROW = 200  # estimation prudente (ligne + index), vérifiée à l'essai réel
WEIGHT_PAUSE = 4800  # 80 % des 6000 autorisés par minute
DEFAULT_RETRY = 60.0
MAX_RETRIES = 5
RECENT = timedelta(minutes=10)  # plage vide plus récente : pas mémorisée (kline peut-être pas encore publiée)


class DiskFull(SimTradeError):
    """Espace disque insuffisant pour le téléchargement demandé."""


@dataclass
class PairReport:
    pair: str
    rows: int = 0  # lignes écrites pendant ce téléchargement
    requests: int = 0
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)  # toutes les minutes absentes de la base
    binance: list[tuple[datetime, datetime]] = field(default_factory=list)  # dont : renvoyées vides par Binance
    pending: list[tuple[datetime, datetime]] = field(default_factory=list)  # dont : encore à télécharger
    interval: str = "1m"

    @property
    def missing(self) -> int:
        return missing_steps(self.gaps, self.interval)


def gap_report(store: MarketStore, pr: PairReport, start: datetime, end: datetime) -> PairReport:
    pr.gaps = store.gaps(pr.pair, start, end, pr.interval)
    pr.binance = intersect_ranges(store.binance_empty(pr.pair, pr.interval), start, end)
    pr.pending = subtract_ranges(pr.gaps, pr.binance)
    return pr


@dataclass
class DownloadReport:
    start: datetime
    end: datetime
    pairs: list[PairReport]
    waits: list[float] = field(default_factory=list)  # attentes (poids, 429/418), en secondes
    size_bytes: int = 0
    free_bytes_after: int = 0


def segments(
    store: MarketStore, pair: str, start: datetime, end: datetime, interval: str = "1m"
) -> list[tuple[datetime, datetime]]:
    """Ce qui reste à télécharger dans [start, end) : tous les trous (avant, entre et après les minutes
    enregistrées, par exemple entre un téléchargement et des bougies live), sauf les plages déjà demandées
    et renvoyées vides par Binance."""
    if start >= end:
        return []
    return subtract_ranges(store.gaps(pair, start, end, interval), store.binance_empty(pair, interval))


def estimate_rows(store: MarketStore, pairs: Sequence[str], start: datetime, end: datetime, interval: str = "1m") -> int:
    step = timedelta(milliseconds=STORED[interval])
    return sum((b - a) // step for pair in pairs for a, b in segments(store, pair, start, end, interval))


class Downloader:
    def __init__(
        self,
        store: MarketStore,
        history: BinanceHistory,
        *,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        disk_usage: Callable[[str], object] = shutil.disk_usage,
        clock: Callable[[], datetime] = utcnow,
        min_free: int = MIN_FREE_BYTES,
        progress: Callable[[str], None] | None = None,
        interval: str = "1m",
    ):
        if interval not in STORED:
            raise ValueError(f"intervalle non stocké : {interval}")
        self.interval = interval
        self.step = timedelta(milliseconds=STORED[interval])
        self.store = store
        self.history = history
        self._sleep = sleep
        self._disk_usage = disk_usage
        self.clock = clock
        self.min_free = min_free
        self.progress = progress

    def _free(self) -> int:
        folder = Path(self.store.path).parent if self.store.path != ":memory:" else Path.cwd()
        return int(self._disk_usage(str(folder)).free)  # type: ignore[attr-defined]

    def check_disk(self, pairs: Sequence[str], start: datetime, end: datetime) -> int:
        """Lève DiskFull si l'espace libre estimé après téléchargement < min_free ; renvoie l'estimation (octets)."""
        needed = estimate_rows(self.store, pairs, start, end, self.interval) * BYTES_PER_ROW
        free = self._free()
        if free - needed < self.min_free:
            raise DiskFull(
                f"Espace disque insuffisant : {free / 1e9:.1f} Go libres, téléchargement estimé à "
                f"{needed / 1e9:.2f} Go, il faut garder au moins {self.min_free / 1024**3:.0f} Go libres."
            )
        return needed

    def _empty(self, pair: str, a: datetime, b: datetime, current: datetime) -> None:
        """Mémorise une plage demandée et renvoyée vide (hors minutes trop récentes)."""
        self.store.add_binance_empty(pair, a, min(b, current - RECENT), self.interval)

    async def _wait(self, seconds: float, report: DownloadReport, why: str) -> None:
        report.waits.append(seconds)
        if self.progress:
            self.progress(f"attente {seconds:.0f} s ({why})")
        await self._sleep(seconds)

    async def _page(self, pair: str, a: datetime, b: datetime, report: DownloadReport, pr: PairReport):
        for attempt in range(MAX_RETRIES + 1):
            try:
                pr.requests += 1
                page = await self.history.page(pair, self.interval, a, b)
            except RateLimited as exc:
                if attempt == MAX_RETRIES:
                    raise
                await self._wait(exc.retry_after or DEFAULT_RETRY, report, f"HTTP {exc.status}")
                continue
            weight = self.history.used_weight
            if weight is not None and weight >= WEIGHT_PAUSE:
                now = self.clock()
                await self._wait(61 - now.second, report, f"poids {weight}")
            return page
        raise AssertionError("inaccessible")

    async def download(self, pairs: Sequence[str], start: datetime, end: datetime | None = None) -> DownloadReport:
        current = bucket_start(self.clock(), int(self.step.total_seconds()))
        end = min(end or current, current)  # bougies clôturées seulement
        self.check_disk(pairs, start, end)
        report = DownloadReport(start, end, [])
        for pair in pairs:
            pr = PairReport(pair, interval=self.interval)
            report.pairs.append(pr)
            for a, b in segments(self.store, pair, start, end, self.interval):
                cursor = a
                while cursor < b:
                    raw = await self._page(pair, cursor, b, report, pr)
                    page = [c for c in raw if cursor <= c.open_time < b]
                    if page:
                        expected = cursor  # trous chez Binance avant la page et entre ses lignes
                        for c in page:
                            if c.open_time > expected:
                                self._empty(pair, expected, c.open_time, current)
                            expected = c.open_time + self.step
                        pr.rows += self.store.save(page, DOWNLOAD)
                        cursor = page[-1].open_time + self.step
                        if self.progress:
                            self.progress(f"{pair} : jusqu'à {cursor:%Y-%m-%d %H:%M} UTC ({pr.rows} lignes)")
                    if len(raw) < self.history.page_size:
                        self._empty(pair, cursor, b, current)  # page incomplète : plus rien sur ce segment
                        break
            gap_report(self.store, pr, start, end)
        report.size_bytes = self.store.size_bytes()
        report.free_bytes_after = self._free()
        return report
