"""Références du backtest : ne rien faire, achat 00:00 / vente 23:55, entrées au hasard reproductibles."""

from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from test_backtest import DAY, MIN, ones, strat
from simtrade import references
from simtrade.backtest import BacktestConfig, EvalCache, Market, run
from simtrade.models import SESSION_CLOSE, Side

D = Decimal
CFG = BacktestConfig(full_window=False)


def walk_prices(n: int, seed: int = 5) -> list[Decimal]:
    x, price, out = seed, D("100"), []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2**31
        price += D((x % 41) - 20) / 100
        out.append(price)
    return out


def test_ne_rien_faire():
    s = references.do_nothing(CFG, DAY, DAY + timedelta(days=2))
    assert s.pnl == 0 and s.trades == 0 and s.max_drawdown_pct == 0 and s.sharpe is None and s.sessions == 2


def test_achat_minuit_vente_cloture_a_la_main():
    prices = [D(100)] * 1440 + [D(102)] * 1440
    prices[1435] = D(101)  # ouverture de 23:55 le premier jour
    m = Market(ones(DAY, prices), "5m")
    r = references.open_close(m, DAY, DAY + timedelta(days=2), CFG)
    assert [(o.side, o.created_at) for o in r.orders] == [
        (Side.BUY, DAY), (Side.SELL, DAY.replace(hour=23, minute=55)),
        (Side.BUY, DAY + timedelta(days=1)), (Side.SELL, DAY + timedelta(days=1, hours=23, minutes=55)),
    ]
    buy, sell = r.orders[:2]
    assert buy.price == D(100) * D("1.0001") and sell.price == D(101) * D("0.9999") and sell.kind == SESSION_CLOSE
    qty = (D("1000") / (buy.price * D("1.001"))).quantize(D("0.00000001"), rounding=ROUND_DOWN)
    assert buy.quantity == qty
    assert r.trades[0].exit_reason == "cloture_session" and r.stats.trades == 2


def test_tirages_reproductibles_par_graine():
    data = ones(DAY.replace(hour=6), walk_prices(600))
    m = Market(data, "5m")
    s = strat()
    start, end = DAY.replace(hour=6), DAY.replace(hour=16)
    a = references.random_entries(s, m, start, end, CFG, 0.3, seed=7)
    b = references.random_entries(s, m, start, end, CFG, 0.3, seed=7, cache=EvalCache())
    c = references.random_entries(s, m, start, end, CFG, 0.3, seed=8)
    assert a.orders == b.orders and a.orders
    assert [o.created_at for o in a.orders] != [o.created_at for o in c.orders]


def test_tirages_meme_garde_fou_et_memes_sorties():
    m = Market(ones(DAY.replace(hour=6), walk_prices(600)), "5m")
    start, end = DAY.replace(hour=6), DAY.replace(hour=16)
    r = references.random_entries(strat(), m, start, end, CFG, 1.0, seed=1)  # p = 1 : une entrée dès que possible
    buys = [o for o in r.orders if o.side is Side.BUY]
    assert len(buys) <= CFG.limits.max_trades  # garde-fou : 6 entrées par session au plus
    assert all(b2.created_at - b1.created_at >= CFG.limits.min_entry_gap for b1, b2 in zip(buys, buys[1:]))
    assert {t.exit_reason for t in r.trades} <= {"stop", "objectif", "cloture_session"}


def test_probabilite_et_part_battue():
    m = Market(ones(DAY.replace(hour=6), walk_prices(600)), "5m")
    start, end = DAY.replace(hour=6), DAY.replace(hour=16)
    cache = EvalCache()
    base = run(strat(), m, start, end, CFG, cache=cache)
    p = references.entry_rate(base)
    assert base.eligible > 0 and p == base.entries / base.eligible
    draws = references.random_draws(strat(), m, start, end, CFG, p, seeds=range(1, 11), cache=cache)
    assert len(draws) == 10
    assert references.share_beaten(D("1e9"), draws) == 1.0 and references.share_beaten(D("-1e9"), draws) == 0.0
