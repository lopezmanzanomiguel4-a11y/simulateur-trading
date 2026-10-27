"""Moteur : frais, prix moyen, P&L, refus d'ordres, capital, performance. Prix simulés, sans réseau."""

from decimal import Decimal

import pytest

from conftest import quote
from simtrade.engine import MAX_CAPITAL, normalize_pair, validate_capital
from simtrade.errors import InvalidCapital, InvalidInput, OrderRejected
from simtrade.models import Side

D = Decimal


# --- Achat ---


def test_achat_au_ask_avec_frais_inclus_dans_le_montant(portfolio):
    # 1001 USDT au total, ask 100 : 10 unités x 100 = 1000 + 0,1 % de frais = 1001
    order = portfolio.buy(quote(bid="99", ask="100"), "1001")
    assert order.side is Side.BUY
    assert order.price == D("100")  # ask, pas bid
    assert order.quantity == D("10")
    assert order.gross == D("1000")
    assert order.fee == D("1")
    assert order.cash_flow == D("-1001")
    assert portfolio.account.cash == D("8999")


def test_prix_moyen_inclut_les_frais(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    assert portfolio.positions["BTCUSDT"].avg_price == D("100.1")


def test_prix_moyen_pondere_sur_deux_achats(portfolio):
    portfolio.buy(quote(ask="100"), "1001")  # 10 unités, coût 1001
    portfolio.buy(quote(ask="200"), "2002")  # 10 unités, coût 2002
    position = portfolio.positions["BTCUSDT"]
    assert position.quantity == D("20")
    assert position.cost_basis == D("3003")
    assert position.avg_price == D("150.15")


def test_achat_ne_depasse_jamais_le_montant(portfolio):
    order = portfolio.buy(quote(ask="80659.95"), "10000")
    assert -order.cash_flow <= D("10000")
    assert portfolio.account.cash >= 0
    assert order.fee == (order.gross * D("0.001")).quantize(D("0.00000001"))


def test_achat_virgule_decimale_acceptee(portfolio):
    order = portfolio.buy(quote(ask="100"), "100,1")
    assert order.quantity == D("1")


# --- Vente ---


def test_vente_totale_au_bid_avec_pnl_net(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    order = portfolio.sell(quote(bid="110", ask="111"), "tout")
    assert order.price == D("110")  # bid, pas ask
    assert order.gross == D("1100")
    assert order.fee == D("1.1")
    assert order.cash_flow == D("1098.9")
    assert order.realized_pnl == D("97.9")  # 1098,9 - 1001
    assert "BTCUSDT" not in portfolio.positions
    assert portfolio.account.cash == D("10097.9")
    assert portfolio.realized_pnl == D("97.9")
    assert portfolio.fees_paid == D("2.1")


def test_vente_partielle_garde_le_prix_moyen(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    order = portfolio.sell(quote(bid="110"), "4")
    # produit 440 - 0,44 = 439,56 ; coût 4/10 x 1001 = 400,4
    assert order.realized_pnl == D("39.16")
    position = portfolio.positions["BTCUSDT"]
    assert position.quantity == D("6")
    assert position.cost_basis == D("600.6")
    assert position.avg_price == D("100.1")


def test_vente_a_perte(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    order = portfolio.sell(quote(bid="90"), "tout")
    assert order.realized_pnl == D("899.1") - D("1001")


def test_aller_retour_sans_mouvement_coute_spread_et_frais(portfolio):
    portfolio.buy(quote(bid="99", ask="100"), "1001")
    portfolio.sell(quote(bid="99", ask="100"), "tout")
    # 10 x 99 = 990 - 0,99 de frais = 989,01 récupérés pour 1001 dépensés
    assert portfolio.account.cash == D("10000") - D("1001") + D("989.01")


# --- Refus ---


@pytest.mark.parametrize("amount", ["0", "-50"])
def test_refuse_montant_nul_ou_negatif(portfolio, amount):
    with pytest.raises(OrderRejected, match="strictement positif"):
        portfolio.buy(quote(), amount)


def test_refuse_montant_non_numerique(portfolio):
    with pytest.raises(InvalidInput, match="pas un nombre"):
        portfolio.buy(quote(), "beaucoup")


def test_refuse_solde_insuffisant(portfolio):
    with pytest.raises(OrderRejected, match="Solde insuffisant"):
        portfolio.buy(quote(), "10000.01")
    assert portfolio.account.cash == D("10000")
    assert portfolio.positions == {}


def test_refuse_montant_trop_faible(portfolio):
    with pytest.raises(OrderRejected, match="trop faible"):
        portfolio.buy(quote(ask="80000"), "0.0001")


def test_refuse_vente_sans_position(portfolio):
    with pytest.raises(OrderRejected, match="Aucune position"):
        portfolio.sell(quote(), "1")


def test_refuse_vente_superieure_a_la_position(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    with pytest.raises(OrderRejected, match="Quantité insuffisante"):
        portfolio.sell(quote(), "10.00000001")
    assert portfolio.positions["BTCUSDT"].quantity == D("10")


@pytest.mark.parametrize("qty", ["0", "-1"])
def test_refuse_quantite_nulle_ou_negative(portfolio, qty):
    portfolio.buy(quote(ask="100"), "1001")
    with pytest.raises(OrderRejected, match="strictement positive"):
        portfolio.sell(quote(), qty)


def test_refus_ne_modifie_rien(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    with pytest.raises(OrderRejected):
        portfolio.sell(quote(pair="ETHUSDT"), "1")
    assert portfolio.account.cash == D("8999")
    assert portfolio.fees_paid == D("1")


# --- Paires ---


@pytest.mark.parametrize("raw", ["BTCUSDT", "btcusdt", "btc", "BTC/USDT", "btc-usdt"])
def test_normalise_les_paires(raw):
    assert normalize_pair(raw) == "BTCUSDT"


def test_refuse_paire_inconnue():
    with pytest.raises(InvalidInput, match="Paire inconnue"):
        normalize_pair("DOGEUSDT")


# --- Valorisation et performance ---


def test_performance_nulle_sans_position(portfolio):
    v = portfolio.valuation({})
    assert v.total_value == D("10000")
    assert v.performance_pct == 0


def test_valorisation_en_gain(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    v = portfolio.valuation({"BTCUSDT": quote(bid="120", ask="121")})
    line = v.positions[0]
    assert line.value == D("1200")  # au bid
    assert line.unrealized_pnl == D("199")
    assert v.total_value == D("10199")
    assert v.performance_pct == D("1.99")


def test_valorisation_en_perte(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    v = portfolio.valuation({"BTCUSDT": quote(bid="99", ask="100")})
    assert v.unrealized_pnl == D("-11")
    assert v.total_value == D("9989")
    assert v.performance_pct == D("-0.11")


def test_performance_inclut_le_realise(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    portfolio.sell(quote(bid="110"), "tout")
    v = portfolio.valuation({})
    assert v.realized_pnl == D("97.9")
    assert v.performance_pct == D("0.979")


def test_performance_relative_au_capital_initial(portfolio):
    portfolio.account.initial_capital = D("2000")
    portfolio.account.cash = D("2000")
    portfolio.buy(quote(ask="100"), "1001")
    v = portfolio.valuation({"BTCUSDT": quote(bid="120")})
    assert v.performance_pct == D("9.95")  # +199 sur 2000


def test_valorisation_sans_prix_refusee(portfolio):
    portfolio.buy(quote(ask="100"), "1001")
    with pytest.raises(Exception, match="Pas de prix"):
        portfolio.valuation({})


# --- Capital ---


@pytest.mark.parametrize("raw", ["0", "-100", "10000001", "abc", "NaN", "inf", "100.123", ""])
def test_refuse_capital_invalide(raw):
    with pytest.raises(InvalidCapital):
        validate_capital(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [("10000000", MAX_CAPITAL), ("0.01", D("0.01")), ("2500", D("2500")), ("1234,5", D("1234.50")), ("10.000", D("10"))],
)
def test_accepte_capital_valide(raw, expected):
    assert validate_capital(raw) == expected


def test_capital_par_defaut():
    assert validate_capital() == D("10000")
