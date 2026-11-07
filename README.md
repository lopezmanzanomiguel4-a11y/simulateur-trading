# Simulateur de paper trading crypto

Simulateur d'achat/vente de cryptomonnaies **avec de l'argent fictif** et des **prix réels**, en ligne de commande ou via une API HTTP locale.
Étapes 1 à 7 d'un projet qui recevra plus tard une interface.

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
- **Ordres en attente** : achat ou vente limite, stop-loss, take-profit (objectif) et paire stop + objectif liée (l'un annule l'autre), exécutés par le flux live de l'API, avec le glissement enregistré quand le prix saute le stop.
- **Comptes multiples** ouverts en parallèle, chacun avec son capital, en mode **manuel** ou **automatique**.
- **Sessions intraday** pour les comptes automatiques : plus d'entrée après 23:00 UTC, clôture forcée de toutes les positions à 23:55 UTC (horaires paramétrables), avec rattrapage si l'API était arrêtée.
- **Garde-fou de risque** sur chaque entrée d'un compte automatique : stop-loss obligatoire, taille max, perte max de session, entrées max, positions max, délai entre entrées. Les refus sont journalisés. Les sorties ne sont jamais bloquées.
- **Stratégies intraday** décrites en JSON (sans code libre), combinant des **briques** pures : RSI en zone extrême, croisement d'EMA, écart au VWAP de session, cassure du range d'ouverture, filtre de volatilité. Une stratégie activée sur un compte automatique trade seule pendant que l'API tourne, toujours via le garde-fou, et chaque signal est journalisé.
- **Backtest et catalogue** : bougies 1m sauvegardées (6 mois téléchargeables), journal et statistiques partagés par le live et le backtest, simulateur qui réutilise le code de décision du live avec des règles d'exécution prudentes, sélection des paramètres en échantillon, validation hors échantillon contre des références (dont 100 tirages au hasard), et un catalogue des stratégies admises (`simtrade/catalogue.json`, rapports dans `backtests/`).

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
python -m simtrade info                 # chemin de la base, comptes, horaires de session
python -m simtrade api                  # lance l'API sur http://127.0.0.1:8000 (voir plus bas)
```

Ordres en attente (exécutés **uniquement pendant que l'API tourne**, voir Limites) :

```powershell
python -m simtrade limite btc achat 78000 500       # achète pour 500 USDT quand l'ask <= 78 000
python -m simtrade limite eth vente 2600 0.1        # vend 0,1 ETH quand le bid >= 2 600
python -m simtrade stop btc 76000                   # stop-loss sur toute la position (quantité facultative)
python -m simtrade objectif btc 85000 0.002         # take-profit sur 0,002 BTC
python -m simtrade stop-objectif sol 90 120         # stop + objectif liés : le premier exécuté annule l'autre
python -m simtrade ordres                           # ordres en attente du compte (--tous : aussi exécutés/annulés)
python -m simtrade annuler 3                        # annule l'ordre n°3 (et son ordre lié)
```

Entrées avec stop et garde-fou (obligatoires sur un compte automatique) :

```powershell
python -m simtrade entrer btc 500 --stop 79000 --objectif 82000 -c robot   # achat + stop de protection, d'un bloc
python -m simtrade limite btc achat 78000 500 --stop 77000 -c robot        # le stop est posé à l'exécution
python -m simtrade deplacer-stop 4 79500 -c robot   # compte auto : seulement vers le haut
python -m simtrade risque -c robot                  # limites du compte
python -m simtrade risque -c robot taille=30 perte=1.5 trades=4 positions=1 delai=10m
python -m simtrade refus                            # journal des refus (--compte, --limite)
```

Stratégies (exécutées **uniquement pendant que l'API tourne**) :

```powershell
python -m simtrade strategies                                   # modèles de départ et bornes de chaque paramètre
python -m simtrade strategie activer retour-vwap -c robot       # active un modèle (remplace la stratégie active)
python -m simtrade strategie activer tendance-ema -c robot stop.multiple=2.5 croisement_ema.rapide=8
python -m simtrade strategie activer --fichier ma-strategie.json -c robot
python -m simtrade strategie -c robot                           # stratégie active
python -m simtrade strategie desactiver -c robot
python -m simtrade signaux                                      # journal des signaux (--compte, --decision, --limite)
```

Bougies, journal et statistiques :

```powershell
python -m simtrade marche telecharger                   # 183 jours de klines 1m (BTC, ETH, SOL) jusqu'à aujourd'hui 00:00 UTC
python -m simtrade marche telecharger --paires BTCUSDT --depuis 2026-05-01 --jusqu-a 2026-06-01
python -m simtrade marche etat                          # lignes, période, trous et taille de marche.db
python -m simtrade journal -c robot                     # signaux, refus, ordres et trades, dans l'ordre (--limite)
python -m simtrade stats -c robot                       # P&L, drawdown, réussite, Sharpe... (--strategie, --depuis, --jusqu-a)
```

Backtest et catalogue (sans réseau ; `backtest` et `catalogue construire` lisent `marche.db`, `catalogue` lit `catalogue.json`) :

```powershell
python -m simtrade backtest retour-vwap --paire ETHUSDT --depuis 2026-06-01 --jusqu-a 2026-06-08
python -m simtrade backtest retour-vwap ecart_vwap.seuil_pct=0.4 rsi_extreme.seuil=25 --depuis 2026-06-01 --jusqu-a 2026-06-08 --spread 0.05
python -m simtrade catalogue construire                 # grille, sélection, hors échantillon, catalogue.json et backtests/ (--etape 7|7b, défaut 7b ; --processus, --sortie)
python -m simtrade catalogue                            # résultats du catalogue
```

Comptes et sessions :

```powershell
python -m simtrade comptes                          # comptes ouverts, * = compte courant
python -m simtrade comptes creer robot 2000 --auto  # nouveau compte automatique de 2 000 USDT
python -m simtrade comptes choisir robot            # devient le compte courant
python -m simtrade comptes archiver robot           # archive (ordres en attente annulés)
python -m simtrade acheter btc 100 --compte robot   # --compte (ou -c) sur acheter, vendre, ordres, portefeuille, historique, reset...
python -m simtrade session                          # horaires de session (UTC)
python -m simtrade session 22:00 23:50              # dernière entrée 22:00, clôture 23:50
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

### Ordres en attente

| Ordre | Déclenché quand | Exécuté à |
|---|---|---|
| Achat limite (montant USDT, frais inclus) | $\text{ask} \le$ limite | l'ask de la cotation qui déclenche |
| Vente limite, objectif | $\text{bid} \ge$ prix | le bid de la cotation qui déclenche |
| Stop | $\text{bid} \le$ stop | le bid disponible, **même s'il a sauté sous le stop** |

Positions longues uniquement : pas de vente à découvert, pas de stop d'achat. Un ordre qui partirait immédiatement à la pose est refusé (passer un ordre au marché). Les ordres manuels restent valables jusqu'à annulation.

**Glissement** enregistré sur chaque ordre exécuté depuis une attente, avec $p_d$ le prix de déclenchement, $p_e$ le prix d'exécution et $q$ la quantité :

$$g_{\text{vente}} = (p_d - p_e)\, q \qquad g_{\text{achat}} = (p_e - p_d)\, q$$

$g > 0$ est un coût (stop sauté), $g < 0$ une amélioration (limite exécutée à un meilleur prix).

**Réservations** : un achat limite bloque son montant, une vente en attente bloque sa quantité (une paire stop + objectif compte une fois). Un ordre au marché ne peut utiliser que la part libre ; `tout` sur un stop ou un objectif désigne toute la position au moment du déclenchement et suppose qu'aucune partie n'est déjà réservée. Au déclenchement, une vente est plafonnée à la quantité détenue ; sans position, l'ordre est annulé.

### Comptes, mode manuel ou automatique, sessions

Plusieurs comptes peuvent être ouverts en même temps, chacun avec son capital. Le **compte courant** est celui utilisé sans `--compte` (CLI) ni `?compte=` (API). Un compte est **manuel** ou **automatique** à sa création, sans mélange : tout ordre passé sur un compte automatique, par n'importe quelle interface, suit les règles de session ; un compte manuel ne les suit jamais.

Règles de session des comptes automatiques (horaires communs à tous les comptes, en UTC, modifiables avec `session` ou `PUT /session`) :
- session = journée UTC ; aucun achat (au marché ou limite, à la pose comme au déclenchement) à partir de la **dernière entrée** (23:00 par défaut) ; les ventes restent permises ;
- à la **clôture** (23:55 par défaut), l'API annule les ordres en attente du compte puis vend toutes ses positions au bid (ordres de type `cloture_session`) ;
- si la cotation a plus de 5 s, la clôture est reportée à la première cotation fraîche (vérification chaque seconde) ;
- **rattrapage** : si l'API était arrêtée à l'heure de clôture, les positions de la session passée sont vendues à la première cotation fraîche après le redémarrage, au prix du moment. Une clôture faite plus de 5 s après l'heure prévue est de type `cloture_tardive`.

### Garde-fou de risque (comptes automatiques)

Module pur (`risk.py`), appelé avant chaque **entrée** d'un compte automatique : achat au marché, pose d'un achat limite, et déclenchement d'un achat limite. **Les sorties n'y passent jamais.** Stop, objectif, vente au marché, vente limite et clôture de session sont exécutés même si une limite est dépassée. Les comptes manuels ne sont jamais filtrés.

| Règle (code) | Défaut | Détail |
|---|---|---|
| `stop_obligatoire` | toujours | toute entrée porte un stop, sous le prix d'entrée |
| `taille_max` | 20 % | exposition sur la paire après l'entrée (positions au bid + achats limites en attente) ≤ x % de la valeur actuelle du compte |
| `perte_max_session` | 2 % | perte de la session, réalisée + latente, ≥ x % de la valeur en début de session : plus d'entrée jusqu'à la session suivante, même si le prix remonte |
| `trades_max_session` | 6 | entrées de la session (achats exécutés + achats limites en attente) |
| `positions_max` | 2 | paires exposées (position ou achat limite en attente) ; renforcer une paire déjà exposée reste possible |
| `delai_entrees` | 15 min | entre deux entrées sur la même paire |
| `prix_perime` | toujours | chaque position et la paire visée ont une cotation de moins de 5 s |

Perte de session, avec $E$ la valeur actuelle du compte au bid, $q_i$ et $C_i$ la quantité et le prix de revient de chaque position :

$$\text{PnL}_s = \sum_{\text{ordres de la session}} \text{pnl\_réalisé} + \sum_i \left(q_i \cdot \text{bid}_i - C_i\right), \qquad E_0 = E - \text{PnL}_s$$

La limite est atteinte si $\text{PnL}_s \le -\,p \cdot E_0$. Un **arrêt de session** est alors enregistré, à la première tentative d'entrée ou par la vérification de l'API (chaque seconde).

Les limites sont **propres à chaque compte** (défauts ci-dessus tant qu'elles ne sont pas modifiées) et recopiées au reset.

**Stop de protection** : chaque entrée d'un compte automatique pose (ou remplace) **le** stop de la paire, sur toute la position, avec un objectif lié facultatif. Le stop ne baisse jamais : après une nouvelle entrée, le stop actif est le plus haut des deux (ancien et nouveau), et un objectif qui serait sous ce stop est ignoré. Un stop au niveau ou au-dessus du bid à l'entrée est refusé et journalisé (`stop_obligatoire`). Pour un achat au marché (`entrer`, `POST /ordres/entree`), l'achat et le stop sont faits dans une seule transaction : si le stop ou l'objectif est invalide, rien n'est acheté. Pour un achat limite, le stop est posé à l'exécution. Si le bid est déjà sous le stop au déclenchement, l'achat est rejeté (`stop_deja_touche`). Sur un compte automatique, ce stop :
- ne peut pas être **annulé** tant que la position est ouverte (son objectif, si) ;
- ne se **déplace que vers le haut**, toujours sous le bid ;
- ne **réserve pas** la quantité : une vente au marché ou limite reste toujours possible, et le stop couvre ce qui reste ;
- est annulé automatiquement quand la position est fermée.

**Journal** : chaque refus est enregistré, même si l'ordre ne l'est pas, avec la date, le moment, la paire, le montant, le stop, les règles violées, le message et l'état du compte (valeur, perte de session, entrées, exposition). Un achat limite refusé au déclenchement passe à `rejete` (raison `garde_fou:<règles>`).

### Stratégies intraday (comptes automatiques)

Une stratégie est un objet JSON validé, **sans code libre** :

```json
{
  "nom": "retour-vwap", "paire": "BTCUSDT", "intervalle": "5m",
  "entree": [{"brique": "ecart_vwap", "cote": "sous", "seuil_pct": "0.6"},
             {"brique": "rsi_extreme", "zone": "bas", "n": 14, "seuil": "30"},
             {"brique": "volatilite", "n": 14, "min_pct": "0.10", "max_pct": "1.20"}],
  "sortie": [{"brique": "retour_vwap"}, {"brique": "duree_max", "bougies": 12}],
  "stop": {"type": "atr", "multiple": "1.5", "n": 14},
  "objectif": {"type": "r", "multiple": "2"},
  "taille": {"pct_capital": "10"},
  "heures_actives": {"debut": "06:00", "fin": "22:00"}
}
```

- **Entrée** : toutes les briques d'entrée vraies (ET). **Sortie sur signal** : au moins une brique de sortie vraie (OU).
- Décimaux en chaîne, entiers en entier, heures en `HH:MM` UTC. Clé ou brique inconnue, valeur hors bornes, brique en double : refus avec un message qui nomme le paramètre (`'entree.rsi_extreme.seuil' hors bornes : 50 (de 5 à 45)`).
- Types, bornes et défauts : `python -m simtrade strategies` ou `GET /strategies` (`schema`).
- **Une seule stratégie active par compte** : en activer une autre remplace la précédente. Seulement sur un compte **automatique**. Le reset et l'archivage la désactivent. La désactiver ne vend rien : une position ouverte garde son stop, son objectif et la clôture de session.
- La définition est **figée** en base à l'activation (table `strategy_activations`).

**Briques** (fonctions pures de `bricks.py`, sur bougies **clôturées** uniquement ; $c_T$ = clôture de la dernière bougie clôturée de l'intervalle) :

| Brique | Vraie quand | Paramètres (bornes, défaut) |
|---|---|---|
| `rsi_extreme` | zone `bas` : $\text{RSI}_n \le s$ ; zone `haut` : $\text{RSI}_n \ge s$ | `n` 2–50 (14) ; `seuil` bas 5–45 (30), haut 60–95 (70) |
| `croisement_ema` | `haussier` : $E^{r}_{T-1} \le E^{l}_{T-1}$ et $E^{r}_{T} > E^{l}_{T}$ (`baissier` : l'inverse) | `rapide` 3–50 (9), `lente` 10–200 (21), rapide < lente |
| `ecart_vwap` | $\dfrac{c_T - \text{VWAP}_s}{\text{VWAP}_s} \times 100 \le -s$ (`sous`) ou $\ge s$ (`dessus`) | `seuil_pct` 0,1–3 (0,6) |
| `au_dessus_vwap` | $c_T > \text{VWAP}_s$ | |
| `cassure_range` | première clôture de la session au-dessus du plus haut $H$ du range : $c_{T-1} \le H < c_T$ | `debut` 00:00–20:00 (00:00), `minutes` 15–120 (30) |
| `volatilite` | $\dfrac{\text{ATR}_n}{c_T} \times 100 \in [\text{min}, \text{max}]$ | `n` 5–50 (14), `min_pct` 0,05–2 (0,10), `max_pct` 0,2–5 (1,50) |
| `retour_vwap` (sortie) | $c_T \ge \text{VWAP}_s$ | |
| `retour_dans_range` (sortie) | $c_T < H$ | ceux de `cassure_range` |
| `duree_max` (sortie) | $N$ bougies complètes depuis l'entrée | `bougies` 1–96 (12) |

$\text{VWAP}_s$ : VWAP de session (bougies 1m depuis 00:00 UTC jusqu'à la clôture de la bougie $T$). Range d'ouverture : plus haut et plus bas des bougies 1m de `[debut, debut + minutes)`, toutes présentes. ATR de Wilder, avec le vrai range $\text{TR}_t = \max(h_t - l_t,\ |h_t - c_{t-1}|,\ |l_t - c_{t-1}|)$ :

$$\text{ATR}_n = \frac{1}{n}\sum_{t=1}^{n} \text{TR}_t, \quad \text{puis} \quad \text{ATR}_t = \frac{(n-1)\,\text{ATR}_{t-1} + \text{TR}_t}{n}$$

**Stop et objectif**, calculés sur les bougies clôturées et appliqués à l'**ask d'exécution** $a$ (stop arrondi au centime inférieur, objectif au centime supérieur) :

| Type | Stop | Objectif |
|---|---|---|
| `pct` | $a\,(1 - d)$, `valeur` 0,2–5 % (0,6) | $a\,(1 + d)$, `valeur` **0,3**–10 % (1) |
| `atr` | $d = k \cdot \text{ATR}_n / c_T$, `multiple` 0,5–5 (1,5), `n` 5–50 (14) | `multiple` 0,5–10 (3) |
| `range` | bas du range $\times (1 - \text{marge})$, `marge_pct` 0–1 (0,05) | |
| `r` | | $d_o = m \cdot d_s$, `multiple` 1–5 (2), avec $d_s$ la distance réelle du stop |

- Un stop calculé hors de $[0{,}2\,\% ; 3\,\%]$ sous l'ask : signal ignoré (`stop_hors_bornes`), jamais ramené dans les bornes.
- **Objectif minimum** : il doit couvrir les frais aller-retour ($f = 0{,}001$ par ordre) plus le spread, avec une marge de 0,10 % :

$$d_o \ge 2f \times 100 + \text{spread}_\% + 0{,}10 = 0{,}30 + \text{spread}_\% \quad (\%)$$

  Un objectif `pct` sous 0,30 % est refusé à la validation ; un objectif (de tout type) sous ce minimum au moment du signal est ignoré (`objectif_trop_proche`).
- **Taille** : `pct_capital` (1–20 %, défaut 10) de la valeur du compte au bid, frais inclus. Le garde-fou garde le dernier mot (taille max du compte).

**Modèles de départ** (`simtrade/modeles/`, BTCUSDT 5m, long uniquement) :

| | `retour-vwap` | `tendance-ema` | `cassure-range` |
|---|---|---|---|
| Entrée | `ecart_vwap` sous 0,6 % ET `rsi_extreme` bas 30 ET `volatilite` 0,10–1,20 % | `croisement_ema` haussier 9/21 ET `au_dessus_vwap` ET `volatilite` 0,15–2 % | `cassure_range` (00:00, 30 min) ET `volatilite` 0,10–1,50 % |
| Stop | 1,5 × ATR14 | 2 × ATR14 | bas du range − 0,05 % |
| Objectif | 2 R | 2 R | 1,5 R |
| Sortie sur signal | `retour_vwap` OU `duree_max` 12 | `croisement_ema` baissier 9/21 | `retour_dans_range` OU `duree_max` 24 |
| Heures actives (UTC) | 06:00–22:00 | 06:00–22:00 | 00:30–22:00 |

**Modèles à faible fréquence** (étape 7b, objectif visé ≥ 1 %, long uniquement) :

| | `tendance-1h` (1h) | `cassure-matin` (15m) | `repli-tendance` (15m) |
|---|---|---|---|
| Entrée | `croisement_ema` haussier 9/21 ET `volatilite` 0,5–3 % | `cassure_range` (00:00, 480 min) ET `hauteur_range` ≥ 1,5 % | `au_dessus_ema` 200 ET `rsi_extreme` bas 30 ET `volatilite` 0,25–2 % |
| Stop | 1,5 × ATR14 | bas du range − 0,05 % | 2 × ATR14 |
| Objectif | 3 × ATR14 | 1,5 R | 1,5 % |
| Sortie sur signal | `croisement_ema` baissier 9/21 | `retour_dans_range` | `rsi_extreme` haut 70 OU `duree_max` 32 |
| Heures actives (UTC) | 01:00–22:00 | 08:00–20:00 | 02:00–22:00 |

Briques ajoutées : `au_dessus_ema` (`n` 20–300, défaut 200 : dernière clôture au-dessus de l'EMA `n` de l'intervalle) et `hauteur_range` (`min_pct` 0,1–5 % : hauteur du range de `cassure_range`, $h = \frac{\text{haut} - \text{bas}}{\text{haut}} \times 100$, au moins `min_pct`). `cassure_range.minutes` va jusqu'à 480.

**Exécution** (`runner.py`, une vérification par seconde dans l'API) :
- seule la **dernière bougie clôturée** de l'intervalle est évaluée, une fois, quand elle est **définitive** : remplacée par sa kline Binance, ainsi que les bougies 1m qui la composent (2 à 7 s après la clôture en général). Au-delà de 20 s sans kline, un signal est ignoré (`bougie_non_definitive`). La vue est datée de la clôture de la bougie : le même code (`strategies.evaluate`) servira au backtest ;
- **entrée** : achat au marché + stop + objectif liés (entrée + stop atomique de l'étape 5), donc **toujours via le garde-fou** ; l'ordre et le signal sont écrits dans **la même transaction** ;
- **une décision au plus par stratégie, par bougie et par type** (index unique en base) : un signal ne produit jamais deux ordres, même après un redémarrage ; un refus du garde-fou est journalisé (refus + signal `refuse`) et jamais réessayé sur la même bougie ;
- **sortie sur signal** : vente de toute la position au bid, **sans garde-fou** ; le stop et l'objectif de la position sont annulés ;
- une position à la fois par stratégie (pas de renfort : `deja_en_position`) ;
- après un arrêt, une bougie clôturée depuis plus d'un intervalle n'est pas évaluée (pas de rattrapage de signaux passés) ;
- en cas d'erreur (base indisponible...), la même bougie est réessayée au plus 3 fois.

**Signal ignoré** (journalisé avec ses raisons) si : `prix_perime`, `flux_transactions_muet`, `horloge_decalee`, `horloge_non_mesuree`, historique non `complet` de la série de l'intervalle ou de la série 1m (`historique_partiel`...), `repli_coinbase`, `vwap_non_fiable` (stratégie qui lit le VWAP), `bougie_non_definitive`, `pas_de_cotation_fraiche`, et pour une entrée seulement : `hors_heures`, `apres_derniere_entree`, `arret_session`, `deja_en_position`, `stop_hors_bornes`, `objectif_trop_proche`. Un ordre impossible pour une autre raison (solde...) : `ordre_impossible`. Les sorties sur signal ne dépendent pas des heures actives ; le stop, l'objectif et la clôture de session restent actifs quoi qu'il arrive.

**Journal des signaux** (table `signals`, `GET /signaux`, `python -m simtrade signaux`) : seuls les signaux (conditions remplies) sont enregistrés, avec la bougie, le type (`entree`/`sortie`), la décision (`ordre`, `refuse`, `ignore`), les raisons, le n° d'ordre ou de refus, et le détail (valeurs des briques, bid/ask, stop, objectif, montant). La dernière évaluation de chaque stratégie, signal ou non, est dans `GET /comptes/{nom}/strategie` (`etat`).

### Reset et base de données

`reset` n'efface rien : il **archive** le compte (le compte courant, ou celui donné avec `--compte`) avec ses ordres et ouvre un compte neuf de même nom et de même mode, avec le capital choisi et la date du reset. Les positions ouvertes sont abandonnées, pas vendues, et les ordres en attente annulés. `historique` et `portefeuille` ne montrent qu'un compte à la fois.

**Migration** : une base plus ancienne (étapes 1 à 3 : version 0 ; étape 4 : version 1 ; étape 5 : version 2) est mise à niveau automatiquement vers la version 3 à la première ouverture. Elle est d'abord **sauvegardée** à côté de la base (`simtrade.db.avant-v3-<date>.bak`), puis migrée en une seule transaction. La base passe ensuite en mode WAL : la CLI et l'API se bloquent moins entre elles. Le nombre de lignes des comptes, positions et ordres et l'intégrité sont vérifiés avant validation. En cas d'échec, tout est annulé, la base reste dans l'ancien format et le message indique la sauvegarde. Le compte actif devient `principal` (manuel), les anciens comptes `archive-<n°>`.

La base est stockée dans `%LOCALAPPDATA%\simtrade\simtrade.db` (hors du projet et de OneDrive). Pour en utiliser une autre :

```powershell
$env:SIMTRADE_DB = "C:\chemin\vers\autre.db"
```

## API (FastAPI)

```powershell
python -m simtrade api
```

Lance un serveur local sur `http://127.0.0.1:8000` (pas accessible depuis le réseau). Il garde **une seule** connexion websocket de cotations ouverte (Binance, sinon Coinbase) et met les cotations en cache : les ordres sont exécutés instantanément au dernier prix reçu. Une **seconde** connexion reçoit les transactions exécutées (Binance `aggTrade`, sinon Coinbase `matches`) pour les bougies et le volume. Si un flux tombe, il se reconnecte tout seul (attente de 1, 2, 4… jusqu'à 30 s), indépendamment de l'autre. Un **chien de garde** refait la connexion des cotations si elle ne livre plus rien pendant 10 s. Un ordre n'est jamais exécuté sur une cotation de plus de 5 s (erreur 503) ; `/prix` renvoie quand même tout le cache, avec `"perime": true` pour ces cotations (503 seulement si aucune cotation n'a encore été reçue). Documentation interactive : `http://127.0.0.1:8000/docs`.

| Méthode | Route | Corps JSON | Réponse |
|---|---|---|---|
| GET | `/prix` | | bid, ask, spread, source, heure, âge et `perime` de chaque cotation |
| POST | `/ordres/achat` | `{"paire": "btc", "montant": "500"}` | ordre exécuté + solde |
| POST | `/ordres/vente` | `{"paire": "eth", "quantite": "0.05"}` ou `"tout"` | ordre exécuté + solde |
| POST | `/ordres/entree` | `{"paire": "btc", "montant": "500", "stop": "79000", "objectif": "82000"}` (`objectif` facultatif) | ordre exécuté + solde + `protection` |
| POST | `/ordres/limite` | `{"paire": "btc", "sens": "achat", "prix": "78000", "montant": "500", "stop": "77000"}` (vente : `quantite` ; `stop` et `objectif` : achat seulement) | ordre en attente |
| PUT | `/ordres/{n°}/stop` | `{"prix": "79500"}` | stop déplacé |
| POST | `/ordres/stop`, `/ordres/objectif` | `{"paire": "btc", "prix": "76000", "quantite": "tout"}` (`quantite` facultative) | ordre en attente |
| POST | `/ordres/stop-objectif` | `{"paire": "sol", "stop": "90", "objectif": "120"}` | les deux ordres liés |
| GET | `/ordres/attente?tous=false` | | ordres en attente (`tous=true` : aussi exécutés, annulés, rejetés) |
| DELETE | `/ordres/{n°}` | | ordres annulés (avec l'ordre lié) |
| GET | `/portefeuille` | | positions, valeur, P&L, frais, performance |
| GET | `/historique` | | compte + ordres (avec `type_ordre`, `prix_declenchement`, `glissement`) |
| POST | `/reset` | `{"capital": "2500"}` (facultatif, défaut 10000) | nouveau compte |
| GET | `/comptes?archives=false` | | comptes ouverts (ou tous) |
| POST | `/comptes` | `{"nom": "robot", "capital": "2000", "mode": "auto"}` | compte créé |
| POST | `/comptes/{nom}/choisir`, `/comptes/{nom}/archiver` | | compte |
| GET, PUT | `/session` | `{"derniere_entree": "23:00", "cloture": "23:55"}` (PUT) | horaires UTC |
| GET, PUT | `/comptes/{nom}/risque` | `{"taille_max_pct": "30", "perte_max_session_pct": "1.5", "entrees_max_session": "4", "positions_max": "1", "delai_entrees": "10m"}` (PUT, champs facultatifs) | limites du garde-fou |
| GET | `/risque/refus?compte=&limite=100` | | journal des refus, du plus récent au plus ancien |
| GET | `/strategies` | | modèles de départ et `schema` (types, bornes, défauts de chaque paramètre) |
| GET, PUT, DELETE | `/comptes/{nom}/strategie` | `{"modele": "tendance-ema", "parametres": {"stop.multiple": "2.5"}}` ou `{"definition": {...}}` (PUT) | stratégie active et `etat` (dernière évaluation) ; PUT active (remplace), DELETE désactive |
| GET | `/signaux?compte=&decision=&limite=100` | | journal des signaux, du plus récent au plus ancien |
| GET | `/journal?compte=&limite=200` | | signaux, refus, ordres et trades du compte, du plus ancien au plus récent (les `limite` derniers) |
| GET | `/stats?compte=&strategie=&depuis=&jusqu_a=` | | statistiques du compte ou d'une stratégie (dates `AAAA-MM-JJ` ou `AAAA-MM-JJTHH:MM`, UTC), effectif et minutes sans bougie |
| GET | `/catalogue` | | catalogue des stratégies backtestées (`null` s'il n'a pas encore été construit) |
| GET | `/info` | | chemin de la base, compte courant, `comptes_ouverts`, `comptes_archives`, `session`, `refus_session`, `strategies` (actives, nombre d'évaluations, durée max d'une évaluation), `boucle` (retard de la boucle asyncio), état des flux (`flux`, `flux_volume`), `historique`, `horloge` |
| GET | `/bougies/{paire}?intervalle=1m&limite=100` | | bougies OHLC, volume, notional, transactions, `origine`, `complete`, `cloturee`, `qualite` |
| GET | `/indicateurs/{paire}?intervalle=1m&fenetre=1440&sma=20&ema=20&rsi=14&inclure_courante=false&vwap=jour_utc` | | SMA, EMA, RSI, VWAP, variation, volume, `qualite` |
| WS | `/ws/prix` | | chaque cotation en direct (d'abord le cache, puis les mises à jour) |

Les routes d'ordres, `/ordres/attente`, `/portefeuille`, `/historique`, `/reset` et `DELETE /ordres/{n°}` acceptent `?compte=<nom ou n°>` (compte courant par défaut).

**Logs et boucle** : les logs de l'API (uvicorn et `simtrade.*`) sont horodatés en UTC à la milliseconde (`2026-10-28 18:01:36.506 UTC WARNING simtrade.hub : …`). Une tâche mesure le retard de la boucle asyncio toutes les 100 ms et écrit un avertissement au-delà de 200 ms (`/info.boucle`). Une boucle bloquée n'envoie plus les pings websocket, et Binance coupe alors la connexion (« keepalive ping timeout »). Toute la base SQLite de l'API passe par un **thread dédié**, pour qu'une attente de verrou (par exemple la CLI qui écrit) ne bloque jamais la boucle.

**Flux de cotations** (`/info.flux`) : `debit_par_s` et `debit_max_par_s` (cotations par seconde), `retard_traitement_max_ms` (traitement d'une cotation : cache, abonnés, ordres en attente), `coupures_keepalive`, et `file_reception` (trames en attente au plus et pauses de lecture). Binance livre les cotations par rafales (jusqu'à 540 trames d'une seule lecture TCP mesurées) : la file de réception websocket accepte 1 024 trames avant de suspendre la lecture du socket (16 par défaut dans la bibliothèque `websockets`).

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

### Bougies, journal et statistiques

**Bougies sauvegardées** (`marche.db`) : base SQLite **séparée** de la base de trading, dans le même dossier (donc dans le dossier temporaire si `SIMTRADE_DB` y pointe), ou là où pointe `SIMTRADE_MARCHE_DB`. Elle ne contient que des klines **1m définitives** (ouverture, haut, bas, clôture, volume, notional, nombre de transactions), une ligne par paire et par minute : réenregistrer la même kline ne crée pas de doublon. Les bougies 5m et 15m n'y sont pas stockées : `market.aggregate` les reconstitue depuis les 1m pour le backtest (ouverture de la première minute, plus haut et plus bas, clôture de la dernière, sommes du volume, du notional et des transactions) ; s'il leur manque une minute, elles sont marquées `complete: false`.
- **En live**, l'API enregistre chaque bougie 1m dès qu'elle est remplacée par sa kline (ainsi que les klines 1m clôturées du préchargement), dans le thread SQLite : la boucle asyncio ne touche jamais la base.
- **Téléchargement** (`marche telecharger`, CLI seulement) : klines publiques Binance, pages de 1 000 minutes enregistrées une à une ; chaque commande demande toutes les minutes absentes de la période, trous intérieurs compris (par exemple entre la fin d'un téléchargement et des bougies enregistrées en live). Une plage que Binance renvoie vide (panne de l'exchange) est mémorisée dans `marche.db` et n'est plus redemandée, sauf si elle date de moins de 10 min (kline peut-être pas encore publiée) ; `marche etat` sépare ces « trous Binance » des trous « à télécharger ». Le poids utilisé est lu dans l'en-tête `X-MBX-USED-WEIGHT-1M` : dès 4 800 (sur 6 000 par minute), attente de la minute suivante ; sur une réponse 429 ou 418, attente de `Retry-After` (60 s par défaut) puis nouvel essai de la même page. Refus avant tout téléchargement si l'espace libre estimé après coup (200 octets par ligne) tombe sous 5 Go.

**Trade** (aller-retour) : reconstitué depuis les ordres, de la position nulle à la position nulle sur une paire (renforts et ventes partielles compris). Motif de sortie, d'après le dernier ordre de vente : `stop`, `objectif`, `cloture_session` (clôture de session, tardive ou non), `signal` (vente liée à un signal de sortie), sinon `manuel`. Stratégie : celle du signal d'entrée lié au premier achat. P&L net des deux frais, avec $C$ le coût des achats (frais inclus) et $P$ le produit des ventes (frais déduits) :

$$\text{PnL}_{\text{trade}} = P - C$$

**Journal** (`/journal`, `journal`) : signaux, refus du garde-fou, ordres et trades d'un compte, dans l'ordre chronologique ; à heure égale, la décision avant son effet (signal, refus, ordre, trade).

**Courbe de valeur** $V_m$ : valeur du compte à la fin de chaque minute $m$, solde plus positions marquées au bid. Le bid passé n'étant pas enregistré, il est estimé depuis la clôture 1m $c_{i,m}$ de chaque paire $i$ et un spread fixe $s = 0{,}02\,\%$ (même convention que le backtest) ; une minute absente de `marche.db` reprend le dernier prix connu ; `minutes_sans_bougie` compte ces minutes quand une position est ouverte (une fois par paire concernée) :

$$V_m = \text{solde}_m + \sum_i q_{i,m}\, c_{i,m} \left(1 - \frac{s}{2}\right)$$

**Statistiques** (`/stats`, `stats`), sur les $n$ trades fermés de la période, de P&L $p_k$, avec $V_0$ la valeur au début de la période :

| Statistique | Formule |
|---|---|
| P&L net | $V_{\text{fin}} - V_0$ (en %, rapporté à $V_0$) |
| Drawdown maximal | $\max_m \dfrac{\max_{m' \le m} V_{m'} - V_m}{\max_{m' \le m} V_{m'}} \times 100$, le pic partant de $V_0$ |
| Taux de réussite | $\dfrac{\#\{k : p_k > 0\}}{n} \times 100$ (`null` sans trade) |
| Gain moyen par trade | $\dfrac{1}{n}\sum_{k=1}^{n} p_k$ |
| Frais payés | somme des frais des ordres de la période |
| Sharpe | $\dfrac{\bar r}{\sigma_r}\sqrt{365}$, taux sans risque nul |

Pour le Sharpe, $r_d = \dfrac{V_d}{V_{d-1}} - 1$ est le rendement de chaque session (jour UTC) de la période, avec $V_d$ la valeur en fin de jour ($V_0$ avant le premier) : un jour sans trade ni position a un rendement nul et compte quand même. $\bar r$ est la moyenne et $\sigma_r$ l'écart type (dénominateur $N - 1$) des $N$ rendements. `null` sous 2 sessions ou si $\sigma_r = 0$. Le nombre de sessions (`sessions`) est toujours affiché. Le même module (`stats.py`) sert au backtest.

### Backtest et catalogue

**Simulateur** (`backtest.py`, pur, CLI seulement : l'API ne fait que lire le catalogue). Il rejoue une stratégie sur les klines 1m de `marche.db` avec le **même code de décision que le live** : `strategies.evaluate` (sur la fenêtre du live : 499 bougies de l'intervalle et 1 439 bougies 1m clôturées), raisons d'ignorer (`entry_blockers`), stop et objectif (`levels`), garde-fou (`risk.check_entry`, état du compte reconstitué en mémoire), montants et frais (`engine.Portfolio`), sessions. Un test vérifie que le live et le simulateur prennent les mêmes décisions, bougie par bougie, et passent les mêmes ordres. Règles d'exécution, avec $o$, $h$, $l$ l'ouverture, le haut et le bas d'une minute et $s = 0{,}02\,\%$ le spread simulé :

$$\text{ask} = o\left(1 + \frac{s}{2}\right) \qquad \text{bid} = o\left(1 - \frac{s}{2}\right)$$

- décision à la clôture $T$ de chaque bougie ; exécution à l'ouverture de la minute qui commence en $T$ (achat à l'ask, vente sur signal au bid), ordre daté $T + 1$ s comme le live, qui décide quelques secondes après la clôture ;
- stop touché si $l\left(1 - \frac{s}{2}\right) \le \text{stop}$, exécuté au niveau du stop ; objectif touché si $h\left(1 - \frac{s}{2}\right) \ge \text{objectif}$, exécuté au niveau de l'objectif ; les deux dans la même minute : **stop** ; ouverture déjà sous le stop (ou au-dessus de l'objectif) : exécution à l'ouverture, au bid, glissement enregistré ;
- clôture forcée à 23:55 (ouverture de la minute, au bid), pas d'entrée à partir de 23:00 ; frais de 0,1 % par ordre ;
- perte max de session vérifiée au plus bas de chaque minute en position, au bid estimé $l\left(1 - \frac{s}{2}\right)$, et après chaque vente ;
- trou dans la fenêtre (minute absente, bougie incomplète, historique plus court que celui du live) : signal ignoré (`historique_partiel`) ; minute d'exécution absente : `pas_de_cotation_fraiche`.

**Références**, sur les mêmes données et avec la même taille de position : ne rien faire ; acheter à 00:00 et vendre à la clôture de session, chaque jour, sans stop ; entrées au hasard. Pour ces dernières, chaque clôture éligible (sans position, heures actives, avant la dernière entrée, session non arrêtée, sans trou, VWAP fiable) donne une entrée avec la probabilité $p = \dfrac{\text{entrées de la stratégie}}{\text{clôtures éligibles}}$, puis le même calcul du stop et de l'objectif, les mêmes sorties et le même garde-fou ; 100 tirages, graines 1 à 100.

**Régimes** (`regimes.py`, bougies 5m clôturées seulement), sur les 100 dernières bougies : ATR de Wilder sur 14 bougies en % de la clôture, et ratio d'efficacité sur 24 bougies :

$$\text{ER}_T = \frac{|c_T - c_{T-24}|}{\sum_{t=T-23}^{T} |c_t - c_{t-1}|}$$

`forte_volatilite` si l'ATR % atteint le quantile 80 % calculé sur les bougies 5m du 07/05 au 07/09 (échantillon et ses deux jours de fenêtre), par paire ; sinon `tendance` si l'ER atteint son quantile 70 % ; sinon `range`. Le régime de chaque trade est celui de son entrée.

**Catalogue** (`catalogue construire`, règles figées avant tout calcul) :
- données du 07/05 au 07/11/2026 ; échantillon du 09/05 au 07/09 (les deux premiers jours remplissent la fenêtre du live), hors échantillon du 07/09 au 07/11 ; intervalle 5m ; BTC, ETH, SOL ;
- grille de 3 × 3 combinaisons par modèle (`ecart_vwap.seuil_pct` × `rsi_extreme.seuil`, `croisement_ema.rapide` × `croisement_ema.lente`, `cassure_range.minutes` × `objectif.multiple`) ; la phase en échantillon ne lit aucune bougie au-delà de la coupure ;
- candidate : au moins 30 trades et un P&L > 0 en échantillon. Sélection par **voisinage** : score $S_{ij} = \frac{1}{|N_{ij}|}\sum_{(a,b) \in N_{ij}} \text{PnL}_{ab}$, où $N_{ij}$ contient la combinaison et ses voisines directes (haut, bas, gauche, droite) ; la candidate de meilleur score l'emporte (égalité : la plus proche des défauts) ; sans candidate, le couple modèle-paire est « rejeté en échantillon » ;
- bornes publiées : le plus grand rectangle de la grille qui contient la combinaison choisie et dont toutes les combinaisons sont candidates ;
- hors échantillon, lancé une seule fois : la stratégie, la même à $s = 0{,}05\,\%$ (sensibilité), et les références. Admission : P&L > 0, P&L supérieur à l'achat 00:00 / vente clôture, meilleur qu'au moins 90 % des tirages, au moins 30 trades (sinon « données insuffisantes »). Le percentile exact atteint face aux tirages est enregistré. Une stratégie admise dont le P&L est ≤ 0 à 0,05 % porte `"fragile_spread": true` ;
- régimes retenus pour une stratégie : P&L par trade > 0 avec au moins 20 trades dans ce régime en échantillon ;
- sorties : `simtrade/catalogue.json` (chaque entrée passe `strategies.validate`, bornes comprises dans le schéma) et un rapport Markdown par couple dans `backtests/`, plus `backtests/synthese.md`. Un catalogue vide est un résultat valide.

**Étape 7b** (`catalogue construire --etape 7b`, mêmes règles) : données du 16/10/2025 au 07/11/2026 (la fenêtre du live en 1h couvre ≈ 21 jours) ; échantillon du 09/11/2025 au 07/09/2026, hors échantillon inchangé (07/09 → 07/11/2026) ; chaque modèle garde son intervalle ; grilles `objectif.multiple` × `croisement_ema.lente` (tendance-1h), `hauteur_range.min_pct` × `objectif.multiple` (cassure-matin), `entree.rsi_extreme.seuil` × `objectif.valeur` (repli-tendance). Les régimes restent calculés sur les bougies 5m. `catalogue.json` passe au format 2 : une construction par étape, et l'étape, le commit, les périodes et l'intervalle dans chaque entrée et chaque résultat ; une construction remplace seulement les résultats de sa propre étape. Synthèse : `backtests/synthese-7b.md`.

Calcul en parallèle (processus `spawn`, un par cœur moins un, au plus 11), chaque processus lisant ses bougies dans `marche.db`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Les tests couvrent le moteur (frais, prix moyen, P&L réalisé/latent, performance, refus d'ordres, validation du capital), la persistance (redémarrage, reset, transactions), le parsing des messages Binance/Coinbase, la bascule entre sources, les commandes CLI, les routes de l'API, la reconnexion du flux, le refus des cotations périmées, la construction des bougies, le raccord avec l'historique (klines simulées par `httpx.MockTransport`), les indicateurs (valeurs calculées à la main), la qualité des données et les routes de marché. Pour l'étape 4 : déclenchement et glissement des ordres en attente (dont un stop sauté), exécution par le flux, paires liées, réservations, redémarrage, ordres posés ou annulés par une autre connexion, comptes en parallèle, entrées refusées après l'heure limite, clôture de fin de session, clôture reportée et rattrapage (horloge simulée), migration d'une base des étapes 1 à 3 et annulation complète en cas d'échec. Pour l'étape 5 : chaque règle du garde-fou à la limite exacte, « tout le capital sans stop » refusé et journalisé, entrée + stop atomique, stop qui part après la perte max, clôture de session jamais bloquée, annulation du dernier stop refusée, retard de boucle détecté, aucun accès SQLite depuis l'écouteur de cotations, migration v1 → v2. Pour l'étape 6 : chaque brique (valeurs calculées à la main), chaque borne de paramètre acceptée à la limite et refusée au-delà, objectif sous 0,30 % refusé, aucun signal sur une bougie non clôturée, un seul ordre par signal même après un redémarrage, panne entre l'achat et le commit (ni ordre ni signal, puis un seul ordre), aucun ordre si `qualite` est mauvaise (chaque raison), hors heures et après la dernière entrée, refus du garde-fou journalisé sans nouvel essai, sortie sur signal malgré la perte max, bougie non définitive, rejeu bougie par bougie identique au live, file de réception et chien de garde du flux, migration v2 → v3, routes et CLI des stratégies. Pour l'étape 7 : base des bougies (upsert idempotent, trous, chemin à côté de la base de trading), agrégation 1m → 5m/15m égale aux klines de l'intervalle, téléchargement (pagination, reprise, deux fois = mêmes lignes, écart comblé entre un bloc téléchargé et un bloc live, plage vide chez Binance demandée une seule fois, trous, 429 et 418 puis reprise, poids, refus pour espace disque), enregistrement live dans le thread SQLite et jamais depuis la boucle, trades et motifs de sortie, journal chronologique, statistiques calculées à la main (drawdown, Sharpe avec un jour à 0, taux de réussite, courbe marquée au bid), routes et CLI ; simulateur (futur empoisonné sans effet sur les décisions, stop et objectif dans la même minute, ouverture sous le stop, clôture de 23:55, pas d'entrée après 23:00, VWAP remis à zéro à minuit, frais et spread d'un aller-retour à la main, trou dans la fenêtre, perte max au plus bas de la minute), mêmes décisions et mêmes ordres que le live, références (tirages reproductibles par graine), régimes calculés sur le passé seulement, sélection par voisinage, rectangle des bornes (zone en L), échantillon qui ne lit jamais le hors échantillon, entrées de catalogue valides, catalogue vide valide, calcul en processus identique au calcul en série. Ils utilisent des données simulées ; une garde dans `tests/conftest.py` fait échouer tout test qui tenterait une résolution de nom ou une connexion hors de la machine.

## Architecture

```
simtrade/
  models.py    structures de données (Quote, Position, Account, Order, PendingOrder)
  engine.py    moteur pur : calculs, frais, refus, réservations, validation (aucun I/O)
  orders.py    règles pures des ordres en attente : déclenchement, glissement, refus à la pose
  sessions.py  règles pures des sessions : dernière entrée, clôture, retard
  risk.py      garde-fou pur : limites, état du compte, règles d'entrée
  bricks.py    briques pures des stratégies (bougies clôturées uniquement), ATR
  strategies.py  stratégies JSON : schéma, validation, évaluation, stop et objectif, raisons d'ignorer (pur)
  modeles/     stratégies de départ (JSON)
  runner.py    StrategyRunner : évalue les stratégies actives à chaque bougie clôturée et passe les ordres
  storage.py   SQLite : comptes, positions, ordres, ordres en attente, sessions, réglages, limites, refus
  migrations.py  mise à niveau du schéma (sauvegarde, transaction unique, contrôles)
  dbworker.py  thread SQLite dédié de l'API
  loopmon.py   logs horodatés en UTC et mesure du retard de la boucle asyncio
  feeds/       sources de prix : Binance, Coinbase, bascule, source simulée
  service.py   TradingService : point d'entrée unique des interfaces
  watcher.py   OrderWatcher (ordres touchés par chaque cotation) et SessionScheduler (clôtures)
  market.py    calculs de marché purs : bougies OHLC, raccord avec les klines, SMA, EMA, RSI, VWAP, variation, qualité (aucun I/O)
  history.py   historique public Binance (REST) : klines paginées, heure du serveur, poids et limite de requêtes
  marche.py    base des bougies 1m (marche.db) : upsert, lecture par période, trous
  download.py  téléchargement des klines 1m : reprise, poids, attente sur 429/418, garde d'espace disque
  trades.py    trades reconstitués depuis les ordres, motifs de sortie, journal chronologique (pur)
  stats.py     courbe de valeur, P&L, drawdown, réussite, gain moyen, frais, Sharpe (pur)
  backtest.py  simulateur : même code de décision que le live, règles d'exécution prudentes (pur)
  references.py  références : ne rien faire, achat 00:00 / vente clôture, entrées au hasard
  regimes.py   régimes de marché (volatilité, efficacité) sur les bougies passées (pur)
  catalogue.py  grille, sélection, hors échantillon, admission, catalogue.json et rapports (processus spawn)
  catalogue.json  catalogue des stratégies admises (lu par GET /catalogue)
backtests/     rapports Markdown du catalogue
  hub.py       PriceHub (cotations + transactions, cache, bougies, raccord avec l'historique, reconnexion) et CachedFeed
  api.py       API FastAPI : create_app(), routes, websocket /ws/prix
  cli.py       ligne de commande
```

L'API ne fait qu'appeler `TradingService`, qui reçoit un `CachedFeed` à la place d'une connexion directe. Dans l'API, `PriceHub` appelle `OrderWatcher` pour **chaque** cotation reçue (sans file d'attente intermédiaire, donc sans cotation sautée). Le watcher garde les ordres en attente en mémoire ; un ordre touché est mis dans la file (FIFO) du thread SQLite, et la boucle asyncio ne touche jamais la base. Il recharge sa liste après chaque changement fait par le service, et chaque seconde si une autre connexion (la CLI) a écrit dans la base (`PRAGMA data_version`).

## Limites

- **Tout est en USDT.** Pas de conversion en EUR, pas d'autres paires que les trois listées.
- **Pas de carnet d'ordres complet** : seul le meilleur bid/ask est utilisé. Un gros ordre est exécuté entièrement à ce prix, sans glissement (*slippage*), ce qui est optimiste pour de gros montants.
- **Ordres en attente exécutés seulement quand l'API tourne.** La CLI pose, liste et annule, mais ne surveille rien : sans `python -m simtrade api` lancé, aucun stop ne part et aucune session n'est fermée.
- **Les ordres ne se déclenchent que sur une cotation reçue en direct** (de moins de 5 s). Flux coupé : rien ne se déclenche ; à la reconnexion, la première cotation est évaluée et, si le prix a sauté le stop, l'ordre est exécuté à ce prix avec le glissement enregistré.
- **Redémarrage** : les ordres en attente sont en base et rechargés au démarrage de l'API, puis évalués sur la première cotation fraîche. Un prix qui a touché le stop puis est revenu pendant l'arrêt **n'exécute pas** l'ordre (il l'aurait été en vrai) : le chemin du prix pendant l'arrêt est inconnu.
- **Clôture tardive au prix du moment** (première cotation fraîche), pas à celui de l'heure de clôture.
- **Repli Coinbase** : les ordres continuent de se déclencher sur le bid/ask Coinbase, dont le spread est plus large ; un stop peut donc partir plus tôt. La source est enregistrée sur chaque ordre.
- **Déclenchement sur le meilleur bid/ask**, pas sur le dernier prix de transaction comme Binance : un stop peut partir sur un simple écartement du spread.
- **Pas d'intervention manuelle sur un compte automatique** autre que par les mêmes routes, qui le traitent comme automatique (règles de session et garde-fou compris).
- **Stratégies exécutées seulement quand l'API tourne**, sur la dernière bougie clôturée : un signal produit pendant un arrêt est perdu, pas rattrapé.
- **Signal décidé 2 à 20 s après la clôture** (attente de la kline définitive) : l'entrée se fait au prix de ce moment-là, pas à la clôture de la bougie.
- **Stratégies long uniquement** (pas de vente à découvert), une position à la fois par stratégie, une stratégie par compte.
- **Le garde-fou ne protège que les comptes automatiques.** Un compte manuel peut tout faire, par construction.
- **Perte de session estimée au bid, hors frais de sortie** : la perte réelle à la vente serait un peu plus forte (0,1 % de frais).
- **Arrêt de session détecté au mieux chaque seconde** (ou à la tentative d'entrée) : un passage très bref sous la limite, entre deux vérifications et sans tentative d'entrée, peut ne pas être enregistré.
- **CLI : une connexion websocket par commande** : chaque commande met 1 à 4 s à obtenir un prix (le temps d'établir la connexion). L'API n'a pas ce délai.
- **Coinbase en secours est moins liquide en USDT** : le spread BTC-USDT y est nettement plus large que sur Binance (constaté : environ 3,5 pb contre moins de 0,01 pb), et le canal ticker ne se met à jour qu'à chaque transaction.
- CLI : pas de reconnexion automatique si le flux continu `prix` est coupé en cours de route (l'API, elle, se reconnecte).
- **Backtest : spread fixe** ($s = 0{,}02\,\%$, sensibilité à 0,05 %), carnet d'ordres ignoré, exécution complète au prix simulé. Le chemin du prix à l'intérieur d'une minute est inconnu : stop et objectif exécutés à leur niveau exact, stop retenu quand les deux sont touchés.
- **Tests multiples** : jusqu'à 9 couples modèle-paire testés hors échantillon (ceux qui ont une candidate en échantillon) au seuil de 10 % face aux tirages au hasard peuvent donner environ un faux positif par hasard. Une stratégie admise n'est pas une stratégie gagnante garantie ; le percentile exact est publié.
- **Un seul découpage échantillon / hors échantillon** (2 mois hors échantillon) : peu de trades par régime, résultats sensibles à la période.
- **Bougies en mémoire rechargées au démarrage** : l'API recharge 1 journée en 1m et 500 bougies au-delà depuis Binance à chaque démarrage. Seules les 1m définitives sont sauvegardées (`marche.db`) ; une minute où l'API ne tournait pas manque, sauf à la télécharger ensuite (`marche telecharger`).
- **Courbe de valeur au bid estimé** (clôture 1m × (1 − s/2), s = 0,02 %), pas au vrai bid du moment, qui n'est pas enregistré.
- **Raccord au démarrage et après une coupure** : la bougie en cours manque les transactions exécutées avant que le websocket ne livre (de l'ordre de 1 % du nombre de transactions d'une minute BTC) ; elle est marquée `complete: false` jusqu'à son remplacement par la kline. Les indicateurs par défaut (bougies clôturées) l'utilisent entre sa clôture et son remplacement (2 à 7 s), et durablement si la réconciliation échoue.
- **Clôture jugée sur l'horloge locale** : l'écart avec Binance est mesuré et signalé (au-delà de 1 s), pas corrigé.
- **Volume** : celui de la source qui répond, pas un volume global. En repli sur Coinbase, l'historique reste celui de Binance : bougies de deux places différentes, VWAP signalé non fiable. Sur Coinbase, les paires USDT sont peu échangées et la source de transactions est jugée muette si rien n'arrive en 15 s.
- Ouvrir l'API ouvre **deux** connexions websocket (cotations et transactions) et environ 15 requêtes REST au démarrage, puis environ 3 à 5 par minute pour la réconciliation.
- API sans authentification : elle n'écoute que sur 127.0.0.1, ne pas l'exposer sur le réseau.
- Un seul utilisateur (plusieurs comptes fictifs, mais aucune notion de propriétaire).
- Simulation pédagogique : ni conseil financier, ni lien avec un vrai compte.
