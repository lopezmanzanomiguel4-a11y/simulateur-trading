"""Formatage des nombres à la française : 10 000,00."""

from __future__ import annotations

from decimal import Decimal


def num(value: Decimal, decimals: int = 2, sign: bool = False) -> str:
    spec = f"{'+' if sign else ''},.{decimals}f"
    return format(value, spec).replace(",", " ").replace(".", ",")


def usdt(value: Decimal, decimals: int = 2, sign: bool = False) -> str:
    return f"{num(value, decimals, sign)} USDT"


def qty(value: Decimal) -> str:
    return num(value, 8)


def price(value: Decimal) -> str:
    return num(value, 2 if abs(value) >= 1 else 6)


def pct(value: Decimal) -> str:
    return f"{num(value, 2, sign=True)} %"
