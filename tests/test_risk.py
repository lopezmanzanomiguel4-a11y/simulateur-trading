"""Garde-fou, règles pures : chaque règle à la limite exacte et juste au-delà."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from simtrade.errors import InvalidInput
from simtrade.risk import (
    ENTRY_GAP,
    MARKET_ENTRY,
    MAX_LOSS,
    MAX_POSITIONS,
    MAX_SIZE,
    MAX_TRADES,
    STALE_PRICE,
    STOP_REQUIRED,
    EntryRequest,
    RiskLimits,
    RiskState,
    check_entry,
    loss_breached,
    parse_gap,
    update_limits,
)

D = Decimal
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
LIMITS = RiskLimits()  # 20 %, 2 %, 6 entrées, 2 positions, 15 min


def state(**kw):
    base = dict(equity=D("10000"), session_pnl=D("0"), entries=0)
    base.update(kw)
    return RiskState(**base)


def entry(amount="1000", stop="95", pair="BTCUSDT", price="100", now=NOW):
    return EntryRequest(pair, D(amount), D(price), None if stop is None else D(stop), MARKET_ENTRY, now)


def rules(request, st, limits=LIMITS):
    return [r.rule for r in check_entry(request, st, limits)]


def test_entree_raisonnable_acceptee():
    assert rules(entry(), state()) == []


def test_tout_le_capital_sans_stop_refuse():
    refused = check_entry(entry(amount="10000", stop=None), state(), LIMITS)
    assert [r.rule for r in refused] == [STOP_REQUIRED, MAX_SIZE]
    assert "stop-loss obligatoire" in refused[0].message
    assert "2 000,00 USDT" in refused[1].message  # 20 % de 10 000


def test_stop_au_dessus_du_prix_d_entree_refuse():
    assert rules(entry(stop="100"), state()) == [STOP_REQUIRED]
    assert rules(entry(stop="99.99"), state()) == []


def test_taille_max_a_la_limite_exacte():
    assert rules(entry(amount="2000"), state()) == []
    assert rules(entry(amount="2000.01"), state()) == [MAX_SIZE]
    exposed = state(exposure={"BTCUSDT": D("500")})  # position déjà ouverte sur la paire
    assert rules(entry(amount="1500"), exposed) == []
    assert rules(entry(amount="1500.01"), exposed) == [MAX_SIZE]


def test_taille_max_sur_la_valeur_actuelle_du_compte():
    assert rules(entry(amount="1800"), state(equity=D("9000"))) == []
    assert rules(entry(amount="1800.01"), state(equity=D("9000"))) == [MAX_SIZE]


def test_perte_max_a_la_limite_exacte():
    # E = 9800, PnL = -200 -> E0 = 10 000 ; limite 2 % de E0 = 200
    at_limit = state(equity=D("9800"), session_pnl=D("-200"))
    assert at_limit.start_equity == D("10000")
    assert loss_breached(at_limit, LIMITS) and rules(entry(), at_limit) == [MAX_LOSS]
    below = state(equity=D("9800.01"), session_pnl=D("-199.99"))
    assert not loss_breached(below, LIMITS) and rules(entry(), below) == []


def test_arret_de_session_bloque_meme_sans_perte():
    assert rules(entry(), state(halted=True)) == [MAX_LOSS]


def test_entrees_max_par_session():
    assert rules(entry(), state(entries=5)) == []
    assert rules(entry(), state(entries=6)) == [MAX_TRADES]


def test_positions_max():
    two = state(exposure={"ETHUSDT": D("500"), "SOLUSDT": D("500")})
    assert rules(entry(pair="BTCUSDT"), two) == [MAX_POSITIONS]
    assert rules(entry(pair="ETHUSDT"), two) == []  # renforcer une paire déjà exposée reste possible


def test_delai_entre_deux_entrees_sur_une_paire():
    st = state(last_entry={"BTCUSDT": NOW - timedelta(minutes=14, seconds=59)})
    refused = check_entry(entry(), st, LIMITS)
    assert [r.rule for r in refused] == [ENTRY_GAP] and "encore 1 s" in refused[0].message
    assert rules(entry(), state(last_entry={"BTCUSDT": NOW - timedelta(minutes=15)})) == []
    assert rules(entry(pair="ETHUSDT"), st) == []  # autre paire : pas de délai


def test_prix_perime():
    assert rules(entry(), state(stale=("ETHUSDT",))) == [STALE_PRICE]


def test_refus_cumules():
    st = state(entries=6, halted=True, stale=("BTCUSDT",), exposure={"ETHUSDT": D("1"), "SOLUSDT": D("1")})
    assert set(rules(entry(amount="5000", stop=None), st)) == {
        STOP_REQUIRED, STALE_PRICE, MAX_LOSS, MAX_TRADES, MAX_POSITIONS, MAX_SIZE
    }


def test_modifier_les_limites():
    limits = update_limits(LIMITS, {"taille": "30", "perte": "1,5", "trades": "4", "positions": "1", "delai": "10m"})
    assert limits == RiskLimits(D("30"), D("1.5"), 4, 1, timedelta(minutes=10))
    assert parse_gap("900s") == parse_gap("15") == parse_gap("0.25h") == timedelta(minutes=15)


@pytest.mark.parametrize(
    "changes",
    [{"taille": "0"}, {"taille": "101"}, {"perte": "abc"}, {"trades": "0"}, {"positions": "1.5"}, {"delai": "-1m"}, {"levier": "2"}],
)
def test_limites_invalides_refusees(changes):
    with pytest.raises(InvalidInput):
        update_limits(LIMITS, changes)
