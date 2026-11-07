"""Backtest d'une stratégie sur des klines 1m : simulateur pur (aucun I/O), même code de décision que le live.

Réutilisés tels quels : strategies.evaluate / entry_blockers / levels, risk.check_entry (état du compte
reconstitué en mémoire), orders.check_new, engine.Portfolio (montants, frais), sessions, trades, stats.

Règles d'exécution (D9 de RAPPORT-ETAPE7, détaillées au README) :
- décision à la clôture T de chaque bougie de l'intervalle, sur la fenêtre du live : MAX_CANDLES − 1
  bougies clôturées de l'intervalle et de 1m (la série du hub contient aussi la bougie en cours) ;
- exécution à l'ouverture de la minute qui commence en T : ask = ouverture × (1 + s/2), bid = × (1 − s/2) ;
  l'ordre est daté T + 1 s (le live décide quelques secondes après la clôture : même comptage de duree_max) ;
- stop touché si bas × (1 − s/2) ≤ stop, objectif si haut × (1 − s/2) ≥ objectif, exécutés à leur niveau ;
  les deux dans la même minute : stop ; ouverture déjà au-delà : exécution à l'ouverture (bid), glissement noté ;
- clôture forcée à l'heure de clôture de session (23:55, ouverture de la minute, au bid) ; pas d'entrée après
  la dernière entrée (23:00) ;
- trou dans la fenêtre (minute ou bougie absente ou incomplète) : signal ignoré (`historique_partiel`) ;
  minute de l'exécution absente : signal ignoré (`pas_de_cotation_fraiche`) ;
- arrêt de session (perte max) : vérifié au plus bas de chaque minute en position (bas × (1 − s/2), même règle
  prudente que les stops ; le live voit chaque cotation) et après chaque vente.

Swing (stratégie `horizon: swing`, étape 7c) : mêmes règles, sur des klines 1h au lieu de 1m :
- la barre de base est l'heure : décision à la clôture T de la bougie 1h / 4h / 1d (fenêtre SWING_BARS),
  exécution à l'ouverture de la barre 1h qui commence en T ; stop / objectif testés sur chaque barre 1h ;
- pas de clôture de session ni d'heure limite d'entrée, pas d'arrêt de session ;
- stop suiveur : sur chaque barre, test du stop au niveau d'AVANT (ouverture puis bas), puis seulement
  relèvement avec le haut de la barre au bid : plus haut retenu = max(plus haut, haut × (1 − s/2)),
  niveau = max(niveau, plus haut − distance) ; distance figée à l'entrée ;
- garde-fou swing (SWING_LIMITS par défaut) : perte sur 7 jours glissants (réalisé + latent), exposition ;
- taille au risque si `taille.risque_pct` ; courbe de valeur à la fin de chaque heure.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any

from simtrade import orders as rules_orders
from simtrade import stats as stats_mod
from simtrade import strategies
from simtrade import trades as trades_mod
from simtrade.bricks import MarketView
from simtrade.engine import FEE_RATE, Portfolio
from simtrade.errors import InvalidInput, OrderRejected
from simtrade.market import BAR_SECONDS, INTERVALS, MAX_CANDLES, Candle, aggregate, bucket_start, session_start
from simtrade.models import (
    AUTO, LATE_SESSION_CLOSE, MARKET, SESSION_CLOSE, STOP, TARGET, TRAILING, Account, Order, Quote, Side,
)
from simtrade.risk import (
    MARKET_ENTRY, MAX_LOSS, SWING_LIMITS, EntryRequest, RiskLimits, RiskState, check_entry, loss_breached, risk_size,
)
from simtrade.service import ENTRY, EXIT, IGNORED, IMPOSSIBLE, ORDER, REFUSED
from simtrade.sessions import SessionRules, close_time, is_late, session_date
from simtrade.strategies import Evaluation, Plan, Strategy

SOURCE = "backtest"
MINUTE = timedelta(minutes=1)
DECISION_DELAY = timedelta(seconds=1)  # ordre sur signal : T + 1 s
INTRA_MINUTE = timedelta(seconds=30)  # stop / objectif touché pendant la minute : daté du milieu de la minute
WEEK = timedelta(days=7)
PARTIAL_HISTORY = "historique_partiel"
DEFAULT_SPREAD_PCT = Decimal("0.02")
HUNDRED = Decimal(100)
CENT = Decimal("0.01")
PRICE_STEP = Decimal("0.00000001")


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_STEP, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class BacktestConfig:
    spread_pct: Decimal = DEFAULT_SPREAD_PCT  # s, en % du prix
    fee_rate: Decimal = FEE_RATE
    rules: SessionRules = SessionRules()
    limits: RiskLimits = RiskLimits()
    capital: Decimal = Decimal("10000")
    # True : une fenêtre plus courte que celle du live (début des données) compte comme un trou.
    full_window: bool = True

    @property
    def half_spread(self) -> Decimal:
        return self.spread_pct / HUNDRED / 2

    def quote(self, pair: str, price: Decimal, at: datetime) -> Quote:
        h = self.half_spread
        return Quote(pair, _price(price * (1 - h)), _price(price * (1 + h)), SOURCE, at)

    def bid(self, price: Decimal) -> Decimal:
        return _price(price * (1 - self.half_spread))


# --- Données préparées (une fois par paire et par intervalle) ---


class Market:
    """Klines de base triées d'une paire (1m ; 1h pour le swing), bougies de l'intervalle agrégées, et index
    des fenêtres du live. `ones`, `by_minute` : les bougies de base (des heures en swing)."""

    def __init__(self, candles_1m: Sequence[Candle], interval: str):
        ones = sorted((c for c in candles_1m if c.interval == "1m"), key=lambda c: c.open_time)
        base = "1m"
        if not ones:
            ones = sorted((c for c in candles_1m if c.interval == "1h"), key=lambda c: c.open_time)
            base = "1h"
        if not ones:
            raise InvalidInput("Backtest : aucune bougie 1m.")
        if base == "1m" and (interval not in INTERVALS or interval == "1m"):
            raise InvalidInput(f"Backtest : intervalle invalide '{interval}'.")
        if base == "1h" and interval not in strategies.SWING_INTERVALS:
            raise InvalidInput(f"Backtest swing : intervalle invalide '{interval}' (1h, 4h ou 1d).")
        self.pair = ones[0].pair
        self.interval = interval
        self.base = base
        self.base_step = timedelta(seconds=BAR_SECONDS[base])
        self.step = timedelta(seconds=BAR_SECONDS[interval])
        self.ones = ones
        self.one_times = [c.open_time for c in ones]
        self.by_minute = {c.open_time: c for c in ones}
        self.bars = ones if interval == base else aggregate(ones, interval)
        self.bar_by_close = {c.close_time: k for k, c in enumerate(self.bars)}
        self._bad = [0]
        for c in self.bars:
            self._bad.append(self._bad[-1] + (0 if c.complete else 1))
        if base == "1m":
            self.n_bars = MAX_CANDLES[interval] - 1
            self.n_ones = MAX_CANDLES["1m"] - 1
        else:  # swing : pas de série 1m dans la vue (aucune brique swing ne la lit)
            self.n_bars = strategies.SWING_BARS[interval]
            self.n_ones = 0

    @property
    def first(self) -> datetime:
        return self.ones[0].open_time

    def window(self, at: datetime, full: bool = True) -> tuple[MarketView, bool] | None:
        """Vue du live à la clôture `at`, et True si la fenêtre a un trou. None : pas de bougie close en `at`."""
        k = self.bar_by_close.get(at)
        if k is None:
            return None
        k0 = max(0, k - self.n_bars + 1)
        bars = self.bars[k0 : k + 1]
        i0 = bisect.bisect_left(self.one_times, at - self.n_ones * self.base_step)
        i1 = bisect.bisect_left(self.one_times, at)
        ones = self.ones[i0:i1]
        from_bars = at - self.n_bars * self.step
        from_ones = at - self.n_ones * self.base_step
        if not full:
            from_bars = max(from_bars, bucket_start(self.first, int(self.step.total_seconds())))
            from_ones = max(from_ones, self.first)
        partial = (
            bars[0].open_time != from_bars
            or len(bars) != (at - from_bars) // self.step
            or self._bad[k + 1] - self._bad[k0] > 0
            or len(ones) != (at - from_ones) // self.base_step
        )
        return MarketView(at, bars, ones), partial

    def candles(self, start: datetime, end: datetime) -> list[Candle]:
        i0 = bisect.bisect_left(self.one_times, start)
        i1 = bisect.bisect_left(self.one_times, end)
        return self.ones[i0:i1]


def prepare(candles_1m: Sequence[Candle], interval: str) -> Market:
    return Market(candles_1m, interval)


@dataclass
class EvalCache:
    """Évaluations sans position (entered_at = None) et plans, par clôture : partagés entre les tirages au
    hasard d'une même stratégie sur les mêmes données."""

    evaluations: dict[datetime, Evaluation] = field(default_factory=dict)
    plans: dict[datetime, Plan | None] = field(default_factory=dict)
    reasons: dict[datetime, tuple[str, ...]] = field(default_factory=dict)  # trous, VWAP non fiable


# --- Résultat ---


@dataclass(frozen=True)
class Signal:
    """Décision sur une bougie (même forme que SignalRecord pour trades.links_from_signals et le journal)."""

    id: int
    created_at: datetime
    candle_open_time: datetime
    kind: str  # entree | sortie
    decision: str  # ordre | ignore | refuse
    reasons: tuple[str, ...]
    order_id: int | None
    details: dict[str, Any]


@dataclass
class BacktestResult:
    strategy: str
    pair: str
    interval: str
    start: datetime
    end: datetime
    config: BacktestConfig
    orders: list[Order]
    signals: list[Signal]
    evaluations: list[tuple[datetime | None, bool | None, bool | None]]  # (bougie, entrée, sortie)
    trades: list[trades_mod.Trade]
    curve: stats_mod.Curve
    stats: stats_mod.Stats
    decisions: int = 0  # clôtures évaluées
    eligible: int = 0  # clôtures où une entrée était possible (sans position, heures actives, sans trou...)
    ignored: Counter = field(default_factory=Counter)  # raisons des signaux ignorés ou refusés
    halts: list[date] = field(default_factory=list)  # sessions arrêtées (perte max)

    @property
    def entries(self) -> int:
        return sum(1 for o in self.orders if o.side is Side.BUY)


# --- Simulateur ---


class _Sim:
    def __init__(
        self,
        strategy: Strategy,
        market: Market,
        config: BacktestConfig,
        entries: Callable[[], bool] | None,
        cache: EvalCache | None,
    ):
        self.s = strategy
        self.m = market
        self.c = config
        self.entries = entries
        self.cache = cache if cache is not None else EvalCache()
        self.pf = Portfolio(
            Account(None, config.capital, config.capital, market.first, "backtest", AUTO), fee_rate=config.fee_rate
        )
        self.protection: tuple[Decimal, Decimal | None] | None = None  # (stop, objectif) de la position ouverte
        self.trail: tuple[Decimal, Decimal] | None = None  # stop suiveur : (distance figée, plus haut retenu au bid)
        self.swing = strategy.swing
        self.intra = market.base_step / 2  # stop / objectif touché pendant la barre : daté de son milieu
        self.sells: list[tuple[datetime, Decimal]] = []  # (date, réalisé) : perte sur 7 jours du swing
        self.entered_at: datetime | None = None
        self.orders: list[Order] = []
        self.signals: list[Signal] = []
        self.evaluations: list[tuple[datetime | None, bool | None, bool | None]] = []
        self.realized: dict[date, Decimal] = {}
        self.day_entries: dict[date, int] = {}
        self.halted: set[date] = set()
        self.last_entry: dict[str, datetime] = {}
        self.decisions = 0
        self.eligible = 0
        self.ignored: Counter = Counter()
        self.uses_duration = strategy.brick("duree_max") is not None

    @property
    def in_position(self) -> bool:
        return self.s.paire in self.pf.positions

    # --- Journal ---

    def _signal(self, at: datetime, candle_open: datetime, kind: str, decision: str, reasons=(), order_id=None,
                details: dict | None = None) -> None:
        if decision != ORDER:
            self.ignored.update(reasons)
        self.signals.append(
            Signal(len(self.signals) + 1, at, candle_open, kind, decision, tuple(reasons), order_id,
                   {"strategie": self.s.nom, **(details or {})})
        )

    def _record(self, order: Order) -> Order:
        order = replace(order, id=len(self.orders) + 1)
        self.orders.append(order)
        return order

    # --- Ventes ---

    def _sell(self, price: Decimal, at: datetime, kind: str, trigger: Decimal | None = None) -> Order:
        quote = Quote(self.s.paire, price, price, SOURCE, at)
        order = self.pf.sell(quote, "tout", at)
        slip = rules_orders.slippage(Side.SELL, trigger, order.price, order.quantity) if trigger is not None else Decimal(0)
        order = self._record(replace(order, kind=kind, trigger_price=trigger, slippage=slip))
        day = session_date(at)
        self.realized[day] = self.realized.get(day, Decimal(0)) + order.realized_pnl
        self.sells.append((at, order.realized_pnl))
        self.protection = None
        self.trail = None
        self.entered_at = None
        self._halt_check(day, None)
        return order

    def _halt_check(self, day: date, price: Decimal | None) -> None:
        """Arrêt de session si la perte max est atteinte, la position marquée au bid estimé de `price`.
        Swing : pas d'arrêt de session (la perte sur 7 jours bloque les entrées dans check_entry)."""
        if self.swing or day in self.halted:
            return
        unrealized = Decimal(0)
        equity = self.pf.account.cash
        position = self.pf.positions.get(self.s.paire)
        if position is not None and price is not None:
            value = position.quantity * self.c.bid(price)
            unrealized = value - position.cost_basis
            equity += value
        elif position is not None:
            return
        state = RiskState(equity, self.realized.get(day, Decimal(0)) + unrealized, 0)
        if loss_breached(state, self.c.limits):
            self.halted.add(day)

    def _session_due(self, t: datetime) -> bool:
        if self.swing:
            return False
        return self.entered_at is not None and t >= close_time(session_date(self.entered_at), self.c.rules)

    @property
    def _stop_kind(self) -> str:
        return STOP if self.trail is None else TRAILING

    def _open(self, c: Candle, t: datetime) -> None:
        """Ouverture de la minute : clôture de session due, ou stop / objectif déjà dépassés."""
        if self._session_due(t):
            kind = LATE_SESSION_CLOSE if is_late(self.entered_at, t, self.c.rules) else SESSION_CLOSE  # type: ignore[arg-type]
            self._sell(self.c.bid(c.open), t, kind)
            return
        stop, target = self.protection  # type: ignore[misc]
        bid = self.c.bid(c.open)
        if bid <= stop:
            self._sell(bid, t, self._stop_kind, stop)
        elif target is not None and bid >= target:
            self._sell(bid, t, TARGET, target)

    def _inside(self, c: Candle, t: datetime) -> None:
        """Pendant la barre : stop d'abord (prudent), puis objectif, exécutés à leur niveau ; ensuite
        seulement, relèvement du stop suiveur avec le haut de la barre (jamais avant le test)."""
        stop, target = self.protection  # type: ignore[misc]
        if self.c.bid(c.low) <= stop:
            self._sell(stop, t + self.intra, self._stop_kind, stop)
        elif target is not None and self.c.bid(c.high) >= target:
            self._sell(target, t + self.intra, TARGET, target)
        elif self.trail is not None:
            self._raise_trail(self.c.bid(c.high))

    def _raise_trail(self, high: Decimal) -> None:
        distance, kept = self.trail  # type: ignore[misc]
        kept = max(kept, high)
        self.trail = (distance, kept)
        stop, target = self.protection  # type: ignore[misc]
        self.protection = (max(stop, kept - distance), target)

    # --- Décision à la clôture ---

    def _evaluate(self, at: datetime, view: MarketView) -> Evaluation:
        if self.in_position and self.uses_duration:
            return strategies.evaluate(self.s, view, self.entered_at)
        ev = self.cache.evaluations.get(at)
        if ev is None:
            ev = self.cache.evaluations[at] = strategies.evaluate(self.s, view, None)
        return ev

    def _plan(self, at: datetime, view: MarketView, ev: Evaluation) -> Plan | None:
        if ev.plan is not None:
            return ev.plan
        if at not in self.cache.plans:
            self.cache.plans[at] = strategies.plan(self.s, view)
        return self.cache.plans[at]

    def _data_reasons(self, view: MarketView, partial: bool) -> tuple[str, ...]:
        if partial:
            return (PARTIAL_HISTORY,)
        if self.s.uses_vwap:
            start = session_start(view.now)
            if sum((c.volume for c in view.candles_1m if c.open_time >= start), Decimal(0)) == 0:
                return (strategies.VWAP_UNRELIABLE,)
        return ()

    def decide(self, at: datetime, c: Candle | None) -> None:
        win = self.m.window(at, self.c.full_window)
        if win is None:
            return  # aucune bougie clôturée en `at` (toutes ses minutes manquent) : rien à évaluer
        view, partial = win
        ev = self._evaluate(at, view)
        self.decisions += 1
        self.evaluations.append((ev.candle_open, ev.entry, ev.exit))
        now = at + DECISION_DELAY
        has_position = self.in_position  # contexte lu avant la sortie, comme le runner
        day = session_date(now)
        candle_open = ev.candle_open or at - self.m.step
        reasons_cache: list[tuple[str, ...]] = []

        def data_reasons() -> tuple[str, ...]:
            if not reasons_cache:
                if at not in self.cache.reasons:
                    self.cache.reasons[at] = self._data_reasons(view, partial)
                reasons_cache.append(self.cache.reasons[at])
            return reasons_cache[0]

        if has_position and ev.exit:
            reasons = data_reasons() or (() if c is not None else (strategies.NO_FRESH_QUOTE,))
            if reasons:
                self._signal(now, candle_open, EXIT, IGNORED, reasons)
            else:
                order = self._sell(self.c.bid(c.open), now, MARKET)  # type: ignore[union-attr]
                self._signal(now, candle_open, EXIT, ORDER, (), order.id, {"bid": str(order.price)})

        blockers = strategies.entry_blockers(self.s, now, self.c.rules, has_position, day in self.halted)
        possible = not blockers and not data_reasons()
        if possible:
            self.eligible += 1
        if self.entries is not None:  # entrées au hasard : tirage sur chaque clôture éligible
            if not possible or not self.entries():
                return
            plan = self._plan(at, view, ev)
            if plan is None:
                return
        else:
            if not ev.entry:
                return
            plan = ev.plan
        reasons = data_reasons() + blockers
        if not reasons and c is None:
            reasons = (strategies.NO_FRESH_QUOTE,)
        if reasons:
            self._signal(now, candle_open, ENTRY, IGNORED, reasons)
            return
        self._enter(now, candle_open, c, plan)  # type: ignore[arg-type]

    def _risk_state(self, now: datetime, bid: Decimal) -> RiskState:
        day = session_date(now)
        equity = self.pf.account.cash
        exposure: dict[str, Decimal] = {}
        unrealized = Decimal(0)
        for pair, position in self.pf.positions.items():
            value = position.quantity * bid
            exposure[pair] = value
            equity += value
            unrealized += value - position.cost_basis
        week_start = now - WEEK
        week = sum((pnl for at, pnl in self.sells if at > week_start), Decimal(0))
        return RiskState(
            equity=equity,
            session_pnl=self.realized.get(day, Decimal(0)) + unrealized,
            entries=self.day_entries.get(day, 0),
            exposure=exposure,
            last_entry=dict(self.last_entry),
            halted=day in self.halted,
            week_pnl=week + unrealized,
        )

    def _enter(self, now: datetime, candle_open: datetime, c: Candle, plan: Plan) -> None:
        pair = self.s.paire
        quote = self.c.quote(pair, c.open, now)
        levels = strategies.levels(plan, quote.ask, quote.bid)
        state = self._risk_state(now, quote.bid)
        if self.s.risque_pct is not None:
            amount = Decimal(0)
            if levels.stop < quote.ask:
                amount = risk_size(state.equity, self.pf.free_cash, quote.ask, levels.stop, self.s.risque_pct,
                                   self.c.limits.max_position_pct, self.c.fee_rate)
        else:
            amount = (state.equity * self.s.taille_pct / HUNDRED).quantize(CENT, rounding=ROUND_DOWN)
        details = {"bid": str(quote.bid), "ask": str(quote.ask), "stop": str(levels.stop),
                   "objectif": None if levels.target is None else str(levels.target), "montant": str(amount)}
        if levels.trail_distance is not None:
            details["distance_suiveur"] = str(levels.trail_distance)
        if levels.reasons:
            self._signal(now, candle_open, ENTRY, IGNORED, levels.reasons, details=details)
            return
        try:
            amount = self.pf.check_buy(amount)
        except OrderRejected as exc:
            self._signal(now, candle_open, ENTRY, IGNORED, (IMPOSSIBLE,), details={**details, "message": str(exc)})
            return
        request = EntryRequest(pair, amount, quote.ask, levels.stop, MARKET_ENTRY, now, bid=quote.bid)
        refusals = check_entry(request, state, self.c.limits)
        if refusals:
            rules = tuple(r.rule for r in refusals)
            if MAX_LOSS in rules:
                self.halted.add(session_date(now))
            self._signal(now, candle_open, ENTRY, REFUSED, rules, details=details)
            return
        try:
            rules_orders.check_new(STOP, Side.SELL, pair, levels.stop, quote)
            if levels.target is not None:
                if levels.target <= levels.stop:
                    raise OrderRejected("objectif sous le stop")
                rules_orders.check_new(TARGET, Side.SELL, pair, levels.target, quote)
            order = self._record(self.pf.buy(quote, amount, now))
        except OrderRejected as exc:
            self._signal(now, candle_open, ENTRY, IGNORED, (IMPOSSIBLE,), details={**details, "message": str(exc)})
            return
        self.protection = (levels.stop, levels.target)
        if self.s.trailing and levels.trail_distance is not None:
            self.trail = (levels.trail_distance, levels.stop + levels.trail_distance)
        self.entered_at = now
        day = session_date(now)
        self.day_entries[day] = self.day_entries.get(day, 0) + 1
        self.last_entry[pair] = now
        self._signal(now, candle_open, ENTRY, ORDER, (), order.id, details)

    # --- Boucle ---

    def run(self, start: datetime, end: datetime) -> BacktestResult:
        base = self.m.base_step
        t = bucket_start(start, int(base.total_seconds()))
        if t < start:
            t += base
        per_bar = int(self.m.step // base)
        index = int((t - bucket_start(t, int(self.m.step.total_seconds()))) // base)
        while t < end:
            c = self.m.by_minute.get(t)
            if c is not None and self.in_position:
                self._open(c, t)
            if index % per_bar == 0:
                self.decide(t, c)
            if c is not None and self.in_position:
                self._inside(c, t)
                if self.in_position:
                    self._halt_check(session_date(t), c.low)
            t += base
            index += 1
        links = trades_mod.links_from_signals(self.signals)
        trades = trades_mod.trades_from_orders(self.orders, links)
        marks = {self.s.paire: self.m.candles(start - timedelta(days=1), end)}
        curve = stats_mod.equity_curve(self.c.capital, self.orders, marks, start, end, self.c.spread_pct, base)
        stats = stats_mod.compute(trades, curve, self.c.capital, self.pf.fees_paid)
        return BacktestResult(
            self.s.nom, self.s.paire, self.s.intervalle, start, end, self.c, self.orders, self.signals,
            self.evaluations, trades, curve, stats, self.decisions, self.eligible, self.ignored, sorted(self.halted),
        )


def run(
    strategy: Strategy,
    data: Market | Sequence[Candle],
    start: datetime,
    end: datetime,
    config: BacktestConfig = BacktestConfig(),
    *,
    entries: Callable[[], bool] | None = None,
    cache: EvalCache | None = None,
) -> BacktestResult:
    """Rejoue `strategy` sur [start, end). `data` : klines 1m (avec l'historique avant `start` pour les
    fenêtres) ou Market déjà préparé. `entries` : remplace le signal d'entrée par un tirage sur chaque
    clôture éligible (références au hasard). Le futur n'est jamais lu : une décision en T ne voit que les
    bougies closes en T, et l'exécution que la minute qui commence en T."""
    market = data if isinstance(data, Market) else prepare(data, strategy.intervalle)
    if market.pair != strategy.paire or market.interval != strategy.intervalle:
        raise InvalidInput(
            f"Backtest : données {market.pair} {market.interval} pour une stratégie {strategy.paire} {strategy.intervalle}."
        )
    if strategy.swing != (market.base == "1h"):
        raise InvalidInput(
            f"Backtest : une stratégie {strategy.horizon} se rejoue sur des klines "
            f"{'1h' if strategy.swing else '1m'}, pas {market.base}."
        )
    if strategy.swing and not config.limits.swing:
        config = replace(config, limits=SWING_LIMITS)  # garde-fou swing par défaut (perte 7 jours, exposition)
    if start >= end:
        raise InvalidInput("Backtest : la période est vide.")
    return _Sim(strategy, market, config, entries, cache).run(start, end)
