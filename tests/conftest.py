from datetime import datetime, timezone
from decimal import Decimal

import pytest

from simtrade.engine import Portfolio
from simtrade.models import Account, Quote

D = Decimal


def quote(pair: str = "BTCUSDT", bid: str = "100", ask: str = "100") -> Quote:
    return Quote(pair, D(bid), D(ask), "Test", datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(Account(1, D("10000"), D("10000"), datetime(2026, 1, 1, tzinfo=timezone.utc)))
