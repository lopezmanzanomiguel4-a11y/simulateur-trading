"""Emplacement de la base SQLite."""

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
