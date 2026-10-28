# Simulateur de paper trading crypto

Simulateur d'achat/vente de cryptomonnaies **avec de l'argent fictif** et des **prix réels**, en ligne de commande ou via une API HTTP locale.
Étapes 1 à 3 d'un projet qui recevra plus tard une interface.

## Ce que ça fait

- **Prix en temps réel** (BTCUSDT, ETHUSDT, SOLUSDT) via le websocket public de **Binance**, sans compte.
  Si Binance ne répond pas en 5 s, bascule automatiquement sur le websocket public de **Coinbase** (paires BTC-USDT, ETH-USDT, SOL-USDT).
- **Bid/ask** : on achète au meilleur prix vendeur (ask) et on vend au meilleur prix acheteur (bid), le spread est donc payé comme en vrai.
- **Frais de 0,1 %** sur chaque ordre, calculés sur quantité × prix, affichés à chaque ordre et dans l'historique.
- **Portefeuille** : solde USDT, positions, prix de revient moyen, P&L réalisé et latent, frais payés, performance totale en %.
- **Refus clairs** des ordres impossibles : solde insuffisant, vente sans position, quantité supérieure à ce qu'on détient, montant trop faible, paire inconnue.
- **Capital de départ paramétrable** (10 000 USDT par défaut, maximum 10 000 000).
- **Persistance SQLite** : le portefeuille survit à un redémarrage.
- **Calculs de marché** (API) : bougies OHLC 1m, 5m, 15m et 1h préchargées depuis l'historique Binance puis construites en direct sur les transactions, SMA, EMA, RSI, VWAP de session et variation, avec des signaux de qualité des données.

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
python -m simtrade api                  # lance l'API sur http://127.0.0.1:8000 (voir plus bas)
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

## API (FastAPI)

```powershell
python -m simtrade api
```

Lance un serveur local sur `http://127.0.0.1:8000` (pas accessible depuis le réseau). Il garde **une seule** connexion websocket de cotations ouverte (Binance, sinon Coinbase) et met les cotations en cache : les ordres sont exécutés instantanément au dernier prix reçu. Une **seconde** connexion reçoit les transactions exécutées (Binance `aggTrade`, sinon Coinbase `matches`) pour les bougies et le volume. Si un flux tombe, il se reconnecte tout seul (attente de 1, 2, 4… jusqu'à 30 s), indépendamment de l'autre. Un ordre n'est jamais exécuté sur une cotation de plus de 5 s (erreur 503) ; `/prix` renvoie quand même tout le cache, avec `"perime": true` pour ces cotations (503 seulement si aucune cotation n'a encore été reçue). Documentation interactive : `http://127.0.0.1:8000/docs`.

| Méthode | Route | Corps JSON | Réponse |
|---|---|---|---|
| GET | `/prix` | | bid, ask, spread, source, heure, âge et `perime` de chaque cotation |
| POST | `/ordres/achat` | `{"paire": "btc", "montant": "500"}` | ordre exécuté + solde |
| POST | `/ordres/vente` | `{"paire": "eth", "quantite": "0.05"}` ou `"tout"` | ordre exécuté + solde |
| GET | `/portefeuille` | | positions, valeur, P&L, frais, performance |
| GET | `/historique` | | compte actif + ordres |
| POST | `/reset` | `{"capital": "2500"}` (facultatif, défaut 10000) | nouveau compte |
| GET | `/info` | | chemin de la base, compte actif, état des flux (`flux`, `flux_volume`), `historique`, `horloge` |
| GET | `/bougies/{paire}?intervalle=1m&limite=100` | | bougies OHLC, volume, notional, transactions, `origine`, `complete`, `cloturee`, `qualite` |
| GET | `/indicateurs/{paire}?intervalle=1m&fenetre=1440&sma=20&ema=20&rsi=14&inclure_courante=false&vwap=jour_utc` | | SMA, EMA, RSI, VWAP, variation, volume, `qualite` |
| WS | `/ws/prix` | | chaque cotation en direct (d'abord le cache, puis les mises à jour) |

**Nombres** : envoyés et renvoyés **en chaîne** (`"0.00000001"`, jamais de float ni de `1E-8`). Un nombre JSON (`"montant": 500`) est refusé.

**Erreurs** : `{"erreur": "<message>"}` avec le code 400 (saisie invalide, capital refusé, ordre impossible) ou 503 (pas de prix frais).

Exemples dans un second terminal PowerShell. Il faut écrire `curl.exe` (dans Windows PowerShell, `curl` seul est un alias d'`Invoke-WebRequest`) et échapper les guillemets du JSON avec `\"` :

```powershell
curl.exe http://127.0.0.1:8000/prix
curl.exe -X POST http://127.0.0.1:8000/ordres/achat -H "Content-Type: application/json" -d '{\"paire\": \"btc\", \"montant\": \"500\"}'
curl.exe -X POST http://127.0.0.1:8000/ordres/vente -H "Content-Type: application/json" -d '{\"paire\": \"btc\", \"quantite\": \"tout\"}'
curl.exe http://127.0.0.1:8000/portefeuille
curl.exe http://127.0.0.1:8000/historique
curl.exe -X POST http://127.0.0.1:8000/reset -H "Content-Type: application/json" -d '{\"capital\": \"2500\"}'
curl.exe http://127.0.0.1:8000/info
```

La CLI et l'API utilisent la même base SQLite : un ordre passé dans l'une apparaît dans l'autre.

### Calculs de marché

**Historique** : au démarrage, l'API charge les klines publiques Binance (`GET /api/v3/klines`, sans compte) : 1440 bougies en `1m` (une journée UTC, 2 requêtes de 1000 max), 500 en `5m`, `15m` et `1h`. Elle mesure aussi l'écart entre l'horloge locale et `GET /api/v3/time` (exposé dans `/info`).

**Bougies live** : construites sur le **prix des transactions** (`aggTrade` Binance, `matches` Coinbase), datées à l'**heure de l'exchange** (champ `T` / `time`), bornes alignées sur l'epoch UTC. Le mid ne sert plus aux bougies ; les ordres restent exécutés à l'ask (achat) et au bid (vente).

**Raccord historique / live** (clé = début de la bougie, jamais de doublon) :
- **bougie clôturée : la kline gagne**. Toutes les 5 s, chaque bougie live clôturée depuis plus de 2 s est remplacée par sa kline définitive ;
- **bougie en cours : le live gagne**, amorcé par la kline en cours. Pendant le chargement, les transactions reçues sont mises en tampon ; celles déjà reçues à la réponse sont supposées comptées par la kline, les suivantes sont ajoutées. Cette bougie est marquée `complete: false` jusqu'à son remplacement : les transactions exécutées entre l'instantané REST et la première transaction reçue par le websocket manquent (constaté : 22 sur 2532 pour une minute BTC). Plus généralement, une bougie live n'est `complete` que si le websocket livrait déjà des transactions avant son début (heure exchange) ;
- **après une coupure du flux de transactions** (ou une bascule de source) : même procédure, depuis la plus ancienne bougie live non vérifiée, ce qui comble le trou ;
- une minute sans transaction donne une **bougie plate** (clôture précédente, volume nul), comme les klines Binance ;
- historique injoignable : les bougies live continuent (marquées incomplètes après une coupure), nouvel essai avec une attente de 1, 2, 4… 30 s.

**Indicateurs** : par défaut sur les bougies **clôturées** uniquement (`inclure_courante=true` pour ajouter la bougie en cours), sur les `fenetre` dernières bougies, arrondis à 8 décimales, `null` s'il n'y a pas assez de bougies.

Notations : $c_t$ la clôture de la bougie $t$ (la dernière est $c_T$), $o_1$ l'ouverture de la première bougie de la fenêtre, $V_t$ le volume et $N_t = \sum_k p_k \, q_k$ le notional de la bougie $t$.

| Indicateur | Formule |
|---|---|
| SMA | $\text{SMA}_n = \dfrac{1}{n} \sum_{t=T-n+1}^{T} c_t$ |
| EMA | $E_n = \dfrac{1}{n} \sum_{t=1}^{n} c_t$, puis $E_t = \alpha \, c_t + (1-\alpha) \, E_{t-1}$ avec $\alpha = \dfrac{2}{n+1}$ |
| RSI (Wilder) | $\Delta_t = c_t - c_{t-1}$ ; $H$ et $B$ = moyennes des hausses $\max(\Delta_t, 0)$ et des baisses $\max(-\Delta_t, 0)$ sur les $n$ premières variations, puis $H_t = \dfrac{(n-1)\,H_{t-1} + \max(\Delta_t,0)}{n}$ (idem pour $B$) ; $\text{RSI} = 100 - \dfrac{100}{1 + H/B}$, 100 si $B = 0$, 50 si $H = B = 0$ |
| VWAP de session (défaut, `vwap=jour_utc`) | $\dfrac{\sum_{t \ge t_0} N_t}{\sum_{t \ge t_0} V_t}$ sur les bougies **1m** depuis $t_0$ = 00:00 UTC du jour, quel que soit `intervalle` |
| VWAP glissant (`vwap=fenetre`) | $\dfrac{\sum_t N_t}{\sum_t V_t}$ sur les bougies de la fenêtre, sans remise à zéro |
| Variation | $\dfrac{c_T - o_1}{o_1} \times 100$ |

La réponse précise l'ancrage du VWAP (`ancrage`, `debut`, `fin`, `nb_bougies`, `volume`).

**Qualité** (`qualite` dans `/bougies` et `/indicateurs`) : `prix_perime` (cotation de plus de 5 s ou absente), `flux_transactions_muet` (flux déconnecté ou aucune transaction de la paire depuis 30 s), `source_transactions`, `repli_coinbase`, `historique` (`complet`, `partiel`, `indisponible`, `desactive`), `derniere_synchro`, `bougie_courante_complete`, `ecart_horloge_ms`, `vwap_fiable` (faux en cas de repli Coinbase, de flux muet, d'historique incomplet ou de volume nul) et `raisons` (chaque problème détecté, dont `horloge_decalee` au-delà de 1 s d'écart).

```powershell
curl.exe "http://127.0.0.1:8000/bougies/btc?intervalle=5m&limite=10"
curl.exe "http://127.0.0.1:8000/indicateurs/eth?intervalle=1m&rsi=14&sma=20"
curl.exe "http://127.0.0.1:8000/indicateurs/sol?inclure_courante=true&vwap=fenetre&fenetre=60"
```

Paramètre invalide (intervalle ou ancrage inconnu, entier ou booléen illisible, hors bornes) : erreur 400 avec un message qui nomme le paramètre. `limite`, `fenetre`, `sma` et `ema` vont de 1 à 1440, `rsi` de 1 à 1439.

## Tests

```powershell
python -m pytest
```

Les tests couvrent le moteur (frais, prix moyen, P&L réalisé/latent, performance, refus d'ordres, validation du capital), la persistance (redémarrage, reset, transactions), le parsing des messages Binance/Coinbase, la bascule entre sources, les commandes CLI, les routes de l'API, la reconnexion du flux, le refus des cotations périmées, la construction des bougies, le raccord avec l'historique (klines simulées par `httpx.MockTransport`), les indicateurs (valeurs calculées à la main), la qualité des données et les routes de marché. Ils utilisent des données simulées ; une garde dans `tests/conftest.py` fait échouer tout test qui tenterait une résolution de nom ou une connexion hors de la machine.

## Architecture

```
simtrade/
  models.py    structures de données (Quote, Position, Account, Order)
  engine.py    moteur pur : calculs, frais, refus, validation (aucun I/O)
  storage.py   SQLite : comptes, positions, ordres
  feeds/       sources de prix : Binance, Coinbase, bascule, source simulée
  service.py   TradingService : point d'entrée unique des interfaces
  market.py    calculs de marché purs : bougies OHLC, raccord avec les klines, SMA, EMA, RSI, VWAP, variation, qualité (aucun I/O)
  history.py   historique public Binance (REST) : klines paginées, heure du serveur
  hub.py       PriceHub (cotations + transactions, cache, bougies, raccord avec l'historique, reconnexion) et CachedFeed
  api.py       API FastAPI : create_app(), routes, websocket /ws/prix
  cli.py       ligne de commande
```

L'API ne fait qu'appeler `TradingService`, qui reçoit un `CachedFeed` à la place d'une connexion directe.

## Limites

- **Tout est en USDT.** Pas de conversion en EUR, pas d'autres paires que les trois listées.
- **Pas de carnet d'ordres complet** : seul le meilleur bid/ask est utilisé. Un gros ordre est exécuté entièrement à ce prix, sans glissement (*slippage*), ce qui est optimiste pour de gros montants.
- **Ordres au marché uniquement** : pas d'ordres limites ni de stop pour l'instant.
- **CLI : une connexion websocket par commande** : chaque commande met 1 à 4 s à obtenir un prix (le temps d'établir la connexion). L'API n'a pas ce délai.
- **Coinbase en secours est moins liquide en USDT** : le spread BTC-USDT y est nettement plus large que sur Binance (constaté : environ 3,5 pb contre moins de 0,01 pb), et le canal ticker ne se met à jour qu'à chaque transaction.
- CLI : pas de reconnexion automatique si le flux continu `prix` est coupé en cours de route (l'API, elle, se reconnecte).
- **Bougies en mémoire seulement** : rechargées depuis Binance à chaque démarrage (1 journée en 1m, 500 bougies au-delà), rien n'est sauvegardé localement.
- **Raccord au démarrage et après une coupure** : la bougie en cours manque les transactions exécutées avant que le websocket ne livre (de l'ordre de 1 % du nombre de transactions d'une minute BTC) ; elle est marquée `complete: false` jusqu'à son remplacement par la kline. Les indicateurs par défaut (bougies clôturées) l'utilisent entre sa clôture et son remplacement (2 à 7 s), et durablement si la réconciliation échoue.
- **Clôture jugée sur l'horloge locale** : l'écart avec Binance est mesuré et signalé (au-delà de 1 s), pas corrigé.
- **Volume** : celui de la source qui répond, pas un volume global. En repli sur Coinbase, l'historique reste celui de Binance : bougies de deux places différentes, VWAP signalé non fiable. Sur Coinbase, les paires USDT sont peu échangées et la source de transactions est jugée muette si rien n'arrive en 15 s.
- Ouvrir l'API ouvre **deux** connexions websocket (cotations et transactions) et environ 15 requêtes REST au démarrage, puis environ 3 à 5 par minute pour la réconciliation.
- API sans authentification : elle n'écoute que sur 127.0.0.1, ne pas l'exposer sur le réseau.
- Un seul utilisateur, un seul compte actif à la fois.
- Simulation pédagogique : ni conseil financier, ni lien avec un vrai compte.
