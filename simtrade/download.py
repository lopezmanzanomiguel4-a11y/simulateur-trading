"""Téléchargement de klines 1m Binance vers `marche.db` (CLI : python -m simtrade marche telecharger).

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
from simtrade.marche import DOWNLOAD, MINUTE, MarketStore, missing_minutes
from simtrade.models import utcnow

MIN_FREE_BYTES = 5 * 1024**3  # 5 Go libres en permanence
BYTES_PER_ROW = 200  # estimation prudente (ligne + index), vérifiée à l'essai réel
WEIGHT_PAUSE = 4800  # 80 % des 6000 autorisés par minute
DEFAULT_RETRY = 60.0
MAX_RETRIES = 5


class DiskFull(SimTradeError):
    """Espace disque insuffisant pour le téléchargement demandé."""


@dataclass
class PairReport:
    pair: str
    rows: int = 0  # lignes écrites pendant ce téléchargement
    requests: int = 0
    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)  # minutes absentes chez Binance

    @property
    def missing(self) -> int:
        return missing_minutes(self.gaps)


@dataclass
class DownloadReport:
    start: datetime
    end: datetime
    pairs: list[PairReport]
    waits: list[float] = field(default_factory=list)  # attentes (poids, 429/418), en secondes
    size_bytes: int = 0
    free_bytes_after: int = 0


def segments(store: MarketStore, pair: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Ce qui reste à télécharger : avant la première minute enregistrée et après la dernière (reprise).
    Les trous intérieurs ne sont pas retéléchargés : ils sont rapportés."""
    bounds = store.bounds(pair)
    if bounds is None or bounds[1] < start or bounds[0] >= end:
        return [(start, end)] if start < end else []
    first, last = bounds
    out = []
    if first > start:
        out.append((start, first))
    if last + MINUTE < end:
        out.append((max(start, last + MINUTE), end))
    return out


def estimate_rows(store: MarketStore, pairs: Sequence[str], start: datetime, end: datetime) -> int:
    return sum((b - a) // MINUTE for pair in pairs for a, b in segments(store, pair, start, end))


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
    ):
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
        needed = estimate_rows(self.store, pairs, start, end) * BYTES_PER_ROW
        free = self._free()
        if free - needed < self.min_free:
            raise DiskFull(
                f"Espace disque insuffisant : {free / 1e9:.1f} Go libres, téléchargement estimé à "
                f"{needed / 1e9:.2f} Go, il faut garder au moins {self.min_free / 1024**3:.0f} Go libres."
            )
        return needed

    async def _wait(self, seconds: float, report: DownloadReport, why: str) -> None:
        report.waits.append(seconds)
        if self.progress:
            self.progress(f"attente {seconds:.0f} s ({why})")
        await self._sleep(seconds)

    async def _page(self, pair: str, a: datetime, b: datetime, report: DownloadReport, pr: PairReport):
        for attempt in range(MAX_RETRIES + 1):
            try:
                pr.requests += 1
                page = await self.history.page(pair, "1m", a, b)
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
        current = bucket_start(self.clock(), 60)
        end = min(end or current, current)  # minutes clôturées seulement
        self.check_disk(pairs, start, end)
        report = DownloadReport(start, end, [])
        for pair in pairs:
            pr = PairReport(pair)
            report.pairs.append(pr)
            for a, b in segments(self.store, pair, start, end):
                cursor = a
                while cursor < b:
                    raw = await self._page(pair, cursor, b, report, pr)
                    page = [c for c in raw if cursor <= c.open_time < b]
                    if page:
                        pr.rows += self.store.save(page, DOWNLOAD)
                        cursor = page[-1].open_time + MINUTE
                        if self.progress:
                            self.progress(f"{pair} : jusqu'à {cursor:%Y-%m-%d %H:%M} UTC ({pr.rows} lignes)")
                    if len(raw) < self.history.page_size:
                        break  # page incomplète : plus rien chez Binance sur ce segment
            pr.gaps = self.store.gaps(pair, start, end)
        report.size_bytes = self.store.size_bytes()
        report.free_bytes_after = self._free()
        return report
