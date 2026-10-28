"""API HTTP locale : python -m simtrade api (127.0.0.1:8000).

Toutes les routes sont `async def` : la connexion SQLite, créée dans le lifespan,
n'est utilisable que depuis le thread de la boucle asyncio.
Les nombres sortent en chaîne décimale fixe (format(d, "f")), jamais en float.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import aclosing, asynccontextmanager
from datetime import datetime
from decimal import Decimal

import anyio
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from simtrade.config import db_path
from simtrade import market
from simtrade.engine import DEFAULT_CAPITAL, normalize_pair
from simtrade.errors import FeedUnavailable, InvalidCapital, InvalidInput, OrderRejected, SimTradeError
from simtrade.feeds.base import PriceFeed
from simtrade.hub import CachedFeed, PriceHub
from simtrade.models import Account, Order, Quote
from simtrade.service import TradeResult, TradingService
from simtrade.storage import Storage

HOST = "127.0.0.1"
PORT = 8000

ServiceFactory = Callable[[PriceFeed], TradingService]

STATUS = {InvalidInput: 400, InvalidCapital: 400, OrderRejected: 400, FeedUnavailable: 503}


def default_service(feed: PriceFeed) -> TradingService:
    return TradingService(Storage(db_path()), feed)


# --- Corps des requêtes (chaînes uniquement : un nombre JSON serait un float) ---


class BuyBody(BaseModel):
    paire: str
    montant: str


class SellBody(BaseModel):
    paire: str
    quantite: str


class ResetBody(BaseModel):
    capital: str = str(DEFAULT_CAPITAL)


# --- Sérialisation ---


def dec(value: Decimal) -> str:
    return format(value, "f")


def iso(dt: datetime) -> str:
    return dt.isoformat()


def quote_json(quote: Quote, feed: CachedFeed) -> dict:
    age = feed.age(quote)
    return {
        "paire": quote.pair,
        "bid": dec(quote.bid),
        "ask": dec(quote.ask),
        "spread": dec(quote.spread),
        "source": quote.source,
        "received_at": iso(quote.received_at),
        "age_secondes": round(age, 3),
        "perime": age > feed.max_age,
    }


def account_json(account: Account) -> dict:
    return {
        "id": account.id,
        "capital_initial": dec(account.initial_capital),
        "solde": dec(account.cash),
        "cree_le": iso(account.created_at),
    }


def order_json(order: Order) -> dict:
    return {
        "id": order.id,
        "date": iso(order.created_at),
        "type": order.side.value,
        "paire": order.pair,
        "quantite": dec(order.quantity),
        "prix": dec(order.price),
        "montant": dec(order.gross),
        "frais": dec(order.fee),
        "flux_usdt": dec(order.cash_flow),
        "pnl_realise": dec(order.realized_pnl),
        "source": order.source,
    }


def trade_json(result: TradeResult) -> dict:
    return {"ordre": order_json(result.order), "solde": dec(result.cash)}


def candle_json(candle: market.Candle, now: datetime) -> dict:
    return {
        "ouverture_le": iso(candle.open_time),
        "ouverture": dec(candle.open),
        "haut": dec(candle.high),
        "bas": dec(candle.low),
        "cloture": dec(candle.close),
        "volume": dec(candle.volume),
        "notional": dec(candle.notional),
        "transactions": candle.transactions,
        "origine": candle.origin,
        "complete": candle.complete,
        "cloturee": candle.is_closed(now),
    }


def opt(value: Decimal | None) -> str | None:
    return None if value is None else dec(value)


def opt_iso(dt: datetime | None) -> str | None:
    return None if dt is None else iso(dt)


def vwap_json(result: market.VwapResult) -> dict:
    return {
        "valeur": opt(result.value),
        "ancrage": result.anchor,
        "debut": opt_iso(result.start),
        "fin": opt_iso(result.end),
        "nb_bougies": result.count,
        "volume": dec(result.volume),
    }


def quality_json(q: market.Quality) -> dict:
    return {
        "prix_perime": q.prix_perime,
        "flux_transactions_muet": q.flux_transactions_muet,
        "source_transactions": q.source_transactions,
        "repli_coinbase": q.repli_coinbase,
        "historique": q.historique,
        "derniere_synchro": opt_iso(q.derniere_synchro),
        "bougie_courante_complete": q.bougie_courante_complete,
        "ecart_horloge_ms": None if q.ecart_horloge is None else round(q.ecart_horloge * 1000),
        "vwap_fiable": q.vwap_fiable,
        "raisons": list(q.raisons),
    }


# --- Erreurs de paramètres de requête (la query string, pas le corps JSON) ---

QUERY_CHOICES = {"intervalle": tuple(market.INTERVALS), "vwap": market.VWAP_ANCHORS}


def query_error_message(error: dict) -> str:
    name, raw, ctx = error["loc"][-1], error.get("input"), error.get("ctx") or {}
    kind = error["type"]
    if kind == "int_parsing":
        return f"Paramètre '{name}' invalide : '{raw}' n'est pas un entier."
    if kind == "greater_than_equal":
        return f"Paramètre '{name}' hors bornes : minimum {ctx.get('ge')} (reçu : {raw})."
    if kind == "less_than_equal":
        return f"Paramètre '{name}' hors bornes : maximum {ctx.get('le')} (reçu : {raw})."
    if kind == "literal_error":
        choices = ", ".join(QUERY_CHOICES.get(name, ()))
        return f"Paramètre '{name}' invalide : '{raw}'. Valeurs possibles : {choices}."
    if kind == "bool_parsing":
        return f"Paramètre '{name}' invalide : '{raw}' (true ou false attendu)."
    return f"Paramètre '{name}' invalide : '{raw}'."


# --- Application ---


def create_app(service: ServiceFactory | None = None, hub: PriceHub | None = None) -> FastAPI:
    """`service` reçoit le CachedFeed et renvoie le TradingService ; il est appelé dans le lifespan."""
    make_service = service or default_service

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        price_hub = hub or PriceHub()
        feed = CachedFeed(price_hub)
        svc = make_service(feed)
        task = asyncio.create_task(price_hub.run())
        app.state.hub, app.state.feed, app.state.service = price_hub, feed, svc
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            svc.storage.close()
            await price_hub.aclose()

    app = FastAPI(title="Simulateur de paper trading", lifespan=lifespan)

    @app.exception_handler(SimTradeError)
    async def simtrade_error(request: Request, exc: SimTradeError) -> JSONResponse:
        status = next((code for cls, code in STATUS.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"erreur": str(exc)}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        query = [e for e in exc.errors() if e.get("loc", ())[:1] == ("query",)]
        if query:
            return JSONResponse({"erreur": query_error_message(query[0])}, status_code=400)
        # loc = ("body", "<champ>") pour un champ ; ("body", <position>) pour un JSON illisible.
        fields = [
            e["loc"][1]
            for e in exc.errors()
            if e["type"] != "json_invalid" and len(e.get("loc", ())) >= 2 and isinstance(e["loc"][1], str)
        ]
        if fields:
            message = f"Champ '{fields[0]}' manquant ou invalide (chaîne attendue)."
        else:
            message = "Corps de requête JSON invalide."
        return JSONResponse({"erreur": message}, status_code=400)

    def svc_of(request: Request) -> TradingService:
        return request.app.state.service

    @app.get("/prix")
    async def prix(request: Request) -> dict:
        # Lecture directe du cache : les cotations périmées sont renvoyées, marquées "perime".
        feed: CachedFeed = request.app.state.feed
        quotes = [feed.hub.quotes[p] for p in feed.hub.pairs if p in feed.hub.quotes]
        if not quotes:
            raise FeedUnavailable("Aucune cotation reçue pour l'instant (flux en connexion).")
        return {"cotations": [quote_json(q, feed) for q in quotes]}

    @app.post("/ordres/achat")
    async def achat(body: BuyBody, request: Request) -> dict:
        return trade_json(await svc_of(request).buy(body.paire, body.montant))

    @app.post("/ordres/vente")
    async def vente(body: SellBody, request: Request) -> dict:
        return trade_json(await svc_of(request).sell(body.paire, body.quantite))

    @app.get("/portefeuille")
    async def portefeuille(request: Request) -> dict:
        v = await svc_of(request).valuation()
        return {
            "compte": account_json(v.account),
            "positions": [
                {
                    "paire": pv.position.pair,
                    "quantite": dec(pv.position.quantity),
                    "prix_moyen": dec(pv.position.avg_price),
                    "bid": dec(pv.bid),
                    "valeur": dec(pv.value),
                    "pnl_latent": dec(pv.unrealized_pnl),
                    "pnl_latent_pct": dec(pv.unrealized_pct),
                }
                for pv in v.positions
            ],
            "solde": dec(v.cash),
            "valeur_positions": dec(v.positions_value),
            "valeur_totale": dec(v.total_value),
            "pnl_realise": dec(v.realized_pnl),
            "pnl_latent": dec(v.unrealized_pnl),
            "frais_payes": dec(v.fees_paid),
            "performance_pct": dec(v.performance_pct),
        }

    @app.get("/historique")
    async def historique(request: Request) -> dict:
        account, orders = svc_of(request).history()
        return {"compte": account_json(account), "ordres": [order_json(o) for o in orders]}

    @app.post("/reset")
    async def reset(request: Request, body: ResetBody | None = None) -> dict:
        account = svc_of(request).reset((body or ResetBody()).capital)
        return {"compte": account_json(account)}

    @app.get("/info")
    async def info(request: Request) -> dict:
        svc = svc_of(request)
        h: PriceHub = request.app.state.hub
        return {
            "base": svc.storage.path,
            "compte_actif": account_json(svc.account()),
            "comptes_archives": len(svc.accounts()) - 1,
            "flux": {
                "source": h.source or h.feed.name,
                "connecte": h.connected,
                "derniere_cotation": iso(h.last_quote_at) if h.last_quote_at else None,
                "derniere_erreur": h.last_error,
                "reconnexions": h.reconnects,
            },
            "flux_volume": {
                "actif": h.feed.has_trades,
                "connecte": h.trade_status.connected,
                "derniere_transaction": iso(h.last_trade_at) if h.last_trade_at else None,
                "derniere_erreur": h.trade_status.last_error,
                "reconnexions": h.trade_status.reconnects,
                "source": h.trade_source,
                "repli": h.trade_fallback,
            },
            "historique": {
                "etat": h.history_overall(),
                "derniere_synchro": opt_iso(h.last_sync),
                "derniere_erreur": h.history_error,
            },
            "horloge": {
                "ecart_ms": round(h.clock_offset.offset * 1000) if h.clock_offset else None,
                "aller_retour_ms": round(h.clock_offset.round_trip * 1000) if h.clock_offset else None,
                "mesure_le": iso(h.clock_offset.measured_at) if h.clock_offset else None,
                "erreur": h.clock_error,
            },
        }

    def hub_of(request: Request) -> PriceHub:
        return request.app.state.hub

    def session_vwap(h: PriceHub, pair: str, now: datetime, inclure_courante: bool) -> market.VwapResult:
        candles = h.market.candles(pair, "1m")
        return market.vwap_session(candles if inclure_courante else market.closed(candles, now), now)

    @app.get("/bougies/{paire}")
    async def bougies(
        paire: str,
        request: Request,
        intervalle: market.IntervalName = "1m",
        limite: int = Query(100, ge=1, le=market.LIMIT_MAX),
    ) -> dict:
        h, pair = hub_of(request), normalize_pair(paire)
        now = h.clock()
        candles = h.market.candles(pair, intervalle, limite)
        volume = session_vwap(h, pair, now, False).volume
        return {
            "paire": pair,
            "intervalle": intervalle,
            "bougies": [candle_json(c, now) for c in candles],
            "qualite": quality_json(h.quality(pair, intervalle, volume)),
        }

    @app.get("/indicateurs/{paire}")
    async def indicateurs(
        paire: str,
        request: Request,
        intervalle: market.IntervalName = "1m",
        fenetre: int = Query(market.LIMIT_MAX, ge=1, le=market.LIMIT_MAX),
        sma: int = Query(20, ge=1, le=market.LIMIT_MAX),
        ema: int = Query(20, ge=1, le=market.LIMIT_MAX),
        rsi: int = Query(14, ge=1, le=market.LIMIT_MAX - 1),
        inclure_courante: bool = False,
        vwap: market.VwapAnchor = "jour_utc",
    ) -> dict:
        h, pair = hub_of(request), normalize_pair(paire)
        now = h.clock()
        candles = h.market.candles(pair, intervalle)
        if not inclure_courante:
            candles = market.closed(candles, now)
        candles = candles[-fenetre:]
        closes = [c.close for c in candles]
        vw = market.vwap_window(candles) if vwap == "fenetre" else session_vwap(h, pair, now, inclure_courante)
        last = candles[-1] if candles else None
        return {
            "paire": pair,
            "intervalle": intervalle,
            "inclure_courante": inclure_courante,
            "nb_bougies": len(candles),
            "debut": opt_iso(candles[0].open_time if candles else None),
            "derniere_bougie": None
            if last is None
            else {"ouverture_le": iso(last.open_time), "complete": last.complete, "cloturee": last.is_closed(now)},
            "dernier_prix": opt(closes[-1]) if closes else None,
            "sma": {"periode": sma, "valeur": opt(market.sma(closes, sma))},
            "ema": {"periode": ema, "valeur": opt(market.ema(closes, ema))},
            "rsi": {"periode": rsi, "valeur": opt(market.rsi(closes, rsi))},
            "vwap": vwap_json(vw),
            "variation_pct": opt(market.variation_pct(candles)),
            "volume": dec(sum((c.volume for c in candles), Decimal(0))),
            "qualite": quality_json(h.quality(pair, intervalle, vw.volume)),
        }

    @app.websocket("/ws/prix")
    async def ws_prix(websocket: WebSocket) -> None:
        await websocket.accept()
        feed: CachedFeed = websocket.app.state.feed

        # Un envoi vers un client parti ne lève pas toujours d'erreur : une tâche écoute la
        # fermeture, et la première des deux qui se termine arrête l'autre.
        async with anyio.create_task_group() as tg:

            async def send_quotes() -> None:
                try:
                    async with aclosing(websocket.app.state.service.stream_prices()) as stream:
                        async for quote in stream:
                            await websocket.send_json(quote_json(quote, feed))
                except (WebSocketDisconnect, OSError):  # OSError : client parti pendant l'envoi
                    pass
                tg.cancel_scope.cancel()

            async def wait_disconnect() -> None:
                while (await websocket.receive())["type"] != "websocket.disconnect":
                    pass
                tg.cancel_scope.cancel()

            tg.start_soon(send_quotes)
            tg.start_soon(wait_disconnect)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
