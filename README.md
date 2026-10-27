# Simulateur de paper trading crypto

Simulateur d'achat/vente de cryptomonnaies **avec de l'argent fictif** et des **prix réels**, en ligne de commande.
Étape 1 d'un projet qui recevra plus tard une API (FastAPI) et une interface.

## Ce que ça fait

- **Prix en temps réel** (BTCUSDT, ETHUSDT, SOLUSDT) via le websocket public de **Binance**, sans compte.
  Si Binance ne répond pas en 5 s, bascule automatiquement sur le websocket public de **Coinbase** (paires BTC-USDT, ETH-USDT, SOL-USDT).
- **Bid/ask** : on achète au meilleur prix vendeur (ask) et on vend au meilleur prix acheteur (bid), le spread est donc payé comme en vrai.
- **Frais de 0,1 %** sur chaque ordre, calculés sur quantité × prix, affichés à chaque ordre et dans l'historique.
- **Portefeuille** : solde USDT, positions, prix de revient moyen, P&L réalisé et latent, frais payés, performance totale en %.
- **Refus clairs** des ordres impossibles : solde insuffisant, vente sans position, quantité supérieure à ce qu'on détient, montant trop faible, paire inconnue.
- **Capital de départ paramétrable** (10 000 USDT par défaut, maximum 10 000 000).
- **Persistance SQLite** : le portefeuille survit à un redémarrage.

## Installation (Windows / PowerShell)

```powershell
git clone https://github.com/lopezmanzanomiguel4-a11y/simulateur-trading.git
cd simulateur-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Python 3.11 ou plus récent (testé avec 3.14).

## Utilisation

```powershell
python -m simtrade prix                 # prix en direct, rafraîchis chaque seconde (Ctrl+C pour arrêter)
python -m simtrade prix --once          # une seule cotation, avec le spread
python -m simtrade acheter BTCUSDT 500  # achète pour 500 USDT au total, frais inclus
python -m simtrade vendre ETHUSDT 0.05  # vend 0,05 ETH
python -m simtrade vendre sol tout      # vend toute la position SOL
python -m simtrade portefeuille         # positions, valeur, P&L, performance
python -m simtrade historique           # tous les ordres du compte actif, avec frais
python -m simtrade reset                # nouveau compte à 10 000 USDT (demande confirmation)
python -m simtrade reset 2500           # nouveau compte à 2 500 USDT
python -m simtrade reset 2500 --yes     # sans confirmation
python -m simtrade info                 # chemin de la base, compte actif
```

Les paires s'écrivent `BTCUSDT`, `btc`, `BTC/USDT` ou `btc-usdt`. Les nombres acceptent le point ou la virgule (`0,05`).

### Conventions de calcul

Notations : $f = 0{,}001$ (taux de frais), $q$ la quantité, $C$ le prix de revient d'une position (coût des achats, **frais inclus**), $C_0$ le capital initial.

| Élément | Règle |
|---|---|
| Achat de $M$ USDT | $q = \left\lfloor \dfrac{M}{\text{ask}\,(1+f)} \right\rfloor_{10^{-8}}$, total débité $q \cdot \text{ask}\,(1+f) \le M$ |
| Frais | $f \cdot q \cdot \text{prix}$, à l'achat (prix = ask) comme à la vente (prix = bid) |
| Prix moyen | $\bar{p} = \dfrac{C}{q}$ |
| P&L réalisé (vente de $q_v$ sur $q$) | $q_v \cdot \text{bid}\,(1-f) - C \cdot \dfrac{q_v}{q}$, donc **net de tous les frais** |
| P&L latent | $q \cdot \text{bid} - C$ (**frais de sortie non déduits**) |
| Performance totale | $\dfrac{\text{solde} + \sum_i q_i \cdot \text{bid}_i - C_0}{C_0} \times 100$ |

Juste après un achat, la performance est donc légèrement négative : c'est le coût du spread et des frais.

### Reset et base de données

`reset` n'efface rien : il **archive** le compte actif (avec ses ordres) et ouvre un compte neuf avec le capital choisi et la date du reset. Les positions ouvertes sont abandonnées, pas vendues. `historique` et `portefeuille` ne montrent que le compte actif.

La base est stockée dans `%LOCALAPPDATA%\simtrade\simtrade.db` (hors du projet et de OneDrive). Pour en utiliser une autre :

```powershell
$env:SIMTRADE_DB = "C:\chemin\vers\autre.db"
```

## Tests

```powershell
python -m pytest
```

Les tests couvrent le moteur (frais, prix moyen, P&L réalisé/latent, performance, refus d'ordres, validation du capital), la persistance (redémarrage, reset, transactions), le parsing des messages Binance/Coinbase, la bascule entre sources et les commandes CLI. Ils utilisent des prix simulés et ne touchent pas au réseau.

## Architecture

```
simtrade/
  models.py    structures de données (Quote, Position, Account, Order)
  engine.py    moteur pur : calculs, frais, refus, validation (aucun I/O)
  storage.py   SQLite : comptes, positions, ordres
  feeds/       sources de prix : Binance, Coinbase, bascule, source simulée
  service.py   TradingService : point d'entrée unique des interfaces
  cli.py       ligne de commande
```

Une future API FastAPI n'aura qu'à appeler `TradingService` (ses méthodes réseau sont déjà `async`).

## Limites

- **Tout est en USDT.** Pas de conversion en EUR, pas d'autres paires que les trois listées.
- **Pas de carnet d'ordres complet** : seul le meilleur bid/ask est utilisé. Un gros ordre est exécuté entièrement à ce prix, sans glissement (*slippage*), ce qui est optimiste pour de gros montants.
- **Ordres au marché uniquement** : pas d'ordres limites ni de stop pour l'instant.
- **Une connexion websocket par commande** : chaque commande met 1 à 4 s à obtenir un prix (le temps d'établir la connexion). Un processus permanent (API) supprimera ce délai.
- **Coinbase en secours est moins liquide en USDT** : le spread BTC-USDT y est nettement plus large que sur Binance (constaté : environ 3,5 pb contre moins de 0,01 pb), et le canal ticker ne se met à jour qu'à chaque transaction.
- Pas de reconnexion automatique si le flux continu `prix` est coupé en cours de route.
- Un seul utilisateur, un seul compte actif à la fois.
- Simulation pédagogique : ni conseil financier, ni lien avec un vrai compte.
