"""Règles pures des ordres en attente : déclenchement, glissement, refus à la pose."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import quote
from simtrade.errors import OrderRejected
from simtrade.models import LIMIT, STOP, TARGET, PendingOrder, Side
from simtrade.orders import check_kind, check_min_buy, check_new, slippage, triggered

D = Decimal
T0 = datetime(2026, 10, 28, tzinfo=timezone.utc)


def pending(kind, side, price):
    return PendingOrder(1, "BTCUSDT", kind, side, D(price), None, None, T0)


def test_achat_limite_declenche_sur_l_ask():
    order = pending(LIMIT, Side.BUY, "100")
    assert triggered(order, quote(bid="99", ask="100"))
    assert triggered(order, quote(bid="97", ask="98"))
    assert not triggered(order, quote(bid="99.99", ask="100.01"))  # bid sous la limite, ask au-dessus


def test_vente_limite_et_objectif_declenchent_sur_le_bid():
    for kind in (LIMIT, TARGET):
        order = pending(kind, Side.SELL, "110")
        assert triggered(order, quote(bid="110", ask="110.1"))
        assert not triggered(order, quote(bid="109.99", ask="110.5"))  # ask au-dessus, bid en dessous


def test_stop_declenche_sur_le_bid_pas_sur_l_ask():
    order = pending(STOP, Side.SELL, "100")
    assert triggered(order, quote(bid="100", ask="100.1"))
    assert triggered(order, quote(bid="95", ask="95.1"))
    assert not triggered(order, quote(bid="100.01", ask="100.02"))


def test_glissement_positif_si_defavorable():
    assert slippage(Side.SELL, D("98"), D("95"), D("10")) == D("30")  # stop sauté : 3 x 10
    assert slippage(Side.SELL, D("98"), D("98"), D("10")) == 0
    assert slippage(Side.SELL, D("110"), D("111"), D("2")) == D("-2")  # objectif amélioré
    assert slippage(Side.BUY, D("100"), D("99"), D("3")) == D("-3")  # limite achat améliorée


@pytest.mark.parametrize(
    "kind, side, price, bid, ask",
    [
        (STOP, Side.SELL, "100", "99", "99.1"),  # stop au-dessus du bid
        (TARGET, Side.SELL, "100", "101", "101.1"),  # objectif sous le bid
        (LIMIT, Side.SELL, "100", "100", "100.1"),
        (LIMIT, Side.BUY, "101", "99.9", "100"),  # limite achat au-dessus de l'ask
    ],
)
def test_pose_refusee_si_execution_immediate(kind, side, price, bid, ask):
    with pytest.raises(OrderRejected, match="exécuté immédiatement"):
        check_new(kind, side, "BTCUSDT", D(price), quote(bid=bid, ask=ask))


def test_pose_acceptee_hors_du_prix():
    check_new(STOP, Side.SELL, "BTCUSDT", D("99"), quote(bid="100", ask="100.1"))
    check_new(TARGET, Side.SELL, "BTCUSDT", D("101"), quote(bid="100", ask="100.1"))
    check_new(LIMIT, Side.BUY, "BTCUSDT", D("99"), quote(bid="100", ask="100.1"))


@pytest.mark.parametrize("kind", [STOP, TARGET])
def test_pas_de_stop_ni_d_objectif_a_l_achat(kind):
    with pytest.raises(OrderRejected, match="seuls les achats limites"):
        check_kind(kind, Side.BUY)


def test_type_inconnu():
    with pytest.raises(OrderRejected, match="Type d'ordre inconnu"):
        check_kind("trailing", Side.SELL)


def test_montant_trop_faible_au_prix_limite():
    with pytest.raises(OrderRejected, match="Montant trop faible"):
        check_min_buy("BTCUSDT", D("0.0000001"), D("80000"), D("0.001"))
