"""Interface en ligne de commande : python -m simtrade <commande>."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import datetime

from simtrade import fmt
from simtrade.config import DB_ENV_VAR, db_path
from simtrade.engine import DEFAULT_CAPITAL, FEE_RATE, validate_capital
from simtrade.errors import SimTradeError
from simtrade.feeds import default_feed
from simtrade.models import PAIRS, Quote, Side, base_asset
from simtrade.service import TradeResult, TradingService
from simtrade.storage import Storage

FEE_LABEL = f"Frais ({fmt.num(FEE_RATE * 100, 1)} %)"


def build_service() -> TradingService:
    return TradingService(Storage(db_path()), default_feed())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simtrade", description="Simulateur de paper trading crypto (USDT).")
    sub = parser.add_subparsers(dest="command", required=True, metavar="commande")

    p = sub.add_parser("prix", help="prix bid/ask en direct des paires suivies")
    p.add_argument("--once", action="store_true", help="affiche une seule cotation puis quitte")

    p = sub.add_parser("acheter", help="achat au marché (au prix ask)")
    p.add_argument("paire", help="BTCUSDT, ETHUSDT, SOLUSDT (ou btc, eth, sol)")
    p.add_argument("montant", help="montant total en USDT, frais inclus")

    p = sub.add_parser("vendre", help="vente au marché (au prix bid)")
    p.add_argument("paire")
    p.add_argument("quantite", metavar="quantité", help='quantité de crypto, ou "tout"')

    sub.add_parser("portefeuille", help="positions, valeur, P&L et performance")
    sub.add_parser("historique", help="ordres du compte actif, avec frais")

    p = sub.add_parser("reset", help="remet le compte à zéro avec un nouveau capital")
    p.add_argument("montant", nargs="?", default=str(DEFAULT_CAPITAL), help="capital en USDT (défaut : 10000)")
    p.add_argument("--yes", "-y", action="store_true", help="ne demande pas de confirmation")

    sub.add_parser("info", help="chemin de la base et comptes enregistrés")
    return parser


def main(argv: Sequence[str] | None = None, service: TradingService | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = service or build_service()
    try:
        handler = COMMANDS[args.command]
        result = handler(service, args)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result
    except SimTradeError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nArrêt.")
        return 0
    finally:
        service.storage.close()


# --- Affichage ---


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], left: int = 1) -> str:
    """Tableau texte ; les `left` premières colonnes sont alignées à gauche, les autres à droite."""
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths)))

    return "\n".join([line(headers), "  ".join("-" * w for w in widths), *(line(r) for r in rows)])


def local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d/%m/%Y %H:%M:%S")


def warn_fallback(service: TradingService) -> None:
    for error in getattr(service.feed, "errors", []):
        print(f"(source écartée, {error})")


def print_trade(service: TradingService, result: TradeResult) -> None:
    o = result.order
    asset = base_asset(o.pair)
    warn_fallback(service)
    title = "Achat exécuté" if o.side is Side.BUY else "Vente exécutée"
    price_label = "Prix (ask)" if o.side is Side.BUY else "Prix (bid)"
    rows = [
        ("Paire", o.pair),
        ("Quantité", f"{fmt.qty(o.quantity)} {asset}"),
        (price_label, fmt.usdt(o.price)),
        ("Montant hors frais", fmt.usdt(o.gross)),
        (FEE_LABEL, fmt.usdt(o.fee, 4)),
    ]
    if o.side is Side.BUY:
        rows.append(("Total débité", fmt.usdt(-o.cash_flow)))
    else:
        rows.append(("Net crédité", fmt.usdt(o.cash_flow)))
        rows.append(("P&L réalisé (net)", fmt.usdt(o.realized_pnl, sign=True)))
    rows.append(("Solde USDT", fmt.usdt(result.cash)))
    width = max(len(k) for k, _ in rows)
    print(f"{title} : ordre n°{o.id}, source {o.source}, {local(o.created_at)}")
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")


# --- Commandes ---


async def cmd_prix(service: TradingService, args: argparse.Namespace) -> int:
    if args.once:
        quotes = await service.prices()
        warn_fallback(service)
        print_quotes(quotes)
        return 0

    print("Prix en direct (bid = prix de vente, ask = prix d'achat). Ctrl+C pour arrêter.")
    latest: dict[str, Quote] = {}
    last_print = 0.0
    async for quote in service.stream_prices():
        if not latest:
            warn_fallback(service)
            print(f"Source : {quote.source}")
        latest[quote.pair] = quote
        now = time.monotonic()
        if len(latest) == len(PAIRS) and now - last_print >= 1:
            last_print = now
            parts = [f"{p} {fmt.price(latest[p].bid)} / {fmt.price(latest[p].ask)}" for p in PAIRS]
            print(f"{datetime.now():%H:%M:%S}  " + "  |  ".join(parts), flush=True)
    return 0


def print_quotes(quotes: dict[str, Quote]) -> None:
    source = next(iter(quotes.values())).source
    print(f"Source : {source}, {datetime.now():%d/%m/%Y %H:%M:%S}")
    rows = [
        (
            p,
            fmt.price(quotes[p].bid),
            fmt.price(quotes[p].ask),
            fmt.num(quotes[p].spread, 4),
            fmt.num(quotes[p].spread_pct * 100, 2),
        )
        for p in PAIRS
        if p in quotes
    ]
    print(table(["Paire", "Bid (vente)", "Ask (achat)", "Spread", "Spread (pb)"], rows))
    print("pb = points de base (1 pb = 0,01 %)")


async def cmd_acheter(service: TradingService, args: argparse.Namespace) -> int:
    print_trade(service, await service.buy(args.paire, args.montant))
    return 0


async def cmd_vendre(service: TradingService, args: argparse.Namespace) -> int:
    print_trade(service, await service.sell(args.paire, args.quantite))
    return 0


async def cmd_portefeuille(service: TradingService, args: argparse.Namespace) -> int:
    v = await service.valuation()
    warn_fallback(service)
    a = v.account
    print(f"Compte n°{a.id}, ouvert le {local(a.created_at)} avec {fmt.usdt(a.initial_capital)}")
    print()
    if v.positions:
        rows = [
            (
                pv.position.pair,
                fmt.qty(pv.position.quantity),
                fmt.price(pv.position.avg_price),
                fmt.price(pv.bid),
                fmt.num(pv.value),
                fmt.num(pv.unrealized_pnl, sign=True),
                fmt.pct(pv.unrealized_pct),
            )
            for pv in v.positions
        ]
        headers = ["Paire", "Quantité", "Prix moyen*", "Bid", "Valeur", "P&L latent", "%"]
        print(table(headers, rows))
        print("* prix de revient moyen, frais d'achat inclus. Valeur et P&L latent au bid, hors frais de sortie.")
    else:
        print("Aucune position ouverte.")
    print()
    summary = [
        ("Solde USDT", fmt.usdt(v.cash)),
        ("Valeur des positions", fmt.usdt(v.positions_value)),
        ("Valeur totale", fmt.usdt(v.total_value)),
        ("P&L réalisé (net)", fmt.usdt(v.realized_pnl, sign=True)),
        ("P&L latent", fmt.usdt(v.unrealized_pnl, sign=True)),
        ("Frais payés", fmt.usdt(v.fees_paid, 4)),
        ("Performance totale", fmt.pct(v.performance_pct)),
    ]
    width = max(len(k) for k, _ in summary)
    for key, value in summary:
        print(f"{key.ljust(width)}  {value.rjust(22)}")
    return 0


def cmd_historique(service: TradingService, args: argparse.Namespace) -> int:
    account, orders = service.history()
    print(f"Compte n°{account.id}, ouvert le {local(account.created_at)} avec {fmt.usdt(account.initial_capital)}")
    if not orders:
        print("Aucun ordre sur ce compte.")
        return 0
    rows = [
        (
            str(o.id),
            local(o.created_at),
            o.side.value,
            o.pair,
            fmt.qty(o.quantity),
            fmt.price(o.price),
            fmt.num(o.gross),
            fmt.num(o.fee, 4),
            fmt.num(o.cash_flow, sign=True),
            fmt.num(o.realized_pnl, sign=True) if o.side is Side.SELL else "",
            o.source,
        )
        for o in orders
    ]
    headers = ["N°", "Date", "Type", "Paire", "Quantité", "Prix", "Montant", "Frais", "Flux USDT", "P&L réalisé", "Source"]
    print(table(headers, rows, left=4))
    total_fees = sum(o.fee for o in orders)
    print(f"\n{len(orders)} ordre(s), frais totaux : {fmt.usdt(total_fees, 4)}")
    return 0


def cmd_reset(service: TradingService, args: argparse.Namespace) -> int:
    capital = validate_capital(args.montant)  # refuse avant de demander confirmation
    if not args.yes:
        print(f"Remettre le compte à zéro avec {fmt.usdt(capital)} ?")
        print("Les positions ouvertes seront abandonnées (pas vendues). L'ancien compte reste archivé dans la base.")
        try:
            answer = input("Confirmer ? (o/N) ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("o", "oui", "y", "yes"):
            print("Annulé, rien n'a changé.")
            return 0
    account = service.reset(capital)
    print(f"Compte n°{account.id} ouvert le {local(account.created_at)} avec {fmt.usdt(account.initial_capital)}.")
    return 0


def cmd_info(service: TradingService, args: argparse.Namespace) -> int:
    active = service.account()
    accounts = service.accounts()
    print(f"Base SQLite : {service.storage.path}")
    print(f"(modifiable avec la variable d'environnement {DB_ENV_VAR})")
    print(f"Compte actif : n°{active.id}, {fmt.usdt(active.initial_capital)} au {local(active.created_at)}")
    print(f"Comptes archivés : {len(accounts) - 1}")
    return 0


COMMANDS = {
    "prix": cmd_prix,
    "acheter": cmd_acheter,
    "vendre": cmd_vendre,
    "portefeuille": cmd_portefeuille,
    "historique": cmd_historique,
    "reset": cmd_reset,
    "info": cmd_info,
}
