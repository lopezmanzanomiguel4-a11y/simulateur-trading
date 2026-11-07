# repli-tendance sur BTCUSDT (15m)

Statut : **donnees_insuffisantes** (5 trades hors échantillon (minimum 30))

Échantillon : 09/11/2025 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0,02 %, frais 0,1 % par ordre, capital 10 000,00 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `entree.rsi_extreme.seuil`, colonnes : `objectif.valeur`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 1 | 1,5 | 2 |
|---|---|---|---|
| **35** | [+0,49 (37)*] | -42,97 (36) | -57,41 (35) |
| **40** | -126,55 (110) | -120,69 (105) | -119,16 (102) |
| **45** | -415,64 (189) | -363,15 (176) | -384,32 (169) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 1 | 1,5 | 2 |
|---|---|---|---|
| **35** | -56,34 | -55,14 | -73,18 |
| **40** | -165,60 | -154,50 | -170,39 |
| **45** | -301,78 | -320,95 | -288,88 |

Paramètres choisis : `entree.rsi_extreme.seuil` = 35, `objectif.valeur` = 1.
Bornes (plus grand rectangle de candidates) : `entree.rsi_extreme.seuil` de 35 à 35, `objectif.valeur` de 1 à 1.

## Hors échantillon (lancé une fois)

| | P&L | P&L % | Trades | Réussite | Drawdown max | Sharpe | Frais |
|---|---|---|---|---|---|---|---|
| Stratégie (s = 0,02 %) | -8,93 | -0,089 % | 5 | 40,0 % | 0,178 % | -1,41 | 9,99 |
| Stratégie (s = 0,05 %) | -21,44 | -0,214 % | 5 | 40,0 % | 0,282 % | -3,58 | 9,97 |
| Achat 00:00 / vente clôture | -177,40 | -1,774 % | 61 | 41,0 % | 2,472 % | -2,86 | 121,22 |
| Ne rien faire | +0,00 | +0,000 % | 0 | – | 0,000 % | – | 0,00 |

Tirages au hasard : 100 (graines 1 à 100), probabilité d'entrée p = 5 / 4795 = 0,00104 par clôture éligible. Stratégie meilleure que 59 tirages sur 100 : **percentile 59** (critère : ≥ 90 %). P&L des tirages : min -56,93, médiane -12,26, max +24,09.

## Régimes

Seuils fixés en échantillon : ATR 14 ≥ 0,2312 % → forte volatilité ; sinon efficacité (24 bougies) ≥ 0,2564 → tendance ; sinon range.

| Régime | Trades (éch.) | P&L / trade (éch.) | Trades (hors éch.) | P&L / trade (hors éch.) |
|---|---|---|---|---|
| range | 4 | -4,75 | 1 | -1,95 |
| tendance | 14 | -0,17 | 3 | +1,17 |
| forte_volatilite | 19 | +1,15 | 1 | -10,49 |

Régimes retenus (≥ 20 trades et P&L par trade > 0 en échantillon) : aucun.

Signaux ignorés ou refusés hors échantillon : deja_en_position 11, hors_heures 2.
