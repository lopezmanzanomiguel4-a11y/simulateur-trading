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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from simtrade.config import db_path
from simtrade.engine import DEFAULT_CAPITAL
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


def quote_json(quote: Quote, age: float) -> dict:
    return {
        "paire": quote.pair,
        "bid": dec(quote.bid),
        "ask": dec(quote.ask),
        "spread": dec(quote.spread),
        "source": quote.source,
        "received_at": iso(quote.received_at),
        "age_secondes": round(age, 3),
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

    app = FastAPI(title="Simulateur de paper trading", lifespan=lifespan)

    @app.exception_handler(SimTradeError)
    async def simtrade_error(request: Request, exc: SimTradeError) -> JSONResponse:
        status = next((code for cls, code in STATUS.items() if isinstance(exc, cls)), 500)
        return JSONResponse({"erreur": str(exc)}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [str(e["loc"][-1]) for e in exc.errors() if e.get("loc")]
        if fields and fields[0] != "body":
            message = f"Champ '{fields[0]}' manquant ou invalide (chaîne attendue)."
        else:
            message = "Corps de requête JSON invalide."
        return JSONResponse({"erreur": message}, status_code=400)

    def svc_of(request: Request) -> TradingService:
        return request.app.state.service

    @app.get("/prix")
    async def prix(request: Request) -> dict:
        feed: CachedFeed = request.app.state.feed
        quotes = await svc_of(request).prices()
        return {"cotations": [quote_json(q, feed.age(q)) for q in quotes.values()]}

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
        }

    @app.websocket("/ws/prix")
    async def ws_prix(websocket: WebSocket) -> None:
        await websocket.accept()
        feed: CachedFeed = websocket.app.state.feed

        async def send_quotes() -> None:
            async with aclosing(websocket.app.state.service.stream_prices()) as stream:
                async for quote in stream:
                    await websocket.send_json(quote_json(quote, feed.age(quote)))

        async def wait_disconnect() -> None:
            # Un envoi vers un client parti ne lève pas toujours d'erreur : on écoute la fermeture.
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(send_quotes()), asyncio.create_task(wait_disconnect())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, WebSocketDisconnect):
                raise result

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
