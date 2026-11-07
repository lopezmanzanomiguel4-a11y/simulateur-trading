"""Allers-retours reconstitués depuis les ordres, motifs de sortie, journal chronologique (pur)."""

from types import SimpleNamespace

from etape7 import MIN, T0, D, order
from simtrade.models import SESSION_CLOSE, STOP, TARGET, Side
from simtrade.trades import Links, journal, links_from_signals, trades_from_orders


def sig(sid, kind, order_id, strategy="s1", at=T0, decision="ordre"):
    return SimpleNamespace(id=sid, kind=kind, decision=decision, order_id=order_id, details={"strategie": strategy},
                           created_at=at)


def test_aller_retour_net_des_frais():
    orders = [order(1, T0, Side.BUY, "1", "100"), order(2, T0 + 5 * MIN, Side.SELL, "1", "110", kind=TARGET)]
    (t,) = trades_from_orders(orders)
    # achat : 100 + 0,1 de frais ; vente : 110 − 0,11
    assert t.cost == D("100.1") and t.proceeds == D("109.89") and t.fees == D("0.21")
    assert t.pnl == D("9.79")
    assert t.exit_reason == "objectif" and t.order_ids == (1, 2)


def test_motifs_de_sortie_et_strategie():
    orders = [
        order(1, T0, Side.BUY, "1", "100"), order(2, T0 + MIN, Side.SELL, "1", "99", kind=STOP),
        order(3, T0 + 2 * MIN, Side.BUY, "1", "100"), order(4, T0 + 3 * MIN, Side.SELL, "1", "101"),
        order(5, T0 + 4 * MIN, Side.BUY, "1", "100"), order(6, T0 + 5 * MIN, Side.SELL, "1", "101", kind=SESSION_CLOSE),
        order(7, T0 + 6 * MIN, Side.BUY, "1", "100"), order(8, T0 + 7 * MIN, Side.SELL, "1", "101"),
    ]
    signals = [sig(1, "entree", 1), sig(2, "entree", 3, "s2"), sig(3, "sortie", 4), sig(4, "entree", 9, decision="refuse")]
    trades = trades_from_orders(orders, links_from_signals(signals))
    assert [t.exit_reason for t in trades] == ["stop", "signal", "cloture_session", "manuel"]
    assert [t.strategy for t in trades] == ["s1", "s2", None, None]


def test_position_de_zero_a_zero_avec_renfort_et_ventes_partielles():
    orders = [
        order(1, T0, Side.BUY, "1", "100"),
        order(2, T0 + MIN, Side.BUY, "1", "102"),
        order(3, T0 + 2 * MIN, Side.SELL, "0.5", "103"),
        order(4, T0 + 3 * MIN, Side.SELL, "1.5", "104", kind=STOP),
        order(5, T0 + 4 * MIN, Side.BUY, "1", "100"),  # encore ouverte : pas de trade
    ]
    (t,) = trades_from_orders(orders)
    assert t.quantity == D("2") and t.order_ids == (1, 2, 3, 4)
    assert t.pnl == (D("51.5") - D("0.0515") + D("156") - D("0.156")) - (D("100.1") + D("102.102"))
    assert t.exit_reason == "stop"


def test_paires_independantes():
    orders = [
        order(1, T0, Side.BUY, "1", "100"),
        order(2, T0 + MIN, Side.BUY, "1", "10", pair="ETHUSDT"),
        order(3, T0 + 2 * MIN, Side.SELL, "1", "11", pair="ETHUSDT"),
        order(4, T0 + 3 * MIN, Side.SELL, "1", "101"),
    ]
    assert [(t.pair, t.closed_at) for t in trades_from_orders(orders, Links())] == [
        ("ETHUSDT", T0 + 2 * MIN), ("BTCUSDT", T0 + 3 * MIN)
    ]


def test_journal_chronologique():
    orders = [order(1, T0 + MIN, Side.BUY, "1", "100"), order(2, T0 + 3 * MIN, Side.SELL, "1", "101", kind=TARGET)]
    signals = [sig(1, "entree", 1, at=T0 + MIN), sig(2, "entree", None, at=T0 + 2 * MIN, decision="refuse")]
    refusals = [SimpleNamespace(id=1, created_at=T0 + 2 * MIN)]
    trades = trades_from_orders(orders, links_from_signals(signals))
    entries = journal(signals, refusals, orders, trades)
    assert [(e.at, e.kind) for e in entries] == [
        (T0 + MIN, "signal"), (T0 + MIN, "ordre"),
        (T0 + 2 * MIN, "signal"), (T0 + 2 * MIN, "refus"),
        (T0 + 3 * MIN, "ordre"), (T0 + 3 * MIN, "trade"),
    ]
