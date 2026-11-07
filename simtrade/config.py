"""Emplacement des bases SQLite (trading et bougies)."""

from __future__ import annotations

import os
from pathlib import Path

DB_ENV_VAR = "SIMTRADE_DB"


def db_path() -> Path:
    """SIMTRADE_DB si définie, sinon %LOCALAPPDATA%\\simtrade\\simtrade.db (~/.simtrade hors Windows)."""
    override = os.environ.get(DB_ENV_VAR)
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "simtrade" if local else Path.home() / ".simtrade"
    return base / "simtrade.db"


MARCHE_ENV_VAR = "SIMTRADE_MARCHE_DB"
MARCHE_NAME = "marche.db"


def marche_db_path(trading_db: str | Path | None = None) -> Path | str:
    """Base des bougies : SIMTRADE_MARCHE_DB si définie, sinon `marche.db` à côté de la base de trading
    (`trading_db`, ou db_path()). Une base de trading en mémoire donne une base de bougies en mémoire."""
    override = os.environ.get(MARCHE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if trading_db is not None and str(trading_db) == ":memory:":
        return ":memory:"
    base = Path(trading_db) if trading_db is not None else db_path()
    return base.parent / MARCHE_NAME


CATALOGUE_PATH = Path(__file__).parent / "catalogue.json"  # construit par : python -m simtrade catalogue construire
