"""Interface en ligne de commande : python -m simtrade <commande>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from simtrade import fmt, strategies
from simtrade.config import DB_ENV_VAR, db_path
from simtrade.engine import DEFAULT_CAPITAL, FEE_RATE, validate_capital
from simtrade.errors import SimTradeError
from simtrade.feeds import default_feed
from simtrade.models import (
    AUTO,
    LIMIT,
    MANUAL,
    MARKET,
    PAIRS,
    STOP,
    TARGET,
    Account,
    Order,
    PendingOrder,
    Quote,
    Side,
    base_asset,
)
from simtrade.service import TradeResult, TradingService
from simtrade.storage import Storage

FEE_LABEL = f"Frais ({fmt.num(FEE_RATE * 100, 1)} %)"


def build_service() -> TradingService:
    return TradingService(Storage(db_path()), default_feed())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simtrade", description="Simulateur de paper trading crypto (USDT).")
    sub = parser.add_subparsers(dest="command", required=True, metavar="commande")
    acc = argparse.ArgumentParser(add_help=False)
    acc.add_argument("--compte", "-c", help="nom ou numéro du compte (défaut : le compte courant)")

    p = sub.add_parser("prix", help="prix bid/ask en direct des paires suivies")
    p.add_argument("--once", action="store_true", help="affiche une seule cotation puis quitte")

    p = sub.add_parser("acheter", parents=[acc], help="achat au marché (au prix ask)")
    p.add_argument("paire", help="BTCUSDT, ETHUSDT, SOLUSDT (ou btc, eth, sol)")
    p.add_argument("montant", help="montant total en USDT, frais inclus")

    p = sub.add_parser("vendre", parents=[acc], help="vente au marché (au prix bid)")
    p.add_argument("paire")
    p.add_argument("quantite", metavar="quantité", help='quantité de crypto, ou "tout"')

    p = sub.add_parser("entrer", parents=[acc], help="achat au marché + stop de protection (obligatoire en compte auto)")
    p.add_argument("paire")
    p.add_argument("montant", help="montant total en USDT, frais inclus")
    p.add_argument("--stop", required=True, help="prix du stop-loss, sous le bid")
    p.add_argument("--objectif", help="take-profit lié au stop (facultatif)")
    p.add_argument("--suiveur", action="store_true", help="stop suiveur : monte avec le plus haut bid, distance = bid - stop")

    p = sub.add_parser("limite", parents=[acc], help="ordre limite (achat à l'ask <= prix, vente au bid >= prix)")
    p.add_argument("paire")
    p.add_argument("sens", choices=[s.value for s in Side])
    p.add_argument("prix")
    p.add_argument("taille", help='achat : montant USDT frais inclus ; vente : quantité ou "tout"')
    p.add_argument("--stop", help="achat : stop de protection posé à l'exécution (obligatoire en compte auto)")
    p.add_argument("--objectif", help="achat : objectif lié au stop, posé à l'exécution")

    p = sub.add_parser("deplacer-stop", parents=[acc], help="déplace un stop (compte auto : seulement vers le haut)")
    p.add_argument("numero", metavar="n°")
    p.add_argument("prix")

    p = sub.add_parser("risque", parents=[acc], help="limites du garde-fou du compte (affiche ou modifie)")
    p.add_argument(
        "changements",
        nargs="*",
        metavar="clé=valeur",
        help="taille=20 (%%), perte=2 (%%), trades=6, positions=2, delai=15m",
    )

    p = sub.add_parser("refus", parents=[acc], help="journal des refus du garde-fou (tous les comptes sans --compte)")
    p.add_argument("--limite", type=int, default=20, help="nombre de refus affichés (défaut 20)")

    sub.add_parser("strategies", help="modèles de stratégie et bornes de leurs paramètres")

    p = sub.add_parser("strategie", parents=[acc], help="stratégie active du compte (affiche, active, désactive)")
    actions = p.add_subparsers(dest="action", metavar="action")
    q = actions.add_parser("activer", parents=[acc], help="active un modèle (remplace la stratégie active)")
    q.add_argument("modele", metavar="modèle", nargs="?", help="nom du modèle (voir : strategies)")
    q.add_argument("changements", nargs="*", metavar="clé=valeur", help="ex. stop.multiple=2 rsi_extreme.seuil=35")
    q.add_argument("--fichier", help="définition complète en JSON (à la place d'un modèle)")
    actions.add_parser("desactiver", parents=[acc], help="désactive la stratégie (la position garde son stop)")

    p = sub.add_parser("signaux", parents=[acc], help="journal des signaux des stratégies (tous les comptes sans --compte)")
    p.add_argument("--limite", type=int, default=20, help="nombre de signaux affichés (défaut 20)")
    p.add_argument("--decision", choices=["ordre", "refuse", "ignore"], help="filtre sur la décision")

    for name, label in (("stop", "stop-loss : vente au bid quand bid <= prix"), ("objectif", "take-profit : vente au bid quand bid >= prix")):
        p = sub.add_parser(name, parents=[acc], help=label)
        p.add_argument("paire")
        p.add_argument("prix")
        p.add_argument("quantite", metavar="quantité", nargs="?", default="tout", help='défaut : "tout"')

    p = sub.add_parser("stop-objectif", parents=[acc], help="stop + objectif liés : le premier exécuté annule l'autre")
    p.add_argument("paire")
    p.add_argument("stop")
    p.add_argument("objectif")
    p.add_argument("quantite", metavar="quantité", nargs="?", default="tout", help='défaut : "tout"')

    p = sub.add_parser("ordres", parents=[acc], help="ordres en attente du compte")
    p.add_argument("--tous", action="store_true", help="inclut les ordres exécutés, annulés et rejetés")

    p = sub.add_parser("annuler", parents=[acc], help="annule un ordre en attente (et son ordre lié)")
    p.add_argument("numero", metavar="n°")

    sub.add_parser("portefeuille", parents=[acc], help="positions, valeur, P&L et performance")
    sub.add_parser("historique", parents=[acc], help="ordres exécutés du compte, avec frais")

    p = sub.add_parser("reset", parents=[acc], help="remet le compte à zéro avec un nouveau capital")
    p.add_argument("montant", nargs="?", default=str(DEFAULT_CAPITAL), help="capital en USDT (défaut : 10000)")
    p.add_argument("--yes", "-y", action="store_true", help="ne demande pas de confirmation")

    p = sub.add_parser("comptes", help="liste, crée, choisit ou archive des comptes")
    actions = p.add_subparsers(dest="action", metavar="action")
    q = actions.add_parser("creer", help="ouvre un compte en parallèle des autres")
    q.add_argument("nom")
    q.add_argument("capital", nargs="?", default=str(DEFAULT_CAPITAL))
    q.add_argument("--auto", action="store_true", help="compte automatique (soumis aux règles de session)")
    q.add_argument("--swing", action="store_true", help="compte auto swing : positions conservées d'un jour à l'autre")
    q.add_argument("--frais", default="0.1", help="frais en %% par ordre : 0.1 (défaut) ou 0.075 (BNB)")
    q = actions.add_parser("choisir", help="devient le compte courant")
    q.add_argument("nom")
    q = actions.add_parser("archiver", help="archive un compte (ses ordres en attente sont annulés)")
    q.add_argument("nom")

    p = sub.add_parser("session", help="horaires de session (UTC) des comptes automatiques")
    p.add_argument("derniere_entree", metavar="dernière_entrée", nargs="?", help="HH:MM (défaut 23:00)")
    p.add_argument("cloture", metavar="clôture", nargs="?", help="HH:MM (défaut 23:55)")

    p = sub.add_parser("journal", parents=[acc], help="signaux, refus, ordres et trades du compte, dans l'ordre")
    p.add_argument("--limite", type=int, default=50, help="nombre de lignes (les plus récentes, défaut 50)")

    p = sub.add_parser("stats", parents=[acc], help="statistiques du compte (ou d'une stratégie)")
    p.add_argument("--strategie", help="nom de la stratégie (trades de cette stratégie seulement)")
    p.add_argument("--depuis", help="début (AAAA-MM-JJ ou AAAA-MM-JJTHH:MM, UTC)")
    p.add_argument("--jusqu-a", dest="jusqu_a", help="fin (exclue)")

    p = sub.add_parser("marche", help="base des bougies 1m et 1h (marche.db) : téléchargement et état")
    actions = p.add_subparsers(dest="action", required=True, metavar="action")
    q = actions.add_parser("telecharger", help="télécharge les klines 1m Binance (reprend là où on s'est arrêté)")
    q.add_argument("--paires", nargs="+", default=list(PAIRS), help="paires (défaut : les trois)")
    q.add_argument("--jours", type=int, default=183, help="nombre de jours jusqu'à aujourd'hui 00:00 UTC (défaut 183)")
    q.add_argument("--depuis", help="début (AAAA-MM-JJ, UTC), remplace --jours")
    q.add_argument("--jusqu-a", dest="jusqu_a", help="fin exclue (défaut : aujourd'hui 00:00 UTC)")
    q.add_argument("--intervalle", choices=("1m", "1h"), default="1m", help="1m (défaut) ou 1h (swing)")
    q.add_argument("--swing", action="store_true", help="les 10 paires du swing (remplace --paires)")
    q = actions.add_parser("etat", help="lignes, période, trous et taille de marche.db")
    q.add_argument("--paires", nargs="+", default=list(PAIRS))
    q.add_argument("--intervalle", choices=("1m", "1h"), default="1m")
    q.add_argument("--swing", action="store_true", help="les 10 paires du swing (remplace --paires)")

    p = sub.add_parser("backtest", help="rejoue un modèle sur les bougies de marche.db (frais et spread compris)")
    p.add_argument("modele", metavar="modèle", help="nom du modèle (voir : strategies)")
    p.add_argument("surcharges", nargs="*", metavar="clé=valeur", help="ex. ecart_vwap.seuil_pct=0.8")
    p.add_argument("--paire", default="BTCUSDT")
    p.add_argument("--depuis", required=True, help="début (AAAA-MM-JJ, UTC) ; 2 jours avant sont lus pour les fenêtres")
    p.add_argument("--jusqu-a", dest="jusqu_a", required=True, help="fin exclue (AAAA-MM-JJ, UTC)")
    p.add_argument("--spread", default="0.02", help="spread simulé en %% (défaut 0.02)")

    p = sub.add_parser("catalogue", help="catalogue des stratégies backtestées (affiche, ou construit)")
    p.add_argument("action", nargs="?", choices=("afficher", "construire"), default="afficher")
    p.add_argument("--processus", type=int, help="processus de calcul (défaut : cœurs - 1, au plus 11)")
    p.add_argument("--sortie", help="dossier de sortie (catalogue.json et backtests/) ; défaut : le dépôt")
    p.add_argument("--etape", choices=("7", "7b"), default="7b",
                   help="modèles et périodes construits (défaut 7b) ; les résultats des autres étapes sont gardés")

    sub.add_parser("info", help="chemin de la base et comptes enregistrés")
    sub.add_parser("api", help="lance l'API HTTP sur http://127.0.0.1:8000 (exécute les ordres en attente)")
    return parser


def main(argv: Sequence[str] | None = None, service: TradingService | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "api":
        return cmd_api()  # l'API ouvre sa propre base dans son lifespan
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
        service.close_market_store()
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


def account_title(a: Account) -> str:
    return f"Compte n°{a.id} ({a.name}, {a.mode}), ouvert le {local(a.created_at)} avec {fmt.usdt(a.initial_capital)}"


def side_label(o: Order) -> str:
    return o.side.value if o.kind == MARKET else f"{o.side.value} ({o.kind})"


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
    print_trade(service, await service.buy(args.paire, args.montant, compte=args.compte))
    return 0


async def cmd_vendre(service: TradingService, args: argparse.Namespace) -> int:
    print_trade(service, await service.sell(args.paire, args.quantite, compte=args.compte))
    return 0


# --- Ordres en attente ---

NO_WATCH = "Exécution automatique uniquement pendant que l'API tourne (python -m simtrade api)."


def print_pending(orders: Sequence[PendingOrder]) -> None:
    rows = [
        (
            str(o.id),
            local(o.created_at),
            o.kind,
            o.side.value,
            o.pair,
            fmt.price(o.trigger_price),
            fmt.usdt(o.amount) if o.amount is not None else ("tout" if o.quantity is None else fmt.qty(o.quantity)),
            str(o.group_id) if o.group_id is not None else "",
            o.status if o.reason is None else f"{o.status} ({o.reason})",
            "protection" if o.protection else (f"stop {fmt.price(o.stop_price)}" if o.stop_price is not None else ""),
        )
        for o in orders
    ]
    headers = ["N°", "Posé le", "Type", "Sens", "Paire", "Prix", "Taille", "Lié à", "Statut", "Note"]
    print(table(headers, rows, left=5))


async def cmd_entrer(service: TradingService, args: argparse.Namespace) -> int:
    result = await service.buy(
        args.paire, args.montant, compte=args.compte, stop=args.stop, target=args.objectif, trailing=args.suiveur
    )
    print_trade(service, result)
    protection = service.storage.protection_orders(result.order.account_id, result.order.pair)
    print("Protection de la position :")
    print_pending(protection)
    print(NO_WATCH)
    return 0


async def cmd_limite(service: TradingService, args: argparse.Namespace) -> int:
    order = await service.place_order(
        args.paire, LIMIT, args.sens, args.prix, args.taille, compte=args.compte, stop=args.stop, target=args.objectif
    )
    print("Ordre limite posé :")
    print_pending([order])
    if order.stop_price is not None:
        target = f", objectif {fmt.price(order.target_price)}" if order.target_price is not None else ""
        print(f"À l'exécution : stop de protection à {fmt.price(order.stop_price)}{target}.")
    print(NO_WATCH)
    return 0


async def cmd_deplacer_stop(service: TradingService, args: argparse.Namespace) -> int:
    order = await service.move_stop(args.numero, args.prix, compte=args.compte)
    print(f"Stop n°{order.id} ({order.pair}) déplacé à {fmt.price(order.trigger_price)}.")
    return 0


def cmd_risque(service: TradingService, args: argparse.Namespace) -> int:
    if args.changements:
        changes = {}
        for item in args.changements:
            key, sep, value = item.partition("=")
            if not sep:
                print(f"Erreur : '{item}' n'est pas de la forme clé=valeur.", file=sys.stderr)
                return 1
            changes[key.strip().lower()] = value
        account, limits = service.set_risk_limits(changes, compte=args.compte)
        print("Limites enregistrées.")
    else:
        account, limits = service.risk_limits(compte=args.compte)
    gap = int(limits.min_entry_gap.total_seconds())
    rows = [
        ("Stop-loss", "obligatoire pour toute entrée"),
        ("Taille max (taille)", f"{fmt.num(limits.max_position_pct)} % de la valeur du compte par paire"),
        ("Perte max (perte)", f"{fmt.num(limits.max_session_loss_pct)} % par session (réalisé + latent)"),
        ("Entrées max (trades)", f"{limits.max_trades} par session"),
        ("Positions max (positions)", f"{limits.max_positions} paires exposées"),
        ("Délai entre entrées (delai)", f"{gap // 60} min {gap % 60:02d} s sur une même paire"),
    ]
    print(f"Garde-fou du compte '{account.name}' (n°{account.id}, {account.mode})")
    if account.mode != AUTO:
        print("(compte manuel : ces limites ne s'appliquent qu'aux comptes automatiques)")
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")
    return 0


def cmd_refus(service: TradingService, args: argparse.Namespace) -> int:
    records = service.refusals(compte=args.compte, limit=args.limite)
    if not records:
        print("Aucun refus du garde-fou.")
        return 0
    rows = [
        (
            str(r.id),
            local(r.created_at),
            str(r.account_id),
            r.moment,
            r.pair,
            fmt.usdt(r.amount) if r.amount is not None else "",
            ", ".join(r.rules),
        )
        for r in records
    ]
    print(table(["N°", "Date", "Compte", "Moment", "Paire", "Montant", "Règles"], rows, left=5))
    print(f"\nDernier refus : {records[0].message}")
    return 0


def parse_changes(items: Sequence[str]) -> dict[str, str] | None:
    changes = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            print(f"Erreur : '{item}' n'est pas de la forme clé=valeur.", file=sys.stderr)
            return None
        changes[key.strip()] = value.strip()
    return changes


def cmd_strategies(service: TradingService, args: argparse.Namespace) -> int:
    for name, raw in strategies.models().items():
        s = strategies.validate(raw)
        entry = " ET ".join(b.name for b in s.entree)
        exits = " OU ".join(b.name for b in s.sortie) or "aucune"
        print(f"{name} : {s.paire} {s.intervalle}, entrée {entry} ; sortie {exits} ; "
              f"stop {s.stop.name}, objectif {s.objectif.name}, {fmt.num(s.taille_pct)} % du capital, "
              f"{s.debut:%H:%M}-{s.fin:%H:%M} UTC")
    print("\nParamètres (type, bornes, défaut) :")
    print(json.dumps(strategies.schema(), ensure_ascii=False, indent=1))
    return 0


def print_strategy(s: strategies.Strategy) -> None:
    print(json.dumps(s.to_json(), ensure_ascii=False, indent=1))


def cmd_strategie(service: TradingService, args: argparse.Namespace) -> int:
    if args.action == "activer":
        changes = parse_changes(args.changements)
        if changes is None:
            return 1
        definition = None
        if args.fichier:
            definition = json.loads(Path(args.fichier).read_text(encoding="utf-8"))
        account, _, strategy = service.activate_strategy(
            compte=args.compte, modele=args.modele, parametres=changes, definition=definition
        )
        print(f"Stratégie '{strategy.nom}' activée sur le compte '{account.name}' (exécutée quand l'API tourne) :")
        print_strategy(strategy)
        return 0
    if args.action == "desactiver":
        account, record = service.deactivate_strategy(compte=args.compte)
        if record is None:
            print(f"Aucune stratégie active sur le compte '{account.name}'.")
        else:
            print(f"Stratégie '{record.name}' désactivée sur le compte '{account.name}'. "
                  "Une position ouverte garde son stop, son objectif et la clôture de session.")
        return 0
    account, record = service.active_strategy(compte=args.compte)
    if record is None:
        print(f"Aucune stratégie active sur le compte '{account.name}'.")
        return 0
    print(f"Compte '{account.name}' : stratégie '{record.name}' active depuis le {local(record.activated_at)}")
    print_strategy(strategies.loads(record.definition))
    return 0


def cmd_signaux(service: TradingService, args: argparse.Namespace) -> int:
    records = service.signals(compte=args.compte, decision=args.decision, limit=args.limite)
    if not records:
        print("Aucun signal.")
        return 0
    rows = [
        (
            str(r.id),
            local(r.created_at),
            str(r.account_id),
            r.pair,
            r.interval,
            local(r.candle_open_time),
            r.kind,
            r.decision,
            str(r.order_id or ""),
            ", ".join(r.reasons),
        )
        for r in records
    ]
    print(table(["N°", "Date", "Compte", "Paire", "Int.", "Bougie", "Type", "Décision", "Ordre", "Raisons"], rows, left=10))
    return 0


async def cmd_stop(service: TradingService, args: argparse.Namespace) -> int:
    kind = STOP if args.command == "stop" else TARGET
    order = await service.place_order(args.paire, kind, Side.SELL, args.prix, args.quantite, compte=args.compte)
    print(f"Ordre {kind} posé :")
    print_pending([order])
    print(NO_WATCH)
    return 0


async def cmd_stop_objectif(service: TradingService, args: argparse.Namespace) -> int:
    orders = await service.place_bracket(args.paire, args.stop, args.objectif, args.quantite, compte=args.compte)
    print("Stop et objectif liés posés (le premier exécuté annule l'autre) :")
    print_pending(orders)
    print(NO_WATCH)
    return 0


def cmd_ordres(service: TradingService, args: argparse.Namespace) -> int:
    account = service.account(args.compte)
    orders = service.pending_orders(compte=args.compte, all_statuses=args.tous)
    print(account_title(account))
    if not orders:
        print("Aucun ordre en attente." if not args.tous else "Aucun ordre en attente, ni passé.")
        return 0
    print_pending(orders)
    return 0


def cmd_annuler(service: TradingService, args: argparse.Namespace) -> int:
    cancelled = service.cancel_order(args.numero, compte=args.compte)
    print("Annulé : " + ", ".join(f"ordre n°{o.id} ({o.kind} {o.pair})" for o in cancelled) + ".")
    return 0


async def cmd_portefeuille(service: TradingService, args: argparse.Namespace) -> int:
    v = await service.valuation(compte=args.compte)
    warn_fallback(service)
    print(account_title(v.account))
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
    account, orders = service.history(compte=args.compte)
    print(account_title(account))
    if not orders:
        print("Aucun ordre sur ce compte.")
        return 0
    rows = [
        (
            str(o.id),
            local(o.created_at),
            side_label(o),
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
    slipped = [o for o in orders if o.trigger_price is not None]
    if slipped:
        total = sum(o.slippage for o in slipped)
        print(
            f"Glissement des ordres en attente exécutés : {fmt.usdt(total, 4, sign=True)} "
            "(positif = coût, prix exécuté moins bon que le prix de déclenchement)"
        )
    return 0


def cmd_reset(service: TradingService, args: argparse.Namespace) -> int:
    capital = validate_capital(args.montant)  # refuse avant de demander confirmation
    target = service.account(args.compte)
    if not args.yes:
        print(f"Remettre le compte '{target.name}' (n°{target.id}) à zéro avec {fmt.usdt(capital)} ?")
        print(
            "Les positions ouvertes seront abandonnées (pas vendues) et les ordres en attente annulés. "
            "L'ancien compte reste archivé dans la base."
        )
        try:
            answer = input("Confirmer ? (o/N) ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("o", "oui", "y", "yes"):
            print("Annulé, rien n'a changé.")
            return 0
    account = service.reset(capital, compte=args.compte)
    print(f"Compte n°{account.id} ouvert le {local(account.created_at)} avec {fmt.usdt(account.initial_capital)}.")
    return 0


def cmd_comptes(service: TradingService, args: argparse.Namespace) -> int:
    if args.action == "creer":
        a = service.create_account(
            args.nom, args.capital, AUTO if args.auto or args.swing else MANUAL, "swing" if args.swing else "intraday",
            args.frais,
        )
        extra = (", swing" if a.swing else "") + (f", frais {fmt.num(a.fee_rate * 100)} %" if a.fee_rate != FEE_RATE else "")
        print(f"Compte '{a.name}' (n°{a.id}, {a.mode}{extra}) ouvert avec {fmt.usdt(a.initial_capital)}.")
        return 0
    if args.action == "choisir":
        a = service.select_account(args.nom)
        print(f"Compte courant : '{a.name}' (n°{a.id}).")
        return 0
    if args.action == "archiver":
        a = service.archive_account(args.nom)
        print(f"Compte '{a.name}' (n°{a.id}) archivé, ses ordres en attente sont annulés.")
        return 0
    rows = [
        (
            ("* " if a.current else "  ") + a.name,
            str(a.id),
            a.mode,
            fmt.usdt(a.initial_capital),
            fmt.usdt(a.cash),
            local(a.created_at),
        )
        for a in service.open_accounts()
    ]
    print(table(["Nom", "N°", "Mode", "Capital initial", "Solde USDT", "Ouvert le"], rows, left=3))
    print("* compte courant (utilisé sans --compte)")
    return 0


def cmd_session(service: TradingService, args: argparse.Namespace) -> int:
    if args.derniere_entree is not None or args.cloture is not None:
        if args.derniere_entree is None or args.cloture is None:
            print("Erreur : donne les deux heures (dernière entrée et clôture), ex. : session 22:00 23:50", file=sys.stderr)
            return 1
        rules = service.set_session_rules(args.derniere_entree, args.cloture)
        print("Horaires de session enregistrés.")
    else:
        rules = service.session_rules()
    print(f"Comptes automatiques : plus d'entrée à partir de {rules.last_entry:%H:%M} UTC, clôture forcée à {rules.close:%H:%M} UTC.")
    return 0


def cmd_info(service: TradingService, args: argparse.Namespace) -> int:
    active = service.account()
    archived = sum(1 for a in service.accounts() if a.archived)
    rules = service.session_rules()
    print(f"Base SQLite : {service.storage.path}")
    print(f"(modifiable avec la variable d'environnement {DB_ENV_VAR})")
    print(f"Compte courant : '{active.name}' n°{active.id}, {fmt.usdt(active.initial_capital)} au {local(active.created_at)}")
    print(f"Comptes ouverts : {', '.join(a.name for a in service.open_accounts())}")
    print(f"Comptes archivés : {archived}")
    print(f"Session (UTC) : dernière entrée {rules.last_entry:%H:%M}, clôture {rules.close:%H:%M}")
    return 0


def _num(value, digits: int = 2) -> str:
    return "-" if value is None else fmt.num(value, digits)


def cmd_journal(service: TradingService, args: argparse.Namespace) -> int:
    account, entries = service.journal(compte=args.compte, limit=args.limite)
    print(account_title(account))
    if not entries:
        print("Journal vide.")
        return 0
    rows = []
    for e in entries:
        item = e.item
        if e.kind == "signal":
            text = f"{item.kind} {item.pair} {item.interval}, bougie {local(item.candle_open_time)} : {item.decision}"
            if item.reasons:
                text += f" ({', '.join(item.reasons)})"
        elif e.kind == "refus":
            text = f"{item.pair} : {', '.join(item.rules)}, {item.message}"
        elif e.kind == "ordre":
            text = f"{side_label(item)} {fmt.qty(item.quantity)} {item.pair} à {fmt.price(item.price)}, frais {fmt.usdt(item.fee)}"
        else:
            text = (f"{item.pair} {local(item.opened_at)} → {local(item.closed_at)}, P&L {fmt.usdt(item.pnl, sign=True)}, "
                    f"sortie : {item.exit_reason}" + (f", stratégie {item.strategy}" if item.strategy else ""))
        rows.append((local(e.at), e.kind, str(e.ref or ""), text))
    print(table(["Date", "Type", "N°", "Détail"], rows, left=4))
    return 0


def cmd_stats(service: TradingService, args: argparse.Namespace) -> int:
    from simtrade.stats import parse_utc

    start = None if args.depuis is None else parse_utc(args.depuis, "depuis")
    end = None if args.jusqu_a is None else parse_utc(args.jusqu_a, "jusqu-a")
    account, s, curve = service.stats(compte=args.compte, strategie=args.strategie, depuis=start, jusqu_a=end)
    print(account_title(account) + (f", stratégie {args.strategie}" if args.strategie else ""))
    rows = [
        ("P&L net", fmt.usdt(s.pnl, sign=True) + f" ({_num(s.pnl_pct)} %)"),
        ("Drawdown max", f"{_num(s.max_drawdown_pct)} %"),
        ("Trades", str(s.trades)),
        ("Taux de réussite", "-" if s.win_rate is None else f"{_num(s.win_rate, 1)} % ({s.wins}/{s.trades})"),
        ("Gain moyen par trade", "-" if s.avg_pnl is None else fmt.usdt(s.avg_pnl, sign=True)),
        ("Frais payés", fmt.usdt(s.fees)),
        ("Sharpe (quotidien × √365)", "-" if s.sharpe is None else f"{s.sharpe:.2f}"),
        ("Sessions (effectif)", str(s.sessions)),
        ("Minutes sans bougie (prix reporté)", str(curve.missing)),
    ]
    print(table(["Statistique", "Valeur"], rows, left=1))
    if s.sharpe is None:
        print("Sharpe non calculé : moins de 2 sessions, ou rendements tous égaux.")
    return 0


def cmd_marche(service: TradingService, args: argparse.Namespace):
    from datetime import timedelta

    from simtrade.download import PairReport, gap_report
    from simtrade.marche import STORED, missing_steps
    from simtrade.models import SWING_PAIRS

    if args.swing:
        args.paires = list(SWING_PAIRS)
    iv = args.intervalle
    unit = "min" if iv == "1m" else "h"
    store = service.market_store()
    if args.action == "etat":
        print(f"Base des bougies : {store.path} ({store.size_bytes() / 1e6:.1f} Mo)")
        rows = []
        for pair in args.paires:
            bounds = store.bounds(pair, iv)
            if bounds is None:
                rows.append((pair, "0", "-", "-", "-", "-"))
                continue
            r = gap_report(store, PairReport(pair, interval=iv), bounds[0],
                           bounds[1] + timedelta(milliseconds=STORED[iv]))
            rows.append((pair, str(store.count(pair, interval=iv)), local(bounds[0]), local(bounds[1]),
                         f"{len(r.binance)} ({missing_steps(r.binance, iv)} {unit})",
                         f"{len(r.pending)} ({missing_steps(r.pending, iv)} {unit})"))
        print(table(["Paire", "Lignes", "Première", "Dernière", "Trous Binance", "À télécharger"], rows, left=1))
        return 0
    return _download(store, args)


async def _download(store, args: argparse.Namespace) -> int:
    from datetime import timedelta

    from simtrade.download import Downloader
    from simtrade.marche import missing_steps
    from simtrade.history import BinanceHistory
    from simtrade.models import utcnow
    from simtrade.stats import parse_utc

    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end = parse_utc(args.jusqu_a, "jusqu-a") if args.jusqu_a else today
    start = parse_utc(args.depuis, "depuis") if args.depuis else end - timedelta(days=args.jours)
    history = BinanceHistory()
    started = time.perf_counter()
    try:
        downloader = Downloader(store, history, progress=lambda m: print("  " + m, flush=True), interval=args.intervalle)
        report = await downloader.download(args.paires, start, end)
    finally:
        await history.aclose()
    elapsed = time.perf_counter() - started
    print(f"Période : {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC, durée {elapsed:.0f} s")
    unit = "min" if args.intervalle == "1m" else "h"
    rows = [(p.pair, str(p.rows), str(p.requests), f"{len(p.binance)} ({missing_steps(p.binance, p.interval)} {unit})",
             f"{len(p.pending)} ({missing_steps(p.pending, p.interval)} {unit})") for p in report.pairs]
    print(table(["Paire", "Lignes écrites", "Requêtes", "Trous Binance", "À télécharger"], rows, left=1))
    for p in report.pairs:
        for kind, ranges in (("Binance", p.binance), ("à télécharger", p.pending)):
            for a, b in ranges[:20]:
                print(f"  trou {kind} {p.pair} : {a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M} UTC")
    print(f"Taille de marche.db : {report.size_bytes / 1e6:.1f} Mo ; espace libre : {report.free_bytes_after / 1e9:.1f} Go")
    if report.waits:
        print(f"Attentes (poids, 429/418) : {len(report.waits)}, {sum(report.waits):.0f} s au total")
    return 0


def _print_stats(s) -> None:
    rows = [
        ("P&L net", fmt.usdt(s.pnl, sign=True) + f" ({_num(s.pnl_pct)} %)"),
        ("Drawdown max", f"{_num(s.max_drawdown_pct)} %"),
        ("Trades", str(s.trades)),
        ("Taux de réussite", "-" if s.win_rate is None else f"{_num(s.win_rate, 1)} % ({s.wins}/{s.trades})"),
        ("Gain moyen par trade", "-" if s.avg_pnl is None else fmt.usdt(s.avg_pnl, sign=True)),
        ("Frais payés", fmt.usdt(s.fees)),
        ("Sharpe (quotidien × √365)", "-" if s.sharpe is None else f"{s.sharpe:.2f}"),
        ("Sessions (effectif)", str(s.sessions)),
    ]
    print(table(["Statistique", "Valeur"], rows, left=1))


def cmd_backtest(service: TradingService, args: argparse.Namespace) -> int:
    from collections import Counter
    from datetime import timedelta
    from decimal import Decimal

    from simtrade import backtest
    from simtrade.engine import parse_decimal
    from simtrade.errors import InvalidInput
    from simtrade.stats import parse_utc

    start, end = parse_utc(args.depuis, "depuis"), parse_utc(args.jusqu_a, "jusqu-a")
    if start >= end:
        raise InvalidInput("La période est vide (--depuis doit précéder --jusqu-a).")
    changes = {}
    for item in args.surcharges:
        key, sep, value = item.partition("=")
        if not sep:
            raise InvalidInput(f"Surcharge invalide : '{item}' (format clé=valeur).")
        changes[key.strip()] = value.strip()
    raw = strategies.with_overrides(strategies.model(args.modele), {"paire": args.paire, **changes})
    strategy = strategies.validate(raw)
    spread = parse_decimal(args.spread, "Spread")
    if not Decimal(0) <= spread <= Decimal(5):
        raise InvalidInput(f"Spread invalide : {args.spread} (de 0 à 5 %).")
    if strategy.swing:  # klines 1h, avec la fenêtre swing avant le début (SWING_BARS bougies + 1)
        from simtrade.market import BAR_SECONDS

        warmup = timedelta(seconds=BAR_SECONDS[strategy.intervalle]) * (strategies.SWING_BARS[strategy.intervalle] + 1)
        candles = service.market_store().candles(strategy.paire, start - warmup, end, "1h")
    else:
        candles = service.market_store().candles(strategy.paire, start - timedelta(days=2), end)
    if not candles:
        hint = " --intervalle 1h --swing" if strategy.swing else ""
        raise InvalidInput(
            f"Aucune bougie {strategy.paire} dans marche.db pour cette période : "
            f"python -m simtrade marche telecharger{hint}."
        )
    started = time.perf_counter()
    r = backtest.run(strategy, candles, start, end, backtest.BacktestConfig(spread_pct=spread))
    if strategy.swing:
        print("Swing : klines 1h, sans clôture de session, garde-fou swing (perte 7 jours, exposition).")
    print(f"Backtest {strategy.nom} sur {strategy.paire} {strategy.intervalle}, {start:%Y-%m-%d %H:%M} → "
          f"{end:%Y-%m-%d %H:%M} UTC, spread {fmt.num(spread)} %, frais {fmt.num(FEE_RATE * 100, 1)} % "
          f"({time.perf_counter() - started:.1f} s)")
    _print_stats(r.stats)
    print(f"Clôtures évaluées : {r.decisions} ; entrées : {r.entries} ; sessions arrêtées (perte max) : {len(r.halts)}")
    if r.ignored:
        print("Signaux ignorés ou refusés : " + ", ".join(f"{k} {v}" for k, v in r.ignored.most_common()))
    exits = Counter(t.exit_reason for t in r.trades)
    if exits:
        print("Sorties : " + ", ".join(f"{k} {v}" for k, v in sorted(exits.items())))
    return 0


def cmd_catalogue(service: TradingService, args: argparse.Namespace) -> int:
    from simtrade.config import CATALOGUE_PATH

    if args.action == "construire":
        return _build_catalogue(service, args)
    path = Path(args.sortie) / "catalogue.json" if args.sortie else CATALOGUE_PATH
    if not path.exists():
        print(f"Aucun catalogue ({path}) : python -m simtrade catalogue construire.")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    builds = data.get("constructions") or [data]  # format 1 : une seule construction, au premier niveau
    for c in builds:
        p = c["periodes"]
        step = f"étape {c['etape']} : " if "etape" in c else ""
        print(f"{step}catalogue du {c['cree_le'][:16]} (commit {c.get('commit') or '?'}), hors échantillon "
              f"{p['hors_echantillon'][0][:10]} → {p['hors_echantillon'][1][:10]}")
    rows = []
    for r in data["resultats"]:
        trades = r["trades_hors_echantillon"]
        pct = r["percentile_tirages"]
        rows.append((r["modele"], r["paire"], r.get("intervalle", "5m"), r["statut"], r["pnl_hors_echantillon"] or "-",
                     "-" if trades is None else str(trades), "-" if pct is None else f"{pct:.0f}"))
    print(table(["Modèle", "Paire", "Int.", "Statut", "P&L hors éch.", "Trades", "Percentile"], rows, left=4))
    print(f"Stratégies admises : {len(data['entrees'])}" + (" (catalogue vide)" if not data["entrees"] else ""))
    for e in data["entrees"]:
        flag = ", fragile au spread" if e.get("fragile_spread") else ""
        print(f"  {e['modele']} {e['paire']} : {e['parametres']}, régimes retenus : "
              f"{', '.join(e['regimes_retenus']) or 'aucun'}{flag}")
    return 0


def _build_catalogue(service: TradingService, args: argparse.Namespace) -> int:
    import subprocess
    from datetime import timedelta

    from simtrade import catalogue
    from simtrade.config import CATALOGUE_PATH
    from simtrade.errors import InvalidInput

    store = service.market_store()
    grid, periods, _ = catalogue.FAMILIES[args.etape]
    for pair in PAIRS:
        bounds = store.bounds(pair)
        if bounds is None or bounds[0] > periods.data_start or bounds[1] < periods.end - timedelta(minutes=1):
            raise InvalidInput(
                f"Bougies {pair} incomplètes pour {periods.data_start:%Y-%m-%d} → {periods.end:%Y-%m-%d} : "
                f"python -m simtrade marche telecharger --depuis {periods.data_start:%Y-%m-%d} "
                f"--jusqu-a {periods.end:%Y-%m-%d}"
            )
    repo = Path(catalogue.__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True, check=True)
        commit = done.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        commit = None
    out = Path(args.sortie) if args.sortie else None
    cat_path = out / "catalogue.json" if out else CATALOGUE_PATH
    reports = out / "backtests" if out else repo / "backtests"
    processes = args.processus or catalogue.default_processes()
    print(f"Construction du catalogue (étape {args.etape} : {', '.join(grid)}) : {processes} processus, "
          f"bougies {store.path}, sortie {cat_path} et {reports}", flush=True)
    b = catalogue.build(catalogue.StoreLoader(str(store.path)), periods=periods, models=tuple(grid),
                        processes=processes, commit=commit, progress=lambda m: print("  " + m, flush=True))
    data = catalogue.write(b, cat_path, reports)
    print(f"Terminé : {len(b.admitted)} stratégie(s) admise(s) sur {len(b.couples)} (catalogue : "
          f"{len(data['entrees'])} sur {len(data['resultats'])}) ; " + catalogue.durations(b))
    return 0


def cmd_api() -> int:
    from simtrade.api import run  # import paresseux : FastAPI n'est chargé que pour l'API

    run()
    return 0


COMMANDS = {
    "prix": cmd_prix,
    "acheter": cmd_acheter,
    "vendre": cmd_vendre,
    "portefeuille": cmd_portefeuille,
    "historique": cmd_historique,
    "entrer": cmd_entrer,
    "limite": cmd_limite,
    "deplacer-stop": cmd_deplacer_stop,
    "risque": cmd_risque,
    "refus": cmd_refus,
    "strategies": cmd_strategies,
    "strategie": cmd_strategie,
    "signaux": cmd_signaux,
    "stop": cmd_stop,
    "objectif": cmd_stop,
    "stop-objectif": cmd_stop_objectif,
    "ordres": cmd_ordres,
    "annuler": cmd_annuler,
    "reset": cmd_reset,
    "comptes": cmd_comptes,
    "session": cmd_session,
    "info": cmd_info,
    "journal": cmd_journal,
    "stats": cmd_stats,
    "marche": cmd_marche,
    "backtest": cmd_backtest,
    "catalogue": cmd_catalogue,
}
